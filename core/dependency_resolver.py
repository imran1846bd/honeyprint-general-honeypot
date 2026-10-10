"""Resolve registered components into immutable, non-launching build plans.

Factories used for adapter construction and recipe metadata are trusted startup
registrations. They must be free of resource-starting side effects. Recipes are
metadata factories returning platform, deployment, simulators, versions and an
optional dependencies list of {kind, id} mappings. Pin declarations do not prove
that a deployment has been tested. Lifecycle verification remains separate.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from adapters.base_adapter import AdapterError
from core.plugin_registry import PluginRegistry, PluginRegistryError
from core.validator import (
    ConfigValidationError, validate_config, validate_adapter_capabilities,
)


class DependencyResolutionError(ValueError):
    """Required registered metadata, profile or dependency could not be resolved."""


def freeze(value: Any) -> Any:
    """Detach and recursively freeze YAML-like values for a plan snapshot."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise DependencyResolutionError("Plan metadata must contain YAML-like values")


@dataclass(frozen=True)
class InstancePlan:
    instance_id: str
    config: Mapping[str, Any] = field(repr=False)
    adapter: Any = field(repr=False)
    recipe: Mapping[str, Any] = field(repr=False)
    factories: Mapping[tuple[str, str], Any] = field(repr=False)
    # A plan never claims deployment readiness.
    ready: bool = field(default=False, init=False)


@dataclass(frozen=True)
class BuildPlan:
    instances: tuple[InstancePlan, ...]
    telemetry: Mapping[str, Any] = field(repr=False)


def resolve_dependencies(
    config: dict[str, Any], registry: PluginRegistry, *, base_dir: Path
) -> BuildPlan:
    """Validate configuration and resolve adapters, recipe metadata and profiles.

    base_dir explicitly defines relative printer-profile resolution. No software
    is installed, no plugin observers are started and no processes are launched.
    Adapter.validate receives a detached configuration and immutable context.
    """
    from copy import deepcopy

    config = deepcopy(config)
    validate_config(config, registry)
    plans = []
    for cfg in config["instances"]:
        instance_id = cfg["id"]
        try:
            adapter = registry.resolve("platform_adapter", cfg.get("adapter", cfg["platform"]))()
            caps = validate_adapter_capabilities(adapter, cfg)
            recipe_id = cfg.get("recipe", caps["recipes"][cfg["deployment"]])
            recipe = registry.resolve("deployment_recipe", recipe_id)()
            if not isinstance(recipe, Mapping):
                raise DependencyResolutionError("Recipe metadata must be a mapping")
            if recipe.get("platform") != cfg["platform"] or recipe.get("deployment") != cfg["deployment"]:
                raise DependencyResolutionError("Recipe platform or deployment mode mismatch")
            simulators = recipe.get("simulators")
            if not isinstance(simulators, (list, tuple)) or cfg["simulation"] not in simulators:
                raise DependencyResolutionError("Recipe does not support the requested simulator")
            versions = recipe.get("versions")
            if not isinstance(versions, Mapping) or not versions or any(
                not isinstance(name, str) or not name
                or not isinstance(version, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version)
                or version.lower() in {"latest", "main", "master", "head", "stable", "dev"}
                for name, version in versions.items()
            ):
                raise DependencyResolutionError("Recipe must declare explicit software version pins")
            profile = Path(cfg["printer_profile"])
            if not profile.is_absolute():
                profile = Path(base_dir) / profile
            profile = profile.resolve()
            if not profile.is_file() or not profile.read_bytes().strip():
                raise DependencyResolutionError("Printer profile must be a readable nonempty file")
            cfg["printer_profile"] = str(profile)
            refs = [("simulator", cfg["simulation"]), ("storage", config["telemetry"]["backend"])]
            refs += [("manufacturing_stage", name) for name in cfg.get("stages", [])]
            refs += [("extension", name) for name in cfg.get("extensions", [])]
            refs += [("service", cfg[key]) for key in ("frontend", "api") if key in cfg]
            if "plugin" in cfg.get("instrumentation", {}):
                refs.append(("instrumentation", cfg["instrumentation"]["plugin"]))
            dependencies = recipe.get("dependencies", [])
            if not isinstance(dependencies, (list, tuple)):
                raise DependencyResolutionError("Recipe dependencies must be a list")
            for dep in dependencies:
                if not isinstance(dep, Mapping) or set(dep) != {"kind", "id"}:
                    raise DependencyResolutionError("Recipe dependency needs kind and id")
                # Resolve first so invalid identifiers cannot enter a plan.
                registry.resolve(dep["kind"], dep["id"])
                refs.append((dep["kind"], dep["id"]))
            factories = {ref: registry.resolve(*ref) for ref in refs}
            frozen_recipe = freeze(recipe)
            adapter.validate(deepcopy(cfg), MappingProxyType({
                "base_dir": str(Path(base_dir).resolve()), "recipe": frozen_recipe,
            }))
            plans.append(InstancePlan(instance_id, freeze(cfg), adapter,
                                      frozen_recipe, MappingProxyType(factories)))
        except (ConfigValidationError, PluginRegistryError, AdapterError,
                DependencyResolutionError, OSError, TypeError, ValueError) as exc:
            raise DependencyResolutionError(
                f"Instance '{instance_id}': dependency or capability validation failed"
            ) from exc
    return BuildPlan(tuple(plans), freeze(config["telemetry"]))

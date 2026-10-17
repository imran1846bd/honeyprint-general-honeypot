"""Validate initial HoneyPrint configuration structure and registry references.

This is configuration validation, not deployment readiness verification. Adapter
contracts, recipe versions, profile files and runtime compatibility still need
separate checks before any instance can be launched.
"""

import re
from typing import Any

from core.plugin_registry import PluginRegistry, PluginRegistryError


class ConfigValidationError(ValueError):
    """Configuration failed one or more checks; issues contain field locations."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("Invalid configuration:\n" + "\n".join(issues))


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]*\Z")
_ROOT_KEYS = {"schema_version", "instances", "telemetry"}
_INSTANCE_KEYS = {
    "id", "platform", "adapter", "deployment", "printer_profile", "simulation",
    "octoprint_settings", "klipper_settings", "services", "stages", "instrumentation", "frontend", "api", "extensions", "recipe",
}
_TELEMETRY_KEYS = {
    "backend", "capture_requests", "capture_commands", "capture_state_changes",
}


def validate_config(config: dict[str, Any], registry: PluginRegistry) -> None:
    """Raise ConfigValidationError on invalid input without invoking factories.

    Adapter IDs default to the platform ID; custom instances may supply an
    explicit adapter. Services currently map names to integer TCP ports on one
    shared host. Storage backends are checked as identifiers only until the
    storage contract exists. Empty instance lists are allowed for initial setup.
    """
    issues: list[str] = []

    def unknown_keys(value: dict, allowed: set[str], location: str) -> None:
        if any(key not in allowed for key in value):
            issues.append(f"{location}: contains unknown keys")

    def identifier(value: Any, location: str) -> bool:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            issues.append(f"{location}: expected a lowercase identifier")
            return False
        return True

    def registered(kind: str, value: Any, location: str) -> None:
        if identifier(value, location):
            try:
                registry.resolve(kind, value)
            except PluginRegistryError:
                issues.append(f"{location}: no registered {kind}")

    if not isinstance(config, dict):
        raise ConfigValidationError(["configuration: expected a mapping"])
    unknown_keys(config, _ROOT_KEYS, "configuration")
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        issues.append("schema_version: expected integer 1")
    instances = config.get("instances")
    if not isinstance(instances, list):
        issues.append("instances: expected a list")
        instances = []
    seen_ids: set[str] = set()
    used_ports: set[int] = set()
    for index, instance in enumerate(instances):
        loc = f"instances[{index}]"
        if not isinstance(instance, dict):
            issues.append(f"{loc}: expected a mapping")
            continue
        unknown_keys(instance, _INSTANCE_KEYS, loc)
        instance_id = instance.get("id")
        if identifier(instance_id, f"{loc}.id"):
            if instance_id in seen_ids:
                issues.append(f"{loc}.id: duplicate instance ID")
            seen_ids.add(instance_id)
        if 'octoprint_settings' in instance:
            if instance.get('platform') != 'octoprint' or instance.get('deployment') != 'managed':
                issues.append(f'{loc}.octoprint_settings: requires managed OctoPrint')
            try:
                from deployment.octoprint_settings import resolve_octoprint_settings
                resolve_octoprint_settings(instance['octoprint_settings'])
            except ValueError as error:
                issues.append(str(error))
        if 'klipper_settings' in instance:
            if instance.get('platform') != 'klipper' or instance.get('deployment') != 'managed':
                issues.append(f'{loc}.klipper_settings: requires managed Klipper')
            try:
                from deployment.docker.klipper.settings import resolve_klipper_settings
                resolve_klipper_settings(instance['klipper_settings'])
            except ValueError as error:
                issues.append(str(error))
        platform = instance.get("platform")
        identifier(platform, f"{loc}.platform")
        registered("platform_adapter", instance.get("adapter", platform), f"{loc}.adapter")
        if instance.get("deployment") not in ("managed", "attach"):
            issues.append(f"{loc}.deployment: expected managed or attach")
        profile = instance.get("printer_profile")
        if not isinstance(profile, str) or not profile.strip():
            issues.append(f"{loc}.printer_profile: expected a nonempty path")
        registered("simulator", instance.get("simulation"), f"{loc}.simulation")
        services = instance.get("services")
        if not isinstance(services, dict) or not services:
            issues.append(f"{loc}.services: expected a nonempty port mapping")
        else:
            for name, port in services.items():
                identifier(name, f"{loc}.services key")
                if type(port) is not int or not 1 <= port <= 65535:
                    issues.append(f"{loc}.services: ports must be integers from 1 to 65535")
                elif port in used_ports:
                    issues.append(f"{loc}.services: duplicate service port")
                else:
                    used_ports.add(port)
        for field, kind in (("stages", "manufacturing_stage"), ("extensions", "extension")):
            values = instance.get(field, [])
            if not isinstance(values, list):
                issues.append(f"{loc}.{field}: expected a list")
                continue
            seen: set[str] = set()
            for item in values:
                registered(kind, item, f"{loc}.{field}")
                if isinstance(item, str):
                    if item in seen:
                        issues.append(f"{loc}.{field}: duplicate identifier")
                    seen.add(item)
        if "instrumentation" in instance:
            settings = instance["instrumentation"]
            if not isinstance(settings, dict):
                issues.append(f"{loc}.instrumentation: expected a mapping")
            else:
                for name, value in settings.items():
                    identifier(name, f"{loc}.instrumentation key")
                    if name == "plugin":
                        registered("instrumentation", value, f"{loc}.instrumentation.plugin")
                    elif type(value) is not bool:
                        issues.append(f"{loc}.instrumentation: flags must be booleans")
        if "recipe" in instance:
            registered("deployment_recipe", instance["recipe"], f"{loc}.recipe")
        for field in ("frontend", "api"):
            if field in instance:
                registered("service", instance[field], f"{loc}.{field}")
    telemetry = config.get("telemetry")
    if not isinstance(telemetry, dict):
        issues.append("telemetry: expected a mapping")
    else:
        unknown_keys(telemetry, _TELEMETRY_KEYS, "telemetry")
        identifier(telemetry.get("backend"), "telemetry.backend")
        for flag in _TELEMETRY_KEYS - {"backend"}:
            if type(telemetry.get(flag)) is not bool:
                issues.append(f"telemetry.{flag}: expected a boolean")
    if issues:
        raise ConfigValidationError(issues)


def validate_adapter_capabilities(adapter, cfg: dict[str, Any]) -> dict[str, Any]:
    """Check an adapter contract and declared support without starting resources.

    Capabilities contain lists named deployment, simulators, services and stages.
    Optional lists are extensions, frontends, apis and instrumentation (enabled
    observation keys). A recipes mapping assigns one recipe ID per supported
    deployment mode. This verifies declarations, not software readiness.
    """
    from collections.abc import Mapping
    from adapters.base_adapter import PlatformAdapter

    if not isinstance(adapter, PlatformAdapter):
        raise ConfigValidationError(["adapter: must implement PlatformAdapter"])
    issues = []
    if adapter.platform_id != cfg["platform"]:
        issues.append("adapter: platform identity does not match configuration")
    capabilities = adapter.capabilities()
    if not isinstance(capabilities, Mapping):
        raise ConfigValidationError(["adapter.capabilities: expected a mapping"])
    capabilities = dict(capabilities)
    for key in ("deployment", "simulators", "services", "stages", "extensions",
                "frontends", "apis", "instrumentation"):
        values = capabilities.get(key, [] if key in {
            "extensions", "frontends", "apis", "instrumentation"
        } else None)
        if not isinstance(values, (list, tuple)) or any(
            not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in values
        ):
            issues.append(f"adapter.capabilities.{key}: expected identifier list")
            capabilities[key] = []
        else:
            capabilities[key] = list(values)
    requests = {
        "deployment": [cfg["deployment"]], "simulators": [cfg["simulation"]],
        "services": list(cfg["services"]), "stages": cfg.get("stages", []),
        "extensions": cfg.get("extensions", []),
        "frontends": [cfg["frontend"]] if "frontend" in cfg else [],
        "apis": [cfg["api"]] if "api" in cfg else [],
        "instrumentation": [name for name, enabled in cfg.get("instrumentation", {}).items()
                            if enabled is True or name == "plugin"],
    }
    for key, values in requests.items():
        if any(value not in capabilities[key] for value in values):
            issues.append(f"adapter.capabilities.{key}: requested capability unsupported")
    recipes = capabilities.get("recipes")
    if not isinstance(recipes, Mapping) or any(
        mode not in ("managed", "attach") or not isinstance(recipe, str)
        or not _IDENTIFIER.fullmatch(recipe) for mode, recipe in recipes.items()
    ):
        issues.append("adapter.capabilities.recipes: expected mode to recipe ID mapping")
    elif cfg["deployment"] not in recipes:
        issues.append("adapter.capabilities.recipes: no recipe for requested mode")
    else:
        capabilities["recipes"] = dict(recipes)
        if "recipe" in cfg and cfg["recipe"] != recipes[cfg["deployment"]]:
            issues.append("recipe: not supported for requested adapter mode")
    if issues:
        raise ConfigValidationError(issues)
    return capabilities

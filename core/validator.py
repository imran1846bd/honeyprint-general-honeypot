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
    "services", "stages", "instrumentation", "frontend", "api", "extensions",
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

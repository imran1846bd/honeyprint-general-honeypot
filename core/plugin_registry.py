"""Explicit registration and lookup of HoneyPrint plugin factories.

Application startup code registers installed plugins before validation. YAML
contains identifiers only: this registry does not import modules, execute code
paths, discover uploaded packages or instantiate plugins automatically.

Kinds group extension contracts, for example ``platform_adapter``, ``simulator``,
``manufacturing_stage``, ``camera``, ``storage`` and ``event_normalizer``. Custom
kinds and identifiers are supported; contract and capability checks belong to
later validation and adapter implementations.
"""

import re
from typing import Any, Callable


PluginFactory = Callable[..., Any]
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]*\Z")


class PluginRegistryError(ValueError):
    """Base error for invalid plugin registration or lookup."""


class DuplicatePluginError(PluginRegistryError):
    """An identifier is already registered within the requested kind."""


class UnknownPluginError(PluginRegistryError):
    """No factory has been registered for the requested kind and identifier."""


def _check_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PluginRegistryError(
            f"{label} must start with a lowercase letter and contain only "
            "lowercase letters, digits, underscores, dots or hyphens"
        )


class PluginRegistry:
    """An instance-local registry of explicitly registered callable factories.

    A class, function or callable object may act as a factory. Registration and
    lookup never call it. The same ID may be used in different plugin kinds, but
    duplicate registration within a kind is rejected without replacing it.

    Example startup registration for developer-owned code::

        registry = PluginRegistry()
        registry.register("platform_adapter", "lab_printer_v1", LabAdapter)
        adapter_factory = registry.resolve("platform_adapter", "lab_printer_v1")

    ``LabAdapter`` must be provided by installed application code. Registering
    it does not prove compatibility or authorize deployment. Register plugins
    during startup before sharing the registry with validation/runtime code.
    """

    def __init__(self) -> None:
        self._factories: dict[str, dict[str, PluginFactory]] = {}

    def register(
        self, kind: str, plugin_id: str, factory: PluginFactory
    ) -> None:
        """Register a callable without executing it or overwriting an entry."""
        _check_identifier(kind, "Plugin kind")
        _check_identifier(plugin_id, "Plugin ID")
        if not callable(factory):
            raise PluginRegistryError("Plugin factory must be callable")
        plugins = self._factories.get(kind, {})
        if plugin_id in plugins:
            raise DuplicatePluginError(
                f"Plugin '{plugin_id}' is already registered for '{kind}'"
            )
        self._factories.setdefault(kind, {})[plugin_id] = factory

    def resolve(self, kind: str, plugin_id: str) -> PluginFactory:
        """Return a registered factory; never import or instantiate a plugin."""
        _check_identifier(kind, "Plugin kind")
        _check_identifier(plugin_id, "Plugin ID")
        try:
            return self._factories[kind][plugin_id]
        except KeyError:
            raise UnknownPluginError(
                f"Plugin '{plugin_id}' is not registered for '{kind}'"
            ) from None

    def list_plugins(self, kind: str) -> tuple[str, ...]:
        """Return a sorted immutable snapshot of IDs; unknown kinds are empty."""
        _check_identifier(kind, "Plugin kind")
        return tuple(sorted(self._factories.get(kind, {})))

    def list_kinds(self) -> tuple[str, ...]:
        """Return a sorted immutable snapshot of registered plugin kinds."""
        return tuple(sorted(self._factories))

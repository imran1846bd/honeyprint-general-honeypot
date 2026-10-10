"""Shared lifecycle contract for built-in and developer-owned platform adapters.

Concrete adapters implement platform behavior and register their factories under
``platform_adapter`` in PluginRegistry. This module installs no software and
provides no default launch or simulated success behavior. Abstract methods ensure
incomplete subclasses cannot be instantiated; semantic compatibility and resource
ownership still require validation and integration tests.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Mapping


EventSink = Callable[[Mapping[str, Any]], None]


class AdapterError(RuntimeError):
    """A platform adapter could not complete a requested operation."""


class PlatformAdapter(ABC):
    """Common interface for OctoPrint, Klipper and custom platform adapters.

    ``platform_id`` identifies the platform served by this implementation.
    Configuration, context and plan mappings are caller-supplied inputs; adapters
    must not mutate them. A successful lifecycle call must reflect actual platform
    behavior rather than merely accepting configuration.

    The returned instance handle must retain resource ownership information so
    managed resources can be distinguished from externally managed resources.
    Ownership enforcement is the concrete adapter's responsibility.
    """

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """Return the platform identifier, including a developer-defined one."""
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> Mapping[str, Any]:
        """Describe implemented modes, simulators, services and supported stages.

        Declare deployment, simulators, services and stages as identifier lists.
        Optional lists cover extensions, frontends, apis and instrumentation.
        A recipes mapping assigns a registered recipe ID to each deployment
        mode. The selected recipe declares explicit software version pins.
        """
        raise NotImplementedError

    @abstractmethod
    def validate(
        self, cfg: Mapping[str, Any], context: Mapping[str, Any]
    ) -> None:
        """Check platform-specific compatibility before preparation or launch.

        Raise AdapterError for unsupported capabilities or missing dependencies.
        Validation must not install software or start platform processes.
        """
        raise NotImplementedError

    @abstractmethod
    def prepare(self, plan: Mapping[str, Any]) -> None:
        """Prepare managed resources or inspect resources selected for attachment.

        Only use the validated plan. Preserve externally managed configuration
        and processes in attach mode; raise AdapterError on preparation failure.
        """
        raise NotImplementedError

    @abstractmethod
    def launch_or_attach(self, plan: Mapping[str, Any]) -> object:
        """Start managed resources or attach, returning an ownership-aware handle.

        Raise AdapterError on failure and clean up resources created by a failed
        launch. Attaching must not transfer ownership of external processes.
        """
        raise NotImplementedError

    @abstractmethod
    def health(self, instance: object) -> Mapping[str, Any]:
        """Report observed readiness and dependency/instrumentation failures.

        Include enough evidence for the caller to determine readiness. Process
        existence alone is insufficient to establish simulator or stack health.
        """
        raise NotImplementedError

    @abstractmethod
    def observe(self, instance: object, sink: EventSink) -> None:
        """Connect passive observers to a caller-owned event sink.

        Minimize interference with platform behavior. Emit redacted observations
        using the telemetry contract when defined, and surface observer outages
        through health reporting. This hook must return after observer setup.
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self, instance: object, *, owned_only: bool = True) -> None:
        """Detach observers and stop only resources owned by the framework.

        Preserve external platform processes in attach mode. Implementations must
        reject owned_only=False until an explicit external ownership policy exists.
        Repeated stop calls should be safe; failures must raise AdapterError.
        """
        raise NotImplementedError

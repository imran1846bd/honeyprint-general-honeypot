"""Coordinate one instance through its registered platform adapter.

Create one controller per instance. Providers and telemetry persistence are not
started here; the caller supplies the event sink. Adapters retain responsibility
for ownership and rollback when a hook fails before returning a handle.
"""

from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from adapters.base_adapter import EventSink, PlatformAdapter
from core.instance_builder import FrameworkInstance
from core.state_manager import InstancePhase, StateManager
from core.validator import validate_adapter_capabilities


class LifecycleError(RuntimeError):
    """A lifecycle operation could not complete."""


class LifecycleManager:
    """Serialize prepare, launch/attach, observation, health and owned cleanup.

    Health must report an actual boolean ``ready``. Entering running alone never
    establishes readiness. Failed cleanup retains the handle for another stop.
    Failures without a returned handle require adapter-specific recovery; this
    controller cannot prove those resources have been cleaned up.
    """

    def __init__(self, instance: FrameworkInstance, *, base_dir: Path,
                 sink: EventSink) -> None:
        if not isinstance(instance, FrameworkInstance):
            raise LifecycleError("Expected a FrameworkInstance")
        if not isinstance(instance.plan.adapter, PlatformAdapter) or not callable(sink):
            raise LifecycleError("A platform adapter and callable event sink are required")
        self.instance = instance
        self.state = StateManager(instance)
        self._adapter = instance.plan.adapter
        self._sink = sink
        self._base_dir = str(Path(base_dir).resolve())
        self._lock = RLock()
        self._handle = None
        self._attempted = False
        self._plan = MappingProxyType({
            "instance_id": instance.instance_id,
            "config": instance.plan.config,
            "recipe": instance.plan.recipe,
            "factories": instance.plan.factories,
        })

    def _phase(self) -> str:
        return self.state.snapshot()["phase"]

    def _fail(self, operation: str) -> None:
        # Adapter exceptions may contain credentials or platform commands.
        self.state.transition(InstancePhase.FAILED, error=f"{operation} failed")
        raise LifecycleError(f"Instance {operation} failed") from None

    def prepare(self) -> Mapping:
        with self._lock:
            if self._phase() == "prepared":
                return self.state.snapshot()
            if self._phase() not in ("planned", "stopped"):
                raise LifecycleError("Preparation requires a planned or stopped instance")
            self.state.transition(InstancePhase.PREPARING)
            try:
                validate_adapter_capabilities(self._adapter, self.instance.plan.config)
                self._adapter.validate(self.instance.plan.config, MappingProxyType({
                    "base_dir": self._base_dir, "recipe": self.instance.plan.recipe,
                }))
                self._attempted = True
                self._adapter.prepare(self._plan)
            except Exception:
                self._fail("preparation")
            return self.state.transition(InstancePhase.PREPARED)

    def start(self) -> Mapping:
        with self._lock:
            if self._phase() == "running":
                return self.check_health()
            if self._phase() != "prepared":
                raise LifecycleError("Start requires a prepared instance")
            self.state.transition(InstancePhase.STARTING)
            try:
                self._handle = self._adapter.launch_or_attach(self._plan)
                if self._handle is None:
                    raise LifecycleError("Adapter did not return an instance handle")
                self.instance.resources["platform_handle"] = self._handle
                self._adapter.observe(self._handle, self._sink)
            except Exception:
                self._fail("startup")
            self.state.transition(InstancePhase.RUNNING)
            return self.check_health()

    def check_health(self) -> Mapping:
        with self._lock:
            if self._phase() != "running":
                raise LifecycleError("Health checks require a running instance")
            try:
                report = self._adapter.health(self._handle)
                if not isinstance(report, Mapping) or type(report.get("ready")) is not bool:
                    raise LifecycleError("Adapter health must include boolean readiness")
                return self.state.record_health(report["ready"])
            except Exception:
                self._fail("health check")

    def stop(self) -> Mapping:
        with self._lock:
            if self._phase() == "stopped":
                return self.state.snapshot()
            if self._phase() not in ("planned", "prepared", "running", "failed"):
                raise LifecycleError("Instance cannot stop in its current phase")
            self.state.transition(InstancePhase.STOPPING)
            try:
                if self._handle is not None:
                    self._adapter.stop(self._handle, owned_only=True)
                elif self._attempted:
                    # No cleanup token exists in the current adapter contract.
                    raise LifecycleError("Adapter-specific cleanup is required without a handle")
            except Exception:
                self._fail("cleanup")
            self._handle = None
            self._attempted = False
            self.instance.resources.pop("platform_handle", None)
            self.instance.resources.pop("owned_platform_resources", None)
            return self.state.transition(InstancePhase.STOPPED)

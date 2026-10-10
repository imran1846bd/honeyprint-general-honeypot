"""Control lifecycle state transitions for one HoneyPrint instance.

State is in memory only. The lifecycle controller must use one StateManager per
FrameworkInstance and route state writes through it. This module does not call
adapters, launch services, persist state or change resource ownership.
"""

from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from core.instance_builder import FrameworkInstance


class StateError(ValueError):
    """Instance state or a requested transition is invalid."""


class InstancePhase(str, Enum):
    PLANNED = "planned"
    PREPARING = "preparing"
    PREPARED = "prepared"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


_TRANSITIONS = {
    InstancePhase.PLANNED: {InstancePhase.PREPARING, InstancePhase.STOPPING},
    InstancePhase.PREPARING: {InstancePhase.PREPARED, InstancePhase.FAILED},
    InstancePhase.PREPARED: {InstancePhase.STARTING, InstancePhase.STOPPING},
    InstancePhase.STARTING: {InstancePhase.RUNNING, InstancePhase.FAILED},
    InstancePhase.RUNNING: {InstancePhase.STOPPING, InstancePhase.FAILED},
    InstancePhase.STOPPING: {InstancePhase.STOPPED, InstancePhase.FAILED},
    InstancePhase.STOPPED: {InstancePhase.PREPARING},
    # Failed resources must pass through cleanup before another prepare attempt.
    InstancePhase.FAILED: {InstancePhase.STOPPING},
}


class StateManager:
    """Apply guarded state updates to the record created by instance_builder.

    Phase and readiness are separate: entering running leaves ready=False until
    the lifecycle controller supplies a positive health result. A failed instance
    cannot restart until stop/cleanup has succeeded. Stopped-to-preparing permits
    a restart, but the controller must revalidate its plan before preparation.

    The lock serializes calls on this manager. Direct writes to instance.state or
    using multiple managers for the same instance bypass that protection.
    """

    def __init__(self, instance: FrameworkInstance) -> None:
        if not isinstance(instance, FrameworkInstance):
            raise StateError("Expected a FrameworkInstance")
        self._instance = instance
        self._lock = RLock()
        with self._lock:
            self._check_state()

    def _check_state(self) -> InstancePhase:
        state = self._instance.state
        if not isinstance(state, dict):
            raise StateError("Instance state must be a dictionary")
        try:
            phase = InstancePhase(state.get("phase"))
        except (ValueError, TypeError):
            raise StateError("Instance state contains an unknown phase") from None
        if type(state.get("ready")) is not bool:
            raise StateError("Instance readiness must be a boolean")
        if state["ready"] and phase != InstancePhase.RUNNING:
            raise StateError("Only a running instance may be ready")
        error = state.get("error")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise StateError("State error must be a nonempty string or None")
        return phase

    def snapshot(self) -> Mapping[str, Any]:
        """Return an immutable detached snapshot of lifecycle state fields."""
        with self._lock:
            phase = self._check_state()
            return MappingProxyType({
                "instance_id": self._instance.instance_id,
                "phase": phase.value,
                "ready": self._instance.state["ready"],
                "error": self._instance.state.get("error"),
            })

    def transition(
        self, target: str, *, error: str | None = None
    ) -> Mapping[str, Any]:
        """Move to an allowed phase and clear readiness; never call an adapter.

        Failed transitions require a nonempty, redacted error description. Error
        descriptions are retained during cleanup and cleared after successful stop
        or preparation begins. Repeating a phase is rejected; idempotent lifecycle
        operations should be handled by the controller before calling transition.
        """
        try:
            next_phase = InstancePhase(target)
        except (ValueError, TypeError):
            raise StateError("Requested lifecycle phase is unknown") from None
        if next_phase == InstancePhase.FAILED:
            if not isinstance(error, str) or not error.strip():
                raise StateError("A failed transition requires an error description")
        elif error is not None:
            raise StateError("An error description is only allowed for failure")
        with self._lock:
            current = self._check_state()
            if next_phase not in _TRANSITIONS[current]:
                raise StateError(
                    f"Cannot transition from {current.value} to {next_phase.value}"
                )
            self._instance.state.update(phase=next_phase.value, ready=False)
            if next_phase == InstancePhase.FAILED:
                self._instance.state["error"] = error
            elif next_phase in (InstancePhase.STOPPED, InstancePhase.PREPARING):
                self._instance.state.pop("error", None)
            return self.snapshot()

    def record_health(self, ready: bool) -> Mapping[str, Any]:
        """Record a controller-verified health result for a running instance.

        A negative result clears readiness without inventing a lifecycle failure.
        The controller decides whether degraded health requires stop or failure.
        """
        if type(ready) is not bool:
            raise StateError("Health readiness must be a boolean")
        with self._lock:
            if self._check_state() != InstancePhase.RUNNING:
                raise StateError("Health readiness can only be recorded while running")
            self._instance.state["ready"] = ready
            return self.snapshot()

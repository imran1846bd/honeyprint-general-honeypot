"""Run validated instances with bounded telemetry and graceful owned cleanup."""

from pathlib import Path
import math
import threading
import time

from core.dependency_resolver import resolve_dependencies
from core.event_bus import EventBus
from core.instance_builder import build_instances
from core.lifecycle_manager import LifecycleManager
from deployment.control import ControlServer
from telemetry.collector import Collector
from telemetry.normalizer import normalize_event


class FrameworkRuntimeError(RuntimeError):
    """A framework runtime could not start, persist events or stop cleanly."""


class FrameworkRuntime:
    """Foreground controller; SIGINT or the private control channel requests stop.

    Readiness is printed only after adapter evidence and storage readback pass.
    This runner requires storage providers with write_batch, read_event and close.
    Custom adapters use the same registered contracts. No YAML code is imported.
    """

    def __init__(self, config, registry, *, base_dir: Path, runtime_dir: Path):
        self.config = config
        self.registry = registry
        self.base_dir = Path(base_dir)
        self.runtime_dir = Path(runtime_dir).absolute()
        self.stop_event = threading.Event()
        self.bus = EventBus()
        self._controllers = []
        self._storage = None
        self._collector = None
        self._control = None

    def _sink(self, instance_id, platform):
        sink = self.bus.sink(instance_id=instance_id, platform=platform)
        flags = self.config['telemetry']
        def capture(event):
            category = ('capture_requests' if event.get('event_type') == 'http.request'
                        else 'capture_commands' if event.get('event_type') == 'printer.command'
                        else 'capture_state_changes')
            return sink(event) if flags[category] else True
        return capture

    def _lifecycle_event(self, instance, action):
        event = normalize_event({
            'source': 'core', 'event_type': 'framework.lifecycle', 'action': action,
            'outcome': 'completed', 'simulation': True,
            'metadata': {'deployment': instance.deployment},
        }, instance_id=instance.instance_id, platform=instance.plan.config['platform'])
        if not self.bus.publish(event):
            raise FrameworkRuntimeError('Lifecycle evidence could not enter the telemetry queue')
        self._collector.drain()
        if self._storage.read_event(event['event_id']) != event:
            raise FrameworkRuntimeError('Lifecycle evidence was not read back from storage')

    def run(self, *, duration: float | None = None, on_ready=None):
        """Run until duration, Ctrl-C or stop; always attempt cleanup and final drain."""
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            raise ValueError('Run duration must be positive')
        plan = resolve_dependencies(self.config, self.registry, base_dir=self.base_dir)
        instances = build_instances(plan)
        if not instances:
            raise FrameworkRuntimeError('Run requires at least one configured instance')
        if self._controllers or self._storage:
            raise FrameworkRuntimeError('Runtime objects may only run once')
        self._control = ControlServer(self.runtime_dir, self.stop_event)
        self._control.start()
        failure = None
        cleanup_failures = []
        try:
            storage_factory = self.registry.resolve('storage', plan.telemetry['backend'])
            self._storage = storage_factory(self.runtime_dir / 'events.sqlite3')
            if not callable(getattr(self._storage, 'read_event', None)) or not callable(getattr(self._storage, 'close', None)):
                raise FrameworkRuntimeError('Storage provider does not implement runtime verification and close')
            self._collector = Collector(self.bus, self._storage)
            for instance in instances.values():
                if self.stop_event.is_set():
                    break
                controller = LifecycleManager(
                    instance, base_dir=self.base_dir,
                    sink=self._sink(instance.instance_id, instance.plan.config['platform']),
                )
                self._controllers.append(controller)
                controller.prepare()
                report = controller.start()
                deadline = time.monotonic() + 30
                while not report['ready']:
                    self._collector.drain()
                    if self.stop_event.wait(0.2) or time.monotonic() >= deadline:
                        raise FrameworkRuntimeError('Instance did not reach verified readiness')
                    report = controller.check_health()
                self._lifecycle_event(instance, 'start')
                if on_ready:
                    on_ready(instance, report)
            started = time.monotonic()
            while not self.stop_event.wait(0.2):
                self._collector.drain()
                if self.bus.stats()['dropped']:
                    raise FrameworkRuntimeError('Telemetry queue dropped events')
                for controller in self._controllers:
                    if not controller.check_health()['ready']:
                        raise FrameworkRuntimeError('Runtime health or observation coverage degraded')
                if duration is not None and time.monotonic() - started >= duration:
                    break
        except KeyboardInterrupt:
            self.stop_event.set()
        except Exception as exc:
            failure = exc
        finally:
            for controller in reversed(self._controllers):
                try:
                    controller.stop()
                    if self._collector:
                        self._lifecycle_event(controller.instance, 'stop')
                except Exception:
                    cleanup_failures.append(controller.instance.instance_id)
            if self._collector:
                try:
                    self._collector.drain(max_batches=1000)
                    if self._collector.stats()['pending'] or self.bus.stats()['depth']:
                        raise FrameworkRuntimeError('Telemetry remains undrained')
                except Exception:
                    cleanup_failures.append('telemetry')
            if self._storage:
                self._storage.close()
            self._control.close()
        if cleanup_failures:
            raise FrameworkRuntimeError('Runtime cleanup or final persistence failed') from None
        if failure:
            raise FrameworkRuntimeError('Framework runtime failed; inspect owned instance logs') from failure
        return {'instances': len(instances), 'telemetry': dict(self._collector.stats())}

"""Managed upstream Klipper with real AVR firmware and passive state observations.

The MCU and Moonraker remain on an internal Docker network. The loopback bridge
serves Mainsail, native JSON API and G-code files, and forwards live WebSocket RPC.
"""
import math
import re
import threading
import time
from pathlib import Path
from telemetry.artifact_storage import ArtifactStorage
from deployment.mainsail import installed
from adapters.klipper.http_bridge import create_bridge
from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile
from deployment.klipper_environment import KlipperEnvironment, PINS
from plugins.manufacturing.pipeline import STAGES, validate_stages


def revision_matches(version, revision):
    """Match Git's seven to forty digit abbreviated native commit identity."""
    candidates = re.findall(r'(?:^|g|-)([0-9a-f]{7,40})(?=$|[-+])', version)
    return any(revision.startswith(candidate) for candidate in candidates)


class KlipperAdapter(PlatformAdapter):
    platform_id = 'klipper'

    def __init__(self, *, runtime_dir=None):
        self.runtime_dir = Path(runtime_dir or ".honeyprint")
        self.artifacts = None
        self.frontend = None
        self.environment = None
        self.profile = None
        self.stopped = threading.Event()
        self.observer = None
        self.observer_error = False
        self.server = None
        self.server_thread = None
        self.sink = None

    def capabilities(self):
        return {'deployment': ['managed'], 'simulators': ['avr_sim'],
                'services': ['web_port'], 'stages': list(STAGES), 'frontends': ['mainsail'], 'apis': ['moonraker'],
                'recipes': {'managed': 'klipper_managed'}}

    def validate(self, cfg, context):
        if cfg['deployment'] != 'managed' or cfg['simulation'] != 'avr_sim':
            raise AdapterError('Klipper supports managed AVR simulation only')
        load_profile(cfg['printer_profile'])
        validate_stages(cfg.get('stages', ()))

    def prepare(self, plan):
        if self.environment is not None:
            raise AdapterError('Klipper adapter is already prepared')
        self.profile = load_profile(plan['config']['printer_profile'])
        self.frontend = installed() if plan["config"].get("frontend") == "mainsail" else None
        self.artifacts = ArtifactStorage(self.runtime_dir / "artifacts" / plan["config"]["id"])
        self.environment = KlipperEnvironment.prepare()
        self.stopped.clear()
        self.observer_error = False

    def launch_or_attach(self, plan):
        if self.environment is None:
            raise AdapterError('Klipper adapter has not been prepared')
        self.environment.start(self.profile)
        try:
            self.server = create_bridge(self.environment, plan["config"]["services"]["web_port"], lambda event: self.sink(event) if self.sink else None, artifacts=self.artifacts, frontend=self.frontend)
        except Exception:
            self.environment.stop()
            raise AdapterError("Klipper API bridge could not bind") from None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.health(self.environment)["ready"]:
                return self.environment
            time.sleep(0.5)
        self.stop(self.environment)
        raise AdapterError("Klipper did not reach verified MCU readiness")

    def health(self, instance):
        if instance is not self.environment:
            raise AdapterError('Unknown Klipper handle')
        if not instance.running():
            return {'ready': False, 'reason': 'container stopped'}
        try:
            server = instance.request('/server/info')
            state = instance.request('/printer/objects/query?toolhead&extruder&heater_bed&mcu')['status']
            mcu = state['mcu']
            temperature = state['extruder']['temperature']
            ready = (server['klippy_connected'] and server['klippy_state'] == 'ready'
                     and not server['failed_components'] and not server['warnings']
                     and revision_matches(mcu['mcu_version'], PINS['klipper'])
                     and mcu['mcu_constants']['MCU'] == 'atmega644p'
                     and mcu['last_stats'].get('bytes_read', 0) > 0
                     and math.isfinite(temperature) and 0 < temperature < 350
                     and not self.observer_error and not (self.server and self.server.audit_failed))
            return {'ready': bool(ready), 'mcu': 'atmega644p',
                    'mcu_version': mcu['mcu_version'], 'temperature': temperature,
                    'simulation': True, 'virtual_homing': True}
        except (AdapterError, KeyError, ValueError, TypeError):
            return {'ready': False, 'reason': 'native MCU observation unavailable'}

    def observe(self, instance, sink):
        if instance is not self.environment or self.observer is not None:
            raise AdapterError('Invalid Klipper observation setup')
        self.sink = sink
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        def poll():
            previous = None
            while not self.stopped.is_set():
                try:
                    state = instance.request('/printer/objects/query?toolhead&extruder&heater_bed&mcu')['status']
                    metadata = {'position': state['toolhead']['position'],
                                'temperature': state['extruder']['temperature'],
                                'target': state['extruder']['target'],
                                'mcu_bytes_read': state['mcu']['last_stats'].get('bytes_read', 0)}
                    if metadata != previous:
                        accepted = sink({'source': 'klipper', 'event_type': 'printer.state',
                                         'action': 'poll', 'outcome': 'observed',
                                         'simulation': True, 'metadata': metadata})
                        if accepted is False:
                            raise AdapterError('Klipper telemetry was rejected')
                        previous = metadata
                except Exception:
                    self.observer_error = True
                    return
                self.stopped.wait(1)
        self.observer = threading.Thread(target=poll, daemon=True, name='klipper-observer')
        self.observer.start()

    def stop(self, instance, *, owned_only=True):
        if not owned_only or instance is not self.environment:
            raise AdapterError('Klipper cleanup requires its owned handle')
        self.stopped.set()
        if self.server is not None:
            if self.server_thread is not None:
                self.server.shutdown()
                self.server_thread.join(10)
            self.server.server_close()
            self.server = self.server_thread = None
        if self.observer:
            self.observer.join(10)
            if self.observer.is_alive():
                raise AdapterError('Klipper observer did not stop')
            self.observer = None
        instance.stop()
        self.environment = None
        self.sink = None

    def cleanup_prepared(self, plan):
        if self.environment is not None:
            self.stop(self.environment)

"""Managed upstream Klipper with real AVR firmware and passive state observations.

The MCU and Moonraker remain on an internal Docker network. The loopback bridge
serves Mainsail, native JSON API and G-code files, and forwards live WebSocket RPC.
"""
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from telemetry.artifact_storage import ArtifactStorage
from core.storage_paths import artifact_root
from deployment.mainsail import installed
from adapters.klipper.http_bridge import create_bridge
from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile
from deployment.klipper_environment import KlipperEnvironment, PINS
from plugins.manufacturing.pipeline import STAGES, validate_stages
from adapters.klipper.native_transport import request as native_request
from core.gcode_files import safe_label

# A real Klipper printer that hits an MCU shutdown (e.g. "Timer too close" on dense
# G-code) stays online; the operator runs FIRMWARE_RESTART. HoneyPrint does the same
# instead of stopping the honeypot, unless it keeps happening.
RECOVERY_GRACE_SECONDS = 90
RESTART_WINDOW_SECONDS = 600
RESTART_LIMIT = 30  # keep the honeypot online through repeated print-time shutdowns


def revision_matches(version, revision):
    """Match Git's seven to forty digit abbreviated native commit identity."""
    candidates = re.findall(r'(?:^|g|-)([0-9a-f]{7,40})(?=$|[-+])', version)
    return any(revision.startswith(candidate) for candidate in candidates)


class KlipperAdapter(PlatformAdapter):
    platform_id = 'klipper'

    def __init__(self, *, runtime_dir=None, artifact_settings=None):
        from core.runtime_settings import resolve_settings
        self.settings = resolve_settings(artifact_settings)
        self.runtime_dir = Path(runtime_dir or ".honeyprint")
        self.artifacts = None
        self.frontend = None
        self.environment = None
        self.profile = None
        self.stopped = threading.Event()
        self.observer = None
        self.observer_error = False
        self.was_ready = False
        self.server = None
        self.server_thread = None
        self.sink = None
        self.recovering_since = None
        self.restarts = []
        self.resume = None
        self.last_resume = None
        self.last_resume_at = 0.

    def capabilities(self):
        return {'deployment': ['managed'], 'simulators': ['avr_sim'],
                'services': ['web_port'], 'stages': list(STAGES), 'frontends': ['mainsail'], 'apis': ['moonraker'],
                'recipes': {'managed': 'klipper_managed'}}

    def validate(self, cfg, context):
        if cfg['deployment'] != 'managed' or cfg['simulation'] != 'avr_sim':
            raise AdapterError('Klipper supports managed AVR simulation only')
        from deployment.docker.klipper.settings import resolve_klipper_settings
        resolve_klipper_settings(cfg.get('klipper_settings', {}))
        load_profile(cfg['printer_profile'])
        validate_stages(cfg.get('stages', ()))

    def prepare(self, plan):
        if self.environment is not None:
            raise AdapterError('Klipper adapter is already prepared')
        self.profile = load_profile(plan['config']['printer_profile'])
        self.frontend = installed() if plan["config"].get("frontend") == "mainsail" else None
        self.artifacts = ArtifactStorage(artifact_root(self.runtime_dir,self.settings) / plan["config"]["id"],
            max_bytes=(None if self.settings['artifact_max_total_bytes']==104857600 else self.settings['artifact_max_total_bytes']),
            max_artifacts=self.settings["artifact_max_files"],
            max_file_bytes=(None if self.settings['artifact_max_file_bytes']==1048576 else self.settings['artifact_max_file_bytes']))
        self.environment = KlipperEnvironment.prepare()
        self.stopped.clear()
        self.observer_error = False
        self.was_ready = False

    def launch_or_attach(self, plan):
        if self.environment is None:
            raise AdapterError('Klipper adapter has not been prepared')
        self.environment.start(self.profile, plan['config'].get('klipper_settings'))
        try:
            self.server = create_bridge(self.environment, plan["config"]["services"]["web_port"], lambda event: self.sink(event) if self.sink else None, artifacts=self.artifacts, frontend=self.frontend, common_settings=self.settings, printer_profile=self.profile)
        except Exception:
            self.environment.stop()
            raise AdapterError("Klipper API bridge could not bind") from None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.health(self.environment)["ready"]:
                if self.frontend:
                    # Use native paced progress, including for files with slicer offsets.
                    from adapters.klipper.native_transport import request
                    body = json.dumps({
                        'namespace': 'mainsail',
                        'key': 'general.calcPrintProgress',
                        'value': 'file-absolute',
                    }).encode()
                    status, _, _ = request(self.environment, 'POST', '/server/database/item', body)
                    if status != 200:
                        raise AdapterError('Mainsail print progress defaults could not be initialized')
                    # The owned simulator starts at a known in-bounds position.
                    # Mark its axes homed so Mainsail enables both jog directions.
                    volume = self.profile['volume']
                    position = (volume['width'] / 2, volume['depth'] / 2, min(15., volume['height'] / 2))
                    x, y, z = position
                    script = f'SET_KINEMATIC_POSITION X={x} Y={y} Z={z} SET_HOMED=XYZ'
                    body = json.dumps({'script': script}).encode()
                    status, _, _ = request(self.environment, 'POST', '/printer/gcode/script', body)
                    if status != 200:
                        raise AdapterError('Klipper simulator jog position could not be initialized')
                # Restore named uploads after the ephemeral container restarts.
                # Bundled examples are available on a fresh installation too.
                from adapters.klipper.native_transport import upload_file
                root = Path(__file__).resolve().parents[2]
                defaults = [root / 'examples' / 'klipper_print_demo.gcode', *sorted((root / 'gcode').glob('*.gcode'))]
                retained_root = self.artifacts.root / 'uploaded'
                retained = sorted(retained_root.rglob('*.gcode')) if retained_root.exists() else []
                names = {str(path.relative_to(retained_root)) for path in retained}
                sources = [(path, '') for path in defaults if path.is_file() and path.name not in names]
                sources += [(path, '' if path.parent == retained_root else str(path.relative_to(retained_root).parent)) for path in retained]
                for source, directory in sources:
                    status, _, _ = upload_file(self.environment, source.name, source, directory=directory)
                    if status != 201:
                        self.stop(self.environment)
                        raise AdapterError('Klipper G-code could not be restored')
                return self.environment
            time.sleep(0.5)
        self.stop(self.environment)
        raise AdapterError("Klipper did not reach verified MCU readiness")

    def _unhealthy(self, reason):
        if self.was_ready:
            print(f'Klipper runtime became unavailable: {reason}', file=sys.stderr, flush=True)
            self.was_ready = False
            try:
                tail = self.environment.diagnostic_tail()
                if tail:
                    destination = self.runtime_dir / 'klipper-failure.log'
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                                     dir=self.runtime_dir, delete=False) as buffer:
                        pending = Path(buffer.name)
                        try:
                            buffer.write(f'{reason}\n\n{tail[-65536:]}\n')
                            buffer.close()
                            os.replace(pending, destination)
                        finally:
                            pending.unlink(missing_ok=True)
                    print(f'Native Klipper logs: {destination}', file=sys.stderr, flush=True)
            except (AdapterError, OSError):
                pass
        return {'ready': False, 'reason': reason}

    def health(self, instance):
        if instance is not self.environment:
            raise AdapterError('Unknown Klipper handle')
        if self.observer_error:
            return self._unhealthy(self.observer_error)
        if self.server and self.server.audit_failed:
            return self._unhealthy('API audit events could not be queued')
        try:
            if not instance.running():
                return self._unhealthy('owned simulator container stopped')
            server = instance.request('/server/info')
            state = server['klippy_state']
            if self.was_ready and state in ('shutdown', 'error') and server['klippy_connected']:
                return self._recover(instance, state)
            if state != 'ready' or not server['klippy_connected']:
                if (self.recovering_since is not None
                        and time.monotonic() - self.recovering_since < RECOVERY_GRACE_SECONDS):
                    return {'ready': True, 'recovering': True, 'klippy_state': state}
                return self._unhealthy('Moonraker lost its ready Klipper connection')
            if server['failed_components'] or server['warnings']:
                return self._unhealthy('Moonraker reported a component failure or warning')
            state = instance.request('/printer/objects/query?toolhead&extruder&heater_bed&mcu')['status']
            mcu = state['mcu']
            temperature = state['extruder']['temperature']
            if not (revision_matches(mcu['mcu_version'], PINS['klipper'])
                    and mcu['mcu_constants']['MCU'] == 'atmega644p'
                    and mcu['last_stats'].get('bytes_read', 0) > 0
                    and math.isfinite(temperature) and 0 < temperature < 350):
                # Right after a board reset the counters and thermistor readings are still settling.
                if (self.recovering_since is not None
                        and time.monotonic() - self.recovering_since < RECOVERY_GRACE_SECONDS):
                    return {'ready': True, 'recovering': True, 'klippy_state': state}
                return self._unhealthy('simulated MCU telemetry failed validation')
            if self.recovering_since is not None:
                # Recovered only once Klipper is ready and its MCU readings are valid again.
                self.recovering_since = None
                self._record_state('recovered', {'klippy_state': 'ready'})
                if self.resume:
                    self._resume_print(instance, self.resume)
                    self.resume = None
        except (AdapterError, KeyError, ValueError, TypeError):
            if (self.recovering_since is not None
                    and time.monotonic() - self.recovering_since < RECOVERY_GRACE_SECONDS):
                return {'ready': True, 'recovering': True}
            return self._unhealthy('native MCU observation unavailable')
        self.was_ready = True
        return {'ready': True, 'mcu': 'atmega644p', 'mcu_version': mcu['mcu_version'],
                'temperature': temperature, 'simulation': True, 'virtual_homing': True}

    def _record_state(self, action, metadata):
        if self.sink:
            try:
                self.sink({'source': 'klipper', 'event_type': 'printer.state', 'action': action,
                           'outcome': 'observed', 'simulation': True, 'metadata': metadata})
            except Exception:
                pass

    def _recover(self, instance, state):
        """Record an MCU shutdown and restart the firmware, as a printer operator would."""
        now = time.monotonic()
        if self.recovering_since is not None and now - self.recovering_since < RECOVERY_GRACE_SECONDS:
            return {'ready': True, 'recovering': True, 'klippy_state': state}
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW_SECONDS]
        if len(self.restarts) >= RESTART_LIMIT:
            return self._unhealthy('Klipper shut down repeatedly; automatic firmware restarts exhausted')
        reason = ''
        try:
            _, _, body = native_request(instance, 'GET', '/printer/info')
            reason = json.loads(body)['result'].get('state_message', '')
        except Exception:
            pass
        self._record_state('shutdown', {'klippy_state': state, 'reason': safe_label(str(reason), 240)})
        self.resume = self._print_to_resume(instance, str(reason))
        if (self.resume is None and self.last_resume and now - self.last_resume_at < 180
                and 'M112' not in str(reason) and self._job_state(instance) in ('standby', 'printing', 'paused', 'error')):
            # The board crashed again while the previous resume was reheating or homing:
            # keep the same resume point instead of dropping the print.
            self.resume = self.last_resume
        try:
            # FIRMWARE_RESTART cannot reset the emulated board, so reset it directly,
            # then restart the Klipper host so it reconnects to the fresh MCU.
            instance.reset_mcu()
            time.sleep(2)
            status, _, _ = native_request(instance, 'POST', '/printer/restart', b'{}')
            if not 200 <= status < 300:
                raise AdapterError(f'restart returned HTTP {status}')
        except Exception:
            return self._unhealthy('Klipper shut down and the automatic firmware restart failed')
        self.restarts.append(now)
        self.recovering_since = now
        self._record_state('firmware_restart', {'reason': 'automatic recovery', 'restarts_in_window': len(self.restarts)})
        return {'ready': True, 'recovering': True, 'klippy_state': state}

    def _print_to_resume(self, instance, reason):
        """Save a print the emulated board interrupted, so it can continue after recovery.

        Only crashes of the emulated MCU are resumed ("Timer too close", "Move queue
        overflow", ...). An M112 emergency stop is a real action and is never resumed.
        """
        if 'M112' in reason or 'emergency' in reason.lower():
            return None
        try:
            _, _, body = native_request(instance, 'GET',
                '/printer/objects/query?print_stats&virtual_sdcard&gcode_move&extruder&heater_bed')
            status = json.loads(body)['result']['status']
            job, card, move = status['print_stats'], status['virtual_sdcard'], status['gcode_move']
            progress, position = card.get('progress') or 0, card.get('file_position') or 0
            # A crash right at the start (position 0) restarts the file from the beginning.
            # Klipper reports an interrupted print as "paused" at the moment of the MCU shutdown.
            if job.get('state') not in ('printing', 'paused', 'error') or not job.get('filename') or progress >= 1 or position < 0:
                return None
            x, y, z, e = move['gcode_position']
            return {'filename': job['filename'], 'file_position': int(position), 'x': x, 'y': y, 'z': z, 'e': e,
                    'absolute': bool(move.get('absolute_coordinates', True)),
                    'absolute_extrude': bool(move.get('absolute_extrude', True)),
                    'hotend': status['extruder'].get('target') or 0, 'bed': status['heater_bed'].get('target') or 0}
        except Exception:
            return None

    def _job_state(self, instance):
        try:
            _, _, body = native_request(instance, 'GET', '/printer/objects/query?print_stats')
            return json.loads(body)['result']['status']['print_stats'].get('state')
        except Exception:
            return None

    def _resume_print(self, instance, saved):
        """Reheat, return to the saved position and continue the same file from where it stopped."""
        self.last_resume, self.last_resume_at = saved, time.monotonic()
        lines = []
        if saved['bed']: lines += [f"M140 S{saved['bed']:.0f}", f"M190 S{saved['bed']:.0f}"]
        if saved['hotend']: lines += [f"M104 S{saved['hotend']:.0f}", f"M109 S{saved['hotend']:.0f}"]
        lines += ['G28', 'G90', f"G1 Z{saved['z'] + 2:.3f} F600", f"G1 X{saved['x']:.3f} Y{saved['y']:.3f} F3000",
                  f"G1 Z{saved['z']:.3f} F600", f"G92 E{saved['e'] if saved['absolute_extrude'] else 0:.5f}",
                  'G90' if saved['absolute'] else 'G91', 'M82' if saved['absolute_extrude'] else 'M83',
                  f"M23 {saved['filename']}", f"M26 S{saved['file_position']}", 'M24']
        try:
            status, _, _ = native_request(instance, 'POST', '/printer/gcode/script',
                                          json.dumps({'script': '\n'.join(lines)}).encode())
            outcome = 'observed' if 200 <= status < 300 else 'rejected'
        except Exception:
            outcome = 'rejected'
        if self.sink:
            try:
                self.sink({'source': 'klipper', 'event_type': 'printer.job', 'action': 'resumed', 'outcome': outcome,
                           'simulation': True, 'metadata': {'filename': saved['filename'].rsplit('/', 1)[-1][:120],
                                                            'file_position': saved['file_position'],
                                                            'reason': 'automatic resume after MCU recovery'}})
            except Exception:
                pass

    def observe(self, instance, sink):
        if instance is not self.environment or self.observer is not None:
            raise AdapterError('Invalid Klipper observation setup')
        self.sink = sink
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        def poll():
            previous = None
            previous_job = None
            failing_since = None
            while not self.stopped.is_set():
                try:
                    state = instance.request('/printer/objects/query?toolhead&extruder&heater_bed&mcu&print_stats&virtual_sdcard')['status']
                    metadata = {'position': state['toolhead']['position'],
                                'temperature': state['extruder']['temperature'],
                                'target': state['extruder']['target'],
                                'mcu_bytes_read': state['mcu']['last_stats'].get('bytes_read', 0)}
                    if metadata != previous:
                        accepted = sink({'source': 'klipper', 'event_type': 'printer.state',
                                         'action': 'poll', 'outcome': 'observed',
                                         'simulation': True, 'metadata': metadata})
                        if accepted is False:
                            self.observer_error = 'printer state events could not be queued'
                            return
                        previous = metadata
                    job = state['print_stats']
                    name = job.get('filename')
                    progress = state.get('virtual_sdcard', {}).get('progress')
                    job_state = job['state']
                    job_metadata = {'filename': name.rsplit('/', 1)[-1][:120] if isinstance(name, str) else None,
                                    'state': job_state,
                                    'progress': round(progress * 100, 2) if type(progress) in (int, float)
                                    and math.isfinite(progress) else None,
                                    'position': state['toolhead']['position'],
                                    'temperature': state['extruder']['temperature'],
                                    'bed_temperature': state.get('heater_bed', {}).get('temperature')}
                    if job_state == 'error' and job.get('message'):
                        # Klipper's own reason, e.g. "Move exceeds maximum extrusion".
                        job_metadata['reason'] = safe_label(str(job['message']), 200)
                    active = job_state in ('printing', 'paused')
                    if job_metadata != previous_job and (active or previous_job and previous_job['state'] in ('printing', 'paused')):
                        accepted = sink({'source': 'klipper', 'event_type': 'printer.job',
                                         'action': 'progress' if active else job_state,
                                         'outcome': 'observed', 'simulation': True,
                                         'metadata': job_metadata})
                        if accepted is False:
                            self.observer_error = 'print job events could not be queued'
                            return
                    previous_job = job_metadata
                    failing_since = None
                except Exception:
                    # Klipper is briefly unavailable during a firmware restart; keep polling.
                    failing_since = failing_since or time.monotonic()
                    if time.monotonic() - failing_since > RECOVERY_GRACE_SECONDS:
                        self.observer_error = 'printer state polling failed'
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

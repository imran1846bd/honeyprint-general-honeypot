"""Real local OctoPrint adapter with owned Virtual Printer deployments.

Managed mode installs the repository's audit plugin into its own basedir. Attach
mode is read-only and polls state using an externally supplied API key; it cannot
claim HTTP or G-code audit coverage. All connections stay on loopback.
"""

from dataclasses import dataclass, field
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile
from deployment.environment_manager import OctoPrintEnvironment
from recipes.octoprint import OCTOPRINT_VERSION


@dataclass
class OctoPrintHandle:
    instance_id: str
    port: int
    api_key: str = field(repr=False)
    profile_id: str
    environment: object = field(default=None, repr=False)
    observer: object = field(default=None, repr=False)
    stop_event: object = field(default_factory=threading.Event, repr=False)
    received: int = 0
    errors: int = 0
    dropped: int = 0
    stopped: bool = False
    lock: object = field(default_factory=threading.RLock, repr=False)

    @property
    def owned_platform_resources(self):
        return self.environment is not None


def api_request(handle, method, path, body=None):
    """Use fixed local HTTP paths without proxy handling or redirect following."""
    connection = http.client.HTTPConnection('127.0.0.1', handle.port, timeout=2)
    try:
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, body=payload, headers={
            'X-Api-Key': handle.api_key, 'Content-Type': 'application/json',
        })
        response = connection.getresponse()
        data = response.read(1048577)
        if len(data) > 1048576 or not 200 <= response.status < 300:
            raise AdapterError('OctoPrint API request failed')
        return json.loads(data) if data else {}
    except (OSError, ValueError, http.client.HTTPException):
        raise AdapterError('OctoPrint API is unavailable or returned invalid data') from None
    finally:
        connection.close()


class OctoPrintAdapter(PlatformAdapter):
    platform_id = 'octoprint'

    def __init__(self, *, runtime_dir: Path, python: Path | None = None,
                 attach_api_key_env: str | None = None, startup_timeout: float = 60):
        self.runtime_dir = Path(runtime_dir).absolute()
        self.python = Path(python).absolute() if python else None
        self.attach_api_key_env = attach_api_key_env
        self.startup_timeout = startup_timeout
        self._environment = None
        self._channel = None
        self._token = None
        self._credentials = None
        self._handle = None

    def capabilities(self):
        return {
            'deployment': ['managed', 'attach'], 'simulators': ['virtual_printer'],
            'services': ['web_port'], 'stages': [], 'extensions': [],
            'instrumentation': ['plugin', 'http', 'gcode', 'state'],
            'recipes': {'managed': 'octoprint_managed', 'attach': 'octoprint_attach'},
        }

    def validate(self, cfg, context):
        profile = load_profile(cfg['printer_profile'])
        if cfg['platform'] != self.platform_id or cfg['simulation'] != 'virtual_printer':
            raise AdapterError('Only the OctoPrint Virtual Printer recipe is supported')
        if context['recipe']['versions'].get('octoprint') != OCTOPRINT_VERSION:
            raise AdapterError('OctoPrint version pin mismatch')
        if cfg.get('instrumentation', {}).get('plugin', 'honeyprint_audit') != 'honeyprint_audit':
            raise AdapterError('Unsupported observation plugin')
        if cfg['deployment'] == 'managed':
            if not self.python:
                raise AdapterError('Managed mode requires a pinned OctoPrint interpreter')
            OctoPrintEnvironment(self.runtime_dir / cfg['id'], self.python, cfg['id']).verify_runtime()
        else:
            if not self.attach_api_key_env or not os.environ.get(self.attach_api_key_env):
                raise AdapterError('Attach mode requires an API-key environment reference')
            flags = cfg.get('instrumentation', {})
            if flags.get('http') or flags.get('gcode') or 'plugin' in flags:
                raise AdapterError('Attach mode supports state polling only')
        if not profile:
            raise AdapterError('Printer profile is unavailable')

    def prepare(self, plan):
        cfg = plan['config']
        if (self._handle and not self._handle.stopped) or self._environment or self._channel:
            raise AdapterError('Adapter already has active resources')
        self._handle = None
        if cfg['deployment'] == 'attach':
            return
        try:
            self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._channel.bind(('127.0.0.1', 0))
            self._channel.settimeout(0.2)
            self._token = secrets.token_hex(32)
            self._environment = OctoPrintEnvironment(
                self.runtime_dir / cfg['id'], self.python, cfg['id'],
            )
            self._credentials = self._environment.prepare(
                cfg, audit_port=self._channel.getsockname()[1], audit_token=self._token,
            )
        except Exception:
            self.cleanup_prepared(plan)
            raise AdapterError('OctoPrint preparation failed') from None

    def launch_or_attach(self, plan):
        cfg = plan['config']
        profile = load_profile(cfg['printer_profile'])
        managed = cfg['deployment'] == 'managed'
        key = self._credentials['api_key'] if managed else os.environ[self.attach_api_key_env]
        handle = OctoPrintHandle(cfg['id'], cfg['services']['web_port'], key, profile['id'],
                                self._environment if managed else None)
        self._handle = handle
        try:
            if managed:
                # Refuse to launch on an occupied local service port.
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', handle.port))
                self._environment.launch(handle.port)
            deadline = time.monotonic() + self.startup_timeout
            while True:
                if managed and self._environment.process.poll() is not None:
                    raise AdapterError('Owned OctoPrint process exited during startup')
                try:
                    version = api_request(handle, 'GET', '/api/version')
                    if version.get('server') != OCTOPRINT_VERSION:
                        raise AdapterError('Running OctoPrint version differs from the recipe')
                    break
                except AdapterError:
                    if not managed or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            if managed:
                api_request(handle, 'POST', '/api/connection', {
                    'command': 'connect', 'port': 'VIRTUAL', 'baudrate': 115200,
                    'printerProfile': profile['id'], 'autoconnect': False,
                })
                while time.monotonic() < deadline:
                    current = api_request(handle, 'GET', '/api/connection')['current']
                    if current.get('state') == 'Operational':
                        break
                    time.sleep(0.2)
                else:
                    raise AdapterError('Virtual Printer did not become operational')
            else:
                self._check_virtual(handle)
            return handle
        except Exception:
            self.stop(handle)
            raise AdapterError('OctoPrint launch or attachment failed') from None

    def _check_virtual(self, handle):
        connection = api_request(handle, 'GET', '/api/connection')['current']
        printer = api_request(handle, 'GET', '/api/printer')
        if connection.get('port') != 'VIRTUAL' or connection.get('printerProfile') != handle.profile_id:
            raise AdapterError('Selected Virtual Printer or profile does not match')
        if not printer.get('state', {}).get('flags', {}).get('operational'):
            raise AdapterError('Virtual Printer is not operational')
        if 'tool0' not in printer.get('temperature', {}):
            raise AdapterError('Virtual Printer has no temperature response')
        return printer

    def health(self, handle):
        self._verify_handle(handle)
        ready = False
        evidence = {}
        try:
            if handle.stopped:
                return {'ready': False}
            if handle.environment and (not handle.environment.process or handle.environment.process.poll() is not None):
                return {'ready': False, 'process': False}
            evidence['version'] = api_request(handle, 'GET', '/api/version').get('server')
            self._check_virtual(handle)
            evidence['virtual_printer'] = True
            if handle.environment:
                audit = api_request(handle, 'GET', '/api/plugin/honeyprint_audit')
                evidence['audit_active'] = audit.get('active') is True and audit.get('instance_id') == handle.instance_id
                evidence['serial_received'] = audit.get('serial_received', 0)
                evidence['audit_dropped'] = audit.get('dropped', 0)
                evidence['audit_errors'] = audit.get('errors', 0)
                ready = (evidence['audit_active'] and evidence['serial_received'] > 0
                         and evidence['audit_dropped'] == 0 and evidence['audit_errors'] == 0)
            else:
                ready = True
            with handle.lock:
                ready = (ready and evidence['version'] == OCTOPRINT_VERSION
                         and handle.received > 0 and handle.errors == 0 and handle.dropped == 0
                         and bool(handle.observer and handle.observer.is_alive()))
                evidence.update(observed=handle.received, observer_errors=handle.errors,
                                observer_dropped=handle.dropped)
        except (AdapterError, KeyError, TypeError):
            ready = False
        return {'ready': bool(ready), **evidence}

    def observe(self, handle, sink):
        self._verify_handle(handle)
        if handle.observer and handle.observer.is_alive():
            raise AdapterError('Observer is already active')
        def consume():
            previous = None
            while not handle.stop_event.is_set():
                try:
                    if handle.environment:
                        raw, address = self._channel.recvfrom(65536)
                        if address[0] != '127.0.0.1':
                            continue
                        record = json.loads(raw)
                        if not isinstance(record.get('token'), str) or not hmac.compare_digest(record['token'], self._token):
                            continue
                        event = record['event']
                    else:
                        printer = self._check_virtual(handle)
                        flags = printer['state']['flags']
                        snapshot = json.dumps(flags, sort_keys=True)
                        if snapshot == previous:
                            handle.stop_event.wait(0.5)
                            continue
                        previous = snapshot
                        event = {'source': 'api', 'event_type': 'printer.state', 'action': 'poll',
                                 'outcome': 'observed', 'simulation': True, 'metadata': {'flags': flags}}
                    accepted = sink(event)
                    with handle.lock:
                        handle.received += 1
                        if accepted is False:
                            handle.dropped += 1
                except socket.timeout:
                    continue
                except Exception:
                    if not handle.stop_event.is_set():
                        with handle.lock:
                            handle.errors += 1
                        handle.stop_event.wait(0.2)
        handle.observer = threading.Thread(target=consume, name=f'Audit-{handle.instance_id}', daemon=True)
        handle.observer.start()

    def _verify_handle(self, handle):
        if handle is not self._handle:
            raise AdapterError('Instance handle does not belong to this adapter')

    def stop(self, handle, *, owned_only=True):
        if owned_only is not True:
            raise AdapterError('Stopping external resources is unsupported')
        self._verify_handle(handle)
        if handle.stopped:
            return
        # Stop the owned server first so its audit worker can send final events.
        if handle.environment:
            handle.environment.cleanup()
        handle.stop_event.set()
        if handle.observer:
            handle.observer.join(timeout=3)
            if handle.observer.is_alive():
                raise AdapterError('Observer did not stop')
        if self._channel:
            self._channel.close()
            self._channel = None
        self._environment = None
        handle.stopped = True
        # Retain identity for idempotent stop; preparation starts a new handle.

    def cleanup_prepared(self, plan):
        if self._handle and not self._handle.stopped:
            self.stop(self._handle)
        if self._environment:
            self._environment.cleanup()
            self._environment = None
        if self._channel:
            self._channel.close()
            self._channel = None
        self._credentials = self._token = None

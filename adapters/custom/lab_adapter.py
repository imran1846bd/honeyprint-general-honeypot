"""Runnable developer-owned example; a small custom HTTP printer simulation.

This example is independent of OctoPrint and Klipper. It supports a deliberately
limited G-code subset, bounded data-only jobs and no physical device or shell execution.
Developers replace this class with their installed platform implementation.
"""

from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from html import escape
from core.storage_paths import artifact_root
import queue
import threading

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile
from telemetry.artifact_storage import ArtifactStorage
from plugins.simulators.printer import PrinterSimulator
from plugins.manufacturing.pipeline import STAGES, validate_stages, run_job


@dataclass
class LabHandle:
    server: object = field(repr=False)
    profile: dict = field(repr=False)
    stages: tuple = ()
    simulator: object = field(default=None, repr=False)
    artifacts: object = field(default=None, repr=False)
    lock: object = field(default_factory=threading.RLock, repr=False)
    events: object = field(default_factory=lambda: queue.Queue(maxsize=1024), repr=False)
    stopped: object = field(default_factory=threading.Event, repr=False)
    server_thread: object = field(default=None, repr=False)
    observer_thread: object = field(default=None, repr=False)
    received: int = 0
    dropped: int = 0
    errors: int = 0
    owned_platform_resources: bool = True

    def __post_init__(self):
        self.simulator = PrinterSimulator(self.profile)

    @property
    def position(self):
        return self.simulator.motion.position

    def emit(self, kind, action, outcome, metadata=None):
        try:
            self.events.put_nowait({'source': 'custom', 'event_type': kind, 'action': action,
                                   'outcome': outcome, 'simulation': True, 'metadata': metadata or {}})
        except queue.Full:
            with self.lock: self.dropped += 1


class LabAdapter(PlatformAdapter):
    platform_id = 'custom_lab'

    def __init__(self, *, runtime_dir=None, artifact_settings=None):
        from core.runtime_settings import resolve_settings
        self._settings = resolve_settings(artifact_settings)
        self._profile = self._handle = self._artifacts = None
        self._runtime_dir = Path(runtime_dir) if runtime_dir is not None else None

    def capabilities(self):
        return {'deployment': ['managed'], 'simulators': ['lab_sim'],
                'services': ['web_port'], 'stages': list(STAGES),
                'recipes': {'managed': 'lab_managed'}}

    def validate(self, cfg, context):
        if cfg['deployment'] != 'managed' or cfg['simulation'] != 'lab_sim':
            raise AdapterError('Custom example supports managed lab_sim only')
        load_profile(cfg['printer_profile'])
        validate_stages(cfg.get('stages', ()))

    def prepare(self, plan):
        if self._handle and not self._handle.stopped.is_set():
            raise AdapterError('Custom instance is already running')
        self._profile = load_profile(plan['config']['printer_profile'])
        if plan['config'].get('stages'):
            if self._runtime_dir is None: raise AdapterError('Manufacturing requires an artifact directory')
            self._artifacts = ArtifactStorage(artifact_root(self._runtime_dir,self._settings) / plan['config']['id'],
                max_bytes=self._settings['artifact_max_total_bytes'],
                max_artifacts=self._settings['artifact_max_files'],
                max_file_bytes=self._settings['artifact_max_file_bytes'])

    def launch_or_attach(self, plan):
        if self._profile is None: raise AdapterError('Custom instance is not prepared')
        adapter = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup(); self.connection.settimeout(2)
            def log_message(self, *args): pass
            def respond(self, code, data, content_type='application/json'):
                body = json.dumps(data).encode() if content_type == 'application/json' else data.encode()
                self.send_response(code); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                h = adapter._handle
                status = 200 if self.path in ('/', '/api/state') else 404
                if self.path == '/':
                    self.respond(200, '<!doctype html><html><title>'+escape(h.profile['name'])+'</title><h1>'+escape(h.profile['name'])+'</h1><p>Custom Printer Lab: simulated printer API at /api/state.</p><button onclick="fetch(\'/api/preheat\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:\'{}\'}).then(r=>r.json()).then(x=>document.getElementById(\'result\').textContent=JSON.stringify(x))">Preheat</button><pre id="result"></pre></html>', 'text/html; charset=utf-8')
                elif self.path == '/api/state':
                    with h.lock: self.respond(200, dict(h.simulator.snapshot(),printer_name=h.profile['name']))
                else: self.respond(404, {'error': 'unknown endpoint'})
                h.emit('http.request', 'GET', 'observed', {'route': 'state' if self.path == '/api/state' else 'other', 'status': status})
            def do_POST(self):
                h = adapter._handle
                status = 200
                try:
                    if self.path not in ('/api/command', '/api/job', '/api/preheat') or self.headers.get('Transfer-Encoding'):
                        raise ValueError()
                    length = int(self.headers.get('Content-Length', '0'))
                    limit = adapter._settings['upload_max_bytes'] if self.path == '/api/job' else adapter._settings['request_max_bytes']
                    if length > limit: raise OverflowError()
                    if length < 1: raise ValueError()
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict): raise ValueError()
                    if self.path == '/api/preheat':
                        if data: raise ValueError()
                        with h.lock:
                            candidate=deepcopy(h.simulator)
                            candidate.command(f"M104 S{adapter._settings['preheat_hotend_c']}")
                            if candidate.heated_bed: candidate.command(f"M140 S{adapter._settings['preheat_bed_c']}")
                            h.simulator=candidate
                            response=candidate.snapshot()
                        self.respond(200,response)
                        h.emit('printer.command','preheat','accepted')
                    elif self.path == '/api/job':
                        if set(data) != {'gcode'} or not isinstance(data['gcode'], str): raise ValueError()
                        with h.lock:
                            content = data['gcode'].encode('utf-8')
                            if not 1 <= len(content) <= h.artifacts.max_file_bytes: raise ValueError()
                            candidate = deepcopy(h.simulator)
                            result, events = run_job(content, candidate, h.stages)
                            artifact = h.artifacts.store(content)
                            h.simulator = candidate
                        for event in events:
                            h.emit('manufacturing.stage', event['stage'], event['outcome'],
                                   artifact)
                        response = {'simulation': True, 'sha256': result['sha256'], 'stage': result['stage']}
                        if 'inspection' in result: response['inspection'] = result['inspection']
                        self.respond(200, response)
                    else:
                        if set(data) != {'command'} or not isinstance(data['command'], str): raise ValueError()
                        with h.lock:
                            response = h.simulator.command(data['command'])
                            h.simulator.step(1.0)
                        self.respond(200, response)
                        h.emit('printer.command', 'execute', 'accepted', {'gcode': response['gcode']})
                    h.emit('printer.state', 'update', 'observed', {'powered': h.simulator.powered})
                except OverflowError:
                    status=413; self.close_connection=True
                    self.respond(413, {'error':'request too large'})
                except (ValueError, TypeError, OSError, IndexError):
                    status = 400
                    self.respond(400, {'error': 'unsupported or invalid command'})
                    h.emit('printer.command', 'execute', 'rejected')
                h.emit('http.request', 'POST', 'observed', {'route': 'job' if self.path == '/api/job' else 'command', 'status': status})
        server = HTTPServer(('127.0.0.1', plan['config']['services']['web_port']), Handler)
        handle = LabHandle(server, self._profile, stages=tuple(plan['config'].get('stages', ())), artifacts=self._artifacts)
        self._handle = handle
        handle.server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        handle.server_thread.start()
        handle.emit('printer.state', 'start', 'operational')
        return handle

    def health(self, handle):
        self._check(handle)
        with handle.lock:
            return {'ready': bool(not handle.stopped.is_set() and handle.server_thread.is_alive()
                                  and handle.observer_thread and handle.observer_thread.is_alive()
                                  and handle.received > 0 and handle.errors == 0 and handle.dropped == 0),
                    'observed': handle.received, 'dropped': handle.dropped, 'errors': handle.errors}

    def observe(self, handle, sink):
        self._check(handle)
        def consume():
            while not handle.stopped.is_set() or not handle.events.empty():
                try: event = handle.events.get(timeout=0.1)
                except queue.Empty: continue
                try:
                    accepted = sink(event)
                    with handle.lock:
                        handle.received += 1
                        if accepted is False: handle.dropped += 1
                except Exception:
                    with handle.lock: handle.errors += 1
                finally: handle.events.task_done()
        handle.observer_thread = threading.Thread(target=consume, daemon=True)
        handle.observer_thread.start()

    def _check(self, handle):
        if handle is not self._handle: raise AdapterError('Custom handle belongs to another adapter')

    def stop(self, handle, *, owned_only=True):
        self._check(handle)
        if owned_only is not True: raise AdapterError('External resource control is unsupported')
        if handle.stopped.is_set(): return
        if handle.server_thread and handle.server_thread.is_alive():
            handle.server.shutdown()
        handle.server.server_close()
        if handle.server_thread and handle.server_thread.ident is not None:
            handle.server_thread.join(timeout=3)
        handle.stopped.set()
        if handle.observer_thread:
            handle.observer_thread.join(timeout=3)
            if handle.observer_thread.is_alive(): raise AdapterError('Custom observer did not stop')
        self._profile = None

    def cleanup_prepared(self, plan):
        if self._handle and not self._handle.stopped.is_set(): self.stop(self._handle)
        self._profile = None

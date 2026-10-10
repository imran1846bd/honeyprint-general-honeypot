"""Runnable developer-owned example; a small custom HTTP printer simulation.

This example is independent of OctoPrint and Klipper. It supports a deliberately
limited command subset, no uploads and no physical device or shell execution.
Developers replace this class with their installed platform implementation.
"""

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import queue
import re
import threading

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile


@dataclass
class LabHandle:
    server: object = field(repr=False)
    profile: dict = field(repr=False)
    position: dict = field(default_factory=lambda: {'x': 0.0, 'y': 0.0, 'z': 0.0})
    lock: object = field(default_factory=threading.RLock, repr=False)
    events: object = field(default_factory=lambda: queue.Queue(maxsize=1024), repr=False)
    stopped: object = field(default_factory=threading.Event, repr=False)
    server_thread: object = field(default=None, repr=False)
    observer_thread: object = field(default=None, repr=False)
    received: int = 0
    dropped: int = 0
    errors: int = 0
    owned_platform_resources: bool = True

    def emit(self, kind, action, outcome, metadata=None):
        try:
            self.events.put_nowait({'source': 'custom', 'event_type': kind, 'action': action,
                                   'outcome': outcome, 'simulation': True, 'metadata': metadata or {}})
        except queue.Full:
            with self.lock: self.dropped += 1


class LabAdapter(PlatformAdapter):
    platform_id = 'custom_lab'

    def __init__(self): self._profile = self._handle = None

    def capabilities(self):
        return {'deployment': ['managed'], 'simulators': ['lab_sim'],
                'services': ['web_port'], 'stages': [],
                'recipes': {'managed': 'lab_managed'}}

    def validate(self, cfg, context):
        if cfg['deployment'] != 'managed' or cfg['simulation'] != 'lab_sim':
            raise AdapterError('Custom example supports managed lab_sim only')
        load_profile(cfg['printer_profile'])

    def prepare(self, plan):
        if self._handle and not self._handle.stopped.is_set():
            raise AdapterError('Custom instance is already running')
        self._profile = load_profile(plan['config']['printer_profile'])

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
                if self.path == '/':
                    self.respond(200, '<!doctype html><html><title>Custom Printer Lab</title><h1>Custom Printer Lab</h1><p>Simulated printer API is available at /api/state.</p></html>', 'text/html; charset=utf-8')
                elif self.path == '/api/state':
                    with h.lock: self.respond(200, {'simulation': True, 'position': dict(h.position), 'temperature': 20.0})
                else: self.respond(404, {'error': 'unknown endpoint'})
                h.emit('http.request', 'GET', 'observed', {'route': 'state' if self.path == '/api/state' else 'other'})
            def do_POST(self):
                h = adapter._handle
                try:
                    if self.path != '/api/command' or self.headers.get('Transfer-Encoding'):
                        raise ValueError()
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= 4096: raise ValueError()
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict) or set(data) != {'command'} or not isinstance(data['command'], str):
                        raise ValueError()
                    command = data['command'].strip().upper()
                    if command == 'M105':
                        response = {'response': 'ok T:20.0 /0.0 B:20.0 /0.0', 'simulation': True}
                        code = 'M105'
                    elif command == 'G28':
                        with h.lock: h.position.update(x=0.0, y=0.0, z=0.0)
                        response, code = {'response': 'ok', 'simulation': True}, 'G28'
                    else:
                        tokens = command.split()
                        if not tokens or tokens[0] not in ('G0', 'G1') or len(tokens) == 1:
                            raise ValueError()
                        values = {}
                        for token in tokens[1:]:
                            if not re.fullmatch(r'[XYZ][+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', token):
                                raise ValueError()
                            axis = token[0].lower()
                            if axis in values: raise ValueError()
                            value = float(token[1:])
                            dimension = {'x': 'width', 'y': 'depth', 'z': 'height'}[axis]
                            if not 0 <= value <= h.profile['volume'][dimension]: raise ValueError()
                            values[axis] = value
                        with h.lock: h.position.update(values)
                        response, code = {'response': 'ok', 'simulation': True}, tokens[0]
                    self.respond(200, response)
                    h.emit('printer.command', 'execute', 'accepted', {'gcode': code})
                except (ValueError, TypeError, OSError, IndexError):
                    self.respond(400, {'error': 'unsupported or invalid command'})
                    h.emit('printer.command', 'execute', 'rejected')
                h.emit('http.request', 'POST', 'observed', {'route': 'command'})
        server = HTTPServer(('127.0.0.1', plan['config']['services']['web_port']), Handler)
        handle = LabHandle(server, self._profile)
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
        handle.server.shutdown(); handle.server.server_close()
        handle.server_thread.join(timeout=3)
        handle.stopped.set()
        if handle.observer_thread:
            handle.observer_thread.join(timeout=3)
            if handle.observer_thread.is_alive(): raise AdapterError('Custom observer did not stop')
        self._profile = None

    def cleanup_prepared(self, plan):
        if self._handle and not self._handle.stopped.is_set(): self.stop(self._handle)
        self._profile = None

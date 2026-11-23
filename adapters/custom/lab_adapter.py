"""Runnable developer-owned example; a small custom HTTP printer simulation.

This example is independent of OctoPrint and Klipper. It supports a deliberately
limited G-code subset, bounded data-only jobs and no physical device or shell execution.
Developers replace this class with their installed platform implementation.
"""

from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass, field
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import math
from html import escape
import re
from core.storage_paths import artifact_root
import queue
import threading
import time

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.quiet_http import QuietThreadingHTTPServer
from core.profile_loader import load_profile
from telemetry.artifact_storage import ArtifactStorage
from telemetry.ui_capture import SCRIPT, observation
from core.gcode_files import safe_label, valid_gcode_name
from urllib.parse import unquote
from plugins.simulators.printer import PrinterSimulator
from plugins.manufacturing.pipeline import STAGES, validate_stages, run_job
from plugins.manufacturing.job_intake import job_intake
from plugins.manufacturing.build_preparation import build_preparation

# With the default (unset) upload limit, accept real slicer files up to this size, like Klipper.
DEFAULT_UPLOAD_CAP = 536870912


class UploadRejected(ValueError):
    """A refused upload, with the submitted name and reason kept for evidence."""
    def __init__(self, reason, filename=None):
        super().__init__(reason)
        self.filename = filename


@dataclass
class LabHandle:
    server: object = field(repr=False)
    profile: dict = field(repr=False)
    stages: tuple = ()
    simulator: object = field(default=None, repr=False)
    artifacts: object = field(default=None, repr=False)
    lock: object = field(default_factory=threading.RLock, repr=False)
    events: object = field(default_factory=lambda: queue.Queue(maxsize=65536), repr=False)
    stopped: object = field(default_factory=threading.Event, repr=False)
    server_thread: object = field(default=None, repr=False)
    observer_thread: object = field(default=None, repr=False)
    received: int = 0
    dropped: int = 0
    errors: int = 0
    owned_platform_resources: bool = True
    files: dict = field(default_factory=dict, repr=False)
    selected: str | None = None
    job_name: str | None = None
    job_state: str = 'idle'
    job_progress: float = 0.0
    job_generation: int = 0
    job_worker: object = field(default=None, repr=False)

    def __post_init__(self):
        self.simulator = PrinterSimulator(self.profile)

    @property
    def position(self):
        return self.simulator.motion.position

    def emit(self, kind, action, outcome, metadata=None):
        try:
            # Wait briefly for the collector instead of dropping evidence during bursts.
            self.events.put({'source': 'custom', 'event_type': kind, 'action': action,
                             'outcome': outcome, 'simulation': True, 'metadata': metadata or {}}, timeout=2)
        except queue.Full:
            with self.lock: self.dropped += 1

    def run_selected_job(self):
        with self.lock:
            name = self.selected
            if not name or self.artifacts is None or self.job_state in ('printing','paused'):
                raise ValueError('No available selected job')
            content = self.artifacts.read(self.files[name])
            prepared = build_preparation(job_intake(content, name=name), self.simulator)
            commands = prepared['commands']
            self.job_name = name
            self.job_progress = 0.0
            self.job_state = 'printing'
            self.job_generation += 1
            generation = self.job_generation
            self.emit('manufacturing.stage', 'build_preparation', 'completed', {'filename': name})

        def work():
            try:
                for index, command in enumerate(commands, 1):
                    started = False
                    origin = None
                    distance = 0.0
                    while not self.stopped.is_set():
                        with self.lock:
                            if generation != self.job_generation or self.job_state == 'cancelled': return
                            if self.job_state != 'paused':
                                if not started:
                                    origin = dict(self.simulator.motion.position)
                                    self.simulator.command(command)
                                    distance = math.dist(origin.values(), self.simulator.motion.target.values())
                                    started = True
                                    self.emit('printer.command', 'execute', 'accepted',
                                              {'filename': name, 'line': index, 'gcode': command})
                                self.simulator.step(0.1)
                                remaining = math.dist(self.simulator.motion.position.values(),
                                                      self.simulator.motion.target.values())
                                fraction = 1.0 - remaining / distance if distance else 1.0
                                self.job_progress = ((index - 1) + fraction) * 100.0 / len(commands)
                                self.emit('printer.job', 'progress', 'observed',
                                          {'filename': name, 'progress': self.job_progress,
                                           'state': self.job_state,
                                           'position': dict(self.simulator.motion.position),
                                           'temperature': self.simulator.hotend.temperature,
                                           'bed_temperature': self.simulator.bed.temperature})
                                if remaining <= 1e-6: break
                        self.stopped.wait(0.1)
                    if self.stopped.is_set(): return
                with self.lock:
                    if generation != self.job_generation or self.job_state == 'cancelled': return
                    self.job_state = 'complete'
                    self.emit('printer.job', 'complete', 'observed',
                              {'filename': name, 'progress': self.job_progress,
                               'position': dict(self.simulator.motion.position)})
                    self.emit('manufacturing.stage', 'build_execution', 'completed', {'filename': name})
            except (ValueError, OSError, IndexError):
                with self.lock:
                    if generation != self.job_generation or self.job_state == 'cancelled': return
                    self.job_state = 'error'
                    self.emit('printer.job', 'failed', 'observed',
                              {'filename': name, 'progress': self.job_progress})
                    self.emit('manufacturing.stage', 'build_execution', 'failed', {'filename': name})

        self.job_worker = threading.Thread(target=work, daemon=True)
        self.job_worker.start()


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
        if plan['config'].get('stages') and self._runtime_dir is None:
            raise AdapterError('Manufacturing requires an artifact directory')
        if self._runtime_dir is not None:
            # Uploads need the evidence store whether or not manufacturing stages are enabled.
            # Default limits mean "no fixed limit", as on Klipper; explicit settings still apply.
            settings = self._settings
            self._artifacts = ArtifactStorage(artifact_root(self._runtime_dir,settings) / plan['config']['id'],
                max_bytes=None if settings['artifact_max_total_bytes'] == 104857600 else settings['artifact_max_total_bytes'],
                max_artifacts=settings['artifact_max_files'],
                max_file_bytes=None if settings['artifact_max_file_bytes'] == 1048576 else settings['artifact_max_file_bytes'])

    def launch_or_attach(self, plan):
        if self._profile is None: raise AdapterError('Custom instance is not prepared')
        adapter = self
        class Handler(BaseHTTPRequestHandler):
            server_version = 'nginx/1.18.0'; sys_version = ''  # do not announce Python's BaseHTTPServer
            def setup(self):
                super().setup(); self.connection.settimeout(2)
                self.connection_seen = False; self.connection_started = time.monotonic()
            def handle(self):
                try: super().handle()
                finally:
                    if not self.connection_seen and adapter._handle:
                        # Port scan or service probe: connected without a usable HTTP request.
                        raw = getattr(self, 'raw_requestline', b'') or b''
                        adapter._handle.emit('network.connection', 'connect', 'no_data' if not raw else 'invalid',
                            {'service': 'http', 'port': self.server.server_port,
                             'peer': str(self.client_address[0])[:64], 'bytes_received': len(raw),
                             'duration_ms': int((time.monotonic() - self.connection_started) * 1000)})
            def agent(self):
                # The client's self-declared name: browser or scanner tool (nmap, nikto, sqlmap, ...).
                value = self.headers.get('User-Agent') if self.headers else None
                return {'user_agent': safe_label(value, 160, path=True)} if value else {}
            def send_error(self, code, message=None, explain=None):
                if not self.connection_seen and adapter._handle:
                    self.connection_seen = True
                    raw = getattr(self, 'raw_requestline', b'') or b''
                    adapter._handle.emit('http.request', 'INVALID', 'observed',
                        {'route': 'malformed', 'status': int(code), 'bytes_received': len(raw),
                         'peer': str(self.client_address[0])[:64]})
                super().send_error(code, message, explain)
            def log_message(self, *args): pass
            def respond(self, code, data, content_type='application/json'):
                body = json.dumps(data).encode() if content_type == 'application/json' else data.encode()
                self.send_response(code); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.connection_seen = True
                h = adapter._handle
                status = 200 if self.path in ('/', '/api/state', '/api/job', '/api/files', '/honeyprint-interactions.js') or self.path.startswith('/api/files/') else 404
                if self.path == '/':
                    page = '''<!doctype html><html><meta charset="utf-8"><title>HoneyPrint Custom Lab</title>
<h1>''' + escape(h.profile['name']) + '''</h1><p>Custom Printer Lab virtual job and sensor status</p>
<input id="file" type="file" accept=".gcode"><button onclick="upload()">Upload G-code</button>
<select id="names"></select><button onclick="job('select')">Select</button>
<button onclick="job('start')">Start</button><button onclick="job('pause')">Pause</button>
<button onclick="job('resume')">Resume</button><button onclick="job('cancel')">Cancel</button>
<button onclick="post('/api/preheat',{}).then(refresh)">Preheat</button>
<pre id="state"></pre><pre id="message"></pre><script>
async function post(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const x=await r.json();if(!r.ok)throw Error(x.error||'Request failed');return x}
async function upload(){try{const f=document.getElementById('file').files[0];if(!f)throw Error('Choose a G-code file');await post('/api/files',{name:f.name,gcode:await f.text()});document.getElementById('message').textContent='Uploaded '+f.name;await refresh()}catch(e){document.getElementById('message').textContent=e.message}}
async function job(command){try{const name=document.getElementById('names').value;await post('/api/job',command==='select'?{command,filename:name}:{command});document.getElementById('message').textContent=command+' accepted';await refresh()}catch(e){document.getElementById('message').textContent=e.message}}
async function refresh(){try{const [s,j,f]=await Promise.all(['/api/state','/api/job','/api/files'].map(p=>fetch(p).then(r=>r.json())));document.getElementById('state').textContent=JSON.stringify({printer:s,job:j},null,2);const e=document.getElementById('names');const keep=e.value;e.replaceChildren(...f.files.map(n=>new Option(n,n)));if(f.files.includes(keep))e.value=keep}catch(e){document.getElementById('message').textContent=e.message}}
setInterval(refresh,500);refresh();</script><script src="/honeyprint-interactions.js" defer></script></html>'''
                    self.respond(200, page, 'text/html; charset=utf-8')
                elif self.path == '/honeyprint-interactions.js':
                    self.respond(200, SCRIPT.decode('utf-8'), 'text/javascript; charset=utf-8')
                elif self.path == '/api/state':
                    with h.lock: self.respond(200, dict(h.simulator.snapshot(),printer_name=h.profile['name'],
                        job_state=h.job_state,job_name=h.job_name,job_progress=h.job_progress))
                elif self.path == '/api/job':
                    with h.lock: self.respond(200, {'state':h.job_state,'filename':h.job_name,
                                                    'selected':h.selected,'progress':h.job_progress})
                elif self.path == '/api/files':
                    with h.lock: self.respond(200, {'files':sorted(h.files)})
                elif self.path.startswith('/api/files/'):
                    name=unquote(self.path[len('/api/files/'):])
                    if not valid_gcode_name(name) or name not in h.files:
                        status=404;self.respond(404, {'error':'unknown file'})
                    else:
                        with h.lock: content=h.artifacts.read(h.files[name])
                        self.respond(200,content.decode('utf-8','replace'),'text/plain; charset=utf-8')
                else: self.respond(404, {'error': 'unknown endpoint'})
                if self.path not in ('/api/state', '/api/job', '/api/files') or status >= 400:
                    h.emit('http.request', 'GET', 'observed',
                           {'route': 'root' if self.path == '/' else self.path[:128], 'status': status, **self.agent()})
            def do_POST(self):
                self.connection_seen = True
                h = adapter._handle
                status = 200
                upload_bytes = 0; request_digest = None
                try:
                    if self.path == '/honeyprint/interactions':
                        length = int(self.headers.get('Content-Length', '0'))
                        origin = self.headers.get('Origin')
                        if (self.headers.get('Content-Type') != 'application/json' or not 1 <= length <= 1024
                                or self.headers.get('Transfer-Encoding')
                                or (origin and origin != 'http://' + self.headers.get('Host', ''))):
                            raise ValueError()
                        event = observation(json.loads(self.rfile.read(length)))
                        h.emit(event['event_type'], event['action'], event['outcome'], event['metadata'])
                        self.respond(200, {})
                        return
                    if self.path not in ('/api/command', '/api/job', '/api/preheat', '/api/files') or self.headers.get('Transfer-Encoding'):
                        raise ValueError()
                    length = int(self.headers.get('Content-Length', '0'))
                    upload_limit = adapter._settings['upload_max_bytes']
                    upload_cap = DEFAULT_UPLOAD_CAP if upload_limit == 1048576 else upload_limit*2+512
                    limit = (upload_cap if self.path == '/api/files'
                             else upload_limit if self.path == '/api/job'
                             else adapter._settings['request_max_bytes'])
                    upload_bytes = length
                    if length > limit: raise OverflowError()
                    if length < 1: raise ValueError()
                    if self.path == '/api/files': self.connection.settimeout(60)
                    body = self.rfile.read(length)
                    if len(body) != length: raise ValueError()
                    if self.path == '/api/files': request_digest = hashlib.sha256(body).hexdigest()
                    data = json.loads(body)
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
                    elif self.path == '/api/files':
                        name = data.get('name')
                        if not isinstance(name, str) or not (set(data) == {'name','gcode'} or set(data) == {'name','gcode_base64'}):
                            raise UploadRejected('Invalid upload request', name)
                        if not valid_gcode_name(name): raise UploadRejected('Unsupported G-code filename', name)
                        try:
                            # gcode_base64 keeps exact bytes (console uploads); gcode is the browser's text.
                            content = (base64.b64decode(data['gcode_base64'], validate=True) if 'gcode_base64' in data
                                       else data['gcode'].encode('utf-8'))
                        except (binascii.Error, TypeError, AttributeError, UnicodeError):
                            raise UploadRejected('Invalid G-code content', name) from None
                        if not content: raise UploadRejected('Empty G-code file', name)
                        if b'\0' in content: raise UploadRejected('Binary content is not G-code text', name)
                        with h.lock:
                            if h.artifacts is None: raise UploadRejected('Evidence storage is unavailable', name)
                            try: artifact=h.artifacts.store(content)
                            except ValueError as error: raise UploadRejected(str(error), name) from None
                            h.files[name]=artifact['sha256']
                        self.respond(200, {'name':name,'size_bytes':len(content)})
                        h.emit('file.artifact','upload','accepted', {'filename':name, 'sha256':artifact['sha256'],
                                                                      'size_bytes':len(content),'artifact':artifact['artifact']})
                    elif self.path == '/api/job' and 'command' in data:
                        action=data.get('command')
                        if action=='select':
                            name=data.get('filename')
                            if set(data)!={'command','filename'} or not valid_gcode_name(name): raise ValueError()
                            with h.lock:
                                if name not in h.files or h.job_state in ('printing','paused'): raise ValueError()
                                h.selected=name;h.job_state='idle';h.job_progress=0.0
                        else:
                            if set(data)!={'command'}: raise ValueError()
                            if action=='start': h.run_selected_job()
                            elif action in ('pause','resume','cancel'):
                                with h.lock:
                                    expected={'pause':'printing','resume':'paused','cancel':('printing','paused')}
                                    if h.job_state not in (expected[action] if isinstance(expected[action],tuple) else (expected[action],)): raise ValueError()
                                    h.job_state={'pause':'paused','resume':'printing','cancel':'cancelled'}[action]
                                    if action == 'cancel':
                                        h.job_generation += 1
                                        h.simulator.motion.target = dict(h.simulator.motion.position)
                            else: raise ValueError()
                        self.respond(200, {'command':action,'state':h.job_state,'filename':h.selected})
                        h.emit('printer.job',action,'accepted',{'filename':h.selected})
                    elif self.path == '/api/job':
                        if set(data) != {'gcode'} or not isinstance(data['gcode'], str): raise ValueError()
                        with h.lock:
                            if h.job_state in ('printing','paused'): raise ValueError()
                            content = data['gcode'].encode('utf-8')
                            if not 1 <= len(content) <= (h.artifacts.max_file_bytes or DEFAULT_UPLOAD_CAP): raise ValueError()
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
                    if self.path == '/api/files':
                        h.emit('file.artifact','upload','rejected',{'reason':'Upload exceeds the configured limit',
                                                                     'status':413,'request_bytes':upload_bytes})
                except (ValueError, TypeError, OSError, IndexError) as error:
                    status = 400
                    self.respond(400, {'error': 'unsupported or invalid command'})
                    if self.path == '/api/files':
                        meta = {'reason': safe_label(str(error) if isinstance(error, UploadRejected)
                                                     else 'Invalid or unsupported upload data', 160),
                                'status': 400, 'request_bytes': upload_bytes}
                        if getattr(error, 'filename', None): meta['filename'] = safe_label(error.filename)
                        if request_digest: meta['request_sha256'] = request_digest
                        h.emit('file.artifact','upload','rejected',meta)
                    else:
                        h.emit('printer.command', 'execute', 'rejected')
                h.emit('http.request', 'POST', 'observed', {'route': 'job' if self.path == '/api/job' else 'upload' if self.path == '/api/files' else 'command', 'status': status, **self.agent()})
        # Threaded, so a large upload does not block the page's live status requests.
        server = QuietThreadingHTTPServer(('127.0.0.1', plan['config']['services']['web_port']), Handler)
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
        if handle.job_worker and handle.job_worker.is_alive():
            handle.job_worker.join(timeout=3)
        if handle.observer_thread:
            handle.observer_thread.join(timeout=3)
            if handle.observer_thread.is_alive(): raise AdapterError('Custom observer did not stop')
        self._profile = None

    def cleanup_prepared(self, plan):
        if self._handle and not self._handle.stopped.is_set(): self.stop(self._handle)
        self._profile = None

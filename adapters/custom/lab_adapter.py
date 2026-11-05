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
import re
from core.storage_paths import artifact_root
import queue
import threading

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.profile_loader import load_profile
from telemetry.artifact_storage import ArtifactStorage
from plugins.simulators.printer import PrinterSimulator
from plugins.manufacturing.pipeline import STAGES, validate_stages, run_job
from plugins.manufacturing.job_intake import job_intake
from plugins.manufacturing.build_preparation import build_preparation

FILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.gcode$', re.I)


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
    files: dict = field(default_factory=dict, repr=False)
    selected: str | None = None
    job_name: str | None = None
    job_state: str = 'idle'
    job_progress: float = 0.0
    job_worker: object = field(default=None, repr=False)

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
            self.emit('manufacturing.stage', 'build_preparation', 'completed', {'filename': name})

        def work():
            try:
                for index, command in enumerate(commands, 1):
                    while not self.stopped.is_set():
                        with self.lock:
                            if self.job_state == 'cancelled': return
                            if self.job_state != 'paused':
                                self.simulator.command(command)
                                self.simulator.step(1.0)
                                self.job_progress = index * 100.0 / len(commands)
                                self.emit('printer.state', 'progress', 'observed',
                                          {'filename': name, 'progress': self.job_progress})
                                break
                        self.stopped.wait(.1)
                    if self.stopped.wait(.4): return
                with self.lock:
                    self.job_state = 'complete'
                    self.emit('manufacturing.stage', 'build_execution', 'completed', {'filename': name})
            except (ValueError, OSError, IndexError):
                with self.lock:
                    self.job_state = 'error'
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
                status = 200 if self.path in ('/', '/api/state', '/api/job', '/api/files') or self.path.startswith('/api/files/') else 404
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
setInterval(refresh,500);refresh();</script></html>'''
                    self.respond(200, page, 'text/html; charset=utf-8')
                elif self.path == '/api/state':
                    with h.lock: self.respond(200, dict(h.simulator.snapshot(),printer_name=h.profile['name'],
                        job_state=h.job_state,job_name=h.job_name,job_progress=h.job_progress))
                elif self.path == '/api/job':
                    with h.lock: self.respond(200, {'state':h.job_state,'filename':h.job_name,
                                                    'selected':h.selected,'progress':h.job_progress})
                elif self.path == '/api/files':
                    with h.lock: self.respond(200, {'files':sorted(h.files)})
                elif self.path.startswith('/api/files/'):
                    name=self.path[len('/api/files/'):]
                    if not FILE_NAME.fullmatch(name) or name not in h.files:
                        status=404;self.respond(404, {'error':'unknown file'})
                    else:
                        with h.lock: content=h.artifacts.read(h.files[name])
                        self.respond(200,content.decode('utf-8'),'text/plain; charset=utf-8')
                else: self.respond(404, {'error': 'unknown endpoint'})
                if self.path not in ('/api/state', '/api/job', '/api/files') or status >= 400:
                    h.emit('http.request', 'GET', 'observed',
                           {'route': 'root' if self.path == '/' else self.path[:128], 'status': status})
            def do_POST(self):
                h = adapter._handle
                status = 200
                try:
                    if self.path not in ('/api/command', '/api/job', '/api/preheat', '/api/files') or self.headers.get('Transfer-Encoding'):
                        raise ValueError()
                    length = int(self.headers.get('Content-Length', '0'))
                    limit = (adapter._settings['upload_max_bytes']*2+512 if self.path == '/api/files'
                             else adapter._settings['upload_max_bytes'] if self.path == '/api/job'
                             else adapter._settings['request_max_bytes'])
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
                    elif self.path == '/api/files':
                        if set(data) != {'name','gcode'} or not isinstance(data['name'],str) or not FILE_NAME.fullmatch(data['name']) or not isinstance(data['gcode'],str): raise ValueError()
                        content=data['gcode'].encode('utf-8')
                        with h.lock:
                            if h.artifacts is None: raise ValueError()
                            artifact=h.artifacts.store(content)
                            h.files[data['name']]=artifact['sha256']
                        self.respond(200, {'name':data['name'],'size_bytes':len(content)})
                        h.emit('file.artifact','upload','accepted', {'filename':data['name'], 'sha256':artifact['sha256']})
                    elif self.path == '/api/job' and 'command' in data:
                        action=data.get('command')
                        if action=='select':
                            name=data.get('filename')
                            if set(data)!={'command','filename'} or not isinstance(name,str) or not FILE_NAME.fullmatch(name): raise ValueError()
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
                            else: raise ValueError()
                        self.respond(200, {'command':action,'state':h.job_state,'filename':h.selected})
                        h.emit('printer.job',action,'accepted',{'filename':h.selected})
                    elif self.path == '/api/job':
                        if set(data) != {'gcode'} or not isinstance(data['gcode'], str): raise ValueError()
                        with h.lock:
                            if h.job_state in ('printing','paused'): raise ValueError()
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
                h.emit('http.request', 'POST', 'observed', {'route': 'job' if self.path == '/api/job' else 'upload' if self.path == '/api/files' else 'command', 'status': status})
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
        if handle.job_worker and handle.job_worker.is_alive():
            handle.job_worker.join(timeout=3)
        if handle.observer_thread:
            handle.observer_thread.join(timeout=3)
            if handle.observer_thread.is_alive(): raise AdapterError('Custom observer did not stop')
        self._profile = None

    def cleanup_prepared(self, plan):
        if self._handle and not self._handle.stopped.is_set(): self.stop(self._handle)
        self._profile = None

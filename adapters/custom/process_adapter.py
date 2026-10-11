"""Run a developer-selected trusted HTTP server behind the framework evidence bridge.

This is a process owner and HTTP proxy, not a sandbox for untrusted source code.
The entry file binds HONEYPRINT_HOST/HONEYPRINT_PORT and echoes its private
HONEYPRINT_INSTANCE_TOKEN as X-HoneyPrint-Instance on its health endpoint.
"""
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,HTTPServer
from adapters.base_adapter import AdapterError,PlatformAdapter
from core.profile_loader import load_profile


class CustomProcessAdapter(PlatformAdapter):
    platform_id='custom'
    def __init__(self,*,entry,python=None,health_path='/',runtime_dir=None):
        self.entry=Path(entry).absolute();self.python=Path(python or sys.executable).absolute()
        self.health_path=health_path;self.runtime_dir=Path(runtime_dir or '.honeyprint')
        self.process=None;self.server=None;self.thread=None;self.log_thread=None
        self.sink=None;self.failed=False;self.token=None
    def capabilities(self):
        return {'deployment':['managed'],'simulators':['custom_process'],'services':['web_port'],
                'stages':[],'recipes':{'managed':'custom_process_managed'}}
    def validate(self,cfg,context):
        if not self.entry.is_file() or self.entry.suffix!='.py' or not self.python.is_file():
            raise AdapterError('Custom entry file and Python interpreter must exist')
        if not self.health_path.startswith('/') or len(self.health_path)>256 or '\r' in self.health_path or '\n' in self.health_path:
            raise AdapterError('Custom health path is invalid')
        load_profile(cfg['printer_profile'])
    def prepare(self,plan):
        self.validate(plan['config'],{})
        if self.process is not None:raise AdapterError('Custom code instance already prepared')
        self.profile=load_profile(plan['config']['printer_profile'])
        self.log=self.runtime_dir/plan['instance_id']/'custom.log'
        self.log.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.token=secrets.token_urlsafe(32);self.failed=False
        self.front_port=plan['config']['services']['web_port']
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));self.backend_port=probe.getsockname()[1]
    def _start_backend(self):
        environment=dict(os.environ,HONEYPRINT_HOST='127.0.0.1',HONEYPRINT_PORT=str(self.backend_port),
                         HONEYPRINT_INSTANCE_TOKEN=self.token,HONEYPRINT_PROFILE_JSON=json.dumps(self.profile))
        self.process=subprocess.Popen([str(self.python),'-u',str(self.entry)],cwd=self.entry.parent,
                                      env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,start_new_session=True)
        def capture_log():
            with self.log.open('wb') as output:
                os.chmod(self.log,0o600)
                while True:
                    data=self.process.stdout.read1(4096)
                    if not data:break
                    if output.tell()+len(data)>1048576:output.seek(0);output.truncate()
                    output.write(data);output.flush()
        self.log_thread=threading.Thread(target=capture_log,daemon=True);self.log_thread.start()
    def _connection(self, timeout):
        return http.client.HTTPConnection('127.0.0.1', self.backend_port, timeout=timeout)
    def _target_path(self, path):
        return path
    def launch_or_attach(self,plan):
        try:
            self._start_backend()
            adapter=self
            class Handler(BaseHTTPRequestHandler):
                def setup(self):super().setup();self.connection.settimeout(3)
                def log_message(self,*args):pass
                def do_GET(self):self.forward()
                def do_POST(self):self.forward()
                def do_PUT(self):self.forward()
                def do_DELETE(self):self.forward()
                def forward(self):
                    status=400;body=b'{"error":"invalid request"}';headers=[]
                    connection=None
                    try:
                        if not self.path.startswith('/') or self.path.startswith('//') or len(self.path)>8192 or self.headers.get('Transfer-Encoding'):
                            raise ValueError()
                        length=int(self.headers.get('Content-Length','0'))
                        if not 0<=length<=1048576:raise ValueError()
                        data=self.rfile.read(length)
                        if len(data)!=length:raise ValueError()
                        selected={key:self.headers[key] for key in ('Content-Type','Authorization','Cookie','Accept') if key in self.headers}
                        connection=adapter._connection(10)
                        connection.request(self.command,adapter._target_path(self.path),body=data or None,headers=selected)
                        response=connection.getresponse();body=response.read(2097153)
                        if len(body)>2097152:raise ValueError()
                        status=response.status
                        headers=[(key,value) for key,value in response.getheaders() if key.lower() in ('content-type','set-cookie','location')]
                    except Exception:status=502;body=b'{"error":"custom backend request failed"}'
                    finally:
                        if connection:connection.close()
                    self.send_response(status)
                    for key,value in headers:self.send_header(key,value)
                    self.send_header('Content-Length',str(len(body)));self.end_headers()
                    try:self.wfile.write(body)
                    finally:
                        if adapter.sink:
                            try:
                                accepted=adapter.sink({'source':'custom','event_type':'http.request','action':self.command,
                                                      'outcome':'observed','simulation':True,'metadata':{'route':'root' if self.path=='/' else 'other','status':status}})
                                if accepted is False:adapter.failed=True
                            except Exception:adapter.failed=True
            self.server=HTTPServer(('127.0.0.1',self.front_port),Handler)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if self.health(self)['ready']:return self
                if self.process is not None and self.process.poll() is not None:break
                time.sleep(.1)
            raise AdapterError('Custom code failed its owned HTTP health check; inspect custom.log')
        except Exception:
            self.stop(self);raise
    def health(self,instance):
        if instance is not self or self.process is None or self.process.poll() is not None or self.failed:
            return {'ready':False}
        connection=http.client.HTTPConnection('127.0.0.1',self.backend_port,timeout=2)
        try:
            connection.request('GET',self.health_path);response=connection.getresponse()
            return {'ready':bool(200<=response.status<400 and response.getheader('X-HoneyPrint-Instance')==self.token)}
        except OSError:return {'ready':False}
        finally:connection.close()
    def observe(self,instance,sink):
        if instance is not self or self.thread is not None:raise AdapterError('Invalid custom observer handle')
        self.sink=sink;self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def stop(self,instance,*,owned_only=True):
        if instance is not self or not owned_only:raise AdapterError('Custom cleanup requires an owned handle')
        if self.server:
            if self.thread:self.server.shutdown();self.thread.join(15)
            self.server.server_close();self.server=None;self.thread=None
        if self.process:
            # The owned process group also contains workers launched by the custom server.
            try:os.killpg(self.process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=5)
            if self.log_thread:
                self.log_thread.join(2)
                if self.log_thread.is_alive():
                    try:os.killpg(self.process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    self.log_thread.join(5)
                    if self.log_thread.is_alive():raise AdapterError("Custom workers did not release owned log stream")
            self.process.stdout.close();self.process=None;self.log_thread=None
        self.sink=None
    def cleanup_prepared(self,plan):self.stop(self)

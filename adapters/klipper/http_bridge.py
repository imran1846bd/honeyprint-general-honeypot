"""Concurrent loopback Mainsail, native HTTP/upload and live WebSocket bridge."""
import hashlib
import json
from hashlib import sha256
import mimetypes
from pathlib import Path
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs,unquote
from adapters.base_adapter import AdapterError
from core.quiet_http import QuietHandlingMixin
from adapters.klipper.native_transport import request, upload_file
from adapters.klipper.uploads import UploadRejected,prepare_upload,prepare_upload_file
from core.gcode_files import safe_label,valid_gcode_name
from adapters.klipper.websocket_bridge import serve,command_events
from telemetry.ui_capture import SCRIPT, observation

GET_ROUTES={
 '/server/info','/server/config','/server/gcode_store','/server/temperature_store',
 '/printer/info','/printer/objects/list','/printer/objects/query','/printer/gcode/help',
 '/server/files/list','/server/files/directory','/server/files/metadata',
 '/server/database/list','/server/database/item','/server/history/list','/server/history/totals',
 '/server/webcams/list','/server/announcements/list','/server/job_queue/status',
 '/machine/system_info','/machine/proc_stats','/machine/update/status','/access/oneshot_token',
}
POST_ROUTES={
 '/printer/gcode/script','/printer/emergency_stop','/printer/restart','/printer/firmware_restart',
 '/printer/print/start','/printer/print/pause','/printer/print/resume','/printer/print/cancel',
 '/server/files/upload','/server/database/item','/printer/honeyprint/preheat',
}


LOOPBACK_NAMES=('127.0.0.1','localhost','[::1]')


def browser_host(host_header):
    """The loopback name the browser used, so Mainsail calls the API on the same origin.

    Mainsail sends uploads to the hostname in config.json. If the page was opened as
    localhost but config.json said 127.0.0.1, the upload became cross-origin and the
    browser blocked it ("Cannot upload file!") before it reached the printer.
    """
    name=str(host_header or '').rsplit(':',1)[0] if not str(host_header or '').startswith('[') else str(host_header).split(']')[0]+']'
    return name if name in LOOPBACK_NAMES else '127.0.0.1'


def native_reason(output):
    """Moonraker's own error message for a refused request, bounded for evidence."""
    try:
        error=json.loads(output).get('error')
        message=error.get('message') if isinstance(error,dict) else error
        return safe_label(str(message),160) if message else ''
    except (ValueError,AttributeError,TypeError):
        return ''


class BridgeServer(QuietHandlingMixin, ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args):
        self.stopping=threading.Event();self.audit_failed=False
        self.capacity=threading.BoundedSemaphore(16)
        self.clients={};self.lock=threading.Lock()
        super().__init__(*args)
    def process_request(self,connection,address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(connection);return
        try: super().process_request(connection,address)
        except Exception: self.capacity.release();raise
    def process_request_thread(self,connection,address):
        with self.lock: self.clients[connection]=threading.current_thread()
        try: super().process_request_thread(connection,address)
        finally:
            with self.lock: self.clients.pop(connection,None)
            self.capacity.release()
    def server_close(self):
        self.stopping.set()
        super().server_close()
        with self.lock: clients=list(self.clients.items())
        for connection,_ in clients:
            try: connection.shutdown(2)
            except OSError: pass
        for _,thread in clients: thread.join(40)
        if any(thread.is_alive() for _,thread in clients):
            raise AdapterError('Klipper bridge workers did not stop')


def create_bridge(environment,port,emit,*,artifacts=None,frontend=None,common_settings=None,printer_profile=None):
    from core.runtime_settings import resolve_settings
    settings=resolve_settings(common_settings)
    class Handler(BaseHTTPRequestHandler):
        # MainsailOS serves Mainsail through nginx; do not announce Python's BaseHTTPServer.
        server_version='nginx/1.18.0';sys_version=''
        rbufsize=0
        def setup(self):
            super().setup();self.connection.settimeout(5)
            self.connection_seen=False;self.connection_started=time.monotonic()
        def handle(self):
            try: super().handle()
            finally:
                if not self.connection_seen:
                    # Port scan or service probe: connected without a usable HTTP request.
                    raw=getattr(self,'raw_requestline',b'') or b''
                    try:
                        self.record({'source':'klipper','event_type':'network.connection','action':'connect',
                                     'outcome':'no_data' if not raw else 'invalid','simulation':True,
                                     'metadata':{'service':'http','port':self.server.server_port,
                                                 'peer':str(self.client_address[0])[:64],'bytes_received':len(raw),
                                                 'duration_ms':int((time.monotonic()-self.connection_started)*1000)}})
                    except Exception: pass
        def send_error(self,code,message=None,explain=None):
            if not self.connection_seen:
                # The request could not even be parsed (e.g. a binary service probe).
                self.connection_seen=True
                raw=getattr(self,'raw_requestline',b'') or b''
                try:
                    self.record({'source':'klipper','event_type':'http.request','action':'INVALID',
                                 'outcome':'observed','simulation':True,
                                 'metadata':{'route':'malformed','status':int(code),'bytes_received':len(raw),
                                             'peer':str(self.client_address[0])[:64]}})
                except Exception: pass
            super().send_error(code,message,explain)
        def log_message(self,*args): pass
        def record(self,event):
            try:
                if emit(event) is False: raise AdapterError('Evidence rejected')
            except Exception:
                self.server.audit_failed=True
                raise
        def respond(self,status,body,content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            self.connection_seen=True
            if self.path=='/websocket' and self.headers.get('Upgrade','').lower()=='websocket':
                serve(self,environment,self.record,self.server.stopping);return
            self.forward(False)
        def do_POST(self): self.connection_seen=True;self.forward(True)
        def do_DELETE(self): self.connection_seen=True;self.forward(False)
        def do_HEAD(self): self.unsupported()
        def do_OPTIONS(self): self.unsupported()
        def do_PUT(self): self.unsupported()
        def do_PATCH(self): self.unsupported()
        def unsupported(self):
            self.connection_seen=True
            # Methods the printer API does not serve are still interactions: answer and record them.
            path=urlsplit(self.path).path if len(self.path)<=8192 else ''
            if self.command=='OPTIONS':
                self.send_response(204);self.send_header('Allow','GET, POST, DELETE');self.send_header('Content-Length','0')
                self.end_headers();status=204
            else:
                body=b'' if self.command=='HEAD' else b'{"error":"method not allowed"}'
                self.send_response(405);self.send_header('Allow','GET, POST, DELETE')
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);status=405
            metadata={'route':path if path in GET_ROUTES or path in POST_ROUTES else 'other','status':status}
            if metadata['route']=='other' and path:
                metadata['route_fingerprint']=sha256(path.encode()).hexdigest()[:12]
            self.record({'source':'klipper','event_type':'http.request','action':self.command,
                         'outcome':'observed','simulation':True,'metadata':metadata})
        def forward(self,post):
            status=400;output=b'{"error":"unsupported request"}';content_type='application/json'
            route='other';path='';artifact=None;spooled=None;upload=None
            # Every upload attempt leaves a file.artifact record; rejected ones keep the reason.
            rejected=None;received=hashlib.sha256();upload_size=0;upload_recorded=False;spa_route=None;spa_page=False
            try:
                url=urlsplit(self.path)
                if url.scheme or url.netloc or url.fragment or len(self.path)>8192: raise ValueError()
                path=url.path;query=parse_qs(url.query,keep_blank_values=True)
                if path=='/honeyprint/interactions' and self.command=='POST':
                    route=path
                    size=int(self.headers.get('Content-Length','0'))
                    origin=self.headers.get('Origin')
                    if (self.headers.get('Content-Type')!='application/json' or not 1 <= size <= 1024
                            or self.headers.get('Transfer-Encoding')
                            or (origin and origin != 'http://' + self.headers.get('Host',''))):
                        raise ValueError('Invalid browser observation')
                    event=observation(json.loads(self.rfile.read(size)))
                    self.record(event)
                    status=204;output=b''
                elif path=='/honeyprint-interactions.js' and self.command=='GET' and frontend is not None:
                    route=path;status=200;content_type='text/javascript; charset=utf-8';output=SCRIPT
                elif path=='/server/honeyprint/profile' and self.command=='GET':
                    route=path;status=200;output=json.dumps(printer_profile or {}).encode()
                elif frontend is not None and self.command=='GET' and not path.startswith(('/server/','/printer/','/machine/','/access/')):
                    route='frontend' if path=='/' else 'asset'
                    if path=='/config.json':
                        output=json.dumps({'defaultLocale':'en','defaultMode':'dark','defaultTheme':'mainsail',
                                           'hostname':browser_host(self.headers.get('Host')),'port':self.server.server_port,'path':None,
                                           'instancesDB':'moonraker','instances':[]}).encode()
                        status=200
                    else:
                        file=(Path(frontend)/unquote(path).lstrip('/')).resolve()
                        # Mainsail is a single-page app: like a real printer's web server, page
                        # addresses such as /files or /console (no file extension) load index.html,
                        # so reloading a page works. Missing files (/.env, /x.php) stay 404.
                        spa_page=path!='/' and '.' not in path.rsplit('/',1)[-1] and not file.is_file()
                        if path=='/' or spa_page:
                            file=Path(frontend)/'index.html'
                            if spa_page:
                                route='frontend';spa_route=safe_label(unquote(path),160,path=True)
                        if not file.is_relative_to(Path(frontend).resolve()) or file.name.startswith('.') or not file.is_file():
                            status=404;output=b'{"error":"unknown asset"}'
                        else:
                            if file.stat().st_size>16777216: raise ValueError()
                            output=file.read_bytes();content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream';status=200
                            if path=='/' or spa_page:
                                output=output.replace(b'</body>', b'<script src="/honeyprint-interactions.js" defer></script></body>')
                else:
                    download=path.startswith('/server/files/gcodes/')
                    allowed=(path in POST_ROUTES if post else path in GET_ROUTES or download)
                    if self.command=='DELETE': allowed=download
                    if not allowed:
                        status=404;output=b'{"error":"unknown endpoint"}'
                    elif self.headers.get('Transfer-Encoding'):
                        raise ValueError()
                    else:
                        route=path if not download else '/server/files/gcodes/artifact'
                        if download:
                            filename=unquote(path[len('/server/files/gcodes/'):])
                            if not valid_gcode_name(filename):
                                raise ValueError('Invalid G-code path')
                        body=None;ctype=self.headers.get('Content-Type','application/json')
                        if post:
                            size=int(self.headers.get('Content-Length','0'));upload_size=size
                            explicit_upload_limit = path == '/server/files/upload' and settings['upload_max_bytes'] != 1048576
                            if (size < 0 or (path != '/server/files/upload' and size > settings['request_max_bytes'])
                                    or (explicit_upload_limit and size > settings['upload_max_bytes']+131072)):
                                raise OverflowError('Request exceeds limit')
                            if path=='/server/files/upload':
                                if not size or artifacts is None: raise ValueError('Empty upload')
                                if explicit_upload_limit:
                                    body=self.rfile.read(size);received.update(body)
                                    if len(body)!=size: raise ValueError('Incomplete body')
                                    body,ctype,artifact=prepare_upload(body,ctype,artifacts,max_file_bytes=settings['upload_max_bytes'])
                                else:
                                    with tempfile.NamedTemporaryFile(prefix='.upload-', dir=artifacts.root, delete=False) as stream:
                                        spooled=Path(stream.name)
                                        remaining=size
                                        self.connection.settimeout(60)
                                        while remaining:
                                            chunk=self.rfile.read(min(1048576,remaining))
                                            if not chunk: raise ValueError('Incomplete body')
                                            stream.write(chunk);received.update(chunk);remaining-=len(chunk)
                                    upload=prepare_upload_file(spooled,ctype,artifacts)
                                    artifact=upload[3]
                            else:
                                body=self.rfile.read(size)
                                if len(body)!=size: raise ValueError('Incomplete body')
                            if path != '/server/files/upload' and body:
                                data=json.loads(body)
                                if not isinstance(data,dict): raise ValueError()
                                if path=='/printer/gcode/script':
                                    if set(data)!={'script'} or not isinstance(data['script'],str): raise ValueError()
                                    command_events(data['script'],self.record)
                                if path=='/printer/print/start':
                                    filename=data.get('filename','')
                                    if not valid_gcode_name(filename.rsplit('/',1)[-1] if isinstance(filename,str) else None): raise ValueError()
                                if path=='/server/database/item' and data.get('namespace') not in ('mainsail','webcams'):
                                    raise ValueError('Protected namespace')
                        if path=='/printer/print/start' and url.query:
                            filename=query.get('filename',[''])[0]
                            if not valid_gcode_name(filename.rsplit('/',1)[-1]): raise ValueError()
                        target=self.path
                        if path=='/printer/honeyprint/preheat':
                            if body and json.loads(body)!={}: raise ValueError()
                            script=f"M104 S{settings['preheat_hotend_c']}"
                            if printer_profile is None or printer_profile['heated_bed']: script+=f"\nM140 S{settings['preheat_bed_c']}"
                            command_events(script,self.record)
                            target='/printer/gcode/script';body=json.dumps({'script':script}).encode();ctype='application/json'
                        if upload:
                            replaced=(artifacts.root / 'uploaded' / upload[1] / upload[0]).is_file()
                            status,content_type,output=upload_file(
                                environment,upload[0],artifacts.root / artifact['artifact'],upload[2],upload[1])
                            if 200 <= status < 300:
                                # Moonraker's /data is ephemeral. Keep the accepted
                                # named file beside the durable evidence artifacts.
                                import os, shutil, uuid
                                retained = artifacts.root / 'uploaded' / upload[1] / upload[0]
                                retained.parent.mkdir(parents=True, exist_ok=True)
                                pending = retained.with_name('.pending-' + uuid.uuid4().hex)
                                try:
                                    shutil.copyfile(artifacts.root / artifact['artifact'], pending)
                                    os.replace(pending, retained)
                                finally:
                                    pending.unlink(missing_ok=True)
                        else:
                            status,content_type,output=request(environment,self.command,target,body,ctype)
                        if artifact:
                            if upload:
                                artifact={**artifact, 'filename':upload[0], 'directory':upload[1], 'print':upload[2],
                                          'replaced_existing':replaced}
                            if not 200<=status<300:
                                artifact={**artifact, 'status':status, 'reason':native_reason(output) or 'Refused by the printer'}
                            upload_recorded=True
                            self.record({'source':'klipper','event_type':'file.artifact','action':'upload',
                                         'outcome':'accepted' if 200<=status<300 else 'rejected',
                                         'simulation':True,'metadata':artifact})
            except OverflowError:
                self.close_connection=True
                status=413;output=b'{"error":"request too large"}'
                rejected='Upload exceeds the configured limit'
            except (ValueError,UnicodeError) as error:
                status=400;output=b'{"error":"invalid or unsupported data"}'
                rejected=(str(error) if isinstance(error,UploadRejected) else 'Invalid or unsupported upload data',
                          getattr(error,'filename',None))
            except Exception:
                status=502;output=b'{"error":"native request failed"}'
                rejected='Printer backend did not answer'
            finally:
                if spooled is not None: spooled.unlink(missing_ok=True)
            try: self.respond(status,output,content_type)
            finally:
                if rejected and path=='/server/files/upload' and not upload_recorded:
                    reason,submitted=rejected if isinstance(rejected,tuple) else (rejected,None)
                    meta={**(artifact or {}),'reason':safe_label(reason,160),'status':status,'request_bytes':upload_size}
                    if upload: submitted=upload[0]
                    if submitted: meta['filename']=safe_label(submitted)
                    if upload_size and received.digest()!=hashlib.sha256().digest():
                        meta['request_sha256']=received.hexdigest()
                    try:
                        self.record({'source':'klipper','event_type':'file.artifact','action':'upload',
                                     'outcome':'rejected','simulation':True,'metadata':meta})
                    except Exception:
                        pass
                metadata={'route':route,'status':status}
                agent=self.headers.get('User-Agent') if self.headers else None
                if agent:
                    # The client's self-declared name: browser or scanner tool (nmap, nikto, sqlmap, ...).
                    metadata['user_agent']=safe_label(agent,160,path=True)
                if spa_route: metadata['page']=spa_route
                if route=='/server/files/gcodes/artifact':
                    # Which stored print file was downloaded or deleted.
                    metadata['filename']=safe_label(unquote(path[len('/server/files/gcodes/'):]),240)
                if route=='other' and path:
                    metadata['route_group']=(
                        '/server/files/*' if path.startswith('/server/files/') else
                        '/printer/*' if path.startswith('/printer/') else
                        '/server/*' if path.startswith('/server/') else
                        '/machine/*' if path.startswith('/machine/') else
                        '/api/*' if path.startswith('/api/') else
                        '/other/*')
                    metadata['route_fingerprint']=sha256(path.encode()).hexdigest()[:12]
                self.record({'source':'klipper','event_type':'http.request','action':self.command,
                             'outcome':'observed','simulation':True,'metadata':metadata})
    return BridgeServer(('127.0.0.1',port),Handler)

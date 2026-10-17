"""Concurrent loopback Mainsail, native HTTP/upload and live WebSocket bridge."""
import json
import mimetypes
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs,unquote
from adapters.base_adapter import AdapterError
from adapters.klipper.native_transport import request
from adapters.klipper.uploads import prepare_upload,MAX_UPLOAD
from adapters.klipper.websocket_bridge import serve,command_events

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


class BridgeServer(ThreadingHTTPServer):
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
        rbufsize=0
        def setup(self):
            super().setup();self.connection.settimeout(5)
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
            if self.path=='/websocket' and self.headers.get('Upgrade','').lower()=='websocket':
                serve(self,environment,self.record,self.server.stopping);return
            self.forward(False)
        def do_POST(self): self.forward(True)
        def do_DELETE(self): self.forward(False)
        def forward(self,post):
            status=400;output=b'{"error":"unsupported request"}';content_type='application/json'
            route='other';artifact=None
            try:
                url=urlsplit(self.path)
                if url.scheme or url.netloc or url.fragment or len(self.path)>8192: raise ValueError()
                path=url.path;query=parse_qs(url.query,keep_blank_values=True)
                if path=='/server/honeyprint/profile' and self.command=='GET':
                    route=path;status=200;output=json.dumps(printer_profile or {}).encode()
                elif frontend is not None and self.command=='GET' and not path.startswith(('/server/','/printer/','/machine/','/access/')):
                    route='frontend'
                    if path=='/config.json':
                        output=json.dumps({'defaultLocale':'en','defaultMode':'dark','defaultTheme':'mainsail',
                                           'hostname':'127.0.0.1','port':self.server.server_port,'path':None,
                                           'instancesDB':'moonraker','instances':[]}).encode()
                        status=200
                    else:
                        file=(Path(frontend)/unquote(path).lstrip('/')).resolve()
                        if path=='/': file=Path(frontend)/'index.html'
                        if not file.is_relative_to(Path(frontend).resolve()) or file.name.startswith('.') or not file.is_file():
                            status=404;output=b'{"error":"unknown asset"}'
                        else:
                            if file.stat().st_size>16777216: raise ValueError()
                            output=file.read_bytes();content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream';status=200
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
                            if '/' in filename or '\\' in filename or filename.startswith('.') or not filename.lower().endswith('.gcode'):
                                raise ValueError('Invalid G-code path')
                        body=None;ctype=self.headers.get('Content-Type','application/json')
                        if post:
                            size=int(self.headers.get('Content-Length','0'))
                            limit=settings['upload_max_bytes']+131072 if path=='/server/files/upload' else settings['request_max_bytes']
                            if not 0<=size<=limit:
                                raise OverflowError('Request exceeds limit')
                            body=self.rfile.read(size)
                            if len(body)!=size: raise ValueError('Incomplete body')
                            if path=='/server/files/upload':
                                body,ctype,artifact=prepare_upload(body,ctype,artifacts,max_file_bytes=settings['upload_max_bytes'])
                            elif body:
                                data=json.loads(body)
                                if not isinstance(data,dict): raise ValueError()
                                if path=='/printer/gcode/script':
                                    if set(data)!={'script'} or not isinstance(data['script'],str): raise ValueError()
                                    command_events(data['script'],self.record)
                                if path=='/printer/print/start':
                                    filename=data.get('filename','')
                                    if not isinstance(filename,str) or '/' in filename or '\\' in filename or filename.startswith('.') or not filename.lower().endswith('.gcode'): raise ValueError()
                                if path=='/server/database/item' and data.get('namespace') not in ('mainsail','webcams'):
                                    raise ValueError('Protected namespace')
                        if path=='/printer/print/start' and url.query:
                            filename=query.get('filename',[''])[0]
                            if '/' in filename or '\\' in filename or filename.startswith('.') or not filename.lower().endswith('.gcode'): raise ValueError()
                        target=self.path
                        if path=='/printer/honeyprint/preheat':
                            if body and json.loads(body)!={}: raise ValueError()
                            script=f"M104 S{settings['preheat_hotend_c']}"
                            if printer_profile is None or printer_profile['heated_bed']: script+=f"\nM140 S{settings['preheat_bed_c']}"
                            command_events(script,self.record)
                            target='/printer/gcode/script';body=json.dumps({'script':script}).encode();ctype='application/json'
                        status,content_type,output=request(environment,self.command,target,body,ctype)
                        if artifact:
                            self.record({'source':'klipper','event_type':'file.artifact','action':'upload',
                                         'outcome':'accepted' if 200<=status<300 else 'rejected',
                                         'simulation':True,'metadata':artifact})
            except OverflowError:
                self.close_connection=True
                status=413;output=b'{"error":"request too large"}'
            except (ValueError,UnicodeError):
                status=400;output=b'{"error":"invalid or unsupported data"}'
            except Exception:
                status=502;output=b'{"error":"native request failed"}'
            try: self.respond(status,output,content_type)
            finally:
                self.record({'source':'klipper','event_type':'http.request','action':self.command,
                             'outcome':'observed','simulation':True,'metadata':{'route':route,'status':status}})
    return BridgeServer(('127.0.0.1',port),Handler)

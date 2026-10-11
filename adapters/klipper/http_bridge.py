"""Bounded loopback JSON bridge into an isolated Moonraker container.

Only selected native API routes are forwarded. WebSocket and arbitrary file
uploads are deliberately unsupported by this bridge.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from deployment.klipper_environment import docker

GET_ROUTES = {'/server/info', '/printer/info', '/printer/objects/query?toolhead&extruder&heater_bed&mcu'}
POST_ROUTES = {'/printer/gcode/script', '/printer/emergency_stop'}


def create_bridge(environment, port, emit):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.forward(None)
        def do_POST(self):
            self.forward(True)
        def forward(self, post):
            status, output = 400, {'error': 'unsupported request'}
            try:
                if self.path not in (POST_ROUTES if post else GET_ROUTES):
                    status = 404
                elif self.headers.get('Transfer-Encoding'):
                    pass
                else:
                    body = None
                    if post:
                        size = int(self.headers.get('Content-Length', '0'))
                        if not 0 <= size <= 4096:
                            raise ValueError()
                        body = self.rfile.read(size)
                        if self.path.endswith('/script'):
                            data = json.loads(body)
                            if set(data) != {'script'} or not isinstance(data['script'], str):
                                raise ValueError()
                    code = ('import json,urllib.request,urllib.error; '
                            'r=urllib.request.Request(' + repr('http://127.0.0.1:7125' + self.path)
                            + ',data=' + repr(body) + ',headers={"Content-Type":"application/json"},method='
                            + repr(self.command) + '); '
                            's=urllib.request.urlopen(r,timeout=5); '
                            'print(json.dumps({"status":s.status,"body":s.read(1048577).decode()}))')
                    result = json.loads(docker('exec', environment.name,
                                              '/opt/moonraker-env/bin/python', '-c', code, timeout=8))
                    if len(result['body']) > 1048576:
                        raise ValueError()
                    status, output = result['status'], json.loads(result['body'])
            except Exception:
                status, output = 502, {'error': 'native request failed'}
            payload = json.dumps(output).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(payload)
            finally:
                emit({'source': 'klipper', 'event_type': 'http.request',
                      'action': self.command, 'outcome': 'observed', 'simulation': True,
                      'metadata': {'route': self.path.split('?')[0] if self.path in GET_ROUTES | POST_ROUTES else 'other',
                                   'status': status}})
    return HTTPServer(('127.0.0.1', port), Handler)

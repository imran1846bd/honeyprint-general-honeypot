"""Developer-owned HTTP honeypot selected only by HONEYPRINT_CUSTOM_CODE.

Replace the responses and routes with your own honeypot behavior. Honor the
provided binding variables and retain the instance identity health header.
"""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os

PROFILE=json.loads(os.environ['HONEYPRINT_PROFILE_JSON'])
TOKEN=os.environ['HONEYPRINT_INSTANCE_TOKEN']


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def respond(self,status,data):
        body=json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('X-HoneyPrint-Instance',TOKEN)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if self.path in ('/','/health'):
            self.respond(200,{'honeypot':'developer_custom_server','ready':True,'profile':PROFILE})
        else:self.respond(404,{'error':'unknown route'})
    def do_POST(self):
        # This sample receives data; it never executes request content as code.
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 1<=size<=4096:raise ValueError()
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict):raise ValueError()
            self.respond(200,{'received':True,'fields':sorted(data)})
        except ValueError:self.respond(400,{'error':'invalid data'})


if __name__=='__main__':
    HTTPServer((os.environ['HONEYPRINT_HOST'],int(os.environ['HONEYPRINT_PORT'])),Handler).serve_forever()

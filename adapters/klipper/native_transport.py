"""Bounded HTTP and live WebSocket transport into the owned internal container."""
import base64
import json
import subprocess
from adapters.base_adapter import AdapterError

HTTP_HELPER = '''
import base64,json,sys,urllib.request,urllib.error
args=json.load(sys.stdin)
request=urllib.request.Request('http://127.0.0.1:7125'+args['path'],
 data=base64.b64decode(args['body']) if args['body'] is not None else None,
 headers=args['headers'],method=args['method'])
try:
 response=urllib.request.urlopen(request,timeout=30)
except urllib.error.HTTPError as error:
 response=error
with response:
 body=response.read(2097153)
 if len(body)>2097152: raise ValueError('Response exceeds limit')
 print(json.dumps({'status':response.status,'content_type':response.headers.get('Content-Type','application/octet-stream'),
                   'body':base64.b64encode(body).decode()}))
'''

WS_HELPER = '''
import asyncio,json,sys
from tornado.websocket import websocket_connect
async def main():
 ws=await websocket_connect('ws://127.0.0.1:7125/websocket',connect_timeout=5,max_message_size=1048576)
 print(json.dumps({'connected':True}),flush=True)
 async def sender():
  while True:
   line=await asyncio.to_thread(sys.stdin.buffer.readline,1048577)
   if not line or len(line)>1048576: return
   await ws.write_message(json.loads(line)['message'])
 async def receiver():
  while True:
   message=await ws.read_message()
   if message is None: return
   if not isinstance(message,str): raise ValueError('Binary RPC is unsupported')
   print(json.dumps({'message':message}),flush=True)
 tasks=[asyncio.create_task(sender()),asyncio.create_task(receiver())]
 try:
  done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
  for task in done: task.result()
 finally:
  ws.close()
  for task in tasks: task.cancel()
asyncio.run(main())
'''


def request(environment, method, path, body=None, content_type='application/json'):
    if not path.startswith('/') or '\r' in path or '\n' in path or len(path)>8192:
        raise AdapterError('Invalid native API path')
    if body is not None and len(body)>1179648:
        raise AdapterError('Native request exceeds the upload limit')
    payload=json.dumps({'method':method,'path':path,
                        'body':base64.b64encode(body).decode() if body is not None else None,
                        'headers':{'Content-Type':content_type}})
    try:
        result=subprocess.run(['docker','exec','-i',environment.name,'/opt/moonraker-env/bin/python',
                               '-c',HTTP_HELPER],input=payload,text=True,capture_output=True,
                              timeout=35,check=True)
        if len(result.stdout)>3000000: raise ValueError()
        data=json.loads(result.stdout)
        return data['status'],data['content_type'],base64.b64decode(data['body'],validate=True)
    except (OSError,subprocess.SubprocessError,ValueError,KeyError):
        raise AdapterError('Native Moonraker HTTP request failed') from None


def websocket(environment):
    return subprocess.Popen(['docker','exec','-i',environment.name,'/opt/moonraker-env/bin/python',
                             '-u','-c',WS_HELPER],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

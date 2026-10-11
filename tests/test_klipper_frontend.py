"""Real native WebSocket, Mainsail assets, retained upload and AVR print checks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from adapters.klipper.adapter import KlipperAdapter
from deployment.mainsail import installed,prepare


class SocketClient:
    def __init__(self,port):
        self.socket=socket.create_connection(('127.0.0.1',port),timeout=15)
        self.stream=self.socket.makefile('rb',buffering=0)
        key=base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall(f'GET /websocket HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n'.encode())
        status=self.stream.readline()
        if b'101' not in status: raise RuntimeError(status)
        headers={}
        while True:
            line=self.stream.readline()
            if line==b'\r\n':break
            a,b=line.decode().split(':',1);headers[a.lower()]=b.strip()
        expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        assert headers['sec-websocket-accept']==expected
    def exact(self,size):
        data=b''
        while len(data)<size:
            part=self.stream.read(size-len(data))
            if not part: raise EOFError()
            data+=part
        return data
    def send(self,ident,method,params=None):
        data=json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}}).encode()
        size=len(data);mask=os.urandom(4)
        header=bytes([129,128|size]) if size<126 else bytes([129,254])+struct.pack('!H',size)
        self.socket.sendall(header+mask+bytes(v^mask[i%4] for i,v in enumerate(data)))
    def receive(self):
        a,b=self.exact(2);size=b&127
        if size==126:size=struct.unpack('!H',self.exact(2))[0]
        elif size==127:size=struct.unpack('!Q',self.exact(8))[0]
        if a&15!=1 or b&128 or size>1048576: raise ValueError('Unexpected server frame')
        return json.loads(self.exact(size))
    def response(self,ident):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            message=self.receive()
            if message.get('id')==ident:return message
        raise TimeoutError()
    def close(self):
        self.socket.close();self.stream.close()


@unittest.skipUnless(os.environ.get('HONEYPRINT_TEST_KLIPPER')=='1','requires the pinned Docker runtime')
class KlipperFrontendTests(unittest.TestCase):
    def test_native_upload_live_subscription_print_and_open_socket_shutdown(self):
        prepare()
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            plan={'config':{'id':'frontend_test','platform':'klipper','deployment':'managed','simulation':'avr_sim',
                            'printer_profile':str(Path('configs/profiles/virtual_lab.yaml').absolute()),
                            'services':{'web_port':port},'stages':[],'frontend':'mainsail','api':'moonraker'}}
            adapter=KlipperAdapter(runtime_dir=directory);handle=None;client=None;events=[]
            def http(path,data=None,ctype='application/json'):
                r=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data,headers={'Content-Type':ctype})
                with urllib.request.urlopen(r,timeout=35) as response:return response.status,response.read()
            try:
                adapter.prepare(plan);handle=adapter.launch_or_attach(plan);adapter.observe(handle,events.append)
                status,index=http('/');self.assertEqual(status,200);self.assertIn(b'Mainsail',index)
                _,config=http('/config.json');self.assertEqual(json.loads(config)['port'],port)
                client=SocketClient(port)
                client.send(1,'printer.objects.subscribe',{'objects':{'print_stats':None,'toolhead':['position']}})
                self.assertIn('result',client.response(1))
                # Consecutive frames and native replies must not stall in a buffered stream.
                client.send(2,'server.info');client.send(3,'printer.info')
                replies={}
                while not {2,3}<=replies.keys():
                    m=client.receive()
                    if 'id' in m:replies[m['id']]=m
                self.assertTrue(replies[2]['result']['klippy_connected'])
                client.send(4,'machine.reboot');self.assertIn('error',client.response(4))
                client.send(5,'printer.gcode.script',{'script':'G28'})
                self.assertIn('result',client.response(5))
                gcode=b'G28\nG1 X5 Y4 F6000\nM400\nM104 S0\nM140 S0\n'
                body=b'--hptest\r\nContent-Disposition: form-data; name="file"; filename="native.gcode"\r\nContent-Type: application/octet-stream\r\n\r\n'+gcode+b'\r\n--hptest--\r\n'
                status,result=http('/server/files/upload',body,'multipart/form-data; boundary=hptest')
                self.assertEqual(status,201);self.assertEqual(json.loads(result)['item']['path'],'native.gcode')
                self.assertEqual(http('/server/files/gcodes/native.gcode')[1],gcode)
                digest=hashlib.sha256(gcode).hexdigest()
                self.assertEqual(adapter.artifacts.read(digest),gcode)
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    http('/server/files/upload',body.replace(b'native.gcode',b'../printer.cfg'),'multipart/form-data; boundary=hptest')
                self.assertEqual(rejected.exception.code,400);rejected.exception.close()
                client.send(6,'printer.print.start',{'filename':'native.gcode'})
                self.assertIn('result',client.response(6))
                deadline=time.monotonic()+30;complete=False
                while time.monotonic()<deadline:
                    message=client.receive()
                    if message.get('method')=='notify_status_update' and message['params'][0].get('print_stats',{}).get('state')=='complete':
                        complete=True;break
                self.assertTrue(complete,'native print completion notification missing')
                self.assertTrue(adapter.health(handle)['ready'])
                self.assertTrue(any(e['event_type']=='file.artifact' and e['metadata']['sha256']==digest for e in events))
                self.assertTrue(any(e['event_type']=='printer.command' and e['metadata']['gcode']=='G28' for e in events))
                client.close();client=None
                deadline=time.monotonic()+8
                while json.loads(http('/server/info')[1])['result']['websocket_count']:
                    if time.monotonic()>deadline:self.fail('Native WebSocket helper leaked after disconnect')
                    time.sleep(.1)
                client=SocketClient(port)
                # Stop with the WebSocket still connected: native helper and server workers must exit.
                adapter.stop(handle)
                self.assertFalse(handle.created_container);self.assertFalse(handle.created_network)
                self.assertFalse(adapter.server_thread)
                handle=None
            finally:
                if client:client.close()
                if handle:adapter.stop(handle)
                elif adapter.environment is not None:adapter.cleanup_prepared(plan)

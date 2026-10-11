"""Bounded RFC 6455 text frames forwarded to a real Moonraker WebSocket."""
import base64
import hashlib
import json
import re
import select
import struct
import threading
from adapters.klipper.native_transport import websocket

MAX_MESSAGE=1048576
RPC_METHODS={
 'server.connection.identify','server.info','server.config','server.gcode_store','server.temperature_store',
 'server.database.list','server.database.get_item','server.database.post_item','server.database.delete_item',
 'server.files.list','server.files.get_directory','server.files.metadata','server.files.delete_file',
 'server.files.move','server.files.copy','server.files.post_directory','server.files.delete_directory',
 'server.history.list','server.history.totals','server.history.reset_totals','server.history.delete_job',
 'server.webcams.list','server.webcams.get_item','server.webcams.post_item','server.webcams.delete_item',
 'server.announcements.list','server.announcements.dismiss','server.job_queue.status',
 'printer.info','printer.objects.list','printer.objects.query','printer.objects.subscribe',
 'printer.gcode.help','printer.gcode.script','printer.query_endstops.status',
 'printer.print.start','printer.print.pause','printer.print.resume','printer.print.cancel',
 'printer.emergency_stop','printer.restart','printer.firmware_restart',
 'machine.system_info','machine.proc_stats','machine.update.status','access.oneshot_token',
}


def command_events(script, emit):
    for line in script.splitlines()[:1000]:
        token=line.split(';',1)[0].strip().split(maxsplit=1)
        if token:
            code=token[0].upper()
            code=code if re.fullmatch(r'[GMT][0-9]{1,5}(?:\.[0-9]{1,2})?',code) else 'other'
            emit({'source':'klipper','event_type':'printer.command','action':'submit',
                  'outcome':'observed','simulation':True,'metadata':{'gcode':code}})


def read_exact(stream,size):
    data=bytearray()
    while len(data)<size:
        part=stream.read(size-len(data))
        if not part: raise EOFError()
        data.extend(part)
    return bytes(data)


def read_frame(stream):
    a,b=read_exact(stream,2)
    opcode=a&15;final=bool(a&128)
    if a&112 or not b&128 or opcode not in (0,1,8,9,10): raise ValueError('Invalid frame')
    size=b&127
    if size==126: size=struct.unpack('!H',read_exact(stream,2))[0]
    elif size==127:
        size=struct.unpack('!Q',read_exact(stream,8))[0]
        if size& (1<<63): raise ValueError('Invalid frame length')
    if size>MAX_MESSAGE or (opcode>=8 and (not final or size>125)): raise ValueError('Frame limit')
    mask=read_exact(stream,4);data=read_exact(stream,size)
    return final,opcode,bytes(value^mask[i%4] for i,value in enumerate(data))


def write_frame(connection,lock,opcode,data):
    size=len(data)
    if size>MAX_MESSAGE: raise ValueError('Frame limit')
    header=bytes([128|opcode])
    if size<126: header+=bytes([size])
    elif size<65536: header+=bytes([126])+struct.pack('!H',size)
    else: header+=bytes([127])+struct.pack('!Q',size)
    with lock: connection.sendall(header+data)


def validate_rpc(message):
    data=json.loads(message)
    if not isinstance(data,dict) or data.get('jsonrpc')!='2.0' or data.get('method') not in RPC_METHODS:
        raise ValueError('Unsupported RPC')
    params=data.get('params',{})
    if not isinstance(params,dict): raise ValueError('Invalid parameters')
    method=data['method']
    if method=='printer.print.start':
        filename=params.get('filename','')
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.gcode',filename,re.I):
            raise ValueError('Prints require a retained G-code filename')
    if method.startswith('server.files.') and method not in ('server.files.list','server.files.metadata'):
        for key in ('path','source','dest'):
            if key in params and (not isinstance(params[key],str) or not (params[key].startswith('gcodes/') or (method=='server.files.get_directory' and params[key]=='gcodes')) or '..' in params[key].split('/')):
                raise ValueError('File operations are limited to G-code storage')
    if method.startswith('server.database.') and method not in ('server.database.list','server.database.get_item'):
        if params.get('namespace') not in ('mainsail','webcams'): raise ValueError('Protected database namespace')
    return data


def serve(handler,environment,emit,stopping):
    key=handler.headers.get('Sec-WebSocket-Key','')
    try:
        if handler.headers.get('Sec-WebSocket-Version')!='13' or len(base64.b64decode(key,validate=True))!=16:
            raise ValueError()
    except ValueError:
        handler.send_error(400);return
    process=websocket(environment)
    done=threading.Event();lock=threading.Lock();receiver=None
    try:
        if not select.select([process.stdout],[],[],8)[0]: raise TimeoutError()
        hello=json.loads(process.stdout.readline(MAX_MESSAGE+1))
        if hello!={'connected':True}: raise ValueError()
        handler.send_response(101)
        handler.send_header('Upgrade','websocket');handler.send_header('Connection','Upgrade')
        handler.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode())
        handler.end_headers();handler.wfile.flush()
        handler.connection.settimeout(5)
        emit({'source':'klipper','event_type':'http.request','action':'GET','outcome':'observed',
              'simulation':True,'metadata':{'route':'/websocket','status':101}})
        def receive():
            try:
                while not done.is_set() and not stopping.is_set():
                    line=process.stdout.readline(MAX_MESSAGE*6+1)
                    if not line or len(line)>MAX_MESSAGE*6: break
                    message=json.loads(line)['message'].encode()
                    write_frame(handler.connection,lock,1,message)
            except Exception: pass
            finally:
                done.set()
                try: handler.connection.shutdown(2)
                except OSError: pass
        receiver=threading.Thread(target=receive,daemon=True);receiver.start()
        fragmented=bytearray();in_fragment=False
        while not done.is_set() and not stopping.is_set():
            # A timeout while a frame is partial closes the stream, avoiding parser desynchronization.
            if not select.select([handler.connection],[],[],.5)[0]: continue
            final,opcode,payload=read_frame(handler.rfile)
            if opcode==8:
                write_frame(handler.connection,lock,8,payload);break
            if opcode==9:
                write_frame(handler.connection,lock,10,payload);continue
            if opcode==10: continue
            if opcode==1:
                if in_fragment: raise ValueError('Nested text frame')
                fragmented=bytearray(payload);in_fragment=not final
            else:
                if not in_fragment: raise ValueError('Unexpected continuation')
                fragmented.extend(payload)
            if len(fragmented)>MAX_MESSAGE: raise ValueError('Message limit')
            if not final: continue
            in_fragment=False;message=fragmented.decode('utf-8')
            try: rpc=validate_rpc(message)
            except ValueError:
                response={'jsonrpc':'2.0','error':{'code':-32601,'message':'Unsupported or invalid method'}}
                try:
                    candidate=json.loads(message)
                    if isinstance(candidate,dict) and 'id' in candidate: response['id']=candidate['id']
                except ValueError: pass
                write_frame(handler.connection,lock,1,json.dumps(response).encode());continue
            emit({'source':'klipper','event_type':'api.request','action':'rpc','outcome':'observed',
                  'simulation':True,'metadata':{'method':rpc['method']}})
            if rpc['method']=='printer.gcode.script':
                script=rpc.get('params',{}).get('script','')
                if not isinstance(script,str): raise ValueError('Invalid script')
                command_events(script,emit)
            process.stdin.write(json.dumps({'message':message}).encode()+b'\n');process.stdin.flush()
    except Exception:
        if not receiver:
            try: handler.send_error(502,'Native WebSocket unavailable')
            except OSError: pass
    finally:
        done.set()
        if process.poll() is None: process.terminate()
        try: process.wait(timeout=3)
        except Exception: process.kill();process.wait(timeout=3)
        for stream in (process.stdin,process.stdout): stream.close()
        if receiver: receiver.join(3)
        handler.close_connection=True

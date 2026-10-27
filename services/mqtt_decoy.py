"""Small MQTT 3.1.1 printer decoy; no real device commands or retained writes."""
import argparse
import hmac
import json
import os
from pathlib import Path
import signal
import socket
import struct
import threading
from uuid import uuid4

from telemetry.normalizer import normalize_event
from telemetry.sqlite_storage import SQLiteStorage
from services.printer_control import PrinterControl, ControlError, FILENAME

MAX_PACKET = 65536


def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise ConnectionError('socket closed')
        data.extend(part)
    return bytes(data)


def packet(sock):
    first = read_exact(sock, 1)[0]
    length = 0
    multiplier = 1
    for _ in range(4):
        digit = read_exact(sock, 1)[0]
        length += (digit & 127) * multiplier
        if length > MAX_PACKET:
            raise ValueError('packet too large')
        if not digit & 128:
            return first, read_exact(sock, length)
        multiplier *= 128
    raise ValueError('invalid packet length')


def encode_length(length):
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        out.append(digit | (128 if length else 0))
        if not length:
            return bytes(out)


def frame(header, body=b''):
    return bytes((header,)) + encode_length(len(body)) + body


def mqtt_text(value):
    raw = value.encode('utf-8')
    return struct.pack('!H', len(raw)) + raw


def take_field(body, offset):
    if offset + 2 > len(body):
        raise ValueError('short field')
    length = int.from_bytes(body[offset:offset + 2], 'big')
    offset += 2
    if offset + length > len(body):
        raise ValueError('short field')
    return body[offset:offset + length], offset + length


def matches(pattern, topic):
    parts, target = pattern.split('/'), topic.split('/')
    for index, part in enumerate(parts):
        if part == '#':
            return index == len(parts) - 1
        if index >= len(target) or (part != '+' and part != target[index]):
            return False
    return len(parts) == len(target)


def serve(host, port, username, password, database, instance_id, platform, ready_file=None, control=None):
    stop = threading.Event()
    clients = set()
    subscriptions_by_client = {}
    workers = set()
    lock = threading.RLock()
    slots = threading.BoundedSemaphore(32)
    prefix = f'honeyprint/{instance_id}'
    state = {'status': 'idle'}
    with SQLiteStorage(database) as storage:
        def emit(kind, action, outcome, session, metadata):
            storage.write_batch([normalize_event({
                'source': 'mqtt', 'event_type': kind, 'action': action,
                'outcome': outcome, 'simulation': True, 'session_id': session,
                'metadata': metadata}, instance_id=instance_id, platform=platform)])

        def snapshot():
            if control is not None:
                try:
                    current = control.snapshot()
                    return {
                        prefix + '/state': json.dumps({'printer': current['state'], 'platform': platform,
                                                       'job': current.get('job'), 'progress': current.get('progress')}, separators=(',', ':')).encode(),
                        prefix + '/temperature': json.dumps({'tool': current['tool'], 'bed': current['bed']}, separators=(',', ':')).encode(),
                    }
                except ControlError:
                    return {prefix + '/state': b'{"printer":"unavailable"}',
                            prefix + '/temperature': b'{"tool":null,"bed":null}'}
            with lock:
                status = state['status']
            return {
                prefix + '/state': json.dumps({'printer': status, 'platform': platform}, separators=(',', ':')).encode(),
                prefix + '/temperature': b'{"tool":25.0,"bed":25.0}',
            }

        def publish(client, topic, payload, retained=False):
            client.sendall(frame(0x31 if retained else 0x30, mqtt_text(topic) + payload))

        def broadcast(topic, payload):
            with lock:
                for subscriber, patterns in tuple(subscriptions_by_client.items()):
                    if any(matches(pattern, topic) for pattern in patterns):
                        try:
                            publish(subscriber, topic, payload)
                        except OSError:
                            pass

        def watch_platform():
            previous = None
            while not stop.wait(1):
                current = snapshot()
                if current != previous:
                    for topic, payload in current.items():
                        broadcast(topic, payload)
                    previous = current

        def handle(client, peer):
            session = str(uuid4())
            connected = False
            subscriptions = []
            try:
                client.settimeout(30)
                first, body = packet(client)
                if first != 0x10:
                    return
                protocol, offset = take_field(body, 0)
                if offset + 4 > len(body):
                    return
                level, flags = body[offset:offset + 2]
                offset += 4  # level, flags, keepalive
                if protocol != b'MQTT' or level != 4 or flags & 0x3F != 0x02 or flags & 0xC0 != 0xC0:
                    client.sendall(frame(0x20, b'\x00\x01'))
                    return
                client_id, offset = take_field(body, offset)
                candidate, offset = take_field(body, offset)
                secret, offset = take_field(body, offset)
                accepted = (offset == len(body) and len(client_id) <= 128
                            and hmac.compare_digest(candidate, username.encode())
                            and hmac.compare_digest(secret, password.encode()))
                emit('mqtt.auth', 'connect', 'accepted' if accepted else 'rejected', session,
                     {'peer': peer[0], 'client_id_length': len(client_id)})
                client.sendall(frame(0x20, b'\x00\x00' if accepted else b'\x00\x04'))
                if not accepted:
                    return
                connected = True
                while not stop.is_set():
                    first, body = packet(client)
                    kind = first >> 4
                    if first == 0xC0 and not body:
                        client.sendall(frame(0xD0))
                    elif first == 0xE0 and not body:
                        break
                    elif first == 0x82 and len(body) >= 2:
                        identifier = body[:2]
                        offset = 2
                        granted = bytearray()
                        new_patterns = []
                        while offset < len(body):
                            raw, offset = take_field(body, offset)
                            if offset >= len(body):
                                raise ValueError('short subscription')
                            qos = body[offset]
                            offset += 1
                            pattern = raw.decode('utf-8')
                            valid = (qos <= 2 and len(pattern) <= 256
                                     and pattern.split('/')[0] == 'honeyprint'
                                     and all('#' not in segment or (segment == '#' and index == len(pattern.split('/')) - 1)
                                             for index, segment in enumerate(pattern.split('/')))
                                     and all('+' not in segment or segment == '+' for segment in pattern.split('/'))
                                     and any(matches(pattern, topic) for topic in
                                             (*snapshot(), prefix + '/response', prefix + '/command')))
                            granted.append(0 if valid else 0x80)
                            if valid:
                                new_patterns.append(pattern)
                                emit('mqtt.subscribe', 'subscribe', 'accepted', session, {'topic_filter': pattern})
                        if not granted:
                            raise ValueError('empty subscription')
                        subscriptions.extend(new_patterns)
                        with lock:
                            subscriptions_by_client[client] = tuple(subscriptions)
                        client.sendall(frame(0x90, identifier + bytes(granted)))
                        for topic, payload in snapshot().items():
                            if any(matches(pattern, topic) for pattern in new_patterns):
                                publish(client, topic, payload, retained=True)
                    elif kind == 3 and first & 0x06 in (0, 2):
                        raw, offset = take_field(body, 0)
                        topic = raw.decode('utf-8')
                        qos = (first >> 1) & 3
                        identifier = body[offset:offset + 2] if qos else b''
                        offset += 2 if qos else 0
                        if qos and len(identifier) != 2:
                            raise ValueError('missing packet identifier')
                        payload = body[offset:]
                        if not topic or len(topic) > 256 or '\x00' in topic or '+' in topic or '#' in topic:
                            raise ValueError('invalid topic')
                        emit('mqtt.publish', 'publish', 'observed', session,
                             {'topic': topic, 'payload_bytes': len(payload), 'qos': qos})
                        if topic.startswith(prefix + '/upload/'):
                            name = topic[len(prefix + '/upload/'):]
                            try:
                                if control is None or not FILENAME.fullmatch(name):
                                    raise ControlError('Use honeyprint/INSTANCE/upload/NAME.gcode')
                                reply = control.upload(name, payload)
                                outcome = 'accepted'
                            except ControlError as error:
                                reply = str(error)
                                outcome = 'rejected'
                            emit('mqtt.upload', 'upload', outcome, session,
                                 {'filename': name[:128], 'bytes': len(payload)})
                            response = json.dumps({'upload': name, 'result': reply,
                                                   'outcome': outcome}, separators=(',', ':')).encode()
                            broadcast(prefix + '/response', response)
                        elif topic == prefix + '/command':
                            command = payload.decode('utf-8', 'replace').strip()
                            verb = command.split(' ', 1)[0].lower()[:32]
                            if verb not in ('help', 'commands', 'gcodes', 'gcode', 'codes', 'files', 'show', 'select', 'print', 'job', 'status', 'temperature', 'position', 'home', 'move', 'heat', 'bed', 'cooldown', 'run', 'm105', 'm114', 'm115', 'g0', 'g1', 'g28', 'g90', 'g91', 'm82', 'm83', 'm104', 'm140', 'connect', 'start', 'pause', 'resume', 'cancel'):
                                verb = '[other]'
                            if control is not None:
                                try:
                                    if verb == '[other]':
                                        raise ControlError('Unsupported printer command')
                                    reply, current_state = control.execute(command)
                                    current = current_state['state']
                                    outcome = 'accepted'
                                except ControlError as error:
                                    reply = str(error)
                                    current = 'unavailable'
                                    outcome = 'rejected'
                            else:
                                with lock:
                                    current = state['status']
                                    transitions = {'start': ('idle', 'printing'), 'pause': ('printing', 'paused'),
                                                   'resume': ('paused', 'printing'), 'cancel': ('printing', 'idle')}
                                    if verb == 'cancel' and current == 'paused':
                                        state['status'] = 'idle'
                                    elif verb in transitions and current == transitions[verb][0]:
                                        state['status'] = transitions[verb][1]
                                    current = state['status']
                                reply = 'simulated'
                                outcome = 'simulated'
                            emit('mqtt.command', verb, outcome, session, {'topic': topic})
                            response = json.dumps({'command': verb, 'printer': current, 'simulated': True,
                                                   'result': reply}, separators=(',', ':')).encode()
                            broadcast(prefix + '/response', response)
                            broadcast(prefix + '/state', snapshot()[prefix + '/state'])
                        if qos:
                            client.sendall(frame(0x40, identifier))
                    else:
                        raise ValueError('unsupported MQTT packet')
            except (OSError, ConnectionError, ValueError, UnicodeError):
                pass
            finally:
                if connected:
                    emit('mqtt.session', 'close', 'completed', session, {'peer': peer[0]})
                with lock:
                    clients.discard(client)
                    subscriptions_by_client.pop(client, None)
                    workers.discard(threading.current_thread())
                client.close()
                slots.release()

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(32)
        listener.settimeout(0.5)

        def shutdown(*_):
            stop.set()
            listener.close()
            with lock:
                for client in tuple(clients):
                    client.close()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, shutdown)
        try:
            watcher = threading.Thread(target=watch_platform, daemon=True) if control is not None else None
            if watcher is not None:
                watcher.start()
            if ready_file:
                Path(ready_file).write_text('ready')
            while not stop.is_set():
                try:
                    client, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        break
                    raise
                if not slots.acquire(blocking=False):
                    client.close()
                    continue
                with lock:
                    clients.add(client)
                worker = threading.Thread(target=handle, args=(client, peer), daemon=True)
                with lock:
                    workers.add(worker)
                worker.start()
        finally:
            shutdown()
            if watcher is not None:
                watcher.join(timeout=2)
            with lock:
                pending = tuple(workers)
            for worker in pending:
                worker.join(timeout=2)
            if ready_file:
                Path(ready_file).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the HoneyPrint MQTT decoy.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--control-port', type=int)
    parser.add_argument('--runtime-dir', type=Path)
    args = parser.parse_args(argv)
    username = os.environ.pop('HONEYPRINT_MQTT_USERNAME', None)
    password = os.environ.pop('HONEYPRINT_MQTT_PASSWORD', None)
    if not username or not password:
        parser.error('MQTT credentials are missing')
    control = PrinterControl(args.platform, args.control_port, args.runtime_dir, args.instance_id) if args.control_port else None
    serve(args.host, args.port, username, password, args.database,
          args.instance_id, args.platform, args.ready_file, control)


if __name__ == '__main__':
    main()

"""Bounded Telnet printer-console decoy; never executes host commands."""

import argparse
import hmac
import os
from pathlib import Path
import signal
import socket
import threading
from uuid import uuid4

from telemetry.normalizer import normalize_event
from telemetry.sqlite_storage import SQLiteStorage


IAC = 255
PROMPT = b'printer@honeyprint:~$ '
MAX_COMMAND = 4096


def command_metadata(command):
    name = command.strip().split(' ', 1)[0][:64]
    if not name or any(not (c.isalnum() or c in '._/-?') for c in name):
        name = '[other]'
    return {'command': name, 'length': len(command)}


def response(command):
    """Return only fixed virtual-printer replies."""
    verb = command.strip().split(' ', 1)[0].lower()
    if verb in ('exit', 'logout'):
        return b'logout\r\n', True
    if verb in ('', 'help', '?'):
        return b'Commands: help, status, M105, M115, ls, pwd, whoami, exit\r\n', False
    if verb in ('status', 'm105'):
        return b'Printer: idle\r\nT:25.0 /0.0 B:25.0 /0.0\r\n', False
    if verb == 'm115':
        return b'FIRMWARE_NAME:HoneyPrint Virtual Printer\r\n', False
    if verb == 'ls':
        return b'jobs  logs  printer.cfg\r\n', False
    if verb == 'pwd':
        return b'/home/printer\r\n', False
    if verb == 'whoami':
        return b'printer\r\n', False
    return b'Command unavailable in virtual printer console\r\n', False


def read_line(client, limit, echo, line_state):
    """Read one bounded Telnet line while discarding protocol negotiations."""
    buffer = bytearray()
    state = 'data'
    overflow = False
    while True:
        chunk = client.recv(1)
        if not chunk:
            return None, overflow
        byte = chunk[0]
        if line_state['skip_lf']:
            line_state['skip_lf'] = False
            if byte == 10:
                continue
        if state == 'iac':
            if byte == IAC:
                state = 'data'
            elif byte == 250:  # SB: subnegotiation until IAC SE.
                state = 'sub'
            elif byte in (251, 252, 253, 254):
                state = 'option'
            else:
                state = 'data'
            continue
        if state == 'option':
            state = 'data'
            continue
        if state == 'sub':
            if byte == IAC:
                state = 'sub_iac'
            continue
        if state == 'sub_iac':
            state = 'data' if byte == 240 else 'sub'
            continue
        if byte == IAC:
            state = 'iac'
        elif byte in (10, 13):
            line_state['skip_lf'] = byte == 13
            client.sendall(b'\r\n')
            return buffer.decode('utf-8', 'replace'), overflow
        elif byte in (8, 127):
            if buffer:
                buffer.pop()
                if echo:
                    client.sendall(b'\b \b')
        elif 32 <= byte <= 126 or byte >= 128:
            if len(buffer) < limit:
                buffer.append(byte)
                if echo:
                    client.sendall(bytes((byte,)))
            else:
                overflow = True


def serve(host, port, username, password, database, instance_id, platform, ready_file=None):
    stop = threading.Event()
    active = set()
    active_lock = threading.Lock()
    workers = set()
    workers_lock = threading.Lock()
    slots = threading.BoundedSemaphore(32)
    with SQLiteStorage(database) as storage:
        def emit(kind, action, outcome, session_id, metadata):
            event = normalize_event({'source': 'telnet', 'event_type': kind,
                'action': action, 'outcome': outcome, 'simulation': True,
                'session_id': session_id, 'metadata': metadata},
                instance_id=instance_id, platform=platform)
            storage.write_batch([event])

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(32)
        listener.settimeout(0.5)

        def shutdown(*_):
            stop.set()
            listener.close()
            with active_lock:
                for client in tuple(active):
                    client.close()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGHUP, shutdown)

        def handle(client, peer):
            session_id = str(uuid4())
            opened = False
            line_state = {'skip_lf': False}
            try:
                client.settimeout(30)
                with active_lock:
                    active.add(client)
                # WILL ECHO / WILL SUPPRESS-GO-AHEAD; password itself is never echoed.
                client.sendall(bytes((IAC, 251, 1, IAC, 251, 3)))
                client.sendall(b'HoneyPrint virtual printer console\r\n')
                for _ in range(3):
                    client.sendall(b'login: ')
                    candidate, over_user = read_line(client, 64, True, line_state)
                    if candidate is None:
                        return
                    client.sendall(b'Password: ')
                    secret, over_password = read_line(client, 128, False, line_state)
                    if secret is None:
                        return
                    accepted = not (over_user or over_password) and (
                        hmac.compare_digest(candidate, username)
                        and hmac.compare_digest(secret, password))
                    emit('telnet.auth', 'password', 'accepted' if accepted else 'rejected',
                         session_id, {'username': candidate[:64], 'peer': peer[0]})
                    if accepted:
                        break
                    client.sendall(b'Login incorrect\r\n')
                else:
                    return
                opened = True
                emit('telnet.session', 'open', 'accepted', session_id, {'peer': peer[0]})
                client.sendall(PROMPT)
                while not stop.is_set():
                    command, truncated = read_line(client, MAX_COMMAND, True, line_state)
                    if command is None:
                        break
                    if command:
                        emit('telnet.command', 'execute', 'observed', session_id,
                             {**command_metadata(command), 'truncated': truncated})
                    reply, should_exit = response(command)
                    client.sendall(reply)
                    if should_exit:
                        break
                    client.sendall(PROMPT)
            except (OSError, UnicodeError):
                pass
            finally:
                if opened:
                    emit('telnet.session', 'close', 'completed', session_id, {'peer': peer[0]})
                with active_lock:
                    active.discard(client)
                client.close()
                with workers_lock:
                    workers.discard(threading.current_thread())
                slots.release()

        try:
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
                if slots.acquire(blocking=False):
                    worker = threading.Thread(target=handle, args=(client, peer), daemon=True)
                    with workers_lock:
                        workers.add(worker)
                    worker.start()
                else:
                    client.close()
        finally:
            shutdown()
            with workers_lock:
                pending = tuple(workers)
            for worker in pending:
                worker.join(timeout=2)
            if ready_file:
                Path(ready_file).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the HoneyPrint Telnet decoy service.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=2323)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--ready-file', type=Path)
    args = parser.parse_args(argv)
    username = os.environ.pop('HONEYPRINT_TELNET_USERNAME', None)
    password = os.environ.pop('HONEYPRINT_TELNET_PASSWORD', None)
    if not username or not password:
        parser.error('Telnet credentials are missing')
    serve(args.host, args.port, username, password, args.database,
          args.instance_id, args.platform, args.ready_file)


if __name__ == '__main__':
    main()

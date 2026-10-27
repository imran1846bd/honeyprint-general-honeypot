"""Bounded SSH printer-console decoy. Never invokes a host shell or subprocess."""

import argparse
import hmac
import os
from pathlib import Path
import signal
import socket
import threading
import time
from uuid import uuid4

import paramiko

from telemetry.normalizer import normalize_event
from telemetry.sqlite_storage import SQLiteStorage
from services.printer_control import PrinterControl, ControlError, FILENAME, MAX_UPLOAD


PROMPT = b'printer@honeyprint:~$ '
MAX_COMMAND = 4096


def command_metadata(command):
    # Arguments can contain credentials; retain only the command name and size.
    name = command.strip().split(' ', 1)[0][:64]
    if not name or any(not (c.isalnum() or c in '._/-?') for c in name):
        name = '[other]'
    return {'command': name, 'length': len(command)}


def response(command, control=None):
    """Return a bounded virtual-printer reply without invoking a host shell."""
    verb = command.strip().split(' ', 1)[0].lower()
    if verb in ('exit', 'logout'):
        return b'logout\r\n', 0, True
    if verb in ('', 'help', '?'):
        if control is not None and verb:
            reply, _ = control.execute('help')
            return (reply + '\r\n').encode(), 0, False
        return b'Commands: help, status, M105, M114, M115, G28, G0, G1, G90, G91, M82, M83, M104, M140, connect, start, pause, resume, cancel, ls, pwd, whoami, exit\r\n', 0, False
    if control is not None and verb not in ('ls', 'pwd', 'whoami'):
        try:
            reply, _ = control.execute(command)
            return (str(reply) + '\r\n').encode(), 0, False
        except ControlError as error:
            return (str(error) + '\r\n').encode(), 1, False
    if verb in ('status', 'm105'):
        return b'Printer: idle\r\nT:25.0 /0.0 B:25.0 /0.0\r\n', 0, False
    if verb == 'm115':
        return b'FIRMWARE_NAME:HoneyPrint Virtual Printer\r\n', 0, False
    if verb == 'ls':
        return b'jobs  logs  printer.cfg\r\n', 0, False
    if verb == 'pwd':
        return b'/home/printer\r\n', 0, False
    if verb == 'whoami':
        return b'printer\r\n', 0, False
    return b'Command unavailable in virtual printer console\r\n', 127, False


class DecoySession(paramiko.ServerInterface):
    def __init__(self, username, password, emit, peer):
        self.username = username
        self.password = password
        self.emit = emit
        self.peer = peer
        self.request = threading.Event()
        self.command = None
        self.mode = None
        self.session_id = str(uuid4())

    def check_auth_password(self, username, password):
        accepted = hmac.compare_digest(username, self.username) and hmac.compare_digest(password, self.password)
        self.emit('ssh.auth', 'password', 'accepted' if accepted else 'rejected',
                  self.session_id, {'username': username[:64], 'peer': self.peer})
        return paramiko.AUTH_SUCCESSFUL if accepted else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.mode = 'shell'
        self.request.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.mode = 'exec'
        self.command = command.decode('utf-8', 'replace') if isinstance(command, bytes) else str(command)
        self.request.set()
        return True


def host_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.is_symlink():
            raise ValueError('SSH host key cannot be a symlink')
        return paramiko.RSAKey.from_private_key_file(str(path))
    key = paramiko.RSAKey.generate(3072)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        key.write_private_key_file(str(temporary))
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    except FileExistsError:
        return paramiko.RSAKey.from_private_key_file(str(path))
    finally:
        temporary.unlink(missing_ok=True)
    return key


def serve(host, port, username, password, database, instance_id, platform, key_path, ready_file=None, control=None):
    stop = threading.Event()
    active = set()
    active_lock = threading.Lock()
    slots = threading.BoundedSemaphore(32)
    with SQLiteStorage(database) as storage:
        def emit(kind, action, outcome, session_id, metadata):
            event = normalize_event({'source': 'ssh', 'event_type': kind,
                'action': action, 'outcome': outcome, 'simulation': True,
                'session_id': session_id, 'metadata': metadata},
                instance_id=instance_id, platform=platform)
            storage.write_batch([event])

        key = host_key(key_path)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(32)
        listener.settimeout(0.5)

        def shutdown(*_):
            stop.set()
            listener.close()
            with active_lock:
                for transport in tuple(active):
                    transport.close()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGHUP, shutdown)

        def handle(client, peer):
            transport = None
            try:
                client.settimeout(10)
                transport = paramiko.Transport(client)
                with active_lock:
                    active.add(transport)
                transport.add_server_key(key)
                session = DecoySession(username, password, emit, peer[0])
                transport.start_server(server=session)
                while not stop.is_set() and transport.is_active():
                    session.request.clear()
                    session.mode = None
                    session.command = None
                    channel = transport.accept(60)
                    if channel is None or not session.request.wait(10):
                        return
                    channel.settimeout(0.5)
                    emit('ssh.session', 'open', 'accepted', session.session_id, {'peer': peer[0]})
                    try:
                        if session.mode == 'exec':
                            command = session.command[:MAX_COMMAND]
                            emit('ssh.command', 'execute', 'observed', session.session_id,
                                 {**command_metadata(command), 'truncated': len(session.command) > MAX_COMMAND})
                            if command.lower().startswith('upload '):
                                name = command[7:].strip()
                                content = bytearray()
                                deadline = time.monotonic() + 15
                                try:
                                    if control is None or not FILENAME.fullmatch(name):
                                        raise ControlError('Use upload NAME.gcode with a local UTF-8 file on stdin')
                                    while time.monotonic() < deadline:
                                        try:
                                            chunk = channel.recv(4096)
                                        except socket.timeout:
                                            continue
                                        if not chunk:
                                            break
                                        content.extend(chunk)
                                        if len(content) > MAX_UPLOAD:
                                            raise ControlError('G-code upload exceeds 1 MiB')
                                        deadline = time.monotonic() + 15
                                    else:
                                        raise ControlError('G-code upload timed out')
                                    reply = (control.upload(name, bytes(content)) + '\r\n').encode()
                                    code = 0
                                except ControlError as error:
                                    reply = (str(error) + '\r\n').encode()
                                    code = 1
                                emit('ssh.upload', 'upload', 'accepted' if code == 0 else 'rejected',
                                     session.session_id, {'filename': name[:128], 'bytes': len(content)})
                            else:
                                reply, code, _ = response(command, control)
                            channel.sendall(reply)
                            channel.send_exit_status(code)
                            continue
                        channel.sendall(b'HoneyPrint virtual printer console\r\n' + PROMPT)
                        buffer = bytearray()
                        upload_name = None
                        upload_lines = bytearray()
                        while not stop.is_set() and transport.is_active():
                            try:
                                data = channel.recv(1024)
                            except socket.timeout:
                                continue
                            if not data:
                                break
                            for byte in data:
                                if byte in (10, 13):
                                    command = buffer.decode('utf-8', 'replace')
                                    buffer.clear()
                                    if upload_name is not None:
                                        if command == '.':
                                            try:
                                                reply = (control.upload(upload_name, bytes(upload_lines)) + '\r\n').encode()
                                                outcome = 'accepted'
                                            except ControlError as error:
                                                reply = (str(error) + '\r\n').encode()
                                                outcome = 'rejected'
                                            emit('ssh.upload', 'upload', outcome, session.session_id,
                                                 {'filename': upload_name, 'bytes': len(upload_lines)})
                                            upload_name = None
                                            upload_lines.clear()
                                            channel.sendall(b'\r\n' + reply + PROMPT)
                                        elif len(upload_lines) + len(command.encode()) + 1 > MAX_UPLOAD:
                                            upload_name = None
                                            upload_lines.clear()
                                            channel.sendall(b'\r\nG-code upload exceeds 1 MiB\r\n' + PROMPT)
                                        else:
                                            upload_lines.extend(command.encode() + b'\n')
                                            channel.sendall(b'\r\n')
                                        continue
                                    if command:
                                        emit('ssh.command', 'execute', 'observed', session.session_id,
                                             command_metadata(command))
                                    if command.lower().startswith('upload '):
                                        name = command[7:].strip()
                                        if control is not None and FILENAME.fullmatch(name):
                                            upload_name = name
                                            channel.sendall(b'\r\nPaste G-code; finish with a single . line\r\n')
                                        else:
                                            channel.sendall(b'\r\nUse upload NAME.gcode\r\n' + PROMPT)
                                        continue
                                    reply, _, should_exit = response(command, control)
                                    channel.sendall(b'\r\n' + reply)
                                    if should_exit:
                                        return
                                    channel.sendall(PROMPT)
                                elif byte in (8, 127):
                                    if buffer:
                                        buffer.pop()
                                        channel.sendall(b'\b \b')
                                elif 32 <= byte <= 126 and len(buffer) < MAX_COMMAND:
                                    buffer.append(byte)
                                    channel.sendall(bytes((byte,)))
                    finally:
                        emit('ssh.session', 'close', 'completed', session.session_id, {'peer': peer[0]})
                        channel.close()
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                if transport is not None:
                    with active_lock:
                        active.discard(transport)
                    transport.close()
                client.close()
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
                if not slots.acquire(blocking=False):
                    client.close()
                else:
                    threading.Thread(target=handle, args=(client, peer), daemon=True).start()
        finally:
            shutdown()
            if ready_file:
                Path(ready_file).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the HoneyPrint SSH decoy service.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=2222)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--host-key', type=Path, required=True)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--control-port', type=int)
    parser.add_argument('--runtime-dir', type=Path)
    args = parser.parse_args(argv)
    password = os.environ.pop('HONEYPRINT_SSH_PASSWORD', None)
    username = os.environ.pop('HONEYPRINT_SSH_USERNAME', None)
    if not password or not username:
        parser.error('SSH credentials are missing')
    control = PrinterControl(args.platform, args.control_port, args.runtime_dir, args.instance_id) if args.control_port else None
    serve(args.host, args.port, username, password, args.database,
          args.instance_id, args.platform, args.host_key, args.ready_file, control)


if __name__ == '__main__':
    main()

"""Private local control channel for graceful HoneyPrint shutdown."""

import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading


class ControlError(RuntimeError):
    """A runtime control channel could not be created or contacted."""


class ControlServer:
    def __init__(self, runtime_dir: Path, stop_event: threading.Event):
        self.root = Path(runtime_dir).absolute()
        self.stop_event = stop_event
        self._closed = threading.Event()
        self._socket = None
        self._lock_file = None
        self._worker = None
        self._token = secrets.token_hex(32)
        self._record = self.root / 'control.json'

    def start(self):
        try:
            if self.root.is_symlink():
                raise ControlError('Runtime directory must not be a symlink')
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self._lock_file = open(self.root / 'control.lock', 'a')
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._socket = socket.socket()
            self._socket.bind(('127.0.0.1', 0))
            self._socket.listen(4)
            self._socket.settimeout(0.2)
            with open(self._record, 'w', encoding='utf-8') as stream:
                os.chmod(self._record, 0o600)
                json.dump({'port': self._socket.getsockname()[1], 'token': self._token}, stream)
            self._worker = threading.Thread(target=self._serve, name='HoneyPrintControl', daemon=True)
            self._worker.start()
        except Exception:
            # Never remove another runner's control record on lock failure.
            if self._socket:
                self._socket.close()
            if self._lock_file:
                self._lock_file.close()
            self._socket = self._lock_file = None
            raise ControlError('Runtime is already active or control channel could not start') from None

    def _serve(self):
        while not self._closed.is_set():
            try:
                channel, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with channel:
                try:
                    channel.settimeout(1)
                    # One small newline-terminated request per connection.
                    data = bytearray()
                    while not data.endswith(b'\n') and len(data) <= 1024:
                        chunk = channel.recv(1025 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    request = json.loads(data)
                    token = request.get('token')
                    if (len(data) <= 1024 and request.get('command') == 'stop'
                            and isinstance(token, str) and hmac.compare_digest(token, self._token)):
                        self.stop_event.set()
                        channel.sendall(b'OK\n')
                    else:
                        channel.sendall(b'DENIED\n')
                except (OSError, ValueError, AttributeError):
                    continue

    def close(self):
        self._closed.set()
        if self._worker:
            self._worker.join(timeout=2)
        if self._socket:
            self._socket.close()
            self._socket = None
            self._record.unlink(missing_ok=True)
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None


def stop_running(runtime_dir: Path):
    """Request graceful stop; never signal a PID read from a file."""
    try:
        record_path = Path(runtime_dir) / 'control.json'
        if record_path.stat().st_size > 1024:
            raise ValueError()
        record = json.loads(record_path.read_text())
        port, token = record['port'], record['token']
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str):
            raise ValueError()
        with socket.create_connection(('127.0.0.1', port), timeout=2) as channel:
            channel.sendall((json.dumps({'command': 'stop', 'token': token}) + '\n').encode())
            if channel.recv(64) != b'OK\n':
                raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise ControlError('No reachable HoneyPrint control channel for this runtime') from None

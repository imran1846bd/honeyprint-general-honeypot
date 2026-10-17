"""Exercise Telnet login, virtual commands, launcher and shared evidence."""

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def unused_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def receive_until(client, marker):
    received = bytearray()
    while marker not in received:
        data = client.recv(1024)
        if not data:
            raise AssertionError(f'Connection closed before {marker!r}: {received!r}')
        received.extend(data)
    return bytes(received)


class TelnetDecoyTests(unittest.TestCase):
    def test_login_commands_and_evidence(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'events.sqlite3'
            ready = root / 'ready'
            port = unused_port()
            environment = os.environ.copy()
            environment.update(HONEYPRINT_TELNET_USERNAME='admin',
                               HONEYPRINT_TELNET_PASSWORD='sample-secret')
            process = subprocess.Popen([sys.executable, '-m', 'services.telnet_decoy',
                '--port', str(port), '--database', str(database),
                '--instance-id', 'custom_01', '--platform', 'custom_lab',
                '--ready-file', str(ready)], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('Telnet listener did not become ready')
                    time.sleep(0.05)
                with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                    client.settimeout(5)
                    receive_until(client, b'login: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'Password: ')
                    client.sendall(b'wrong\r\n')
                    receive_until(client, b'Login incorrect\r\nlogin: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'Password: ')
                    client.sendall(b'sample-secret\r\n')
                    receive_until(client, b'printer@honeyprint:~$ ')
                    client.sendall(b'M105 private-argument\r\n')
                    self.assertIn(b'T:25.0', receive_until(client, b'printer@honeyprint:~$ '))
                    client.sendall(b'uname -a\r\n')
                    self.assertIn(b'Command unavailable', receive_until(client, b'printer@honeyprint:~$ '))
                    client.sendall(b'exit\r\n')
                    self.assertIn(b'logout', receive_until(client, b'logout'))
                with closing(sqlite3.connect(database)) as connection:
                    events = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
                self.assertTrue(any(e['event_type'] == 'telnet.auth' and e['outcome'] == 'rejected' for e in events))
                self.assertTrue(any(e['event_type'] == 'telnet.auth' and e['outcome'] == 'accepted' for e in events))
                self.assertTrue(any(e['event_type'] == 'telnet.command' and e['metadata']['command'] == 'M105' for e in events))
                self.assertTrue(all(e['instance_id'] == 'custom_01' for e in events))
                self.assertNotIn('sample-secret', json.dumps(events))
                self.assertNotIn('private-argument', json.dumps(events))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)

    def test_launcher_starts_telnet_companion(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_port, telnet_port = unused_port(), unused_port()
            env_file = root / 'test.env'
            env_file.write_text('\n'.join([
                'HONEYPRINT_PLATFORM=custom',
                f'HONEYPRINT_PORT={web_port}',
                f'HONEYPRINT_TELNET_PORT={telnet_port}',
                'HONEYPRINT_TELNET_ENABLED=true',
                'HONEYPRINT_ANALYST_ENABLED=false',
                f'HONEYPRINT_RUNTIME_DIR={root / "runtime"}',
            ]) + '\n')
            process = subprocess.Popen([sys.executable, str(ROOT / 'quickstart.py'),
                '--env-file', str(env_file), '--duration', '1.5'], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        client = socket.create_connection(('127.0.0.1', telnet_port), timeout=0.5)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('Launcher did not start Telnet')
                        time.sleep(0.05)
                with client:
                    client.settimeout(5)
                    receive_until(client, b'login: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'Password: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'printer@honeyprint:~$ ')
                    client.sendall(b'status\r\n')
                    self.assertIn(b'Printer: idle', receive_until(client, b'printer@honeyprint:~$ '))
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn(f'Telnet decoy: telnet 127.0.0.1 {telnet_port}', stdout)
                with closing(sqlite3.connect(root / 'runtime/events.sqlite3')) as connection:
                    events = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
                self.assertTrue(any(e['event_type'] == 'telnet.command' for e in events))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=15)


if __name__ == '__main__':
    unittest.main()

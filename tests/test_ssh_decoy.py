"""Exercise the SSH wire protocol and shared evidence database."""

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

try:
    import paramiko
except ImportError:
    paramiko = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(paramiko, 'Paramiko is an optional SSH test dependency')
class SSHDecoyTests(unittest.TestCase):
    def test_env_launcher_starts_and_stops_ssh_companion(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports = []
            for _ in range(2):
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    ports.append(probe.getsockname()[1])
            env_file = root / 'test.env'
            env_file.write_text('\n'.join([
                'HONEYPRINT_PLATFORM=custom',
                f'HONEYPRINT_PORT={ports[0]}',
                f'HONEYPRINT_SSH_PORT={ports[1]}',
                'HONEYPRINT_SSH_ENABLED=true',
                'HONEYPRINT_ANALYST_ENABLED=false',
                f'HONEYPRINT_RUNTIME_DIR={root / "runtime"}',
            ]) + '\n')
            process = subprocess.Popen([sys.executable, str(ROOT / 'quickstart.py'),
                '--env-file', str(env_file), '--duration', '1.5'], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1', ports[1])) == 0:
                            break
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('Launcher did not start the SSH listener')
                    time.sleep(0.05)
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    client.connect('127.0.0.1', port=ports[1], username='admin',
                                   password='admin', allow_agent=False, look_for_keys=False,
                                   timeout=5, auth_timeout=5)
                    stdin, stdout, stderr = client.exec_command('M105', timeout=5)
                    self.assertIn('T:25.0', stdout.read().decode())
                finally:
                    client.close()
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn(f'SSH decoy: ssh -p {ports[1]}', stdout)
                with closing(sqlite3.connect(root / 'runtime/events.sqlite3')) as connection:
                    events = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
                self.assertTrue(any(e['event_type'] == 'ssh.command' for e in events))
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', ports[1])), 0)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=15)

    def test_login_command_evidence_and_clean_stop(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'events.sqlite3'
            ready = root / 'ready'
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            env = os.environ.copy()
            env.update(HONEYPRINT_SSH_USERNAME='admin', HONEYPRINT_SSH_PASSWORD='admin')
            process = subprocess.Popen([sys.executable, '-m', 'services.ssh_decoy',
                '--port', str(port), '--database', str(database), '--instance-id', 'custom_01',
                '--platform', 'custom_lab', '--host-key', str(root / 'host_key'),
                '--ready-file', str(ready)], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 15
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('SSH listener did not become ready')
                    time.sleep(0.05)
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    with self.assertRaises(paramiko.AuthenticationException):
                        client.connect('127.0.0.1', port=port, username='admin',
                                       password='wrong', allow_agent=False, look_for_keys=False,
                                       timeout=5, auth_timeout=5)
                    client.connect('127.0.0.1', port=port, username='admin',
                                   password='admin', allow_agent=False, look_for_keys=False,
                                   timeout=5, auth_timeout=5)
                    stdin, stdout, stderr = client.exec_command('status', timeout=5)
                    self.assertIn('Printer: idle', stdout.read().decode())
                    self.assertEqual(stdout.channel.recv_exit_status(), 0)
                    stdin, stdout, stderr = client.exec_command('uname -a', timeout=5)
                    self.assertIn('unavailable', stdout.read().decode())
                    self.assertEqual(stdout.channel.recv_exit_status(), 127)
                    shell = client.invoke_shell()
                    shell.settimeout(5)
                    self.assertIn('virtual printer console', shell.recv(1024).decode())
                    shell.send('status\n')
                    reply = ''
                    while 'Printer: idle' not in reply:
                        reply += shell.recv(1024).decode()
                    self.assertIn('Printer: idle', reply)
                    shell.close()
                finally:
                    client.close()
                with closing(sqlite3.connect(database)) as connection:
                    events = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
                self.assertTrue(any(e['event_type'] == 'ssh.auth' and e['outcome'] == 'rejected' for e in events))
                self.assertTrue(any(e['event_type'] == 'ssh.auth' and e['outcome'] == 'accepted' for e in events))
                self.assertTrue(any(e['event_type'] == 'ssh.command' and e['metadata']['command'] == 'status' for e in events))
                self.assertTrue(all(e['instance_id'] == 'custom_01' for e in events))
                self.assertTrue(all('admin' not in str(e.get('metadata', {}).get('password', '')) for e in events))
                self.assertEqual((root / 'host_key').stat().st_mode & 0o777, 0o600)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)


if __name__ == '__main__':
    unittest.main()

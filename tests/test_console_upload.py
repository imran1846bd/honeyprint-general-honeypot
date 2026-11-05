"""Upload through SSH, Telnet and MQTT into one selected printer backend."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import urllib.request

import paramiko

from services.mqtt_decoy import frame, mqtt_text, packet
from tests.test_mqtt_decoy import connect as mqtt_connect
from tests.test_telnet_decoy import receive_until


ROOT = Path(__file__).resolve().parents[1]


class ConsoleUploadTests(unittest.TestCase):
    def test_quickstart_custom_upload_reaches_live_web_job(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ports = []
            for _ in range(2):
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    ports.append(probe.getsockname()[1])
            env_file = root / 'test.env'
            env_file.write_text('\n'.join((
                'HONEYPRINT_PLATFORM=custom', f'HONEYPRINT_PORT={ports[0]}',
                'HONEYPRINT_SSH_ENABLED=true', f'HONEYPRINT_SSH_PORT={ports[1]}',
                'HONEYPRINT_ANALYST_ENABLED=false',
                f'HONEYPRINT_RUNTIME_DIR={root / "runtime"}',
            )) + '\n')
            process = subprocess.Popen([sys.executable, str(ROOT / 'quickstart.py'),
                '--env-file', str(env_file), '--duration', '2'], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1', ports[1])) == 0:
                            break
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('Quickstart SSH listener did not become ready')
                    time.sleep(.05)
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect('127.0.0.1', port=ports[1], username='admin', password='admin',
                            allow_agent=False, look_for_keys=False, timeout=5)
                try:
                    stdin, stdout, _ = ssh.exec_command('upload quick.gcode', timeout=5)
                    stdin.write('G28\nG1 X10 F600\n')
                    stdin.flush()
                    stdin.channel.shutdown_write()
                    self.assertIn('Uploaded quick.gcode', stdout.read().decode())
                    for command in ('select quick.gcode', 'start'):
                        _, stdout, _ = ssh.exec_command(command, timeout=5)
                        self.assertEqual(stdout.channel.recv_exit_status(), 0, stdout.read().decode())
                finally:
                    ssh.close()
                with urllib.request.urlopen(f'http://127.0.0.1:{ports[0]}/api/job', timeout=3) as response:
                    job = json.load(response)
                self.assertEqual(job['filename'], 'quick.gcode')
                self.assertIn(job['state'], ('printing', 'complete'))
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, output + error)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)

    def test_upload_and_start_share_backend_state(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            instance = root / 'octo_01'
            instance.mkdir()
            (instance / 'credentials.json').write_text(json.dumps({'api_key': 'test-key'}))
            state = {'files': {}, 'selected': None, 'printing': False}

            class Backend(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def respond(self, code, value):
                    body = json.dumps(value).encode()
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    if self.path == '/api/files':
                        self.respond(200, {'files': [{'name': name} for name in state['files']]})
                    elif self.path == '/api/printer':
                        self.respond(200, {'state': {'text': 'Printing' if state['printing'] else 'Operational'},
                                           'temperature': {'tool0': {'actual': 20, 'target': 0},
                                                           'bed': {'actual': 20, 'target': 0}}})
                    elif self.path == '/api/job':
                        self.respond(200, {'job': {'file': {'name': state['selected']}},
                                           'state': 'Printing' if state['printing'] else 'Operational',
                                           'progress': {'completion': 12 if state['printing'] else None}})
                    else:
                        self.respond(404, {})

                def do_POST(self):
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length)
                    if self.path == '/api/files/local':
                        match = re.search(rb'filename="([A-Za-z0-9_.-]+\.gcode)"\r\nContent-Type: application/octet-stream\r\n\r\n', body)
                        if not match:
                            self.respond(400, {})
                            return
                        name = match.group(1).decode()
                        state['files'][name] = body[match.end():].split(b'\r\n--honeyprint-', 1)[0]
                        self.respond(201, {'files': {'local': {'name': name}}})
                    elif self.path.startswith('/api/files/local/'):
                        name = self.path.rsplit('/', 1)[-1]
                        if name not in state['files']:
                            self.respond(404, {})
                            return
                        state['selected'] = name
                        state['printing'] = json.loads(body).get('print', False)
                        self.respond(204, {})
                    elif self.path == '/api/job':
                        state['printing'] = state['selected'] is not None and json.loads(body).get('command') == 'start'
                        self.respond(204, {})
                    else:
                        self.respond(404, {})

            backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
            backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
            backend_thread.start()
            processes = []
            try:
                ports = []
                for _ in range(3):
                    with socket.socket() as probe:
                        probe.bind(('127.0.0.1', 0))
                        ports.append(probe.getsockname()[1])
                for index, (module, credential) in enumerate((
                    ('services.ssh_decoy', 'SSH'), ('services.telnet_decoy', 'TELNET'),
                    ('services.mqtt_decoy', 'MQTT'))):
                    ready = root / f'{credential}.ready'
                    env = os.environ.copy()
                    env.update({f'HONEYPRINT_{credential}_USERNAME': 'admin',
                                f'HONEYPRINT_{credential}_PASSWORD': 'admin'})
                    args = [sys.executable, '-m', module, '--port', str(ports[index]),
                            '--control-port', str(backend.server_port), '--runtime-dir', str(root),
                            '--database', str(root / 'events.sqlite3'), '--instance-id', 'octo_01',
                            '--platform', 'octoprint', '--ready-file', str(ready)]
                    if credential == 'SSH':
                        args += ['--host-key', str(root / 'host_key')]
                    processes.append(subprocess.Popen(args, cwd=ROOT, env=env,
                                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                    deadline = time.monotonic() + 10
                    while not ready.exists():
                        if processes[-1].poll() is not None or time.monotonic() > deadline:
                            self.fail(f'{module} did not start')
                        time.sleep(.05)

                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect('127.0.0.1', port=ports[0], username='admin', password='admin',
                            allow_agent=False, look_for_keys=False, timeout=5)
                try:
                    stdin, stdout, _ = ssh.exec_command('upload ssh.gcode', timeout=5)
                    stdin.write('G28\nG1 X10 F600\n')
                    stdin.flush()
                    stdin.channel.shutdown_write()
                    self.assertIn('Uploaded ssh.gcode', stdout.read().decode())
                finally:
                    ssh.close()

                with socket.create_connection(('127.0.0.1', ports[1]), timeout=5) as client:
                    client.settimeout(5)
                    receive_until(client, b'login: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'Password: ')
                    client.sendall(b'admin\r\n')
                    receive_until(client, b'printer@honeyprint:~$ ')
                    client.sendall(b'upload telnet.gcode\r\n')
                    receive_until(client, b'Paste G-code; finish with a single . line')
                    client.sendall(b'G28\r\n.\r\n')
                    self.assertIn(b'Uploaded telnet.gcode', receive_until(client, b'printer@honeyprint:~$ '))

                with socket.create_connection(('127.0.0.1', ports[2]), timeout=5) as client:
                    client.settimeout(5)
                    self.assertEqual(mqtt_connect(client), (0x20, b'\x00\x00'))
                    client.sendall(frame(0x82, b'\x00\x01' + mqtt_text('honeyprint/#') + b'\x00'))
                    self.assertEqual(packet(client), (0x90, b'\x00\x01\x00'))
                    packet(client)
                    packet(client)
                    client.sendall(frame(0x30, mqtt_text('honeyprint/octo_01/upload/mqtt.gcode') + b'G28\n'))
                    for _ in range(6):
                        _, body = packet(client)
                        if b'Uploaded mqtt.gcode' in body:
                            break
                    else:
                        self.fail('MQTT upload response missing')

                    from services.printer_control import PrinterControl
                    control = PrinterControl('octoprint', backend.server_port, root, 'octo_01')
                    control.execute('select ssh.gcode')
                    control.execute('start')
                    for _ in range(8):
                        _, body = packet(client)
                        if b'"printer":"Printing"' in body and b'"job":"ssh.gcode"' in body:
                            break
                    else:
                        self.fail('MQTT did not broadcast the web printer job state')

                self.assertEqual(set(state['files']), {'ssh.gcode', 'telnet.gcode', 'mqtt.gcode'})
                self.assertEqual((state['selected'], state['printing']), ('ssh.gcode', True))
                self.assertEqual(control.snapshot()['job'], 'ssh.gcode')
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)
                backend.shutdown()
                backend.server_close()
                backend_thread.join(timeout=2)

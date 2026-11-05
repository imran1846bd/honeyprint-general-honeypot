"""Protocol-level checks for the optional MQTT printer decoy."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from services.mqtt_decoy import frame, mqtt_text, packet
from telemetry.sqlite_storage import SQLiteStorage

ROOT = Path(__file__).resolve().parents[1]


def connect(client, username='admin', password='admin'):
    body = mqtt_text('MQTT') + bytes((4, 0xC2, 0, 30))
    body += mqtt_text('test-client') + mqtt_text(username) + mqtt_text(password)
    client.sendall(frame(0x10, body))
    return packet(client)


class MQTTDecoyTests(unittest.TestCase):
    def test_two_clients_receive_simulated_state_and_record_safe_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / 'ready'
            database = Path(folder) / 'events.sqlite3'
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            env = os.environ.copy()
            env.update(HONEYPRINT_MQTT_USERNAME='admin', HONEYPRINT_MQTT_PASSWORD='admin')
            process = subprocess.Popen([sys.executable, '-m', 'services.mqtt_decoy',
                '--port', str(port), '--database', str(database), '--instance-id', 'testprinter',
                '--platform', 'custom', '--ready-file', str(ready)], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), process.stderr.read().decode() if process.poll() is not None else '')
                with socket.create_connection(('127.0.0.1', port), timeout=2) as observer:
                    observer.settimeout(2)
                    self.assertEqual(connect(observer), (0x20, b'\x00\x00'))
                    observer.sendall(frame(0x82, b'\x00\x01' + mqtt_text('honeyprint/#') + b'\x00'))
                    self.assertEqual(packet(observer), (0x90, b'\x00\x01\x00'))
                    retained = [packet(observer), packet(observer)]
                    self.assertEqual({item[0] for item in retained}, {0x31})
                    self.assertIn(b'"printer":"idle"', b''.join(item[1] for item in retained))
                    with socket.create_connection(('127.0.0.1', port), timeout=2) as publisher:
                        publisher.settimeout(2)
                        self.assertEqual(connect(publisher, password='wrong'), (0x20, b'\x00\x04'))
                    with socket.create_connection(('127.0.0.1', port), timeout=2) as publisher:
                        publisher.settimeout(2)
                        self.assertEqual(connect(publisher), (0x20, b'\x00\x00'))
                        publisher.sendall(frame(0x30, mqtt_text('honeyprint/testprinter/command') + b'start secret-argument'))
                        observed = [packet(observer), packet(observer)]
                        self.assertIn(b'"printer":"printing"', b''.join(item[1] for item in observed))
                        publisher.sendall(frame(0xC0))
                        self.assertEqual(packet(publisher), (0xD0, b''))
                with SQLiteStorage(database) as storage:
                    events = [row['event'] for row in storage.read_events(limit=100)]
                self.assertTrue(any(event['event_type'] == 'mqtt.command' and event['action'] == 'start' for event in events))
                encoded = json.dumps(events)
                self.assertNotIn('wrong', encoded)
                self.assertNotIn('secret-argument', encoded)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)

    def test_launcher_starts_mqtt_companion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                web_port = probe.getsockname()[1]
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                mqtt_port = probe.getsockname()[1]
            env_file = root / 'test.env'
            env_file.write_text('\n'.join((
                'HONEYPRINT_PLATFORM=custom', f'HONEYPRINT_PORT={web_port}',
                f'HONEYPRINT_MQTT_PORT={mqtt_port}', 'HONEYPRINT_MQTT_ENABLED=true',
                'HONEYPRINT_ANALYST_ENABLED=false',
                f'HONEYPRINT_RUNTIME_DIR={root / "runtime"}',
            )) + '\n')
            process = subprocess.Popen([sys.executable, str(ROOT / 'quickstart.py'),
                '--env-file', str(env_file), '--duration', '1.5'], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        client = socket.create_connection(('127.0.0.1', mqtt_port), timeout=0.5)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('Launcher did not start MQTT')
                        time.sleep(0.05)
                with client:
                    client.settimeout(3)
                    self.assertEqual(connect(client), (0x20, b'\x00\x00'))
                    client.sendall(frame(0x82, b'\x00\x02' + mqtt_text('honeyprint/#') + b'\x00'))
                    self.assertEqual(packet(client), (0x90, b'\x00\x02\x00'))
                    packet(client)
                    packet(client)
                    client.sendall(frame(0x30, mqtt_text('honeyprint/custom_01/command') + b'M104 S190'))
                    updates = [packet(client), packet(client)]
                    self.assertIn(b'"command":"m104"', b''.join(body for _, body in updates))
                    client.sendall(frame(0x30, mqtt_text('honeyprint/custom_01/command') + b'M105'))
                    updates = [packet(client), packet(client)]
                    self.assertIn(b'/190.0', b''.join(body for _, body in updates))
                    client.sendall(frame(0x30, mqtt_text('honeyprint/custom_01/command') + b'gcodes'))
                    updates = [packet(client), packet(client)]
                    self.assertIn(b'G0/G1', b''.join(body for _, body in updates))
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn(f'MQTT decoy: 127.0.0.1:{mqtt_port}', stdout)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=15)


if __name__ == '__main__':
    unittest.main()

"""One URL selection and real API evidence persistence across platform changes."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from deployment.env_config import build_config
from quickstart import runtime_ready

ROOT = Path(__file__).resolve().parents[1]


class GeneralSelectionTests(unittest.TestCase):
    def test_all_single_platforms_use_same_default_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            for platform in ('octoprint', 'klipper', 'custom'):
                selected = build_config({'HONEYPRINT_PLATFORM': platform}, ROOT, Path(tmp)/platform)
                self.assertEqual(len(selected['config']['instances']), 1)
                self.assertEqual(selected['config']['instances'][0]['services']['web_port'], 8081)
                self.assertFalse(selected['analyst_enabled'])
                self.assertEqual(selected['runtime_dir'], ROOT/'.honeyprint/quickstart')

    def test_custom_code_and_external_api_share_url_and_evidence(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = json.dumps({'path': self.path}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        backend = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        worker = threading.Thread(target=backend.serve_forever, daemon=True); worker.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, socket.socket() as probe:
                root = Path(tmp); runtime = root/'shared'
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
                database = runtime/'events.sqlite3'
                baseline = 0
                for mode in ('code', 'api'):
                    env = root/'.env'
                    extra = (f'HONEYPRINT_CUSTOM_CODE={ROOT / "examples/custom_server.py"}\n' if mode == 'code'
                             else f'HONEYPRINT_CUSTOM_API_URL=http://127.0.0.1:{backend.server_port}/base\n')
                    env.write_text(f'HONEYPRINT_PLATFORM=custom\nHONEYPRINT_PORT={port}\nHONEYPRINT_RUNTIME_DIR={runtime}\n'+extra)
                    process = subprocess.Popen([sys.executable, str(ROOT/'quickstart.py'), '--env-file', str(env), '--duration', '1'],
                                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        deadline = time.monotonic()+15
                        while not runtime_ready(database, baseline, {'custom_01'}):
                            if process.poll() is not None or time.monotonic()>deadline:
                                out, err = process.communicate(timeout=5); self.fail(out+err)
                            time.sleep(.05)
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3) as response:
                            body=json.loads(response.read());self.assertEqual(response.status, 200)
                            if mode=='api':self.assertEqual(body['path'], '/base/')
                        out, err = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 0, out+err)
                        self.assertIn(f'Honeypot: http://127.0.0.1:{port}',out)
                        self.assertNotIn('Evidence dashboard:',out)
                        with closing(sqlite3.connect(database)) as db:
                            baseline=db.execute('SELECT MAX(sequence) FROM events').fetchone()[0]
                            events=[json.loads(row[0]) for row in db.execute('SELECT payload FROM events')]
                        self.assertTrue(any(e['platform']=='custom' and e['event_type']=='http.request' for e in events))
                    finally:
                        if process.poll() is None:process.terminate();process.communicate(timeout=20)
                self.assertGreaterEqual(sum(e['event_type']=='framework.lifecycle' and e['action']=='start' for e in events),2)
                # Detaching the local bridge preserves the external API.
                with urllib.request.urlopen(f'http://127.0.0.1:{backend.server_port}/alive',timeout=3) as response:
                    self.assertEqual(response.status,200)
        finally:
            backend.shutdown();worker.join(5);backend.server_close()

    def test_ambiguous_custom_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_CUSTOM_CODE':'examples/custom_server.py',
                              'HONEYPRINT_CUSTOM_API_URL':'http://127.0.0.1:5000'},ROOT,Path(tmp))

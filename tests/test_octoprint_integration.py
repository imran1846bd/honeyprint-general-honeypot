"""Opt-in real OctoPrint tests; no mocked server or claimed MCU simulation.

Set HONEYPRINT_OCTOPRINT_PYTHON to the interpreter installed from the optional
lockfile. Tests allocate local ports, use temporary roots and stop owned servers.
"""

from copy import deepcopy
import os
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
import unittest

from adapters.octoprint.adapter import api_request
from core.builtin_registry import create_builtin_registry
from core.runtime import FrameworkRuntime
from deployment.control import stop_running
from telemetry.sqlite_storage import SQLiteStorage


OCTOPRINT_PYTHON = os.environ.get('HONEYPRINT_OCTOPRINT_PYTHON')
REPOSITORY = Path(__file__).resolve().parents[1]


@unittest.skipUnless(OCTOPRINT_PYTHON, 'Set HONEYPRINT_OCTOPRINT_PYTHON for real integration tests')
class OctoPrintIntegrationTests(unittest.TestCase):
    def test_virtual_printer_attach_preservation_persistence_stop_and_restart(self):
        with TemporaryDirectory() as tmp, socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
            root = Path(tmp) / 'owned'
            registry = create_builtin_registry(runtime_dir=root, octoprint_python=Path(OCTOPRINT_PYTHON))
            config = {'schema_version': 1, 'instances': [{
                'id': 'octo_01', 'platform': 'octoprint', 'deployment': 'managed',
                'simulation': 'virtual_printer',
                'printer_profile': str(REPOSITORY / 'configs/profiles/virtual_lab.yaml'),
                'services': {'web_port': port},
                'instrumentation': {'plugin': 'honeyprint_audit', 'http': True, 'gcode': True, 'state': True},
            }], 'telemetry': {'backend': 'sqlite', 'capture_requests': True,
                             'capture_commands': True, 'capture_state_changes': True}}
            app = FrameworkRuntime(config, registry, base_dir=REPOSITORY, runtime_dir=root)
            ready = []
            def on_ready(instance, report):
                self.assertTrue(report['ready']); self.assertTrue(instance.owns_platform_resources)
                handle = instance.resources['platform_handle']
                import urllib.request
                from http.cookiejar import CookieJar
                cookies = CookieJar()
                browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                with browser.open(f'http://127.0.0.1:{port}/', timeout=5) as page:
                    page.read()
                token = next(c.value for c in cookies if c.name.startswith('csrf_token'))
                credentials = json.loads((root / 'octo_01/credentials.json').read_text())
                self.assertEqual((credentials['username'], credentials['password']), ('admin', 'admin'))
                self.assertFalse(api_request(handle, 'GET', '/api/setup/wizard')['tracking']['required'])
                login = urllib.request.Request(f'http://127.0.0.1:{port}/api/login',
                    data=json.dumps({'user': 'admin', 'pass': 'admin'}).encode(),
                    headers={'Content-Type': 'application/json', 'X-CSRF-Token': token})
                with browser.open(login, timeout=5) as response:
                    self.assertEqual(json.loads(response.read())['name'], 'admin')
                api_request(handle, 'POST', '/api/printer/command', {'command': 'M105'})
                # Attach only observes this externally managed server.
                previous = os.environ.get('HONEYPRINT_TEST_ATTACH_KEY')
                os.environ['HONEYPRINT_TEST_ATTACH_KEY'] = handle.api_key
                config_file = root / 'octo_01/config.yaml'
                before = config_file.read_bytes()
                attached_config = deepcopy(config)
                attached_config['instances'][0]['deployment'] = 'attach'
                attached_config['instances'][0]['instrumentation'] = {'state': True}
                attached_root = Path(tmp) / 'attached'
                attached_registry = create_builtin_registry(runtime_dir=attached_root,
                                                            attach_api_key_env='HONEYPRINT_TEST_ATTACH_KEY')
                attached_app = FrameworkRuntime(attached_config, attached_registry,
                                                base_dir=REPOSITORY, runtime_dir=attached_root)
                try:
                    attached_app.run(duration=0.3, on_ready=lambda i, s: self.assertFalse(i.owns_platform_resources))
                finally:
                    if previous is None: os.environ.pop('HONEYPRINT_TEST_ATTACH_KEY', None)
                    else: os.environ['HONEYPRINT_TEST_ATTACH_KEY'] = previous
                self.assertEqual(config_file.read_bytes(), before)
                self.assertEqual(api_request(handle, 'GET', '/api/connection')['current']['port'], 'VIRTUAL')
                self.assertIsNone(handle.environment.process.poll())
                ready.append(instance.instance_id)
                stop_running(root)
            result = app.run(on_ready=on_ready)
            self.assertEqual(ready, ['octo_01'])
            self.assertEqual(result['telemetry']['pending'], 0)
            self.assertFalse((root / 'control.json').exists())
            with SQLiteStorage(root / 'events.sqlite3') as storage:
                rows = storage.read_events(limit=1000)
                types = {row['event']['event_type'] for row in rows}
                self.assertIn('http.request', types); self.assertIn('printer.command', types)
                lifecycle = [row['event']['action'] for row in rows
                             if row['event']['event_type'] == 'framework.lifecycle']
                self.assertEqual(lifecycle, ['start', 'stop'])
            # A fresh runner can restart the same owned basedir after cleanup.
            restarted = FrameworkRuntime(config, registry, base_dir=REPOSITORY, runtime_dir=root)
            self.assertEqual(restarted.run(duration=0.3)['telemetry']['pending'], 0)
            with socket.socket() as check:
                self.assertNotEqual(check.connect_ex(('127.0.0.1', port)), 0)


if __name__ == '__main__': unittest.main()

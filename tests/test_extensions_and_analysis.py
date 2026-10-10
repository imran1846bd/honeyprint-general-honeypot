"""Custom SDK, real custom runtime, derived alerts and read-only dashboard tests."""

from copy import deepcopy
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
import urllib.request
import urllib.error

from core.builtin_registry import create_builtin_registry
from core.plugin_registry import PluginRegistry, PluginRegistryError
from core.runtime import FrameworkRuntime
from dashboard.server import create_server, render_page
from sdk.extensions import register_extension
from telemetry.analysis.alerts import AlertStore
from telemetry.analysis.evidence import EvidenceReader
from telemetry.analysis.rules import classify_event
from telemetry.analysis.service import Analyzer
from telemetry.normalizer import normalize_event
from telemetry.sqlite_storage import SQLiteStorage


ROOT = Path(__file__).resolve().parents[1]


def captured(kind='http.request', outcome='observed', **metadata):
    return normalize_event({'source': 'test', 'event_type': kind, 'action': 'check',
                            'outcome': outcome, 'simulation': True, 'metadata': metadata},
                           instance_id='lab_01', platform='custom_lab')


class ExtensionAnalysisTests(unittest.TestCase):
    def test_sdk_registration_does_not_execute_factories(self):
        registry = PluginRegistry(); called = []
        manifest = {'api_version': 1, 'name': 'example', 'plugins': [
            {'kind': 'simulator', 'id': 'example', 'factory': lambda: called.append(True)}]}
        register_extension(registry, manifest)
        self.assertEqual(called, [])
        with self.assertRaises(PluginRegistryError): register_extension(registry, manifest)

    def test_sdk_conflict_does_not_partially_register(self):
        registry = PluginRegistry(); registry.register('simulator', 'existing', lambda: None)
        manifest = {'api_version': 1, 'name': 'example', 'plugins': [
            {'kind': 'simulator', 'id': 'new', 'factory': lambda: None},
            {'kind': 'simulator', 'id': 'existing', 'factory': lambda: None}]}
        with self.assertRaises(PluginRegistryError): register_extension(registry, manifest)
        self.assertEqual(registry.list_plugins('simulator'), ('existing',))
        manifest['api_version'] = True
        with self.assertRaises(PluginRegistryError): register_extension(registry, manifest)

    def test_rules_are_contextual_and_stable(self):
        for event, rule in [(captured(status=403), 'access_denied'),
                            (captured(status=404), 'unknown_endpoint'),
                            (captured('printer.command', gcode='M112'), 'emergency_stop'),
                            (captured('printer.command', 'rejected'), 'command_rejected')]:
            result = classify_event(event)
            self.assertEqual(result[0]['rule_id'], rule)
            self.assertEqual(result, classify_event(event))
        self.assertEqual(classify_event(captured(status=200)), ())

    def test_analysis_restart_and_raw_database_preservation(self):
        with TemporaryDirectory() as tmp:
            raw = Path(tmp)/'events.sqlite3'; derived = Path(tmp)/'analysis.sqlite3'
            with SQLiteStorage(raw) as storage: storage.write_batch([captured(status=403)])
            before = raw.read_bytes()
            for expected in (1, 0):
                with EvidenceReader(raw) as evidence, AlertStore(derived) as alerts:
                    self.assertEqual(Analyzer(evidence, alerts).process_once()['findings'], expected)
                    self.assertEqual(len(alerts.read_findings()), 1)
            self.assertEqual(raw.read_bytes(), before)

    def test_alert_conflict_rolls_back_cursor_and_new_finding(self):
        a = classify_event(captured(status=403))[0]
        b = classify_event(captured(status=404))[0]
        with AlertStore(':memory:') as store:
            store.write_batch([a], source='raw', cursor=1)
            with self.assertRaises(ValueError):
                store.write_batch([b, dict(a, message='conflicting retry')], source='raw', cursor=2)
            self.assertEqual(store.cursor('raw'), 1)
            self.assertEqual(len(store.read_findings()), 1)

    def test_dashboard_escapes_captured_html(self):
        event = captured(text='<script>alert(1)</script>')
        page = render_page([{'sequence': 1, 'event': event}], [], instance_id='" autofocus="x')
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('&quot; autofocus=&quot;x', page)

    def test_dashboard_query_and_write_rejection(self):
        with TemporaryDirectory() as tmp:
            raw = Path(tmp)/'events.sqlite3'
            with SQLiteStorage(raw) as storage: storage.write_batch([captured(status=200)])
            before = raw.read_bytes()
            with create_server(raw, port=0) as server:
                thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}); thread.start()
                try:
                    url = f'http://127.0.0.1:{server.server_port}'
                    with urllib.request.urlopen(url+'/api/events') as response:
                        self.assertEqual(len(json.load(response)), 1)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(urllib.request.Request(url, data=b'write'))
                    self.assertEqual(caught.exception.code, 405)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(url+'/?after=-1')
                    self.assertEqual(caught.exception.code, 400)
                finally: server.shutdown(); thread.join(timeout=3)
            self.assertEqual(raw.read_bytes(), before)

    def test_real_custom_runtime_command_rejection_and_alerts(self):
        with TemporaryDirectory() as tmp, socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
            runtime_dir = Path(tmp)/'runtime'
            config = {'schema_version': 1, 'instances': [{
                'id': 'custom_01', 'platform': 'custom_lab', 'deployment': 'managed',
                'simulation': 'lab_sim', 'services': {'web_port': port},
                'printer_profile': str(ROOT/'configs/profiles/virtual_lab.yaml'),
            }], 'telemetry': {'backend': 'sqlite', 'capture_requests': True,
                             'capture_commands': True, 'capture_state_changes': True}}
            registry = create_builtin_registry(runtime_dir=runtime_dir)
            app = FrameworkRuntime(config, registry, base_dir=ROOT, runtime_dir=runtime_dir)
            def ready(instance, report):
                self.assertTrue(instance.owns_platform_resources)
                url = f'http://127.0.0.1:{port}/api/command'
                for cmd, expected in [('G1 X10 Y20', 200), ('G1 X99999', 400)]:
                    request = urllib.request.Request(url, data=json.dumps({'command': cmd}).encode())
                    try:
                        with urllib.request.urlopen(request) as response: self.assertEqual(response.status, expected)
                    except urllib.error.HTTPError as error: self.assertEqual(error.code, expected)
                self.assertEqual(instance.resources['platform_handle'].position['x'], 10)
            self.assertEqual(app.run(duration=0.2, on_ready=ready)['telemetry']['pending'], 0)
            with EvidenceReader(runtime_dir/'events.sqlite3') as raw, AlertStore(Path(tmp)/'analysis.sqlite3') as alerts:
                self.assertGreaterEqual(Analyzer(raw, alerts).process_once()['findings'], 1)
                self.assertIn('command_rejected', [row['finding']['rule_id'] for row in alerts.read_findings()])


if __name__ == '__main__': unittest.main()

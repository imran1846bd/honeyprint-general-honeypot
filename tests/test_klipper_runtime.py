"""Opt-in real Docker integration: AVR readiness, movement, isolation and cleanup."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.request

from adapters.klipper.adapter import KlipperAdapter
from deployment.klipper_environment import docker


@unittest.skipUnless(os.environ.get('HONEYPRINT_TEST_KLIPPER') == '1',
                     'requires running Docker and the pinned Klipper image')
class KlipperRuntimeTests(unittest.TestCase):
    def test_native_mcu_bridge_isolation_cleanup_and_restart(self):
        import socket
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        plan = {'config': {'id': 'klipper_test', 'platform': 'klipper',
                          'deployment': 'managed', 'simulation': 'avr_sim',
                          'printer_profile': str(Path('configs/profiles/virtual_lab.yaml').absolute()),
                          'services': {'web_port': port}, 'stages': []}}
        for iteration in range(2):
            adapter = KlipperAdapter()
            handle = None
            events = []
            try:
                adapter.validate(plan['config'], {})
                adapter.prepare(plan)
                handle = adapter.launch_or_attach(plan)
                self.assertTrue(adapter.health(handle)['ready'])
                adapter.observe(handle, lambda event: events.append(event))
                request = urllib.request.Request(
                    'http://127.0.0.1:%d/printer/gcode/script' % port,
                    data=json.dumps({'script': 'G28\nG1 X12 Y8 F600\nM400'}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=15) as response:
                    self.assertEqual(response.status, 200)
                state = handle.request('/printer/objects/query?toolhead&extruder&heater_bed&mcu')['status']
                self.assertEqual(state['toolhead']['position'][:2], [12., 8.])
                self.assertGreater(state['mcu']['last_stats']['bytes_read'], 0)
                blocked = docker('exec', handle.name, '/opt/moonraker-env/bin/python', '-c',
                                 'import socket;s=socket.socket();s.settimeout(2);print(s.connect_ex(("1.1.1.1",80)))')
                self.assertNotEqual(blocked, '0')
                deadline = time.monotonic() + 5
                while not any(e['event_type'] == 'http.request' for e in events) and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertTrue(any(e['event_type'] == 'http.request' for e in events))
                self.assertTrue(any(e['event_type'] == 'printer.state' for e in events))
            finally:
                if handle is not None:
                    adapter.stop(handle)
                    self.assertFalse(handle.created_container)
                    self.assertFalse(handle.created_network)
                else:
                    adapter.cleanup_prepared(plan)

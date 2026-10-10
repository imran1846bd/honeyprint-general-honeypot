"""Protocol fixture tests; these do not replace a real Klipper integration test."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from adapters.base_adapter import AdapterError
from adapters.klipper.moonraker_client import MoonrakerClient, QUERY

SERVER = {'klippy_connected': True, 'klippy_state': 'ready', 'moonraker_version': 'v0.9.3'}
PRINTER = {'state': 'ready', 'software_version': 'v0.13.0', 'config_file': '/private/config'}
OBJECTS = {'status': {'webhooks': {'state': 'ready'}, 'toolhead': {'position': [1,2,3,4]},
                      'extruder': {'temperature': 200, 'target': 210}, 'print_stats': {'state': 'printing'}}}


@contextmanager
def fixture(*, changes=None, status=200, raw=None):
    calls = []
    replies = {'/server/info': deepcopy(SERVER), '/printer/info': deepcopy(PRINTER), QUERY: deepcopy(OBJECTS)}
    if changes: changes(replies)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get('X-Api-Key')))
            payload = raw if raw is not None else json.dumps({'result': replies[self.path]}).encode()
            self.send_response(status); self.send_header('Content-Length',str(len(payload)));self.end_headers()
            self.wfile.write(payload)
    server = HTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
    thread.start()
    try: yield server.server_port, calls
    finally: server.shutdown();server.server_close();thread.join(timeout=3)


class MoonrakerTests(unittest.TestCase):
    def test_get_only_selected_evidence_and_versions(self):
        with fixture() as (port,calls), patch.dict(os.environ,{'HONEYPRINT_TEST_KEY':'private-key'}):
            report = MoonrakerClient(port=port,api_key_env='HONEYPRINT_TEST_KEY',
                                     expected_klipper='v0.13.0',expected_moonraker='v0.9.3').snapshot()
            self.assertTrue(report['ready'])
            self.assertEqual(report['position'],[1.,2.,3.,4.])
            self.assertEqual([call[0] for call in calls],['GET']*3)
            self.assertTrue(all(call[2]=='private-key' for call in calls))
            self.assertNotIn('private',json.dumps(report))

    def test_unready_status_is_not_ready(self):
        with fixture(changes=lambda r:r['/server/info'].update(klippy_connected=False)) as (port,_):
            self.assertFalse(MoonrakerClient(port=port).snapshot()['ready'])

    def test_version_mismatch_and_nonfinite_status(self):
        with fixture() as (port,_):
            with self.assertRaises(AdapterError): MoonrakerClient(port=port,expected_klipper='v0.12.0').snapshot()
        def change(r): r[QUERY]['status']['extruder']['temperature']=float('nan')
        with fixture(changes=change) as (port,_):
            with self.assertRaises(AdapterError): MoonrakerClient(port=port).snapshot()

    def test_bad_envelopes_errors_redirects_and_size(self):
        for status,body in ((302,b'{}'),(401,b'private-key'),(200,b'{}'),(200,b'[]'),(200,b'A'*1048577)):
            with fixture(status=status,raw=body) as (port,_):
                with self.assertRaises(AdapterError) as caught: MoonrakerClient(port=port).snapshot()
                self.assertNotIn('private-key',str(caught.exception))

    def test_invalid_settings_and_missing_key(self):
        for options in ({'port':True},{'port':0},{'api_key_env':'invalid-key'}, {'expected_klipper':'latest'}):
            with self.assertRaises(AdapterError): MoonrakerClient(**options)
        with fixture() as (port,calls), patch.dict(os.environ,{},clear=True):
            with self.assertRaises(AdapterError): MoonrakerClient(port=port,api_key_env='MISSING').snapshot()
            self.assertEqual(calls,[])
        with self.assertRaises(AdapterError): MoonrakerClient()._get('/printer/gcode/script')


if __name__ == '__main__': unittest.main()

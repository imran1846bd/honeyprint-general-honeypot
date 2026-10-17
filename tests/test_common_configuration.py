"""Common env controls must change real behavior, not only generated configuration."""
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from core.runtime_settings import resolve_settings
from core.builtin_registry import create_builtin_registry
from core.runtime import FrameworkRuntime
from core.validator import validate_config
from deployment.env_config import read_env, build_config
from telemetry.analysis.evidence import EvidenceReader
from telemetry.analysis.alerts import AlertStore
from telemetry.sqlite_storage import SQLiteStorage
from telemetry.normalizer import normalize_event
from dashboard.server import create_server, read_findings
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def event():
    return normalize_event({'source': 'test', 'event_type': 'http.request', 'action': 'GET',
                            'outcome': 'observed', 'simulation': True, 'metadata': {'status': 404}},
                           instance_id='custom_01', platform='custom_lab')


class CommonConfigurationTests(unittest.TestCase):
    def test_common_template_has_values_and_plans_each_platform(self):
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as tmp:
            values = read_env(ROOT / '.env.example')
            self.assertTrue(all(values.values()))
            self.assertFalse(any(k.startswith(('HONEYPRINT_OCTOPRINT_', 'HONEYPRINT_KLIPPER_', 'HONEYPRINT_CUSTOM_')) for k in values))
            for selection in ('octoprint', 'klipper', 'custom'):
                result = build_config(dict(values, HONEYPRINT_PLATFORM=selection), ROOT, Path(tmp)/selection)
                registry = create_builtin_registry(runtime_dir=Path(tmp)/'runtime', runtime_settings=result['config']['runtime'])
                validate_config(result['config'], registry)
                self.assertEqual(result['config']['instances'][0]['services']['web_port'], 8081)
                self.assertEqual(result['config']['runtime'], resolve_settings())

    def test_only_selected_platform_file_is_loaded_and_paths_are_project_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); folder = base/'configs/platforms'; folder.mkdir(parents=True)
            (folder/'octoprint.yaml').write_text('schema_version: 1\nsource: plat/octo\npython: tools/python\n')
            (folder/'klipper.yaml').write_text('not: [valid')
            (folder/'custom.yaml').write_text('schema_version: 1\ncode: absent.py\napi_url: http://invalid/\n')
            result = build_config({'HONEYPRINT_PLATFORM': 'octoprint'}, base, base/'out')
            self.assertEqual(result['octoprint_source'], base/'plat/octo')
            self.assertEqual(result['octoprint_python'], base/'tools/python')
            self.assertIsNone(result['custom_code'])
            with self.assertRaises(ValueError):
                build_config({'HONEYPRINT_PLATFORM': 'klipper'}, base, base/'invalid')

    def test_unknown_or_duplicate_platform_settings_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); folder=base/'configs/platforms';folder.mkdir(parents=True)
            for content in ('schema_version: 1\nsource: a\nsource: b\n', 'schema_version: 1\nunknown: true\n', 'schema_version: 1\nsource: 3\n'):
                (folder/'octoprint.yaml').write_text(content)
                with self.assertRaises(ValueError):build_config({'HONEYPRINT_PLATFORM':'octoprint'},base,base/'out')

    def test_common_invalid_values_rejected(self):
        bad = {'COLLECTOR_BATCH_SIZE': ['0','1001','2.5','9'*1000],
               'HEALTH_INTERVAL_SECONDS': ['0','nan','inf'],
               'ANALYSIS_ENABLED': ['yes'], 'CAPTURE_REQUESTS': ['maybe'],
               'EVIDENCE_DB_NAME': ['../outside.sqlite3','analysis.sqlite3','ANALYSIS.SQLITE3'],
               'ARTIFACT_MAX_FILE_BYTES': ['1048577'], 'RUN_DURATION_SECONDS': ['-1'],
               'DASHBOARD_PAGE_SIZE': ['0','1001'], 'SQLITE_BUSY_TIMEOUT_SECONDS': ['nan']}
        with tempfile.TemporaryDirectory() as tmp:
            for name, vals in bad.items():
                for value in vals:
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_'+name:value},ROOT,Path(tmp))
        for raw in ({'collector_batch_size': True},{'analysis_enabled':'false'},{'unknown':1},
                    {'artifact_max_total_bytes': 5}):
            with self.assertRaises(ValueError):resolve_settings(raw)

    def test_sqlite_timeout_reaches_writer_readers_and_alert_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=Path(tmp)/'raw.sqlite3';alerts=Path(tmp)/'alerts.sqlite3'
            registry=create_builtin_registry(runtime_dir=Path(tmp),runtime_settings={'sqlite_busy_timeout_seconds':.123})
            with registry.resolve('storage','sqlite')(raw) as writer:
                writer.write_batch([event()])
                self.assertEqual(writer._connection.execute('PRAGMA busy_timeout').fetchone()[0],123)
            with EvidenceReader(raw,timeout=.321) as reader, AlertStore(alerts,timeout=.456) as findings:
                self.assertEqual(reader._connection.execute('PRAGMA busy_timeout').fetchone()[0],321)
                self.assertEqual(findings._connection.execute('PRAGMA busy_timeout').fetchone()[0],456)

    def test_configured_queue_and_artifact_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_EVENT_QUEUE_CAPACITY':'1',
                                 'HONEYPRINT_ARTIFACT_MAX_FILE_BYTES':'3','HONEYPRINT_ARTIFACT_MAX_TOTAL_BYTES':'6',
                                 'HONEYPRINT_ARTIFACT_MAX_FILES':'1'},ROOT,Path(tmp)/'generated')
            cfg=result['config'];registry=create_builtin_registry(runtime_dir=Path(tmp),runtime_settings=cfg['runtime'])
            runtime=FrameworkRuntime(cfg,registry,base_dir=ROOT,runtime_dir=Path(tmp))
            self.assertTrue(runtime.bus.publish(event())); self.assertFalse(runtime.bus.publish(event()))
            adapter=registry.resolve('platform_adapter','custom_lab')()
            adapter.prepare({'config':cfg['instances'][0]})
            store=adapter._artifacts
            self.assertEqual(store.read(store.store(b'G28')['sha256']),b'G28')
            with self.assertRaises(ValueError): store.store(b'M105')
            with self.assertRaises(ValueError): store.store(b'G29')

    def test_klipper_upload_store_receives_common_retention_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=build_config({'HONEYPRINT_PLATFORM':'klipper','HONEYPRINT_ARTIFACT_MAX_FILE_BYTES':'3',
                                 'HONEYPRINT_ARTIFACT_MAX_TOTAL_BYTES':'6','HONEYPRINT_ARTIFACT_MAX_FILES':'1'},ROOT,Path(tmp)/'generated')
            registry=create_builtin_registry(runtime_dir=Path(tmp),runtime_settings=result['config']['runtime'])
            adapter=registry.resolve('platform_adapter','klipper')()
            with patch('adapters.klipper.adapter.installed',return_value=None), patch('adapters.klipper.adapter.KlipperEnvironment.prepare'):
                adapter.prepare({'config':result['config']['instances'][0]})
            self.assertEqual(adapter.artifacts.max_file_bytes,3)
            adapter.artifacts.store(b'G28')
            with self.assertRaises(ValueError):adapter.artifacts.store(b'M105')
            with self.assertRaises(ValueError):adapter.artifacts.store(b'G29')

    def test_dashboard_pagination_and_analysis_batch_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=Path(tmp)/'raw.sqlite3'; alerts=Path(tmp)/'alerts.sqlite3'
            with SQLiteStorage(raw) as writer:writer.write_batch([event() for _ in range(5)])
            from telemetry.analysis.service import Analyzer
            with EvidenceReader(raw) as reader, AlertStore(alerts) as store:
                self.assertEqual(Analyzer(reader,store).process_once(limit=2)['processed'],2)
            self.assertEqual(len(read_findings(alerts,limit=1)),1)
            with create_server(raw,alerts=alerts,port=0,page_size=2,request_timeout=.3,sqlite_timeout=.1) as server:
                thread=Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}'
                    with urllib.request.urlopen(url+'/health') as response:self.assertEqual(json.load(response),{'ready':True})
                    with urllib.request.urlopen(url+'/api/events') as response:self.assertEqual(len(json.load(response)),2)
                    with urllib.request.urlopen(url+'/') as response:self.assertIn('Next 2 records',response.read().decode())
                finally:server.shutdown();thread.join(timeout=3)

    def test_real_common_env_run_with_platform_yaml_capture_and_renamed_databases(self):
        for analysis_enabled in (True,False):
            with self.subTest(analysis=analysis_enabled),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);folder=base/'configs/platforms';folder.mkdir(parents=True)
                (folder/'custom.yaml').write_text("schema_version: 1\ncode: ''\napi_url: ''\npython: ''\nhealth_path: /\n")
                web=port(); dashboard=port();runtime=base/'runtime'
                env=base/'.env'
                env.write_text(f'''HONEYPRINT_PLATFORM=custom
HONEYPRINT_INSTANCE_ID=auto
HONEYPRINT_PORT={web}
HONEYPRINT_RUNTIME_DIR={runtime}
HONEYPRINT_EVIDENCE_DB_NAME=interactions.sqlite3
HONEYPRINT_ANALYSIS_DB_NAME=findings.sqlite3
HONEYPRINT_SQLITE_BUSY_TIMEOUT_SECONDS=0.2
HONEYPRINT_CAPTURE_REQUESTS=false
HONEYPRINT_CAPTURE_COMMANDS=false
HONEYPRINT_CAPTURE_STATE_CHANGES=false
HONEYPRINT_ANALYST_ENABLED=true
HONEYPRINT_DASHBOARD_PORT={dashboard}
HONEYPRINT_DASHBOARD_PAGE_SIZE=1
HONEYPRINT_DASHBOARD_REQUEST_TIMEOUT_SECONDS=0.5
HONEYPRINT_DASHBOARD_STARTUP_TIMEOUT_SECONDS=5
HONEYPRINT_ANALYSIS_ENABLED={str(analysis_enabled).lower()}
HONEYPRINT_ANALYSIS_INTERVAL_SECONDS=0.1
HONEYPRINT_ANALYSIS_BATCH_SIZE=1
HONEYPRINT_COLLECTOR_BATCH_SIZE=1
HONEYPRINT_COLLECTOR_MAX_BATCHES=2
HONEYPRINT_COLLECTOR_INTERVAL_SECONDS=0.05
HONEYPRINT_FINAL_DRAIN_MAX_BATCHES=10
HONEYPRINT_HEALTH_INTERVAL_SECONDS=0.05
HONEYPRINT_RUN_DURATION_SECONDS=1
HONEYPRINT_STARTUP_TIMEOUT_SECONDS=5
HONEYPRINT_READY_TIMEOUT_SECONDS=2
HONEYPRINT_SHUTDOWN_TIMEOUT_SECONDS=5
''')
                process=subprocess.Popen([sys.executable,str(ROOT/'quickstart.py'),'--env-file',str(env),'--no-bootstrap'],
                                         cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+10
                    while True:
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{dashboard}/api/events',timeout=.3) as response:
                                self.assertEqual(len(json.load(response)),1)
                            break
                        except (OSError,urllib.error.URLError):
                            if process.poll() is not None or time.monotonic()>deadline:
                                out,err=process.communicate(timeout=5);self.fail(out+err)
                            time.sleep(.03)
                    with urllib.request.urlopen(f'http://127.0.0.1:{web}/',timeout=2) as response:self.assertEqual(response.status,200)
                    with urllib.request.urlopen(f'http://127.0.0.1:{dashboard}/api/alerts',timeout=2) as response:self.assertEqual(json.load(response),[])
                    out,err=process.communicate(timeout=10)
                    self.assertEqual(process.returncode,0,out+err)
                    self.assertIn('Local services ready',out)
                    self.assertFalse((runtime/'events.sqlite3').exists())
                    self.assertFalse((runtime/'analysis.sqlite3').exists())
                    self.assertEqual((runtime/'findings.sqlite3').exists(),analysis_enabled)
                    with closing(sqlite3.connect(runtime/'interactions.sqlite3')) as conn:
                        events=[json.loads(row[0]) for row in conn.execute('SELECT payload FROM events')]
                    self.assertEqual([e['action'] for e in events],['start','stop'])
                    self.assertTrue(all(e['event_type']=='framework.lifecycle' for e in events))
                    for number in (web,dashboard):
                        with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',number)),0)
                finally:
                    if process.poll() is None:process.terminate();process.communicate(timeout=10)

    def test_custom_code_selected_from_yaml_without_platform_env_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);folder=base/'configs/platforms';folder.mkdir(parents=True)
            (folder/'custom.yaml').write_text(f'schema_version: 1\ncode: {ROOT / "examples/custom_server.py"}\nhealth_path: /\n')
            result=build_config({'HONEYPRINT_PLATFORM':'custom'},base,base/'out')
            self.assertEqual(result['custom_code'],ROOT/'examples/custom_server.py')
            self.assertEqual(result['config']['instances'][0]['simulation'],'custom_process')
            (folder/'custom.yaml').write_text('schema_version: 1\napi_url: http://127.0.0.1:5000\n')
            result=build_config({'HONEYPRINT_PLATFORM':'custom'},base,base/'out')
            self.assertEqual(result['config']['instances'][0]['deployment'],'attach')


if __name__=='__main__':unittest.main()

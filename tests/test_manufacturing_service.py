"""Registered shadow stages and shared runtime evidence, including real OctoPrint."""
from pathlib import Path
import os
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from core.builtin_registry import create_builtin_registry
from core.manufacturing_service import ManufacturingService
from core.runtime import FrameworkRuntime
from plugins.manufacturing.pipeline import STAGES, FACTORIES
from telemetry.artifact_storage import ArtifactStorage
from telemetry.analysis.evidence import EvidenceReader

ROOT = Path(__file__).resolve().parents[1]
PROFILE = str(ROOT/'configs/profiles/virtual_lab.yaml')
JOB = b'G28\nM104 S200\nG4 S90\nG1 X10 E5'


class SharedManufacturingTests(unittest.TestCase):
    def plan(self, overrides=None):
        providers = dict(FACTORIES); providers.update(overrides or {})
        return SimpleNamespace(config={'stages': STAGES,'printer_profile': PROFILE},
                               factories={('manufacturing_stage', name): factory for name,factory in providers.items()})

    def test_selected_provider_is_invoked_and_original_artifact_saved(self):
        calls=[];events=[]
        def intake(content, **kwargs):
            calls.append(content)
            return FACTORIES['job_intake'](content,**kwargs)
        with TemporaryDirectory() as tmp:
            artifacts=ArtifactStorage(tmp)
            service=ManufacturingService(self.plan({'job_intake':intake}),artifact_storage=artifacts,sink=events.append)
            result=service.submit(JOB)
            self.assertEqual(calls,[JOB]);self.assertTrue(result['inspection']['passed'])
            self.assertEqual(artifacts.read(result['sha256']),JOB)
            self.assertEqual([event['action'] for event in events],list(STAGES))
            self.assertTrue(all(event['metadata']['execution_mode']=='shadow' for event in events))

    def test_failed_evidence_blocks_further_jobs_and_preserves_state(self):
        with TemporaryDirectory() as tmp:
            service=ManufacturingService(self.plan(),artifact_storage=ArtifactStorage(tmp),sink=lambda event:False)
            before=service.snapshot()
            with self.assertRaises(RuntimeError):service.submit(JOB)
            self.assertEqual(service.snapshot(),before)
            with self.assertRaises(RuntimeError):service.submit(JOB)

    def run_platform(self, platform, python=None):
        with TemporaryDirectory() as tmp, socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
            root=Path(tmp)/'runtime'
            instance={'id':'test_01','platform':platform,'deployment':'managed','printer_profile':PROFILE,
                      'simulation':'virtual_printer' if platform=='octoprint' else 'lab_sim',
                      'services':{'web_port':port},'stages':list(STAGES)}
            if platform=='octoprint':instance['instrumentation']={'plugin':'honeyprint_audit','http':True,'gcode':True,'state':True}
            config={'schema_version':1,'instances':[instance],'telemetry':{'backend':'sqlite','capture_requests':True,
                    'capture_commands':True,'capture_state_changes':True}}
            registry=create_builtin_registry(runtime_dir=root,octoprint_python=python)
            app=FrameworkRuntime(config,registry,base_dir=ROOT,runtime_dir=root)
            services=[]
            def ready(instance, report):
                services.append(instance.resources['manufacturing'])
                result=instance.resources['manufacturing'].submit(JOB)
                self.assertTrue(result['inspection']['passed'])
                self.assertEqual(result['execution_mode'],'shadow')
                if platform=='custom_lab':
                    self.assertEqual(instance.resources['platform_handle'].position['x'],0)
            app.run(duration=0.2,on_ready=ready)
            with self.assertRaises(RuntimeError): services[0].submit(JOB)
            with EvidenceReader(root/'events.sqlite3') as raw:
                stages=[row['event'] for row in raw.read_events() if row['event']['event_type']=='manufacturing.stage']
                self.assertEqual(len(stages),5)
                self.assertTrue(all(event['platform']==platform for event in stages))

    def test_custom_runtime_shared_service(self):self.run_platform('custom_lab')

    @unittest.skipUnless(os.environ.get('HONEYPRINT_OCTOPRINT_PYTHON'),'Requires optional pinned OctoPrint runtime')
    def test_real_octoprint_shadow_service(self):
        self.run_platform('octoprint',Path(os.environ['HONEYPRINT_OCTOPRINT_PYTHON']))


if __name__=='__main__':unittest.main()

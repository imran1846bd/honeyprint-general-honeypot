"""Behavioral coverage for common identity, storage, ingress, preheat and camera controls."""
from contextlib import redirect_stderr
import io,json,os,sys,tempfile,unittest
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
import urllib.request,urllib.error
import yaml
from core.runtime_settings import resolve_settings
from core.storage_paths import DiskSpaceMonitor,artifact_root
from deployment.env_config import build_config,read_env
from core.builtin_registry import create_builtin_registry
from core.validator import validate_config
from core.profile_loader import load_profile
from core.runtime import FrameworkRuntime
from adapters.klipper.http_bridge import create_bridge
from adapters.klipper.uploads import prepare_upload
from telemetry.artifact_storage import ArtifactStorage
from deployment.environment_manager import OctoPrintEnvironment
from deployment.source_camera import SourceCamera

ROOT=Path(__file__).resolve().parents[1]
JOB=b'G28\nM104 S200\nG4 S90\nG1 X10 E5'


def post(url,body=b'{}',ctype='application/json'):
    with urllib.request.urlopen(urllib.request.Request(url,data=body,headers={'Content-Type':ctype}),timeout=3) as response:
        return json.load(response)


class CommonPrinterOptionsTests(unittest.TestCase):
    def test_env_name_paths_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            result=build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_PRINTER_NAME':'Research Printer',
                                 'HONEYPRINT_ARTIFACT_DIR':'evidence/gcode'},base,base/'generated')
            self.assertEqual(load_profile(result['config']['instances'][0]['printer_profile'])['name'],'Research Printer')
            self.assertEqual(result['config']['runtime']['artifact_dir'],str(base/'evidence/gcode'))
            self.assertEqual(artifact_root(base,resolve_settings()),base/'artifacts')
            for key,value in [('HONEYPRINT_PRINTER_NAME','x'*129),('HONEYPRINT_REQUEST_MAX_BYTES','0'),
                              ('HONEYPRINT_UPLOAD_MAX_BYTES','1048577'),('HONEYPRINT_PREHEAT_BED_C','131'),
                              ('HONEYPRINT_PREHEAT_HOTEND_C','nan'),('HONEYPRINT_DISK_WARNING_BYTES','-1'),
                              ('HONEYPRINT_CAMERA_ENABLED','yes'),('HONEYPRINT_CAMERA_SOURCE','absent.mp4')]:
                with self.subTest(key=key),self.assertRaises(ValueError):
                    build_config({'HONEYPRINT_PLATFORM':'custom',key:value},base,base/'invalid')
            with self.assertRaises(ValueError):resolve_settings({'camera_enabled':False,'camera_source':'https://example.test/video'})
            with self.assertRaises(ValueError):resolve_settings({'camera_source':'https://user:pass@example.test/video'})

    def test_unsupported_backend_requests_fail_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            for platform,values in [('klipper',{'HONEYPRINT_CAMERA_SOURCE':'https://example.test/stream'}),
                                    ('custom',{'HONEYPRINT_CUSTOM_API_URL':'http://127.0.0.1:12345','HONEYPRINT_PREHEAT_HOTEND_C':'210'})]:
                result=build_config({'HONEYPRINT_PLATFORM':platform,**values},ROOT,base/'generated')
                registry=create_builtin_registry(runtime_dir=base,custom_api_url=result['custom_api_url'],runtime_settings=result['config']['runtime'])
                with self.assertRaises(ValueError):validate_config(result['config'],registry)

    def test_read_only_octoprint_attach_does_not_accept_managed_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=build_config({'HONEYPRINT_PLATFORM':'octoprint','HONEYPRINT_PREHEAT_HOTEND_C':'210'},ROOT,Path(tmp)/'generated')
            cfg=result['config'];cfg['instances'][0]['deployment']='attach'
            with self.assertRaisesRegex(ValueError,'managed OctoPrint'):
                validate_config(cfg,create_builtin_registry(runtime_dir=Path(tmp),runtime_settings=cfg['runtime']))

    def test_disk_warning_is_transition_based_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);evidence=root/'keep';evidence.write_text('evidence')
            monitor=DiskSpaceMonitor([root],100)
            output=io.StringIO()
            with redirect_stderr(output),patch('core.storage_paths.shutil.disk_usage',side_effect=[SimpleNamespace(free=n) for n in (50,50,200,50)]):
                for _ in range(4):monitor.check()
            self.assertEqual(output.getvalue().count('storage warning'),2)
            self.assertEqual(evidence.read_text(),'evidence')
            with patch('core.storage_paths.shutil.disk_usage') as probe:
                DiskSpaceMonitor([root],0).check();probe.assert_not_called()

    def test_real_lab_identity_preheat_limits_and_shared_artifact_directory(self):
        import socket
        with tempfile.TemporaryDirectory() as tmp,socket.socket() as sock:
            base=Path(tmp);sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            result=build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_PORT':str(port),
                'HONEYPRINT_PRINTER_NAME':'Lab <one>','HONEYPRINT_PREHEAT_HOTEND_C':'210',
                'HONEYPRINT_PREHEAT_BED_C':'65','HONEYPRINT_REQUEST_MAX_BYTES':'64',
                'HONEYPRINT_UPLOAD_MAX_BYTES':'256','HONEYPRINT_ARTIFACT_DIR':str(base/'retained')},ROOT,base/'generated')
            config=result['config'];registry=create_builtin_registry(runtime_dir=base/'run',runtime_settings=config['runtime'])
            app=FrameworkRuntime(config,registry,base_dir=ROOT,runtime_dir=base/'run')
            def ready(instance,report):
                url=f'http://127.0.0.1:{port}'
                with urllib.request.urlopen(url) as response:
                    page=response.read().decode();self.assertIn('Lab &lt;one&gt;',page);self.assertIn('/api/preheat',page)
                with urllib.request.urlopen(url+'/api/state') as response:
                    state=json.load(response);self.assertEqual(state['printer_name'],'Lab <one>')
                    self.assertEqual(state['target_temperature'],20) # No heating on startup.
                state=post(url+'/api/preheat')
                self.assertEqual((state['target_temperature'],state['bed_target']),(210,65))
                with self.assertRaises(urllib.error.HTTPError) as error:post(url+'/api/command',b' '*65)
                self.assertEqual(error.exception.code,413)
                with self.assertRaises(urllib.error.HTTPError) as error:post(url+'/api/job',b' '*257)
                self.assertEqual(error.exception.code,413)
                post(url+'/api/job',json.dumps({'gcode':JOB.decode()}).encode())
                shadow=instance.resources['manufacturing'];shadow.submit(JOB)
                self.assertEqual(shadow.artifacts.root,base/'retained/custom_01')
            app.run(duration=.1,on_ready=ready)
            self.assertEqual(len(list((base/'retained/custom_01').glob('*.gcode'))),1)
            self.assertFalse((base/'run/artifacts').exists())

    def test_klipper_http_preheat_and_upload_limits_without_native_processes(self):
        from test_klipper_bridge_contract import form
        with tempfile.TemporaryDirectory() as tmp:
            artifacts=ArtifactStorage(tmp)
            calls=[]
            def native(environment,method,path,body,ctype):
                calls.append((path,body));return 200,'application/json',b'{"result":"ok"}'
            profile=load_profile(ROOT/'configs/profiles/virtual_lab.yaml');profile['name']='Klipper Lab'
            with patch('adapters.klipper.http_bridge.request',side_effect=native),create_bridge(object(),0,lambda e:True,
                artifacts=artifacts,printer_profile=profile,common_settings={'request_max_bytes':16,'upload_max_bytes':4,
                                                                            'preheat_hotend_c':215,'preheat_bed_c':70}) as server:
                thread=Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}'
                    post(url+'/printer/honeyprint/preheat')
                    self.assertEqual(calls[-1][0],'/printer/gcode/script')
                    self.assertEqual(json.loads(calls[-1][1])['script'],'M104 S215\nM140 S70')
                    with urllib.request.urlopen(url+'/server/honeyprint/profile') as response:self.assertEqual(json.load(response)['name'],'Klipper Lab')
                    count=len(calls)
                    with self.assertRaises(urllib.error.HTTPError) as error:post(url+'/printer/gcode/script',b'x'*17)
                    self.assertEqual(error.exception.code,413);self.assertEqual(len(calls),count)
                    body,ctype=form(content=b'G28\n');post(url+'/server/files/upload',body,ctype)
                    body,ctype=form(content=b'G28\nX')
                    with self.assertRaises(urllib.error.HTTPError):post(url+'/server/files/upload',body,ctype)
                    self.assertEqual(len(list(Path(tmp).glob('*.gcode'))),1)
                finally:server.shutdown();thread.join(timeout=3)

    def test_octoprint_settings_mapping_and_camera_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=base/'source';(source/'tools').mkdir(parents=True)
            (source/'tools/simulated_webcam.py').write_text('# fixture')
            video=source/'simulated_uploaded_prusa.mp4';video.write_bytes(b'fixture')
            result=build_config({'HONEYPRINT_PLATFORM':'octoprint','HONEYPRINT_PRINTER_NAME':'Named printer'},ROOT,base/'generated')
            cfg=result['config']['instances'][0]
            for label,options in [('auto',{}),('off',{'camera_enabled':False}),('url',{'camera_source':'https://example.test/stream?camera=1'}),('file',{'camera_source':str(video)})]:
                with self.subTest(camera=label):
                    env=OctoPrintEnvironment(base/label,Path(sys.executable),'octo_01')
                    try:
                        with patch.object(env,'verify_runtime'),patch.dict(os.environ,{'_HONEYPRINT_OCTOPRINT_SOURCE_DIR':str(source)}),patch('deployment.environment_manager.subprocess.run',return_value=SimpleNamespace(stdout='{"api_key":"fixture"}')):
                            env.prepare(cfg,audit_port=12345,audit_token='fixture',common_settings={**options,'request_max_bytes':4096,'upload_max_bytes':8192,'preheat_hotend_c':205,'preheat_bed_c':55})
                        settings=yaml.safe_load((base/label/'config.yaml').read_text())
                        self.assertEqual(settings['appearance']['name'],'Named printer')
                        self.assertEqual(settings['server']['maxSize'],4096)
                        self.assertEqual(settings['server']['uploads']['maxSize'],8192)
                        self.assertEqual(settings['temperature']['profiles'][0]['extruder'],205)
                        if label in ('auto','file'):self.assertEqual(env.camera.video,video)
                        elif label=='off':self.assertIn('classicwebcam',settings['plugins']['_disabled']);self.assertIsNone(env.camera)
                        else:self.assertEqual(settings['plugins']['classicwebcam']['stream'],'https://example.test/stream?camera=1');self.assertIsNone(env.camera)
                    finally:env.cleanup()

    def test_custom_bridge_enforces_http_limits(self):
        from adapters.custom.process_adapter import CustomProcessAdapter
        import socket
        with tempfile.TemporaryDirectory() as tmp,socket.socket() as sock:
            base=Path(tmp);sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            result=build_config({'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_PORT':str(port)},ROOT,base/'generated')
            adapter=CustomProcessAdapter(entry=ROOT/'examples/custom_server.py',runtime_dir=base/'run',common_settings={'request_max_bytes':16,'upload_max_bytes':32})
            plan={'config':result['config']['instances'][0],'instance_id':'custom_01'}
            try:
                adapter.prepare(plan);adapter.launch_or_attach(plan);adapter.observe(adapter,lambda e:True)
                for ctype,size in [('application/json',17),('application/octet-stream',33)]:
                    with self.assertRaises(urllib.error.HTTPError) as error:post(f'http://127.0.0.1:{port}/',b'x'*size,ctype)
                    self.assertEqual(error.exception.code,413)
            finally:adapter.stop(adapter)


if __name__=='__main__':unittest.main()

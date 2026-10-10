"""Simulation physics, transactional rejection and real HTTP job evidence."""
from copy import deepcopy
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
import urllib.request
import urllib.error

from core.builtin_registry import create_builtin_registry
from core.runtime import FrameworkRuntime
from plugins.simulators.motion import MotionModel
from plugins.simulators.thermal import ThermalModel
from plugins.simulators.extrusion import ExtrusionModel
from plugins.simulators.printer import PrinterSimulator
from plugins.manufacturing.pipeline import STAGES, run_job
from plugins.manufacturing.job_intake import job_intake
from telemetry.analysis.evidence import EvidenceReader

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {'volume': {'width': 220, 'depth': 220, 'height': 250}, 'heated_bed': True}
JOB = b'G28\nM104 S200\nG4 S90\nG1 X10 Y20 E5\n'


class SimulationTests(unittest.TestCase):
    def test_motion_elapsed_time_and_atomic_bounds(self):
        m = MotionModel(dict(x=220,y=220,z=250))
        m.move(dict(x=100), feedrate=600)
        self.assertEqual(m.step(2)['x'], 20)
        before = dict(m.target)
        with self.assertRaises(ValueError): m.move(dict(y=10,z=999))
        self.assertEqual(m.target, before)
        m.step(20)
        m.move(dict(x=-10), relative=True)
        self.assertEqual(m.step(2)['x'], 90)

    def test_heating_cooling_and_invalid_values(self):
        t = ThermalModel(); t.set_target(200)
        self.assertGreater(t.step(30), 100)
        before = t.temperature
        t.shutdown(); self.assertLess(t.step(30), before)
        for value in (True, float('nan'), float('inf'), 10**1000, -1):
            with self.assertRaises(ValueError): t.step(value)

    def test_cold_extrusion_runout_and_retraction(self):
        e = ExtrusionModel()
        with self.assertRaises(ValueError): e.move(5, temperature=20)
        self.assertEqual(e.position, 0)
        e.move(5, temperature=200)
        e.move(4, temperature=20)
        self.assertEqual(e.consumed, 5)
        with self.assertRaises(ValueError): e.move(6, temperature=200, filament_present=False)
        self.assertEqual(e.position, 4)

    def test_command_atomicity_and_emergency_stop(self):
        p = PrinterSimulator(PROFILE)
        before = p.snapshot()
        for cmd in ('G1 X10 E5', 'G1 X999', 'G1 X1 X2', 'G1 XNAN', 'M104 S999', 'M999'):
            with self.assertRaises(ValueError): p.command(cmd)
            self.assertEqual(p.snapshot(), before)
        p.command('M104 S200'); p.step(90); p.command('G1 X10 E5')
        p.command('M112'); state = p.snapshot()
        with self.assertRaises(ValueError): p.command('G1 X20')
        self.assertEqual(p.snapshot(), state)
        self.assertIn('T:', p.command('M105')['response'])

    def test_sensor_fault_blocks_motion(self):
        p = PrinterSimulator(PROFILE)
        p.sensors.set_fault('thermistor_open', True)
        self.assertIsNone(p.snapshot()['sensors']['temperature'])
        with self.assertRaises(ValueError): p.command('G1 X10')
        self.assertEqual(p.snapshot()['position']['x'], 0)
        p.sensors.set_fault('thermistor_open', False)
        p.command('G1 X10'); p.step(1)
        self.assertEqual(p.snapshot()['position']['x'], 10)

    def test_pipeline_order_preflight_and_inspection(self):
        p = PrinterSimulator(PROFILE)
        result, events = run_job(JOB, p, STAGES)
        self.assertEqual([event['stage'] for event in events], list(STAGES))
        self.assertTrue(result['inspection']['passed'])
        self.assertEqual(p.snapshot()['consumed_mm'], 5)
        before = deepcopy(p.snapshot())
        with self.assertRaises(ValueError): run_job(b'G1 X999', p, STAGES)
        self.assertEqual(p.snapshot(), before)
        with self.assertRaises(ValueError): run_job(JOB, p, ('inspection',))
        q = PrinterSimulator(PROFILE)
        run_job(JOB, q, STAGES[:2])
        self.assertEqual(q.snapshot()['position']['x'], 0)

    def test_artifact_limits_and_identity(self):
        self.assertEqual(job_intake(JOB)['sha256'], job_intake(JOB)['sha256'])
        for content in (b'', b'\x00', b'\xff', b'A'*1048577, b'; comments only'):
            with self.assertRaises(ValueError): job_intake(content)
        with self.assertRaises(ValueError): job_intake(JOB, name='../job')

    def test_real_job_api_and_persisted_stage_evidence(self):
        with TemporaryDirectory() as tmp, socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]; probe.close()
            root = Path(tmp)/'runtime'
            config = {'schema_version': 1, 'instances': [{
                'id': 'custom_01', 'platform': 'custom_lab', 'deployment': 'managed',
                'simulation': 'lab_sim', 'services': {'web_port': port}, 'stages': list(STAGES),
                'printer_profile': str(ROOT/'configs/profiles/virtual_lab.yaml'),
            }], 'telemetry': {'backend': 'sqlite', 'capture_requests': True,
                             'capture_commands': True, 'capture_state_changes': True}}
            app = FrameworkRuntime(config, create_builtin_registry(runtime_dir=root), base_dir=ROOT, runtime_dir=root)
            def ready(instance, report):
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api/job',
                                                 data=json.dumps({'gcode': JOB.decode()}).encode())
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertTrue(json.load(response)['inspection']['passed'])
                before = deepcopy(instance.resources['platform_handle'].simulator.snapshot())
                request = urllib.request.Request(f'http://127.0.0.1:{port}/api/job',
                                                 data=json.dumps({'gcode': 'G1 X999'}).encode())
                with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(instance.resources['platform_handle'].simulator.snapshot(), before)
            app.run(duration=0.2, on_ready=ready)
            with EvidenceReader(root/'events.sqlite3') as raw:
                events = [row['event'] for row in raw.read_events()]
                stages = [event['action'] for event in events if event['event_type'] == 'manufacturing.stage']
                self.assertEqual(stages, list(STAGES))
                self.assertNotIn(JOB.decode(), json.dumps(events))


if __name__ == '__main__': unittest.main()

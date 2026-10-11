"""Real subprocess startup, restart, interruption and occupied-port behaviour."""
from contextlib import closing
import json
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from quickstart import runtime_ready

ROOT=Path(__file__).resolve().parents[1]


class QuickstartTests(unittest.TestCase):
    def setup_paths(self, root):
        ports=[]
        for _ in range(2):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));ports.append(probe.getsockname()[1])
        config=root/'config.yaml'
        config.write_text(f'''schema_version: 1
instances:
  - id: custom_01
    platform: custom_lab
    deployment: managed
    simulation: lab_sim
    printer_profile: {ROOT / 'configs/profiles/virtual_lab.yaml'}
    services:
      web_port: {ports[0]}
    stages: []
telemetry:
  backend: sqlite
  capture_requests: true
  capture_commands: true
  capture_state_changes: true
''')
        command=[sys.executable,str(ROOT/'quickstart.py'),'--config',str(config),'--runtime-dir',str(root/'runtime'),
                 '--dashboard-port',str(ports[1])]
        return command,ports

    def assert_closed(self, ports):
        for port in ports:
            with socket.socket() as connection:
                connection.settimeout(0.2)
                self.assertNotEqual(connection.connect_ex(('127.0.0.1',port)),0)

    def test_real_start_stop_and_immediate_restart(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);command,ports=self.setup_paths(root)
            for _ in range(2):
                result=subprocess.run(command+['--duration','0.3'],cwd=ROOT,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('Local services ready',result.stdout)
                self.assert_closed(ports)
            with closing(sqlite3.connect(root/'runtime/events.sqlite3')) as connection:
                events=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
            self.assertEqual(sum(event['event_type']=='framework.lifecycle' and event['action']=='start' for event in events),2)
            self.assertEqual(sum(event['event_type']=='framework.lifecycle' and event['action']=='stop' for event in events),2)

    def test_sigterm_cleans_up_owned_processes(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);command,ports=self.setup_paths(root)
            process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+10
                while not runtime_ready(root/'runtime/events.sqlite3',0,{'custom_01'}):
                    if time.monotonic()>deadline or process.poll() is not None:self.fail('Runtime did not start')
                    time.sleep(0.05)
                process.send_signal(signal.SIGTERM)
                stdout,stderr=process.communicate(timeout=15)
                self.assertEqual(process.returncode,0,stdout+stderr)
                self.assert_closed(ports)
            finally:
                if process.poll() is None:process.kill();process.communicate(timeout=15)

    def test_explicit_framework_stop_closes_companion_services(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);command,ports=self.setup_paths(root)
            process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+10
                while not runtime_ready(root/'runtime/events.sqlite3',0,{'custom_01'}):
                    if time.monotonic()>deadline or process.poll() is not None:self.fail('Runtime did not start')
                    time.sleep(0.05)
                stopped=subprocess.run([sys.executable,str(ROOT/'main.py'),'stop','--runtime-dir',str(root/'runtime')],
                                       cwd=ROOT,capture_output=True,text=True,timeout=5)
                self.assertEqual(stopped.returncode,0,stopped.stderr)
                stdout,stderr=process.communicate(timeout=15)
                self.assertEqual(process.returncode,0,stdout+stderr)
                self.assert_closed(ports)
            finally:
                if process.poll() is None:process.kill();process.communicate(timeout=15)

    def test_occupied_dashboard_port_does_not_start_framework(self):
        with TemporaryDirectory() as tmp,socket.socket() as occupied:
            root=Path(tmp);command,ports=self.setup_paths(root)
            occupied.bind(('127.0.0.1',ports[1]));occupied.listen()
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertFalse((root/'runtime/control.json').exists())
            self.assert_closed([ports[0]])


if __name__=='__main__':unittest.main()

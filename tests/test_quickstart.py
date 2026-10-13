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

from quickstart import runtime_ready, stop_children

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

    def test_shutdown_preserves_active_owned_helper_for_core_cleanup(self):
        code = """import signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
def stop(*args):
 print('owned_helper_alive='+str(child.poll() is None),flush=True)
 child.terminate();child.wait(timeout=5);sys.exit(0)
signal.signal(signal.SIGTERM,stop)
print('ready',flush=True)
while True:time.sleep(.1)
"""
        process=subprocess.Popen([sys.executable,'-c',code],start_new_session=True,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready')
            stop_children([process])
            stdout,stderr=process.communicate(timeout=5)
            self.assertEqual(process.returncode,0,stderr)
            self.assertIn('owned_helper_alive=True',stdout)
        finally:
            if process.poll() is None:
                import os
                os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)

    def test_occupied_dashboard_port_does_not_start_framework(self):
        with TemporaryDirectory() as tmp,socket.socket() as occupied:
            root=Path(tmp);command,ports=self.setup_paths(root)
            occupied.bind(('127.0.0.1',ports[1]));occupied.listen()
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertIn(f'dashboard port {ports[1]} is already in use',result.stderr)
            self.assertIn('HONEYPRINT_DASHBOARD_PORT',result.stderr)
            self.assertFalse((root/'runtime/control.json').exists())
            self.assert_closed([ports[0]])


    def test_existing_runtime_is_preserved_with_a_clear_stop_diagnostic(self):
        import fcntl
        with TemporaryDirectory() as tmp:
            root=Path(tmp);command,ports=self.setup_paths(root)
            runtime=root/'runtime';runtime.mkdir()
            record=runtime/'control.json';record.write_text('existing record')
            with (runtime/'control.lock').open('w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertIn('This runtime is already running',result.stderr)
            self.assertIn('main.py stop --runtime-dir',result.stderr)
            self.assertEqual(record.read_text(),'existing record')
            self.assert_closed(ports)

    def test_occupied_honeypot_port_does_not_start_framework(self):
        with TemporaryDirectory() as tmp,socket.socket() as occupied:
            root=Path(tmp);command,ports=self.setup_paths(root)
            occupied.bind(('127.0.0.1',ports[0]));occupied.listen()
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertIn(f'Honeypot port {ports[0]} is already in use',result.stderr)
            self.assertFalse((root/'runtime/control.json').exists())
            self.assert_closed([ports[1]])



    def test_terminal_close_stops_core_and_companions(self):
        import os
        import pty
        with TemporaryDirectory() as tmp:
            root=Path(tmp);command,ports=self.setup_paths(root)
            pid,terminal=pty.fork()
            if pid == 0:
                os.chdir(ROOT)
                os.execv(command[0],command)
            reaped=False
            try:
                deadline=time.monotonic()+15
                while not runtime_ready(root/'runtime/events.sqlite3',0,{'custom_01'}):
                    ended,_=os.waitpid(pid,os.WNOHANG)
                    if ended:
                        reaped=True;self.fail('Launcher exited before readiness')
                    if time.monotonic()>deadline:self.fail('Terminal runtime did not start')
                    time.sleep(.05)
                # Closing the controlling PTY produces the same hangup as closing a terminal window.
                os.close(terminal);terminal=None
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    ended,_=os.waitpid(pid,os.WNOHANG)
                    if ended:reaped=True;break
                    time.sleep(.05)
                self.assertTrue(reaped,'Launcher did not exit after terminal hangup')
                self.assert_closed(ports)
                self.assertFalse((root/'runtime/control.json').exists())
                with closing(sqlite3.connect(root/'runtime/events.sqlite3')) as db:
                    events=[json.loads(row[0]) for row in db.execute('SELECT payload FROM events')]
                self.assertTrue(any(e['event_type']=='framework.lifecycle' and e['action']=='stop' for e in events))
            finally:
                if terminal is not None:os.close(terminal)
                if not reaped:
                    os.kill(pid,signal.SIGTERM)
                    os.waitpid(pid,0)

if __name__=='__main__':unittest.main()

"""Env selection, literal parsing and real custom-code launch through one command."""
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
from deployment.env_config import read_env,build_config
from quickstart import runtime_ready

ROOT=Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));return probe.getsockname()[1]


class EnvLaunchTests(unittest.TestCase):
    def test_platform_switch_selects_compatible_companions(self):
        with tempfile.TemporaryDirectory() as directory:
            for selection,platform,simulator in [('octoprint','octoprint','virtual_printer'),('klipper','klipper','avr_sim'),('custom','custom_lab','lab_sim')]:
                with self.subTest(platform=selection):
                    output=Path(directory)/selection
                    result=build_config({'HONEYPRINT_PLATFORM':selection,'HONEYPRINT_WIDTH':'300','HONEYPRINT_PORT':'18121'},ROOT,output)
                    instance=result['config']['instances'][0]
                    self.assertEqual((instance['platform'],instance['simulation']),(platform,simulator))
                    self.assertEqual(instance['services']['web_port'],18121)
                    from core.profile_loader import load_profile
                    self.assertEqual(load_profile(instance['printer_profile'])['volume']['width'],300)
                    if selection=='klipper':self.assertEqual(instance['frontend'],'mainsail')
    def test_literal_file_duplicates_unknown_keys_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
            file=Path(directory)/'.env';file.write_text('HONEYPRINT_PLATFORM=custom\nHONEYPRINT_CUSTOM_CODE="$(touch forbidden).py"\n')
            values=read_env(file);self.assertEqual(values['HONEYPRINT_CUSTOM_CODE'],'$(touch forbidden).py')
            with patch.dict(os.environ,{'HONEYPRINT_PLATFORM':'klipper'}):self.assertEqual(read_env(file)['HONEYPRINT_PLATFORM'],'klipper')
            for body in ('HONEYPRINT_PORT=1234\nHONEYPRINT_PORT=5678','UNRECOGNIZED=value'):
                file.write_text(body)
                with self.assertRaises(ValueError):read_env(file)
    def test_invalid_geometry_platform_and_port_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for values in ({'HONEYPRINT_PLATFORM':'unknown'},{'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_PORT':'0'},
                           {'HONEYPRINT_PLATFORM':'custom','HONEYPRINT_WIDTH':'nan'},
                           {'HONEYPRINT_PLATFORM':'all','HONEYPRINT_PORT':'8123'}):
                with self.subTest(values=values),self.assertRaises(ValueError):build_config(values,ROOT,Path(directory))
    def test_real_env_only_switch_builtin_custom_to_developer_code(self):
        import urllib.request
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for own_code in (False,True):
                runtime=root/('own' if own_code else 'lab');web=free_port();dashboard=free_port()
                env=root/'.env'
                env.write_text(f'HONEYPRINT_PLATFORM=custom\nHONEYPRINT_PORT={web}\nHONEYPRINT_DASHBOARD_PORT={dashboard}\nHONEYPRINT_WIDTH=300\nHONEYPRINT_RUNTIME_DIR={runtime}\nHONEYPRINT_CUSTOM_CODE={ROOT / "examples/custom_server.py" if own_code else ""}\n')
                process=subprocess.Popen([sys.executable,str(ROOT/'quickstart.py'),'--env-file',str(env),'--duration','1'],
                                         cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+15
                    while not runtime_ready(runtime/'events.sqlite3',0,{'custom_01'}):
                        if process.poll() is not None or time.monotonic()>deadline:
                            stdout,stderr=process.communicate(timeout=5);self.fail(stdout+stderr)
                        time.sleep(.05)
                    with urllib.request.urlopen(f'http://127.0.0.1:{web}/',timeout=3) as response:
                        data=response.read();self.assertEqual(response.status,200)
                        if own_code:
                            self.assertEqual(json.loads(data)['honeypot'],'developer_custom_server')
                            self.assertEqual(json.loads(data)['profile']['volume']['width'],300)
                            self.assertIsNone(response.getheader('X-HoneyPrint-Instance'))
                        else:self.assertIn(b'Custom Printer Lab',data)
                    stdout,stderr=process.communicate(timeout=15)
                    self.assertEqual(process.returncode,0,stdout+stderr)
                    self.assertIn('Local services ready',stdout)
                    with closing(sqlite3.connect(runtime/'events.sqlite3')) as connection:
                        events=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
                    platform='custom' if own_code else 'custom_lab'
                    self.assertTrue(any(e['platform']==platform and e['event_type']=='http.request' for e in events))
                    for port in (web,dashboard):
                        with socket.socket() as check:self.assertNotEqual(check.connect_ex(('127.0.0.1',port)),0)
                finally:
                    if process.poll() is None:process.terminate();process.communicate(timeout=20)

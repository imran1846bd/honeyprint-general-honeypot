"""Start the local framework, alert watcher and evidence dashboard together."""
import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent


def last_sequence(database):
    if not database.exists(): return 0
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as connection:
        return connection.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]


def runtime_ready(database, baseline, expected):
    if not database.exists(): return False
    try:
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=0.2) as connection:
            rows=connection.execute('SELECT payload FROM events WHERE sequence>?', (baseline,)).fetchall()
        observed=set()
        for row in rows:
            event=json.loads(row[0])
            if event['event_type']=='framework.lifecycle' and event['action']=='start' and event['outcome']=='completed':
                observed.add(event['instance_id'])
        return expected <= observed
    except (sqlite3.Error, ValueError, KeyError): return False


def stop_children(children):
    for process in reversed(children):
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    for process in reversed(children):
        try: process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=5)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Run local HoneyPrint with analysis and dashboard.')
    parser.add_argument('--config',type=Path,default=ROOT/'configs/instances/custom.yaml')
    parser.add_argument('--runtime-dir',type=Path,default=ROOT/'.honeyprint/quickstart')
    parser.add_argument('--dashboard-port',type=int,default=8090)
    parser.add_argument('--octoprint-python',type=Path)
    parser.add_argument('--attach-api-key-env')
    parser.add_argument('--duration',type=float,help='Stop all services after this many ready seconds')
    args=parser.parse_args(argv)
    if not 1 <= args.dashboard_port <= 65535: parser.error('Invalid dashboard port')
    if args.duration is not None and not 0 < args.duration <= 86400: parser.error('Invalid duration')
    children=[];stopping=threading.Event()
    previous={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
    for sig in previous:signal.signal(sig,lambda *_:stopping.set())
    try:
        from core.config_loader import load_config
        config=load_config(args.config)
        expected={instance['id'] for instance in config['instances']}
        if not expected:raise ValueError('No configured instances')
        if any(args.dashboard_port in instance['services'].values() for instance in config['instances']):
            raise ValueError('Dashboard port conflicts with a platform port')
        runtime=args.runtime_dir.absolute();database=runtime/'events.sqlite3';alerts=runtime/'analysis.sqlite3'
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind(('127.0.0.1',args.dashboard_port))
        baseline=last_sequence(database)
        command=[sys.executable,str(ROOT/'main.py'),'run',str(args.config.absolute()),'--runtime-dir',str(runtime)]
        if args.octoprint_python:command+=['--octoprint-python',str(args.octoprint_python.absolute())]
        if args.attach_api_key_env:command+=['--attach-api-key-env',args.attach_api_key_env]
        children.append(subprocess.Popen(command,cwd=ROOT,start_new_session=True))
        deadline=time.monotonic()+90
        while not runtime_ready(database,baseline,expected):
            if stopping.wait(0.1):return 0
            if children[0].poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('Framework startup failed')
        children.append(subprocess.Popen([sys.executable,'-m','telemetry.analysis.service','--database',str(database),
                                          '--alerts',str(alerts),'--watch'],cwd=ROOT,start_new_session=True))
        children.append(subprocess.Popen([sys.executable,'-m','dashboard.server','--database',str(database),
                                          '--alerts',str(alerts),'--port',str(args.dashboard_port)],cwd=ROOT,start_new_session=True))
        deadline=time.monotonic()+10
        while True:
            if stopping.wait(0.1):return 0
            if any(p.poll() is not None for p in children):raise RuntimeError('A local service stopped unexpectedly')
            connection=http.client.HTTPConnection('127.0.0.1',args.dashboard_port,timeout=1)
            try:
                connection.request('GET','/api/events');response=connection.getresponse()
                if response.status==200 and isinstance(json.loads(response.read(1048576)),list):break
            except (OSError,ValueError,http.client.HTTPException):pass
            finally:connection.close()
            if time.monotonic()>deadline:raise RuntimeError('Dashboard startup failed')
        print(f'Local services ready. Dashboard: http://127.0.0.1:{args.dashboard_port}. Ctrl-C stops all services.',flush=True)
        started=time.monotonic()
        while not stopping.wait(0.2):
            if any(p.poll() is not None for p in children):raise RuntimeError('A local service stopped unexpectedly')
            if args.duration is not None and time.monotonic()-started>=args.duration:break
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error,KeyError,ModuleNotFoundError):
        print('Local startup failed. Check dependencies, configuration and the preceding service output.',file=sys.stderr)
        return 1
    finally:
        stop_children(children)
        for sig,handler in previous.items():signal.signal(sig,handler)
        if children and children[0].returncode != 0:
            return 1


if __name__=='__main__':raise SystemExit(main())

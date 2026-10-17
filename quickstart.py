"""Start the local framework, alert watcher and evidence dashboard together."""
import argparse
import errno
from contextlib import closing
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
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def last_sequence(database):
    if not database.exists(): return 0
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as connection:
        return connection.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]


def runtime_ready(database, baseline, expected):
    if not database.exists(): return False
    try:
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=0.2)) as connection:
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
            try:
                if process is children[0]:
                    process.send_signal(signal.SIGTERM)  # Core owns cleanup of its active Docker clients.
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    for process in reversed(children):
        try: process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=5)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Run local HoneyPrint with analysis and dashboard.')
    parser.add_argument('--config',type=Path,help='Explicit YAML override of env selection')
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    parser.add_argument('--custom-code',type=Path)
    parser.add_argument('--custom-api-url')
    parser.add_argument('--analyst', dest='analyst_enabled', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--custom-python',type=Path)
    parser.add_argument('--custom-health-path')
    parser.add_argument('--runtime-dir',type=Path)
    parser.add_argument('--dashboard-port',type=int)
    parser.add_argument('--octoprint-python',type=Path)
    parser.add_argument('--octoprint-source',type=Path)
    parser.add_argument('--klipper-source',type=Path)
    parser.add_argument('--attach-api-key-env')
    parser.add_argument('--no-bootstrap',action='store_true',help='Use already installed platform dependencies')
    parser.add_argument('--duration',type=float,help='Stop all services after this many ready seconds')
    args=parser.parse_args(argv)
    generated=None;selected=None;ssh_ready_directory=None
    try:
        if args.config is None:
            from deployment.env_config import read_env, build_config
            folder=ROOT/'.honeyprint/launch';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            generated=tempfile.TemporaryDirectory(prefix='env-',dir=folder)
            selected=build_config(read_env(args.env_file),args.env_file.absolute().parent,Path(generated.name))
            args.config=selected['config_path']
            for field in ('runtime_dir','dashboard_port','octoprint_python','custom_code','custom_python','custom_health_path','octoprint_source','klipper_source','custom_api_url','analyst_enabled'):
                if getattr(args,field) is None:setattr(args,field,selected[field])
        if args.analyst_enabled is None:args.analyst_enabled=True
        ssh_enabled=selected['ssh_enabled'] if selected else False
        ssh_host=selected['ssh_host'] if selected else '127.0.0.1'
        ssh_port=selected['ssh_port'] if selected else 2222
        args.runtime_dir=args.runtime_dir or ROOT/'.honeyprint/quickstart'
        args.dashboard_port=args.dashboard_port or 8090
        args.custom_health_path=args.custom_health_path or '/'
        if not 1 <= args.dashboard_port <= 65535:raise ValueError('Invalid dashboard port')
    except (OSError,ValueError,RuntimeError) as error:
        if generated:generated.cleanup()
        parser.error(str(error))
    if args.duration is not None and not 0 < args.duration <= 86400: parser.error('Invalid duration')
    children=[];stopping=threading.Event()
    shutdown_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous={sig:signal.getsignal(sig) for sig in shutdown_signals}
    for sig in previous:signal.signal(sig,lambda *_:stopping.set())
    try:
        from core.config_loader import load_config
        config=load_config(args.config)
        from core.builtin_registry import create_builtin_registry
        from core.validator import validate_config
        validate_config(config, create_builtin_registry(runtime_dir=args.runtime_dir, octoprint_python=args.octoprint_python,
                           custom_code=args.custom_code, custom_python=args.custom_python, custom_health_path=args.custom_health_path, custom_api_url=args.custom_api_url))
        # Diagnose a second launch before dependency preparation or child startup.
        import fcntl
        runtime_lock = args.runtime_dir / 'control.lock'
        if runtime_lock.exists():
            with runtime_lock.open('r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise RuntimeError('This runtime is already running. Stop it with Ctrl-C or main.py stop --runtime-dir ' + str(args.runtime_dir) + ' and wait for shutdown before changing platforms') from None
        for instance in config['instances']:
            web_port = instance['services'].get('web_port')
            if instance['deployment'] == 'managed' and web_port:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:probe.bind(('127.0.0.1', web_port))
                    except OSError:
                        raise RuntimeError(f'Honeypot port {web_port} is already in use. Stop the previous run or change HONEYPRINT_PORT') from None
        if args.no_bootstrap and (args.octoprint_source or args.klipper_source):
            raise ValueError('Local sources require bootstrap to verify their builds')
        if not args.no_bootstrap:
            from deployment.bootstrap import install_selected
            args.octoprint_python = install_selected(config, args.octoprint_python, octoprint_source=args.octoprint_source, klipper_source=args.klipper_source)
        expected={instance['id'] for instance in config['instances']}
        if not expected:raise ValueError('No configured instances')
        if args.analyst_enabled and any(args.dashboard_port in instance['services'].values() for instance in config['instances']):
            raise ValueError('Dashboard port conflicts with a platform port')
        if ssh_enabled:
            if len(config['instances']) != 1:
                raise ValueError('SSH decoy requires one selected platform instance')
            if ssh_port == args.dashboard_port and args.analyst_enabled:
                raise ValueError('SSH port conflicts with the analyst dashboard port')
            if any(ssh_port in instance['services'].values() for instance in config['instances']):
                raise ValueError('SSH port conflicts with a platform port')
            with socket.socket() as probe:
                try:probe.bind((ssh_host,ssh_port))
                except OSError:
                    raise RuntimeError(f'SSH port {ssh_port} is already in use. Change HONEYPRINT_SSH_PORT in .env') from None
        runtime=args.runtime_dir.absolute();database=runtime/'events.sqlite3';alerts=runtime/'analysis.sqlite3'
        if args.analyst_enabled:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                probe.bind(('127.0.0.1',args.dashboard_port))
        baseline=last_sequence(database)
        command=[sys.executable,str(ROOT/'main.py'),'run',str(args.config.absolute()),'--runtime-dir',str(runtime)]
        if args.octoprint_python:command+=['--octoprint-python',str(args.octoprint_python.absolute())]
        if args.attach_api_key_env:command+=['--attach-api-key-env',args.attach_api_key_env]
        if args.custom_code:command+=['--custom-code',str(args.custom_code.absolute())]
        if args.custom_api_url:command+=['--custom-api-url',args.custom_api_url]
        if args.custom_python:command+=['--custom-python',str(args.custom_python.absolute())]
        command+=['--custom-health-path',args.custom_health_path]
        children.append(subprocess.Popen(command,cwd=ROOT,start_new_session=True))
        deadline=time.monotonic()+180
        while not runtime_ready(database,baseline,expected):
            if stopping.wait(0.1):return 0
            if children[0].poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('Framework startup failed')
        if ssh_enabled:
            instance=config['instances'][0]
            ssh_ready_directory=tempfile.TemporaryDirectory(prefix='honeyprint-ssh-')
            ssh_ready=Path(ssh_ready_directory.name)/'ready'
            ssh_env=os.environ.copy()
            ssh_env['HONEYPRINT_SSH_USERNAME']=selected['ssh_username']
            ssh_env['HONEYPRINT_SSH_PASSWORD']=selected['ssh_password']
            ssh_command=[sys.executable,'-m','services.ssh_decoy',
                '--host',ssh_host,'--port',str(ssh_port),'--database',str(database),
                '--instance-id',instance['id'],'--platform',instance['platform'],
                '--host-key',str(runtime/'ssh_host_key'),'--ready-file',str(ssh_ready)]
            children.append(subprocess.Popen(ssh_command,cwd=ROOT,env=ssh_env,start_new_session=True))
            deadline=time.monotonic()+10
            while not ssh_ready.exists():
                if stopping.wait(0.1):return 0
                if any(p.poll() is not None for p in children) or time.monotonic()>deadline:
                    raise RuntimeError('SSH service startup failed')
        children.append(subprocess.Popen([sys.executable,'-m','telemetry.analysis.service','--database',str(database),
                                          '--alerts',str(alerts),'--watch'],cwd=ROOT,start_new_session=True))
        if args.analyst_enabled:
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
        links=', '.join(f'http://127.0.0.1:{i["services"]["web_port"]}' for i in config['instances'])
        print(f'Local services ready. Honeypot: {links}. Logs: {database}. Ctrl-C stops all services.',flush=True)
        if ssh_enabled:print(f'SSH decoy: ssh -p {ssh_port} {selected["ssh_username"]}@127.0.0.1',flush=True)
        if args.analyst_enabled:print(f'Optional analyst dashboard: http://127.0.0.1:{args.dashboard_port}',flush=True)
        started=time.monotonic()
        while not stopping.wait(0.2):
            if children[0].poll() == 0:break  # Explicit framework stop also closes companion services.
            if any(p.poll() is not None for p in children):raise RuntimeError('A local service stopped unexpectedly')
            if args.duration is not None and time.monotonic()-started>=args.duration:break
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error,KeyError,ModuleNotFoundError) as error:
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            print(f'Local startup failed: dashboard port {args.dashboard_port} is already in use. Stop the existing run with main.py stop --runtime-dir followed by your selected runtime directory, or change HONEYPRINT_DASHBOARD_PORT in .env.',file=sys.stderr)
        else:
            print(f'Local startup failed: {error}',file=sys.stderr)
        return 1
    finally:
        stop_children(children)
        if ssh_ready_directory:ssh_ready_directory.cleanup()
        if generated:generated.cleanup()
        for sig,handler in previous.items():signal.signal(sig,handler)
        if children and children[0].returncode != 0:
            return 1


if __name__=='__main__':raise SystemExit(main())

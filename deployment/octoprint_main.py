"""Supervise the Docker-backed OctoPrint Main simulator and its evidence."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from urllib.request import urlopen

from core.config_loader import load_config
from deployment.control import ControlServer
from telemetry.normalizer import normalize_event
from telemetry.sqlite_storage import SQLiteStorage

SOURCE = Path(__file__).resolve().parents[1] / 'adapters' / 'octoprint -main'
CONTAINER = IMAGE = 'honeyprint-octoprint-main-local'
PREFIX = 'HONEYPRINT_EVIDENCE '


def prepare_image():
    subprocess.run(['docker', 'build', '-t', IMAGE, str(SOURCE)], check=True)


def run(config_path, runtime):
    config = load_config(config_path)
    instance, = config['instances']
    settings = config['runtime']
    web_port = instance['services']['web_port']
    database = runtime / settings['evidence_db_name']
    timeout = settings['sqlite_busy_timeout_seconds']
    identity = dict(instance_id=instance['id'], platform=instance['platform'])

    def lifecycle(action):
        event = normalize_event(dict(source='framework', event_type='framework.lifecycle',
                                     action=action, outcome='completed', simulation=True), **identity)
        with SQLiteStorage(database, timeout=timeout) as storage:
            storage.write_batch([event])

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopping.set())
    control = ControlServer(runtime, stopping)
    control.start()
    container_id = None
    log_process = None
    reader = None
    read_errors = []
    ready = False
    try:
        from core.profile_loader import load_profile
        profile = load_profile(instance['printer_profile'])
        env = os.environ.copy()
        env['HONEYPRINT_API_KEY'] = env['HONEYPRINT_MAIN_API_KEY']
        command = [
            'docker', 'run', '--detach', '--rm', '--pull=never',
            '--name', CONTAINER, '--publish', f'127.0.0.1:{web_port}:80',
            '--mount', f'type=volume,source={CONTAINER}-data,target=/app/data',
            '--mount', f'type=volume,source={CONTAINER}-logs,target=/app/logs',
            '--env', 'HONEYPRINT_API_KEY',
            '--env', f"HONEYPRINT_PRINTER_NAME={profile['name']}",
            '--env', 'HONEYPRINT_STORAGE_DIR=/app/data/octo',
        ]
        for flag in ('capture_requests', 'capture_commands', 'capture_state_changes'):
            command.extend(['--env', f'HONEYPRINT_{flag.upper()}={int(config["telemetry"][flag])}'])
        container_id = subprocess.check_output([*command, IMAGE], env=env, text=True).strip()
        log_process = subprocess.Popen(
            ['docker', 'logs', '--follow', container_id], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )

        def capture():
            try:
                with SQLiteStorage(database, timeout=timeout) as storage:
                    for line in log_process.stdout:
                        if line.startswith(PREFIX):
                            observation = json.loads(line[len(PREFIX):])
                            storage.write_batch([normalize_event(observation, **identity)])
            except Exception as error:
                read_errors.append(error)

        reader = threading.Thread(target=capture, name='OctoPrintMainEvidence', daemon=True)
        reader.start()
        url = f'http://127.0.0.1:{web_port}'
        deadline = time.monotonic() + settings['startup_timeout_seconds']
        while not stopping.is_set():
            if not reader.is_alive():
                raise RuntimeError('OctoPrint Main evidence stream stopped') from (
                    read_errors[0] if read_errors else None)
            if not ready:
                try:
                    with urlopen(url, timeout=1):
                        pass
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'OctoPrint Main did not respond at {url}') from None
                else:
                    lifecycle('start')
                    ready = True
                    print(f"{instance['id']}: ready; platform=octoprint-main; {url}", flush=True)
            stopping.wait(0.25)
    finally:
        try:
            if container_id:
                subprocess.run(['docker', 'stop', container_id], stdout=subprocess.DEVNULL, check=False)
            if reader:
                reader.join(timeout=5)
            if log_process:
                if log_process.poll() is None:
                    log_process.terminate()
                log_process.wait(timeout=5)
                log_process.stdout.close()
            if ready:
                lifecycle('stop')
        finally:
            control.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('runtime', type=Path)
    args = parser.parse_args()
    run(args.config, args.runtime)

"""Install optional pinned runtimes selected by a validated local configuration."""
import os
from pathlib import Path
import subprocess
import sys
from adapters.base_adapter import AdapterError
from deployment.klipper_environment import KlipperEnvironment

ROOT = Path(__file__).resolve().parents[1]


def install_selected(config, octoprint_python=None):
    """Prepare dependencies only; platform lifecycle remains owned by the core."""
    platforms = {item['platform'] for item in config['instances']}
    selected = octoprint_python
    if 'octoprint' in platforms and selected is None:
        if not (3, 10) <= sys.version_info[:2] <= (3, 13):
            raise AdapterError('The pinned OctoPrint runtime requires Python 3.10 through 3.13')
        directory = ROOT / '.venv-octoprint'
        selected = directory / 'bin/python'
        if not selected.exists():
            print('Preparing pinned OctoPrint environment', flush=True)
            subprocess.run([sys.executable, '-m', 'venv', str(directory)], check=True)
        check = ('import importlib.metadata,pathlib; '
                 'pins=pathlib.Path(' + repr(str(ROOT / 'requirements-octoprint.txt')) + ').read_text().splitlines(); '
                 'assert all(importlib.metadata.version(p.split("==")[0])==p.split("==")[1] '
                 'for p in pins if p and not p.startswith("#"))')
        ready = subprocess.run([str(selected), '-c', check], capture_output=True).returncode == 0
        if not ready:
            subprocess.run([str(selected), '-m', 'pip', 'install', '-r',
                            str(ROOT / 'requirements-octoprint.txt')], check=True)
            subprocess.run([str(selected), '-c', check], check=True)
    if 'klipper' in platforms:
        subprocess.run(['docker', 'info', '--format', '{{.OSType}}'], check=True)
        try:
            KlipperEnvironment.prepare()
        except AdapterError:
            print('Building pinned Klipper, Moonraker and AVR emulator image', flush=True)
            state = ROOT / '.honeyprint/buildx'
            state.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ, BUILDX_CONFIG=str(state))
            subprocess.run(['docker', 'build', '-t', 'honeyprint-klipper:1',
                            str(ROOT / 'deployment/docker/klipper')], env=environment, check=True)
            KlipperEnvironment.prepare()
    return selected

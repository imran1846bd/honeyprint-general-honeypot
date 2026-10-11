"""Build trusted local source snapshots without changing the developer's tree."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from adapters.base_adapter import AdapterError

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.honeyprint/local-sources'
IGNORED = {'.git', '.venv', 'venv', '.push-work', '__pycache__', '.pytest_cache', 'build', 'dist', 'out', '.tox'}


def snapshot(source, platform):
    source = Path(source).expanduser().resolve(strict=True)
    required = {'octoprint': ['pyproject.toml', 'setup.py', 'src/octoprint/__init__.py'],
                'klipper': ['Makefile', 'klippy/klippy.py', 'scripts/klippy-requirements.txt']}[platform]
    if not source.is_dir() or any(not (source / name).is_file() for name in required):
        raise AdapterError(f'Invalid {platform} source folder')
    digest = hashlib.sha256(); files = []; total = 0
    for directory, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED and not d.startswith('.venv'))
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise AdapterError('Local source snapshots do not accept symbolic links')
        for name in sorted(names):
            if name == '.DS_Store' or name.startswith('activity.sqlite3'):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise AdapterError('Local source snapshots require regular files')
            data = path.read_bytes(); total += len(data)
            if total > 512 * 1024 * 1024 or len(files) >= 50000:
                raise AdapterError('Local source snapshot exceeds its size limit')
            relative = path.relative_to(source)
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            digest.update(str(relative).encode() + b'\0' + str(len(data)).encode() + b'\0')
            digest.update(b'x' if executable else b'-'); digest.update(data)
            files.append((relative, data, executable))
    revision = digest.hexdigest(); destination = CACHE / platform / revision / 'source'
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            stage = Path(tmp) / 'source'; stage.mkdir()
            for relative, data, executable in files:
                path = stage / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data); path.chmod(0o755 if executable else 0o644)
            stage.rename(destination)
    return destination, revision


def prepare_sources(platforms, octoprint_source, klipper_source, python=None):
    if octoprint_source and 'octoprint' not in platforms:
        raise AdapterError('OctoPrint source requires an OctoPrint instance')
    if klipper_source and 'klipper' not in platforms:
        raise AdapterError('Klipper source requires a Klipper instance')
    if octoprint_source and python:
        raise AdapterError('Choose either OctoPrint source or an existing interpreter')
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / 'build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if octoprint_source:
            source, digest = snapshot(octoprint_source, 'octoprint')
            environment = source.parent / 'venv'; python = environment / 'bin/python'
            marker = source.parent / 'installed.json'
            if not marker.exists():
                print('Installing local OctoPrint source ' + digest[:12], flush=True)
                subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
                subprocess.run([str(python), '-m', 'pip', 'install', str(source)], check=True)
                code = 'import octoprint,json; print(json.dumps({"version":octoprint.__version__}))'
                result = subprocess.run([str(python), '-c', code], check=True, capture_output=True, text=True)
                version = json.loads(result.stdout)['version']
                marker.write_text(json.dumps({'version': version, 'sha256': digest}))
            metadata = json.loads(marker.read_text())
            os.environ['_HONEYPRINT_OCTOPRINT_VERSION'] = metadata['version']
            os.environ['_HONEYPRINT_OCTOPRINT_SOURCE_SHA256'] = digest
            os.environ['_HONEYPRINT_OCTOPRINT_SOURCE_DIR'] = str(source)
        if klipper_source:
            source, digest = snapshot(klipper_source, 'klipper')
            # Archive downloads lack Git metadata; an owned deterministic commit
            # makes the compiled MCU revision verifiable without inventing an upstream SHA.
            git_env = dict(os.environ, GIT_AUTHOR_NAME='HoneyPrint', GIT_AUTHOR_EMAIL='local@honeyprint.invalid',
                           GIT_COMMITTER_NAME='HoneyPrint', GIT_COMMITTER_EMAIL='local@honeyprint.invalid',
                           GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
            if not (source / '.git').exists():
                for args in [('init',), ('add', '--force', '.'), ('commit', '-m', 'Local source snapshot ' + digest)]:
                    subprocess.run(['git', '-c', 'core.autocrlf=false', *args], cwd=source, env=git_env,
                                   check=True, capture_output=True)
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
            from deployment.klipper_environment import PINS, KlipperEnvironment
            PINS['klipper'] = revision
            os.environ['_HONEYPRINT_KLIPPER_REV'] = revision
            os.environ['_HONEYPRINT_KLIPPER_SOURCE_SHA256'] = digest
            image = 'honeyprint-klipper-local:' + digest[:24]
            os.environ['_HONEYPRINT_KLIPPER_IMAGE'] = image
            try:
                KlipperEnvironment.prepare()
            except AdapterError:
                print('Building local Klipper source ' + digest[:12], flush=True)
                context = source.parent / 'docker'
                shutil.copytree(ROOT / 'deployment/docker/klipper', context, dirs_exist_ok=True)
                target = context / 'source'
                if target.exists(): shutil.rmtree(target)
                shutil.copytree(source, target)
                dockerfile = (context / 'Dockerfile').read_text().splitlines()
                result = []
                for line in dockerfile:
                    if line.startswith('RUN git init /opt/klipper'):
                        result += ['COPY source /opt/klipper',
                                   "RUN cd /opt/klipper && printf 'CONFIG_MACH_AVR=y\\nCONFIG_MACH_atmega644p=y\\nCONFIG_LOW_LEVEL_OPTIONS=y\\nCONFIG_SIMULAVR=y\\n' > .config && make olddefconfig && make -j2"]
                    else: result.append(line)
                result.append('LABEL org.honeyprint.klipper.source.sha256=' + digest)
                (context / 'Dockerfile').write_text('\n'.join(result) + '\n')
                state = ROOT / '.honeyprint/buildx'; state.mkdir(parents=True, exist_ok=True)
                subprocess.run(['docker', 'build', '--build-arg', 'KLIPPER_REV=' + revision, '-t', image,
                                str(context)], env=dict(os.environ, BUILDX_CONFIG=str(state)), check=True)
                KlipperEnvironment.prepare()
        return python

"""Prepare and own an isolated, loopback-only OctoPrint runtime directory.

No package installation is performed. Only the installed pinned interpreter is
used. Existing directories require a matching HoneyPrint ownership marker; an
exclusive lock prevents two framework processes from sharing an instance root.
"""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import yaml

from core.profile_loader import load_profile, octoprint_profile
from recipes.octoprint import OCTOPRINT_VERSION
from deployment.source_camera import SourceCamera


class EnvironmentError(RuntimeError):
    """Runtime verification, preparation or owned process control failed."""


_BOOTSTRAP = '''
import json, sys
from octoprint.settings import settings
from octoprint.access.groups import FilebasedGroupManager
from octoprint.access.users import FilebasedUserManager
args = json.load(sys.stdin)
s = settings(init=True, basedir=args['root'])
g = FilebasedGroupManager()
u = FilebasedUserManager(g, settings=s)
u.add_user(args['username'], args['password'], groups=[g.admin_group], active=True, overwrite=True)
print(json.dumps({'api_key': u.generate_api_key(args['username'])}))
'''


class OctoPrintEnvironment:
    def __init__(self, root: Path, python: Path, instance_id: str):
        self.root = Path(root).absolute()
        self.python = Path(python).absolute()
        self.instance_id = instance_id
        self.process = None
        self.camera = None
        self._lock_file = None
        self._log_file = None

    def verify_runtime(self):
        try:
            result = subprocess.run(
                [str(self.python), '-c', "import importlib.metadata; from packaging.version import Version; print(Version(importlib.metadata.version('OctoPrint')) == Version(" + repr(OCTOPRINT_VERSION) + "))"],
                check=True, capture_output=True, text=True, timeout=15,
            )
            if result.stdout.strip() != 'True':
                raise EnvironmentError('Installed OctoPrint version does not match the recipe')
        except (OSError, subprocess.SubprocessError):
            raise EnvironmentError('Pinned OctoPrint interpreter is unavailable') from None

    def _private_json(self, path, data):
        with open(path, 'w', encoding='utf-8') as stream:
            os.chmod(path, 0o600)
            json.dump(data, stream)

    def prepare(self, config, *, audit_port: int, audit_token: str) -> dict:
        self.verify_runtime()
        profile = load_profile(config['printer_profile'])
        marker = {'instance_id': self.instance_id, 'platform': 'octoprint'}
        fresh = False
        try:
            if self.root.is_symlink():
                raise EnvironmentError('Instance directory must not be a symlink')
            if self.root.exists():
                if json.loads((self.root / 'ownership.json').read_text()) != marker:
                    raise EnvironmentError('Existing instance directory is not owned by this framework')
            else:
                self.root.mkdir(parents=True, mode=0o700)
                fresh = True
                self._private_json(self.root / 'ownership.json', marker)
            if self._lock_file is None:
                self._lock_file = open(self.root / 'instance.lock', 'a')
                try:
                    fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._lock_file.close()
                    self._lock_file = None
                    raise EnvironmentError('Instance directory is already in use') from None
            credentials_path = self.root / 'credentials.json'
            credentials = {'username': 'admin', 'password': 'admin'}
            flags = config.get('instrumentation', {})
            settings = {
                'server': {'firstRun': False, 'onlineCheck': {'enabled': False},
                           'pluginBlocklist': {'enabled': False}, 'commands': {}},
                'plugins': {
                    '_disabled': ['discovery', 'softwareupdate'],
                    'tracking': {'enabled': False},
                    'virtual_printer': {'enabled': True, 'hasBed': profile['heated_bed'],
                                        'numExtruders': 1},
                    'honeyprint_audit': {'port': audit_port, 'token': audit_token,
                                        'instance_id': self.instance_id,
                                        'http': flags.get('http', True),
                                        'gcode': flags.get('gcode', True),
                                        'state': flags.get('state', True)},
                    'discovery': {'publicHost': '127.0.0.1', 'upnp': {'enabled': False}},
                },
                'serial': {'port': 'VIRTUAL', 'autoconnect': False},
                'printerProfiles': {'default': profile['id']},
            }
            source_dir = os.environ.get('_HONEYPRINT_OCTOPRINT_SOURCE_DIR')
            if source_dir and (Path(source_dir) / 'tools/simulated_webcam.py').is_file():
                self.camera = SourceCamera(self.root, self.python, source_dir)
                settings['plugins']['classicwebcam'] = self.camera.settings()
            for folder in ('plugins', 'printerProfiles'):
                (self.root / folder).mkdir(exist_ok=True)
            config_path = self.root / 'config.yaml'
            config_path.write_text(yaml.safe_dump(settings), encoding='utf-8')
            os.chmod(config_path, 0o600)
            (self.root / 'printerProfiles' / (profile['id'] + '.profile')).write_text(
                yaml.safe_dump(octoprint_profile(profile)), encoding='utf-8',
            )
            source = Path(__file__).resolve().parents[1] / 'adapters/octoprint/audit_plugin.py'
            shutil.copyfile(source, self.root / 'plugins/honeyprint_audit.py')
            result = subprocess.run([str(self.python), '-c', _BOOTSTRAP],
                                    input=json.dumps({'root': str(self.root), 'username': credentials['username'], 'password': credentials['password']}),
                                    text=True, capture_output=True, check=True, timeout=30)
            credentials['api_key'] = json.loads(result.stdout.strip().splitlines()[-1])['api_key']
            self._private_json(credentials_path, credentials)
            return credentials
        except Exception:
            self.cleanup()
            if fresh:
                shutil.rmtree(self.root)
            raise EnvironmentError('Owned OctoPrint environment preparation failed') from None

    def launch(self, port: int):
        if self._lock_file is None or self.process is not None:
            raise EnvironmentError('Environment must be prepared before launch')
        self._log_file = open(self.root / 'server-console.log', 'ab')
        os.chmod(self.root / 'server-console.log', 0o600)
        try:
            if self.camera is not None:
                self.camera.start()
            self.process = subprocess.Popen(
                [str(self.python), '-m', 'octoprint', '--basedir', str(self.root),
                 'serve', '--host', '127.0.0.1', '--port', str(port)],
                stdin=subprocess.DEVNULL, stdout=self._log_file, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            self.cleanup()
            raise EnvironmentError('Owned OctoPrint launch failed') from None
        return self.process

    def cleanup(self):
        """Terminate only the process group launched and retained by this object."""
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                self.process.wait(timeout=5)
        self.process = None
        if self.camera is not None:
            self.camera.stop()
            self.camera = None
        if self._log_file:
            self._log_file.close()
            self._log_file = None
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None

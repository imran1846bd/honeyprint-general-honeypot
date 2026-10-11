"""Owned, isolated upstream Klipper containers; no host devices or socket mounts."""
import json
import subprocess
import uuid
from dataclasses import dataclass
from adapters.base_adapter import AdapterError

IMAGE = 'honeyprint-klipper:1'
PINS = {'klipper': 'e2b99a9159f992169c1ba53029dec020e850cc99',
        'moonraker': '9e676eba6b02661a4dfa3ec6e7ac3f3504498e6d',
        'simulavr': '32985f745c237bf8dcd2718235d01c8b1fb0491d'}


def docker(*args, timeout=30):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                timeout=timeout, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise AdapterError('Docker operation failed; check the engine and Klipper image') from None


@dataclass
class KlipperEnvironment:
    token: str
    image: str
    name: str
    network: str
    created_network: bool = False
    created_container: bool = False

    @classmethod
    def prepare(cls):
        info = json.loads(docker('image', 'inspect', IMAGE))[0]
        labels = info['Config'].get('Labels') or {}
        if any(labels.get('org.honeyprint.' + key + '.revision') != value
               for key, value in PINS.items()):
            raise AdapterError('Klipper image source revisions do not match the recipe')
        if info['Config']['User'] != '10001:10001':
            raise AdapterError('Klipper image must use the unprivileged runtime account')
        token = uuid.uuid4().hex
        name = 'honeyprint-klipper-' + token
        return cls(token, info['Id'], name, name + '-network')

    def start(self, profile):
        try:
            docker('network', 'create', '--internal', '--label',
                   'org.honeyprint.owner=' + self.token, self.network)
            self.created_network = True
            docker('run', '--detach', '--name', self.name, '--network', self.network,
                   '--label', 'org.honeyprint.owner=' + self.token,
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--pids-limit=128', '--memory=1g', '--cpus=2',
                   '--log-opt=max-size=10m', '--log-opt=max-file=3',
                   '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700',
                   '--tmpfs=/data:rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=0700',
                   '--env', 'HONEYPRINT_PROFILE_JSON=' + json.dumps(profile), self.image)
            self.created_container = True
        except AdapterError:
            self.stop()
            raise

    def running(self):
        if not self.created_container:
            return False
        data = json.loads(docker('container', 'inspect', self.name))[0]
        return (data['Config']['Labels'].get('org.honeyprint.owner') == self.token
                and data['State']['Running'])

    def request(self, path):
        """Read native Moonraker through Docker exec without giving it host networking."""
        if path not in ('/server/info', '/printer/objects/query?toolhead&extruder&heater_bed&mcu',
                        '/server/gcode_store?count=100'):
            raise AdapterError('Unsupported observation endpoint')
        code = ('import json,urllib.request; '
                'r=urllib.request.urlopen(' + repr('http://127.0.0.1:7125' + path) + ',timeout=3); '
                'print(r.read(1048577).decode())')
        raw = docker('exec', self.name, '/opt/moonraker-env/bin/python', '-c', code, timeout=8)
        if len(raw) > 1048576:
            raise AdapterError('Moonraker response exceeds the observation limit')
        return json.loads(raw)['result']

    def stop(self):
        for kind, name, flag in (('container', self.name, 'created_container'),
                                 ('network', self.network, 'created_network')):
            if not getattr(self, flag):
                continue
            data = json.loads(docker(kind, 'inspect', name))[0]
            labels = data['Config'].get('Labels', {}) if kind == 'container' else data.get('Labels', {})
            if labels.get('org.honeyprint.owner') != self.token:
                raise AdapterError('Refusing to remove a resource with different ownership')
            docker(kind, 'rm', *(['--force'] if kind == 'container' else []), name)
            setattr(self, flag, False)

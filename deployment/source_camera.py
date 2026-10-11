"""Own the optional demo camera supplied with a trusted OctoPrint source tree."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request


class SourceCamera:
    def __init__(self, root, python, source):
        self.root = Path(root)
        self.python = Path(python)
        self.script = Path(source) / 'tools/simulated_webcam.py'
        if not self.script.is_file():
            raise ValueError('The selected source has no demo camera helper')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.process = None
        self.log = None

    def settings(self):
        base = f'http://127.0.0.1:{self.port}'
        return {'stream': base + '/stream', 'snapshot': base + '/snapshot', 'streamRatio': '16:9'}

    def start(self):
        self.log = (self.root / 'camera-console.log').open('ab')
        os.chmod(self.root / 'camera-console.log', 0o600)
        try:
            self.process = subprocess.Popen(
                [str(self.python), str(self.script), '--port', str(self.port)],
                cwd=self.script.parent, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                start_new_session=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('The source demo camera stopped during startup')
                try:
                    with urllib.request.urlopen(self.settings()['snapshot'], timeout=1) as response:
                        frame = response.read(1048577)
                        if response.status == 200 and 2 < len(frame) <= 1048576 and frame.startswith(b'\xff\xd8'):
                            return
                except OSError:
                    pass
                time.sleep(0.1)
            raise RuntimeError('The source demo camera did not serve a verified JPEG')
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                self.process.wait(timeout=5)
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

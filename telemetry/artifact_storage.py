"""Private content-addressed G-code evidence; never import or execute artifacts."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile


class ArtifactStorage:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink(): raise ValueError('Artifact directory cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir(): raise ValueError('Artifact root must be a directory')

    def store(self, content):
        if type(content) is not bytes or not 1 <= len(content) <= 1048576:
            raise ValueError('Artifact must contain at most one MiB of bytes')
        digest = hashlib.sha256(content).hexdigest()
        destination = self.root / (digest + '.gcode')
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(name, destination)
            except FileExistsError:
                if self.read(digest) != content: raise ValueError('Artifact content conflict')
            directory = os.open(self.root, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
        return {'sha256': digest, 'size_bytes': len(content), 'artifact': destination.name}

    def read(self, digest):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid artifact identity')
        path = self.root / (digest + '.gcode')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('Artifact must be a regular file')
            content = stream.read(1048577)
        if len(content) > 1048576 or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('Artifact integrity check failed')
        return content

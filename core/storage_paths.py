"""Shared retained artifact paths and non-destructive disk-space warnings."""
from pathlib import Path
import shutil
import sys


def artifact_root(runtime_dir, settings):
    value=settings['artifact_dir']
    return Path(runtime_dir)/'artifacts' if value=='auto' else Path(value).expanduser().absolute()


class DiskSpaceMonitor:
    def __init__(self, paths, threshold):
        self.paths=tuple(dict.fromkeys(Path(p) for p in paths))
        self.threshold=threshold
        self.warned=set()

    def check(self):
        if not self.threshold: return
        for target in self.paths:
            existing=target
            while not existing.exists() and existing != existing.parent: existing=existing.parent
            free=shutil.disk_usage(existing).free
            if free < self.threshold:
                if target not in self.warned:
                    print(f'HoneyPrint storage warning: {target} has {free} free bytes; threshold {self.threshold}.',file=sys.stderr,flush=True)
                    self.warned.add(target)
            else: self.warned.discard(target)

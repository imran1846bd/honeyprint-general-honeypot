"""Integrity-checked, bounded installation of the upstream Mainsail web client."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from adapters.base_adapter import AdapterError

VERSION='2.19.0'
SHA256='c4d9d96f89851c6ae0709c2f20725447f3fe8c57cf193869b0083441bd93378a'
URL=f'https://github.com/mainsail-crew/mainsail/releases/download/v{VERSION}/mainsail.zip'
ROOT=Path(__file__).resolve().parents[1]/'.honeyprint/mainsail'/VERSION


def installed():
    """Verify every retained upstream file before serving the cached release."""
    try:
        if ROOT.is_symlink(): raise ValueError()
        manifest=json.loads((ROOT/'.honeyprint-manifest.json').read_text())
        if manifest['archive_sha256']!=SHA256 or len(manifest['files'])>1000: raise ValueError()
        for name,digest in manifest['files'].items():
            path=ROOT/name
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()): raise ValueError()
            if path.stat().st_size>16777216 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest: raise ValueError()
        if 'index.html' not in manifest['files']: raise ValueError()
        return ROOT
    except (OSError,ValueError,KeyError,TypeError):
        raise AdapterError('Pinned Mainsail assets are absent or failed integrity verification') from None


def prepare():
    if ROOT.exists():
        return installed()
    ROOT.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        if shutil.which('curl'):
            archive=subprocess.run(['curl','--fail','--silent','--show-error','--location',
                                    '--max-time','40','--max-filesize','4194304',URL],
                                   capture_output=True,check=True,timeout=45).stdout
        else:
            with urllib.request.urlopen(URL,timeout=30) as response:
                archive=response.read(4194305)
    except (OSError,subprocess.SubprocessError):
        raise AdapterError('Verified Mainsail download failed; check HTTPS connectivity') from None
    if len(archive)>4194304 or hashlib.sha256(archive).hexdigest()!=SHA256:
        raise AdapterError('Mainsail release archive integrity check failed')
    temporary=Path(tempfile.mkdtemp(prefix='.install-',dir=ROOT.parent))
    try:
        manifest={}
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            members=package.infolist()
            if len(members)>1000 or sum(m.file_size for m in members)>33554432:
                raise AdapterError('Mainsail archive exceeds installation limits')
            for member in members:
                path=PurePosixPath(member.filename)
                if path.is_absolute() or '..' in path.parts or stat.S_ISLNK(member.external_attr>>16):
                    raise AdapterError('Unsafe Mainsail archive entry')
                if member.is_dir(): continue
                if member.file_size>16777216 or member.filename in manifest: raise AdapterError('Invalid archive entry')
                output=temporary/member.filename
                output.parent.mkdir(parents=True,exist_ok=True)
                content=package.read(member)
                output.write_bytes(content)
                manifest[member.filename]=hashlib.sha256(content).hexdigest()
        (temporary/'.honeyprint-manifest.json').write_text(json.dumps({'archive_sha256':SHA256,'files':manifest}))
        temporary.rename(ROOT)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return installed()

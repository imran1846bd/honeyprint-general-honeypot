"""Artifact identity, retention bounds and tampering checks."""
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from telemetry.artifact_storage import ArtifactStorage


class ArtifactTests(unittest.TestCase):
    def test_private_deduplicated_persistence(self):
        with TemporaryDirectory() as tmp:
            store=ArtifactStorage(Path(tmp)/'evidence')
            result=store.store(b'G28')
            self.assertEqual(store.store(b'G28'),result)
            self.assertEqual(ArtifactStorage(store.root).read(result['sha256']),b'G28')
            self.assertEqual(len(list(store.root.iterdir())),1)
            self.assertEqual(stat.S_IMODE((store.root/result['artifact']).stat().st_mode),0o600)

    def test_quota_and_integrity(self):
        with TemporaryDirectory() as tmp:
            store=ArtifactStorage(tmp,max_bytes=3,max_artifacts=1)
            result=store.store(b'G28')
            self.assertEqual(store.store(b'G28'),result)
            with self.assertRaises(ValueError): store.store(b'M105')
            (store.root/result['artifact']).write_bytes(b'G29')
            with self.assertRaises(ValueError): store.read(result['sha256'])
            with self.assertRaises(ValueError): store.store(b'G28')

    def test_symlink_and_invalid_inputs(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'link').symlink_to(root,target_is_directory=True)
            with self.assertRaises(ValueError): ArtifactStorage(root/'link')
            store=ArtifactStorage(root/'evidence')
            for content in (b'',b'A'*1048577,'G28'):
                with self.assertRaises(ValueError): store.store(content)
            for digest in ('../config','F'*64,'0'*63):
                with self.assertRaises(ValueError): store.read(digest)
            result=store.store(b'G28'); destination=store.root/result['artifact']
            destination.unlink(); destination.symlink_to(root/'outside')
            with self.assertRaises(OSError): store.read(result['sha256'])


if __name__=='__main__': unittest.main()

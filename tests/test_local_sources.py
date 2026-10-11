"""Source identity, immutable original trees and isolated runtime selection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from adapters.base_adapter import AdapterError
from deployment.env_config import build_config
from deployment.local_sources import snapshot, prepare_sources

ROOT = Path(__file__).resolve().parents[1]


class LocalSourceTests(unittest.TestCase):
    def source(self, root):
        source = root / 'developer'; source.mkdir()
        for name in ['Makefile', 'klippy/klippy.py', 'scripts/klippy-requirements.txt']:
            path = source / name; path.parent.mkdir(exist_ok=True); path.write_text('# original\n')
        return source

    def test_snapshot_tracks_changes_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root)
            (source / 'out').mkdir(); (source / 'out/cache').write_text('ignored')
            for name in ('venv', '.push-work'):
                (source / name).mkdir()
                (source / name / 'external').symlink_to('/etc/hosts')
            (source / '.DS_Store').write_bytes(b'local metadata')
            (source / 'activity.sqlite3').write_bytes(b'private events')
            with patch('deployment.local_sources.CACHE', root / 'cache'):
                copy, first = snapshot(source, 'klipper')
                self.assertEqual((copy / 'Makefile').read_text(), '# original\n')
                self.assertFalse((copy / 'out').exists())
                for name in ('venv', '.push-work', '.DS_Store', 'activity.sqlite3'):
                    self.assertFalse((copy / name).exists())
                (source / 'activity.sqlite3').write_bytes(b'new private events')
                (source / 'out/cache').write_text('different')
                self.assertEqual(snapshot(source, 'klipper')[1], first)
                (source / 'Makefile').write_text('# changed\n')
                self.assertNotEqual(snapshot(source, 'klipper')[1], first)
                self.assertEqual((copy / 'Makefile').read_text(), '# original\n')
                self.assertFalse((source / '.git').exists())

    def test_invalid_sources_and_links_fail_without_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.source(root)
            with patch('deployment.local_sources.CACHE', root / 'cache'):
                with self.assertRaises(AdapterError): snapshot(source, 'octoprint')
                (source / 'external').symlink_to('/etc/hosts')
                with self.assertRaises(AdapterError): snapshot(source, 'klipper')
                with self.assertRaises(AdapterError): prepare_sources({'custom'}, source, None)
                self.assertFalse((root / 'cache').exists())
                with self.assertRaises(AdapterError): prepare_sources({'octoprint'}, source, None, Path(sys.executable))

    def test_env_paths_and_child_metadata_select_local_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = build_config({'HONEYPRINT_PLATFORM': 'all', 'HONEYPRINT_OCTOPRINT_SOURCE': 'plat/OctoPrint',
                                   'HONEYPRINT_KLIPPER_SOURCE': 'plat/klipper'}, root, root / 'generated')
            self.assertEqual(result['octoprint_source'], root / 'plat/OctoPrint')
            self.assertEqual(result['klipper_source'], root / 'plat/klipper')
            single = build_config({'HONEYPRINT_PLATFORM': 'custom',
                                   'HONEYPRINT_OCTOPRINT_SOURCE': 'missing/octoprint',
                                   'HONEYPRINT_KLIPPER_SOURCE': 'missing/klipper'}, root, root / 'custom')
            self.assertIsNone(single['octoprint_source'])
            self.assertIsNone(single['klipper_source'])
            environment = dict(os.environ, _HONEYPRINT_OCTOPRINT_VERSION='2.1.0.dev+local',
                               _HONEYPRINT_KLIPPER_REV='a' * 40)
            code = ('import json; from recipes.octoprint import managed_recipe; '
                    'from deployment.klipper_environment import PINS; '
                    'print(json.dumps([managed_recipe()["versions"]["octoprint"],PINS["klipper"]]))')
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), ['2.1.0.dev+local', 'a' * 40])

    def test_native_git_abbreviations_must_match_selected_revision(self):
        from adapters.klipper.adapter import revision_matches
        revision = '1ebb8c7' + 'a' * 33
        self.assertTrue(revision_matches('1ebb8c7', revision))
        self.assertTrue(revision_matches('v0.13.0-10-g1ebb8c7a-dirty', revision))
        self.assertFalse(revision_matches('deadbee', revision))
        self.assertFalse(revision_matches('1ebb8c', revision))

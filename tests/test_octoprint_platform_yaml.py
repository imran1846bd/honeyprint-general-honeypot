"""Verify OctoPrint platform YAML reaches the managed native configuration."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import yaml

from core.builtin_registry import create_builtin_registry
from core.dependency_resolver import freeze
from core.validator import validate_config
from deployment.env_config import build_config
from deployment.environment_manager import OctoPrintEnvironment


class OctoPrintPlatformYamlTests(unittest.TestCase):
    def test_selected_settings_reach_native_config(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'configs/platforms'
            folder.mkdir(parents=True)
            (folder / 'octoprint.yaml').write_text(yaml.safe_dump({
                'schema_version': 1, 'source': '', 'python': '',
                'settings': {'appearance': {'color': 'blue'},
                             'virtual_printer': {'firmwareName': 'Research Printer', 'rxBuffer': 128}}
            }))
            generated = build_config({'HONEYPRINT_PLATFORM': 'octoprint'}, base, base / 'generated')
            config = generated['config']
            validate_config(config, create_builtin_registry(runtime_dir=base, octoprint_python=Path('/unused')))
            instance = config['instances'][0]
            native = OctoPrintEnvironment(base / 'native', Path('/unused'), instance['id'])
            try:
                with patch.object(native, 'verify_runtime'), patch(
                    'deployment.environment_manager.subprocess.run',
                    return_value=SimpleNamespace(stdout='{"api_key":"fixture"}')
                ):
                    native.prepare(freeze(instance), audit_port=12345, audit_token='fixture')
                settings = yaml.safe_load((base / 'native/config.yaml').read_text())
                self.assertEqual(settings['appearance']['color'], 'blue')
                self.assertEqual(settings['plugins']['virtual_printer']['firmwareName'], 'Research Printer')
                self.assertEqual(settings['plugins']['virtual_printer']['rxBuffer'], 128)
                self.assertTrue(settings['plugins']['virtual_printer']['enabled'])
            finally:
                native.cleanup()

    def test_rejects_unknown_options_and_other_platform_ignores_octoprint_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'configs/platforms'
            folder.mkdir(parents=True)
            (folder / 'octoprint.yaml').write_text('schema_version: 1\nsettings:\n  virtual_printer:\n    hasBed: false\n')
            with self.assertRaisesRegex(ValueError, 'unknown option'):
                build_config({'HONEYPRINT_PLATFORM': 'octoprint'}, base, base / 'octo')
            custom = build_config({'HONEYPRINT_PLATFORM': 'custom'}, base, base / 'custom')
            self.assertNotIn('octoprint_settings', custom['config']['instances'][0])

"""Platform scoping, validation and native configuration integration."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import yaml
from deployment.env_config import build_config
from deployment.octoprint_settings import resolve_octoprint_settings
from deployment.environment_manager import OctoPrintEnvironment


class OctoPrintPlatformSettingsTests(unittest.TestCase):
    def test_rejects_invalid_or_common_overrides(self):
        for value in [[], {'appearance': {'name': 'wrong place'}},
                      {'virtual_printer': {'enabled': False}},
                      {'virtual_printer': {'rxBuffer': True}},
                      {'virtual_printer': {'throttle': float('nan')}},
                      {'virtual_printer': {'waitInterval': 0}},
                      {'virtual_printer': {'sendWait': 'true'}},
                      {'virtual_printer': {'firmwareName': 'bad\nline'}},
                      {'appearance': {'color': 'unknown'}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_octoprint_settings(value)

    def test_yaml_reaches_native_settings_and_other_platform_ignores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folder = base / 'configs/platforms'
            folder.mkdir(parents=True)
            specific = {'appearance': {'color': 'blue', 'showFahrenheitAlso': True},
                        'virtual_printer': {'firmwareName': 'Research Firmware', 'rxBuffer': 128, 'sendBusy': True}}
            octo = folder / 'octoprint.yaml'
            octo.write_text(yaml.safe_dump({'schema_version': 1, 'settings': specific}))
            result = build_config({'HONEYPRINT_PLATFORM': 'octoprint', 'HONEYPRINT_PRINTER_NAME': 'Common Name'}, base, base/'generated')
            cfg = result['config']['instances'][0]
            env = OctoPrintEnvironment(base/'instance', Path('/unused/python'), cfg['id'])
            try:
                with patch.object(env, 'verify_runtime'), patch('deployment.environment_manager.subprocess.run', return_value=SimpleNamespace(stdout='{"api_key":"fixture"}')):
                    from core.dependency_resolver import freeze
                    env.prepare(freeze(cfg), audit_port=12345, audit_token='fixture', common_settings={'camera_enabled': False})
                native = yaml.safe_load((base/'instance/config.yaml').read_text())
                self.assertEqual(native['appearance'], {'name': 'Common Name', 'color': 'blue', 'showFahrenheitAlso': True})
                self.assertEqual(native['plugins']['virtual_printer']['firmwareName'], 'Research Firmware')
                self.assertEqual(native['plugins']['virtual_printer']['rxBuffer'], 128)
                self.assertTrue(native['plugins']['virtual_printer']['sendBusy'])
                self.assertTrue(native['plugins']['virtual_printer']['enabled'])
                self.assertIn('honeyprint_audit', native['plugins'])
            finally:
                env.cleanup()
            octo.write_text('invalid: [')
            (folder/'custom.yaml').write_text('schema_version: 1\n')
            custom = build_config({'HONEYPRINT_PLATFORM': 'custom'}, base, base/'custom')
            self.assertNotIn('octoprint_settings', custom['config']['instances'][0])

    def test_defaults_do_not_leak_mutations(self):
        one = resolve_octoprint_settings()
        one['virtual_printer']['rxBuffer'] = 1024
        self.assertEqual(resolve_octoprint_settings()['virtual_printer']['rxBuffer'], 64)

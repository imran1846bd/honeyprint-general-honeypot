"""Platform YAML controls the generated Klipper MCU configuration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml
from core.builtin_registry import create_builtin_registry
from core.profile_loader import load_profile
from core.validator import validate_config
from deployment.docker.klipper.entrypoint import printer_config
from deployment.docker.klipper.settings import resolve_klipper_settings
from deployment.env_config import build_config
from deployment.klipper_environment import KlipperEnvironment
from adapters.base_adapter import AdapterError

ROOT = Path(__file__).resolve().parents[1]


class KlipperPlatformSettingsTests(unittest.TestCase):
    def test_selected_yaml_generates_native_motion_stepper_and_heater_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'configs/platforms'
            folder.mkdir(parents=True)
            (folder / 'klipper.yaml').write_text(yaml.safe_dump({
                'schema_version': 1, 'source': '',
                'settings': {'motion': {'max_velocity': 240, 'max_z_velocity': 25},
                             'steppers': {'z': {'rotation_distance': 10, 'microsteps': 32}},
                             'extruder': {'filament_diameter': 2.85, 'min_extrude_temp': 175},
                             'heated_bed': {'max_temp': 120}},
            }))
            result = build_config({'HONEYPRINT_PLATFORM': 'klipper'}, base, base/'generated')
            config = result['config']
            validate_config(config, create_builtin_registry(runtime_dir=base))
            selected = config['instances'][0]['klipper_settings']
            native = printer_config(load_profile(config['instances'][0]['printer_profile']), selected)
            for fragment in ('max_velocity: 240', 'max_z_velocity: 25',
                             'rotation_distance: 10', 'microsteps: 32',
                             'filament_diameter: 2.85', 'min_extrude_temp: 175',
                             'max_temp: 120'):
                self.assertIn(fragment, native)
            self.assertEqual(result['klipper_source'], None)

    def test_invalid_values_fail_before_start_and_other_platform_skips_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'configs/platforms'
            folder.mkdir(parents=True)
            file = folder/'klipper.yaml'
            for settings in ({'motion': {'max_velocity': 'fast'}},
                             {'motion': {'max_z_velocity': 400}},
                             {'steppers': {'x': {'microsteps': 3}}},
                             {'extruder': {'filament_diameter': 0.5}},
                             {'extruder': {'min_extrude_temp': 290}},
                             {'heated_bed': {'max_temp': 180}},
                             {'motion': {'unknown': 1}}):
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    resolve_klipper_settings(settings)
            file.write_text('schema_version: 1\nsettings:\n  motion:\n    bad: 1\n')
            with self.assertRaises(ValueError):
                build_config({'HONEYPRINT_PLATFORM': 'klipper'}, base, base/'failed')
            (folder/'custom.yaml').write_text('schema_version: 1\n')
            selected = build_config({'HONEYPRINT_PLATFORM': 'custom'}, base, base/'custom')
            self.assertNotIn('klipper_settings', selected['config']['instances'][0])

    def test_container_receives_validated_options_and_rebuilds_old_image(self):
        image = {'Config': {'Labels': {'org.honeyprint.klipper.revision': 'r',
                                      'org.honeyprint.moonraker.revision': 'm',
                                      'org.honeyprint.simulavr.revision': 's'},
                            'User': '10001:10001'}, 'Id': 'image-id'}
        from deployment.klipper_environment import PINS
        for key, value in PINS.items():
            image['Config']['Labels'][f'org.honeyprint.{key}.revision'] = value
        with patch('deployment.klipper_environment.docker', return_value=json.dumps([image])):
            with self.assertRaisesRegex(AdapterError, 'configuration schema'):
                KlipperEnvironment.prepare()
        image['Config']['Labels']['org.honeyprint.klipper.config.schema'] = '2'
        calls = []
        def docker(*args, **kwargs):
            calls.append(args)
            return json.dumps([image]) if args[:2] == ('image', 'inspect') else ''
        with patch('deployment.klipper_environment.docker', side_effect=docker):
            environment = KlipperEnvironment.prepare()
            environment.start({'id': 'virtual_lab'}, {'motion': {'max_velocity': 230}})
        run = next(args for args in calls if args[0] == 'run')
        payload = next(item for item in run if item.startswith('HONEYPRINT_KLIPPER_SETTINGS_JSON='))
        self.assertEqual(json.loads(payload.split('=', 1)[1])['motion']['max_velocity'], 230)
        self.assertTrue(environment.created_container)

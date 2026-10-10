"""Profile schema checks against invalid geometry and platform mapping."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import yaml

from core.profile_loader import load_profile, octoprint_profile, ProfileError


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'profile.yaml'
        self.profile = {'schema_version': 1, 'id': 'lab_01', 'name': 'Lab',
                        'volume': {'width': 220, 'depth': 220, 'height': 250},
                        'heated_bed': True, 'nozzle_diameter': 0.4}

    def load(self, profile):
        self.path.write_text(yaml.safe_dump(profile)); return load_profile(self.path)

    def test_complete_octoprint_mapping(self):
        profile = octoprint_profile(self.load(self.profile))
        self.assertEqual(profile['volume']['width'], 220)
        self.assertEqual(profile['volume']['custom_box'], False)
        self.assertEqual(set(profile['axes']), {'x','y','z','e'})
        self.assertEqual(profile['extruder']['count'], 1)
        self.assertEqual(profile['heatedChamber'], False)

    def test_invalid_dimensions_are_rejected(self):
        for value in [True, 0, -1, 10001, 10**1000, float('nan'), float('inf'), '220']:
            data = deepcopy(self.profile); data['volume']['width'] = value
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProfileError): self.load(data)

    def test_profile_schema_is_strict(self):
        for change in [{'schema_version': True}, {'id': '../escape'},
                       {'heated_bed': 1}, {'nozzle_diameter': 0},
                       {'unsupported': True}, {'volume': {'width': 10}}]:
            data = deepcopy(self.profile); data.update(change)
            with self.subTest(field=list(change)):
                with self.assertRaises(ProfileError): self.load(data)

    def test_missing_and_oversized_files_fail(self):
        with self.assertRaises(ProfileError): load_profile(self.path)
        self.path.write_text('a'*65537)
        with self.assertRaises(ProfileError): load_profile(self.path)


if __name__ == '__main__': unittest.main()

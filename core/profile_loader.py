"""Load a bounded, platform-neutral single-extruder printer profile."""

from math import isfinite
from pathlib import Path
import re

from core.config_loader import load_config, ConfigLoadError


class ProfileError(ValueError):
    """Printer profile is missing or unsupported."""


def load_profile(path: str | Path) -> dict:
    """Validate geometry and nozzle metadata; this does not assert physics fidelity.

    Version 1 supports rectangular lower-left-origin, single-extruder profiles.
    Explicitly reject unsupported fields rather than silently ignoring them.
    """
    try:
        if Path(path).stat().st_size > 65536:
            raise ProfileError('Printer profile exceeds the size limit')
        data = load_config(path)
    except (OSError, ConfigLoadError):
        raise ProfileError('Printer profile could not be loaded') from None
    expected = {'schema_version', 'id', 'name', 'volume', 'heated_bed', 'nozzle_diameter'}
    if set(data) != expected or type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ProfileError('Printer profile must match schema version 1')
    if not isinstance(data['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', data['id']):
        raise ProfileError('Profile ID must be a lowercase identifier up to 64 characters')
    if not isinstance(data['name'], str) or not data['name'].strip() or len(data['name']) > 128:
        raise ProfileError('Profile name must contain 1 to 128 characters')
    if type(data['heated_bed']) is not bool:
        raise ProfileError('heated_bed must be boolean')
    volume = data['volume']
    if not isinstance(volume, dict) or set(volume) != {'width', 'depth', 'height'}:
        raise ProfileError('Volume requires width, depth and height')
    def number(value, maximum):
        if type(value) not in (int, float) or not isfinite(value) or not 0 < value <= maximum:
            raise ProfileError('Profile dimensions must be finite positive supported values')
        return float(value)
    return {
        'schema_version': 1, 'id': data['id'], 'name': data['name'],
        'volume': {key: number(value, 10000) for key, value in volume.items()},
        'heated_bed': data['heated_bed'],
        'nozzle_diameter': number(data['nozzle_diameter'], 10),
    }


def octoprint_profile(profile: dict) -> dict:
    """Translate an already validated HoneyPrint profile to OctoPrint geometry."""
    return {
        'id': profile['id'], 'name': profile['name'], 'model': 'HoneyPrint virtual printer',
        'color': 'default', 'heatedChamber': False,
        'axes': {axis: {'speed': speed, 'inverted': False}
                 for axis, speed in [('x', 6000), ('y', 6000), ('z', 200), ('e', 300)]},
        'volume': {**profile['volume'], 'formFactor': 'rectangular', 'origin': 'lowerleft'},
        'heatedBed': profile['heated_bed'],
        'extruder': {'count': 1, 'nozzleDiameter': profile['nozzle_diameter'],
                     'sharedNozzle': False, 'defaultExtrusionLength': 5, 'offsets': [(0.0, 0.0)]},
    }

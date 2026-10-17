"""Validated native Klipper controls shared by the host and container."""
from collections.abc import Mapping
from copy import deepcopy
import math

DEFAULTS = {
    'motion': {'max_velocity': 200, 'max_accel': 1000,
               'max_z_velocity': 20, 'max_z_accel': 30},
    'steppers': {
        'x': {'microsteps': 16, 'rotation_distance': 40},
        'y': {'microsteps': 16, 'rotation_distance': 40},
        'z': {'microsteps': 16, 'rotation_distance': 8},
    },
    'extruder': {'microsteps': 16, 'rotation_distance': 33.5,
                 'filament_diameter': 1.75, 'min_extrude_temp': 170,
                 'max_temp': 280},
    'heated_bed': {'max_temp': 130},
}
BOUNDS = {
    'max_velocity': (1, 500), 'max_accel': (1, 10000),
    'max_z_velocity': (1, 100), 'max_z_accel': (1, 1000),
    'rotation_distance': (0.1, 100), 'filament_diameter': (1, 3),
    'min_extrude_temp': (0, 280), 'max_temp': (180, 300),
    'bed_max_temp': (50, 150),
}


def resolve_klipper_settings(value=None):
    """Return complete numeric settings; reject unknown keys and unsafe ranges."""
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - set(DEFAULTS):
        raise ValueError('klipper settings: unknown section or expected mapping')
    result = deepcopy(DEFAULTS)
    for section, supplied in value.items():
        if not isinstance(supplied, Mapping) or set(supplied) - set(DEFAULTS[section]):
            raise ValueError(f'klipper.{section}: unknown option or expected mapping')
        if section == 'steppers':
            for axis, axis_values in supplied.items():
                if not isinstance(axis_values, Mapping) or set(axis_values) - set(DEFAULTS['steppers'][axis]):
                    raise ValueError(f'klipper.steppers.{axis}: unknown option or expected mapping')
                result['steppers'][axis].update(axis_values)
        else:
            result[section].update(supplied)
    for section in ('motion', 'steppers', 'extruder', 'heated_bed'):
        groups = [(section, result[section])] if section != 'steppers' else [
            (f'steppers.{axis}', result['steppers'][axis]) for axis in ('x', 'y', 'z')]
        for group, settings in groups:
            for key, number in settings.items():
                if key == 'microsteps':
                    valid = type(number) is int and 1 <= number <= 256 and number & (number - 1) == 0
                else:
                    lo, hi = BOUNDS['bed_max_temp' if group == 'heated_bed' else key]
                    valid = type(number) in (int, float) and math.isfinite(number) and lo <= number <= hi
                if not valid:
                    raise ValueError(f'klipper.{group}.{key}: invalid value')
    motion = result['motion']
    if motion['max_z_velocity'] > motion['max_velocity'] or motion['max_z_accel'] > motion['max_accel']:
        raise ValueError('klipper.motion: Z limits cannot exceed overall limits')
    extruder = result['extruder']
    if extruder['min_extrude_temp'] >= extruder['max_temp']:
        raise ValueError('klipper.extruder: min_extrude_temp must be below max_temp')
    return result

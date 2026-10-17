"""Validated native OctoPrint overrides; common profile and evidence remain separate."""
from copy import deepcopy
from collections.abc import Mapping
import math

DEFAULTS = {
    'appearance': {'color': 'default', 'showFahrenheitAlso': False},
    'virtual_printer': {
        'firmwareName': 'Virtual Marlin 1.0',
        'throttle': 0.01, 'sendWait': False, 'waitInterval': 1.0,
        'rxBuffer': 64, 'commandBuffer': 4,
        'supportM112': True, 'echoOnM117': True,
        'sendBusy': False, 'busyInterval': 2.0,
        'simulateReset': True, 'okBeforeCommandOutput': False,
    },
}
BOUNDS = {'throttle': (0, 1), 'waitInterval': (0.1, 60),
          'rxBuffer': (16, 65536), 'commandBuffer': (1, 1024), 'busyInterval': (0.1, 60)}


def resolve_octoprint_settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - set(DEFAULTS):
        raise ValueError('octoprint settings: unknown section or expected mapping')
    result = deepcopy(DEFAULTS)
    for section, supplied in value.items():
        if not isinstance(supplied, Mapping) or set(supplied) - set(DEFAULTS[section]):
            raise ValueError(f'octoprint.{section}: unknown option or expected mapping')
        for key, item in supplied.items():
            default = DEFAULTS[section][key]
            valid = False
            if type(default) is bool:
                valid = type(item) is bool
            elif key in BOUNDS:
                lo, hi = BOUNDS[key]
                valid = type(item) in (int, float) and math.isfinite(item) and lo <= item <= hi
                if type(default) is int:
                    valid = valid and type(item) is int
            elif key == 'color':
                valid = item in ('default', 'red', 'orange', 'yellow', 'green', 'blue', 'violet') if isinstance(item, str) else False
            elif key == 'firmwareName':
                valid = isinstance(item, str) and 1 <= len(item.strip()) <= 128 and all(ord(c) >= 32 and ord(c) != 127 for c in item)
            if not valid:
                raise ValueError(f'octoprint.{section}.{key}: invalid value')
            result[section][key] = item
    return result

"""Source-pinned real MCU-emulation recipe; builds are explicit deployment work."""
from deployment.klipper_environment import PINS


def managed_recipe():
    return {'platform': 'klipper', 'deployment': 'managed', 'simulators': ['avr_sim'],
            'versions': dict(PINS), 'dependencies': []}

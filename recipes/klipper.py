"""Source-pinned real MCU-emulation recipe; builds are explicit deployment work."""
from deployment.klipper_environment import PINS
from deployment.mainsail import VERSION as MAINSAIL_VERSION


def managed_recipe():
    return {'platform': 'klipper', 'deployment': 'managed', 'simulators': ['avr_sim'],
            'versions': {**PINS, 'mainsail': MAINSAIL_VERSION}, 'dependencies': []}

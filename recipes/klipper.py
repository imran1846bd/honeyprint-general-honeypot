"""Source-pinned real MCU-emulation recipe; builds are explicit deployment work."""
import os
from deployment.klipper_environment import PINS
from deployment.mainsail import VERSION as MAINSAIL_VERSION


def managed_recipe():
    return {'platform': 'klipper', 'deployment': 'managed', 'simulators': ['avr_sim'],
            'versions': {**PINS, 'mainsail': MAINSAIL_VERSION,
                         **({'klipper_source_sha256': os.environ['_HONEYPRINT_KLIPPER_SOURCE_SHA256']}
                            if os.environ.get('_HONEYPRINT_KLIPPER_SOURCE_SHA256') else {})}, 'dependencies': []}

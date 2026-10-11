"""Pinned metadata for the supported local OctoPrint Virtual Printer recipe.

Install the optional locked runtime separately. This metadata factory never
installs software, connects a printer or claims deployment health.
"""

import os

OCTOPRINT_VERSION = os.environ.get('_HONEYPRINT_OCTOPRINT_VERSION', '1.11.8')


def managed_recipe() -> dict:
    return {
        'platform': 'octoprint', 'deployment': 'managed',
        'simulators': ['virtual_printer'],
        'versions': {'octoprint': OCTOPRINT_VERSION,
                     **({'octoprint_source_sha256': os.environ['_HONEYPRINT_OCTOPRINT_SOURCE_SHA256']}
                        if os.environ.get('_HONEYPRINT_OCTOPRINT_SOURCE_SHA256') else {})},
        'dependencies': [],
    }


def attach_recipe() -> dict:
    return {**managed_recipe(), 'deployment': 'attach'}

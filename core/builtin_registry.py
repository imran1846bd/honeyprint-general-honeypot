"""Explicit registrations for the implemented OctoPrint prototype components."""

from pathlib import Path

from adapters.octoprint.adapter import OctoPrintAdapter
from adapters.klipper.adapter import KlipperAdapter
from recipes.klipper import managed_recipe as klipper_recipe
from deployment.klipper_environment import PINS
from adapters.custom.lab_adapter import LabAdapter
from sdk.extensions import register_extension
from core.plugin_registry import PluginRegistry
from recipes.octoprint import managed_recipe, attach_recipe, OCTOPRINT_VERSION
from telemetry.sqlite_storage import SQLiteStorage
from plugins.manufacturing.pipeline import FACTORIES


def create_builtin_registry(*, runtime_dir: Path, octoprint_python: Path | None = None,
                            attach_api_key_env: str | None = None) -> PluginRegistry:
    """Register trusted factories without preparing or launching any runtime.

    The simulator descriptor identifies OctoPrint's bundled Virtual Printer;
    the adapter configures it and health-checks actual communication. Developer
    startup code can register further adapters/providers on this same registry.
    """
    registry = PluginRegistry()
    registry.register("platform_adapter", "klipper", KlipperAdapter)
    registry.register("deployment_recipe", "klipper_managed", klipper_recipe)
    registry.register("simulator", "avr_sim", lambda: {
        "provider": "simulavr", "version": PINS["simulavr"],
    })
    registry.register('platform_adapter', 'octoprint', lambda: OctoPrintAdapter(
        runtime_dir=runtime_dir, python=octoprint_python,
        attach_api_key_env=attach_api_key_env,
    ))
    registry.register('deployment_recipe', 'octoprint_managed', managed_recipe)
    registry.register('deployment_recipe', 'octoprint_attach', attach_recipe)
    registry.register('simulator', 'virtual_printer', lambda: {
        'provider': 'octoprint.virtual_printer', 'version': OCTOPRINT_VERSION,
    })
    registry.register('storage', 'sqlite', SQLiteStorage)
    registry.register('instrumentation', 'honeyprint_audit', lambda: {
        'plugin_file': str(Path(__file__).resolve().parents[1] / 'adapters/octoprint/audit_plugin.py'),
    })
    register_extension(registry, {
        'api_version': 1, 'name': 'custom_lab_example', 'plugins': [
            {'kind': 'platform_adapter', 'id': 'custom_lab', 'factory': lambda: LabAdapter(runtime_dir=runtime_dir)},
            {'kind': 'simulator', 'id': 'lab_sim', 'factory': lambda: {
                'provider': 'custom_lab', 'version': '0.1.0',
            }},
            {'kind': 'deployment_recipe', 'id': 'lab_managed', 'factory': lambda: {
                'platform': 'custom_lab', 'deployment': 'managed',
                'simulators': ['lab_sim'], 'versions': {'custom_lab': '0.1.0'},
                'dependencies': [],
            }},
        ],
    })
    for stage, factory in FACTORIES.items():
        registry.register('manufacturing_stage', stage, factory)
    return registry

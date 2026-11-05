"""Explicit registrations for the implemented OctoPrint prototype components."""

from pathlib import Path

from adapters.octoprint.adapter import OctoPrintAdapter
from adapters.klipper.adapter import KlipperAdapter
from recipes.klipper import managed_recipe as klipper_recipe
from deployment.klipper_environment import PINS
from deployment.mainsail import VERSION as MAINSAIL_VERSION
from adapters.custom.lab_adapter import LabAdapter
from adapters.custom.process_adapter import CustomProcessAdapter
import hashlib
from sdk.extensions import register_extension
from core.plugin_registry import PluginRegistry
from recipes.octoprint import managed_recipe, attach_recipe, OCTOPRINT_VERSION
from telemetry.sqlite_storage import SQLiteStorage
from plugins.manufacturing.pipeline import FACTORIES


def create_builtin_registry(*, runtime_dir: Path, octoprint_python: Path | None = None,
                            attach_api_key_env: str | None = None, custom_code: Path | None = None,
                            custom_python: Path | None = None, custom_health_path: str = '/', custom_api_url: str | None = None, runtime_settings=None) -> PluginRegistry:
    """Register trusted factories without preparing or launching any runtime.

    The simulator descriptor identifies OctoPrint's bundled Virtual Printer;
    the adapter configures it and health-checks actual communication. Developer
    startup code can register further adapters/providers on this same registry.
    """
    from core.runtime_settings import resolve_settings
    settings = resolve_settings(runtime_settings)
    registry = PluginRegistry()
    if custom_code is not None and custom_api_url:
        raise ValueError('Choose custom code or a custom API, not both')
    if custom_api_url:
        from adapters.custom.api_adapter import CustomAPIAdapter, parse_api_url
        parse_api_url(custom_api_url)
        registry.register('platform_adapter', 'custom', lambda: CustomAPIAdapter(
            api_url=custom_api_url, health_path=custom_health_path, runtime_dir=runtime_dir, common_settings=settings))
        registry.register('simulator', 'custom_api', lambda: {'provider': 'external_http', 'version': '1'})
        registry.register('deployment_recipe', 'custom_api_attach', lambda: {
            'platform': 'custom', 'deployment': 'attach', 'simulators': ['custom_api'],
            'versions': {'http_bridge': '1'}, 'dependencies': []})
    if custom_code is not None:
        code = Path(custom_code).absolute()
        if not code.is_file() or code.stat().st_size > 10485760:
            raise ValueError('Custom entry must be an existing bounded Python source file')
        revision = hashlib.sha256(code.read_bytes()).hexdigest()
        registry.register('platform_adapter', 'custom', lambda: CustomProcessAdapter(
            entry=code, python=custom_python, health_path=custom_health_path, runtime_dir=runtime_dir, common_settings=settings))
        registry.register('simulator', 'custom_process', lambda: {'provider': 'developer_http', 'version': revision})
        registry.register('deployment_recipe', 'custom_process_managed', lambda: {
            'platform': 'custom', 'deployment': 'managed', 'simulators': ['custom_process'],
            'versions': {'custom_code': revision}, 'dependencies': []})
    registry.register("platform_adapter", "klipper", lambda: KlipperAdapter(runtime_dir=runtime_dir, artifact_settings=settings))
    registry.register("service", "mainsail", lambda: {"version": MAINSAIL_VERSION, "provider": "mainsail"})
    registry.register("service", "moonraker", lambda: {"version": PINS["moonraker"], "provider": "moonraker"})
    registry.register("deployment_recipe", "klipper_managed", klipper_recipe)
    registry.register("simulator", "avr_sim", lambda: {
        "provider": "simulavr", "version": PINS["simulavr"],
    })
    registry.register('platform_adapter', 'octoprint', lambda: OctoPrintAdapter(
        runtime_dir=runtime_dir, python=octoprint_python,
        attach_api_key_env=attach_api_key_env, common_settings=settings,
    ))
    registry.register('deployment_recipe', 'octoprint_managed', managed_recipe)
    registry.register('deployment_recipe', 'octoprint_attach', attach_recipe)
    registry.register('simulator', 'virtual_printer', lambda: {
        'provider': 'octoprint.virtual_printer', 'version': OCTOPRINT_VERSION,
    })
    registry.register('storage', 'sqlite', lambda path: SQLiteStorage(path, timeout=settings['sqlite_busy_timeout_seconds']))
    registry.register('instrumentation', 'honeyprint_audit', lambda: {
        'plugin_file': str(Path(__file__).resolve().parents[1] / 'adapters/octoprint/audit_plugin.py'),
    })
    register_extension(registry, {
        'api_version': 1, 'name': 'custom_lab_example', 'plugins': [
            {'kind': 'platform_adapter', 'id': 'custom_lab', 'factory': lambda: LabAdapter(runtime_dir=runtime_dir, artifact_settings=settings)},
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

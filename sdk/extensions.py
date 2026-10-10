"""Register an installed developer extension through a versioned Python manifest.

Manifests contain callable objects provided by trusted startup code, not import
paths supplied through YAML or uploaded text. Review/install custom code before
calling this helper; registration cannot sandbox arbitrary Python factories.
"""

from collections.abc import Mapping

from core.plugin_registry import PluginRegistry, PluginRegistryError


SDK_API_VERSION = 1


def register_extension(registry: PluginRegistry, manifest: Mapping) -> None:
    """Preflight every registration before mutating the startup-only registry.

    Schema: {api_version: 1, name: identifier, plugins: [{kind, id, factory}]}.
    Factory contracts are checked by planning and concrete runtime consumers.
    Do not mutate a registry concurrently during application startup.
    """
    if not isinstance(registry, PluginRegistry) or not isinstance(manifest, Mapping):
        raise PluginRegistryError('Expected a registry and extension manifest')
    if set(manifest) != {'api_version', 'name', 'plugins'}:
        raise PluginRegistryError('Extension manifest fields are unsupported')
    if type(manifest['api_version']) is not int or manifest['api_version'] != SDK_API_VERSION:
        raise PluginRegistryError('Extension SDK version is unsupported')
    entries = manifest['plugins']
    if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 1000:
        raise PluginRegistryError('Extension must declare 1 to 1000 plugin entries')
    staged = PluginRegistry()
    registrations = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {'kind', 'id', 'factory'}:
            raise PluginRegistryError('Plugin entries require kind, id and factory')
        staged.register(entry['kind'], entry['id'], entry['factory'])
        registrations.append((entry['kind'], entry['id'], entry['factory']))
    summary = {'api_version': SDK_API_VERSION, 'name': manifest['name'],
               'plugins': tuple((kind, plugin_id) for kind, plugin_id, _ in registrations)}
    factory = lambda: dict(summary)
    staged.register('extension_manifest', manifest['name'], factory)
    registrations.append(('extension_manifest', manifest['name'], factory))
    for kind, plugin_id, _ in registrations:
        if plugin_id in registry.list_plugins(kind):
            raise PluginRegistryError('Extension conflicts with an existing registration')
    for kind, plugin_id, plugin_factory in registrations:
        registry.register(kind, plugin_id, plugin_factory)

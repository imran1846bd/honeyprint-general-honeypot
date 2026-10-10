"""Contract-level planning tests; these do not simulate real platform support."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from adapters.base_adapter import AdapterError, PlatformAdapter
from core.dependency_resolver import DependencyResolutionError, resolve_dependencies
from core.instance_builder import InstanceBuildError, build_instances
from core.plugin_registry import PluginRegistry
from core.validator import ConfigValidationError, validate_adapter_capabilities


class LabAdapter(PlatformAdapter):
    calls = []

    @property
    def platform_id(self):
        return 'research_lab'

    def capabilities(self):
        return {
            'deployment': ['managed', 'attach'], 'simulators': ['lab_sim'],
            'services': ['web_port'], 'stages': ['intake'],
            'recipes': {'managed': 'lab_managed', 'attach': 'lab_attach'},
        }

    def validate(self, cfg, context):
        self.calls.append('validate')
        if cfg['deployment'] != context['recipe']['deployment']:
            raise AdapterError('mode mismatch')

    def prepare(self, plan):
        raise AssertionError('prepare called during planning')

    def launch_or_attach(self, plan):
        raise AssertionError('launch called during planning')

    def health(self, instance):
        raise AssertionError('health called during planning')

    def observe(self, instance, sink):
        raise AssertionError('observe called during planning')

    def stop(self, instance, *, owned_only=True):
        raise AssertionError('stop called during planning')


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'profile.yaml').write_text('model: research\n')
        self.config = {
            'schema_version': 1,
            'instances': [{
                'id': 'lab_01', 'platform': 'research_lab', 'adapter': 'lab_adapter',
                'deployment': 'managed', 'printer_profile': 'profile.yaml',
                'simulation': 'lab_sim', 'services': {'web_port': 8090},
                'stages': ['intake'],
            }],
            'telemetry': {'backend': 'sqlite', 'capture_requests': True,
                          'capture_commands': True, 'capture_state_changes': True},
        }
        self.recipe = {'platform': 'research_lab', 'deployment': 'managed',
                       'simulators': ['lab_sim'], 'versions': {'lab_service': '1.0.0'},
                       'dependencies': [{'kind': 'service', 'id': 'lab_service'}]}
        LabAdapter.calls = []
        self.registry = self.make_registry()

    @staticmethod
    def unused_factory():
        raise AssertionError('dependency factory invoked before lifecycle')

    def make_registry(self, adapter=LabAdapter, include_storage=True):
        r = PluginRegistry()
        r.register('platform_adapter', 'lab_adapter', adapter)
        for kind, name in [('simulator', 'lab_sim'), ('manufacturing_stage', 'intake'),
                           ('service', 'lab_service')]:
            r.register(kind, name, self.unused_factory)
        if include_storage:
            r.register('storage', 'sqlite', self.unused_factory)
        r.register('deployment_recipe', 'lab_managed', lambda: deepcopy(self.recipe))
        r.register('deployment_recipe', 'lab_attach', lambda: {
            **deepcopy(self.recipe), 'deployment': 'attach',
        })
        return r

    def resolve(self, config=None, registry=None):
        return resolve_dependencies(self.config if config is None else config,
                                    self.registry if registry is None else registry,
                                    base_dir=self.root)

    def test_custom_plan_without_launch(self):
        plan = self.resolve()
        self.assertEqual(LabAdapter.calls, ['validate'])
        self.assertFalse(plan.instances[0].ready)
        self.assertEqual(plan.instances[0].config['printer_profile'], str((self.root / 'profile.yaml').resolve()))
        self.assertIn(('storage', 'sqlite'), plan.instances[0].factories)
        self.assertIn(('service', 'lab_service'), plan.instances[0].factories)

    def test_plan_snapshot_is_detached_and_immutable(self):
        plan = self.resolve()
        self.config['instances'][0]['services']['web_port'] = 9000
        self.recipe['versions']['lab_service'] = '2.0'
        self.assertEqual(plan.instances[0].config['services']['web_port'], 8090)
        self.assertEqual(plan.instances[0].recipe['versions']['lab_service'], '1.0.0')
        with self.assertRaises(TypeError):
            plan.instances[0].config['services']['web_port'] = 9001
        self.assertNotIn('printer_profile', repr(plan))

    def test_isolated_instance_state_and_adapters(self):
        other = deepcopy(self.config['instances'][0])
        other.update(id='lab_02', services={'web_port': 8091})
        self.config['instances'].append(other)
        records = build_instances(self.resolve())
        self.assertIsNot(records['lab_01'].plan.adapter, records['lab_02'].plan.adapter)
        records['lab_01'].state['phase'] = 'changed'
        records['lab_01'].resources['something'] = object()
        self.assertEqual(records['lab_02'].state['phase'], 'planned')
        self.assertFalse(records['lab_02'].resources)
        self.assertFalse(records['lab_01'].owns_platform_resources)

    def test_attach_preserves_no_ownership(self):
        self.config['instances'][0]['deployment'] = 'attach'
        instance = build_instances(self.resolve())['lab_01']
        self.assertEqual(instance.deployment, 'attach')
        self.assertFalse(instance.owns_platform_resources)

    def test_empty_plan(self):
        self.config['instances'] = []
        self.assertEqual(build_instances(self.resolve()), {})

    def test_missing_profile(self):
        (self.root / 'profile.yaml').unlink()
        with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_empty_profile(self):
        (self.root / 'profile.yaml').write_text('')
        with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_missing_storage_provider(self):
        with self.assertRaises(DependencyResolutionError):
            self.resolve(registry=self.make_registry(include_storage=False))

    def test_unsupported_capabilities(self):
        for field, value in [('deployment', 'unsupported'), ('simulation', 'other_sim'),
                             ('services', {'ssh_port': 2222}), ('stages', ['inspection'])]:
            with self.subTest(field=field):
                cfg = deepcopy(self.config['instances'][0]); cfg[field] = value
                with self.assertRaises(ConfigValidationError):
                    validate_adapter_capabilities(LabAdapter(), cfg)

    def test_wrong_adapter_contract_and_platform(self):
        cfg = deepcopy(self.config['instances'][0])
        with self.assertRaises(ConfigValidationError): validate_adapter_capabilities(object(), cfg)
        cfg['platform'] = 'other'
        with self.assertRaises(ConfigValidationError): validate_adapter_capabilities(LabAdapter(), cfg)

    def test_missing_required_capability_list(self):
        class MissingCaps(LabAdapter):
            def capabilities(self): return {}
        with self.assertRaises(DependencyResolutionError):
            self.resolve(registry=self.make_registry(adapter=MissingCaps))

    def test_reject_unpinned_versions(self):
        for version in ['latest', 'main', '*', '>=1.0', '', None]:
            with self.subTest(version=version):
                self.recipe['versions']['lab_service'] = version
                with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_recipe_mismatch(self):
        for field, value in [('platform', 'other'), ('deployment', 'attach'), ('simulators', [])]:
            with self.subTest(field=field):
                old = self.recipe[field]; self.recipe[field] = value
                with self.assertRaises(DependencyResolutionError): self.resolve()
                self.recipe[field] = old

    def test_unknown_recipe_dependency(self):
        self.recipe['dependencies'] = [{'kind': 'service', 'id': 'missing'}]
        with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_invalid_recipe_dependency(self):
        self.recipe['dependencies'] = [{'kind': 'service'}]
        with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_adapter_validation_failure(self):
        class Rejecting(LabAdapter):
            def validate(self, cfg, context): raise AdapterError('unsupported runtime')
        with self.assertRaises(DependencyResolutionError):
            self.resolve(registry=self.make_registry(adapter=Rejecting))

    def test_explicit_recipe_override_is_checked(self):
        self.config['instances'][0]['recipe'] = 'lab_attach'
        with self.assertRaises(DependencyResolutionError): self.resolve()

    def test_input_not_mutated(self):
        before = deepcopy(self.config)
        self.resolve()
        self.assertEqual(self.config, before)

    def test_inconsistent_plan_rejected(self):
        plan = self.resolve()
        for broken in [replace(plan, instances=(plan.instances[0], plan.instances[0])),
                       replace(plan, instances=(replace(plan.instances[0], instance_id='other'),))]:
            with self.assertRaises(InstanceBuildError): build_instances(broken)

    def test_singleton_adapter_rejected(self):
        singleton = LabAdapter()
        self.registry = self.make_registry(adapter=lambda: singleton)
        cfg = deepcopy(self.config['instances'][0]); cfg.update(id='lab_02', services={'web_port':8091})
        self.config['instances'].append(cfg)
        with self.assertRaises(InstanceBuildError): build_instances(self.resolve())

    def test_cli_validation_remains_available(self):
        from main import main
        from contextlib import redirect_stdout
        from io import StringIO
        with redirect_stdout(StringIO()):
            self.assertEqual(main(['validate', 'configs/framework.yaml']), 0)


if __name__ == '__main__':
    unittest.main()

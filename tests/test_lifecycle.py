"""Shared lifecycle orchestration checks using a custom adapter contract."""

import unittest
import test_planning as fixtures

from adapters.base_adapter import AdapterError
from core.instance_builder import build_instances
from core.lifecycle_manager import LifecycleManager, LifecycleError


class Handle:
    def __init__(self, owned): self.owned_platform_resources = owned


class RuntimeAdapter(fixtures.LabAdapter):
    def __init__(self):
        self.events = []
        self.ready = True
        self.fail_launch = self.fail_observe = self.fail_stop = False
    def prepare(self, plan): self.events.append('prepare')
    def launch_or_attach(self, plan):
        self.events.append('launch')
        if self.fail_launch: raise AdapterError('secret detail')
        return Handle(plan['config']['deployment'] == 'managed')
    def observe(self, handle, sink):
        self.events.append('observe')
        if self.fail_observe: raise AdapterError('secret detail')
    def health(self, handle): self.events.append('health'); return {'ready': self.ready}
    def stop(self, handle, *, owned_only=True):
        if owned_only is not True: raise AssertionError('external ownership requested')
        self.events.append('stop')
        if self.fail_stop: raise AdapterError('secret detail')
    def cleanup_prepared(self, plan): self.events.append('cleanup_prepared')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def controller(self, mode='managed'):
        self.fixture.config['instances'][0]['deployment'] = mode
        plan = self.fixture.resolve(registry=self.fixture.make_registry(adapter=RuntimeAdapter))
        instance = build_instances(plan)['lab_01']
        return LifecycleManager(instance, base_dir=self.fixture.root, sink=lambda event: None)

    def test_order_health_and_owned_resources(self):
        manager = self.controller(); manager.prepare(); report = manager.start()
        self.assertEqual(manager.instance.plan.adapter.events, ['prepare','launch','observe','health'])
        self.assertTrue(report['ready']); self.assertTrue(manager.instance.owns_platform_resources)
        manager.stop(); self.assertFalse(manager.instance.owns_platform_resources)

    def test_attach_never_claims_platform_ownership(self):
        manager = self.controller('attach'); manager.prepare(); manager.start()
        self.assertFalse(manager.instance.owns_platform_resources)
        manager.stop()

    def test_prepared_instance_can_stop_without_launch(self):
        manager = self.controller(); manager.prepare(); manager.stop()
        self.assertEqual(manager.instance.plan.adapter.events, ['prepare','cleanup_prepared'])
        self.assertEqual(manager.state.snapshot()['phase'], 'stopped')

    def test_failed_launch_uses_preparation_cleanup(self):
        manager = self.controller(); manager.prepare(); manager.instance.plan.adapter.fail_launch = True
        with self.assertRaises(LifecycleError) as caught: manager.start()
        self.assertNotIn('secret', str(caught.exception))
        manager.stop()
        self.assertEqual(manager.instance.plan.adapter.events[-1], 'cleanup_prepared')

    def test_failed_observation_retains_handle_for_stop(self):
        manager = self.controller(); manager.prepare(); manager.instance.plan.adapter.fail_observe = True
        with self.assertRaises(LifecycleError): manager.start()
        self.assertIn('platform_handle', manager.instance.resources)
        manager.stop(); self.assertNotIn('platform_handle', manager.instance.resources)

    def test_cleanup_retry_and_restart(self):
        manager = self.controller(); manager.prepare(); manager.start()
        adapter = manager.instance.plan.adapter; adapter.fail_stop = True
        with self.assertRaises(LifecycleError): manager.stop()
        self.assertEqual(manager.state.snapshot()['phase'], 'failed')
        self.assertIn('platform_handle', manager.instance.resources)
        adapter.fail_stop = False; manager.stop(); manager.stop()
        manager.prepare(); self.assertTrue(manager.start()['ready']); manager.stop()

    def test_invalid_health_fails_without_ready(self):
        manager = self.controller(); manager.prepare(); manager.start()
        manager.instance.plan.adapter.ready = 'yes'
        with self.assertRaises(LifecycleError): manager.check_health()
        self.assertFalse(manager.state.snapshot()['ready']); manager.stop()

    def test_start_before_prepare_is_rejected(self):
        manager = self.controller()
        with self.assertRaises(LifecycleError): manager.start()
        self.assertEqual(manager.instance.plan.adapter.events, [])


if __name__ == '__main__': unittest.main()

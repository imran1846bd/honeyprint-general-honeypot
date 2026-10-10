"""Telemetry contracts, transactional retries and concurrent admission checks."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from core.event_bus import EventBus
from telemetry.collector import Collector, CollectorError
from telemetry.normalizer import normalize_event, validate_event, EventValidationError
from telemetry.sqlite_storage import SQLiteStorage, StorageError


def event(**changes):
    record = normalize_event({'source': 'test', 'event_type': 'test.event', 'action': 'capture',
                              'outcome': 'accepted', 'simulation': True},
                             instance_id='lab_01', platform='custom_lab')
    record.update(changes)
    return record


class TelemetryTests(unittest.TestCase):
    def test_nested_credentials_are_redacted_and_input_is_detached(self):
        metadata = {'Authorization': 'secret', 'nested': {'api-key': 'secret', 'values': [1]}}
        result = validate_event(event(metadata=metadata))
        metadata['nested']['values'][0] = 9
        self.assertEqual(result['metadata']['Authorization'], '[REDACTED]')
        self.assertEqual(result['metadata']['nested']['api-key'], '[REDACTED]')
        self.assertEqual(result['metadata']['nested']['values'], [1])

    def test_identity_cannot_be_supplied_by_observation(self):
        observation = {'source': 'test', 'event_type': 'test.event', 'action': 'capture',
                       'outcome': 'accepted', 'simulation': True, 'instance_id': 'spoofed'}
        with self.assertRaises(EventValidationError):
            normalize_event(observation, instance_id='lab_01', platform='custom_lab')

    def test_schema_rejects_invalid_values(self):
        for change in [{'event_id': 'invalid'}, {'timestamp': '2026-10-10T00:00:00'},
                       {'simulation': 1}, {'metadata': {'v': float('nan')}},
                       {'metadata': {'v': object()}}, {'metadata': {'v': 'a' * 70000}},
                       {'unexpected': True}]:
            with self.subTest(change=list(change)):
                with self.assertRaises(EventValidationError):
                    validate_event(event(**change))

    def test_cyclic_metadata_is_rejected(self):
        cycle = {}; cycle['again'] = cycle
        with self.assertRaises(EventValidationError): validate_event(event(metadata=cycle))

    def test_fifo_capacity_and_readonly_batches(self):
        bus = EventBus(capacity=2)
        first, second = event(metadata={'list': [1]}), event()
        self.assertTrue(bus.publish(first)); self.assertTrue(bus.publish(second))
        self.assertFalse(bus.publish(event()))
        first['metadata']['list'][0] = 99
        batch = bus.poll(limit=1)
        self.assertEqual(batch[0]['metadata']['list'], (1,))
        with self.assertRaises(TypeError): batch[0]['action'] = 'changed'
        self.assertEqual(bus.poll()[0]['event_id'], second['event_id'])
        self.assertEqual(dict(bus.stats()), {'capacity': 2, 'depth': 0,
                                           'accepted': 2, 'dropped': 1, 'dequeued': 2})

    def test_concurrent_publish_never_exceeds_capacity(self):
        bus = EventBus(capacity=20); record = event()
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: bus.publish(record), range(100)))
        self.assertEqual(sum(result), 20)
        self.assertEqual(bus.stats()['dropped'], 80)
        self.assertEqual(len(bus.poll()), 20)

    def test_sqlite_reopen_and_filtered_pagination(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.sqlite3'
            a, b = event(), event(instance_id='lab_02')
            with SQLiteStorage(path) as storage:
                self.assertEqual(storage.write_batch([a, b, a]), 2)
            with SQLiteStorage(path) as storage:
                self.assertEqual(storage.read_event(a['event_id']), a)
                rows = storage.read_events(limit=1)
                self.assertEqual(storage.read_events(after_sequence=rows[0]['sequence'])[0]['event'], b)
                self.assertEqual(storage.read_events(instance_id='lab_02')[0]['event'], b)

    def test_conflicting_retry_rolls_back_entire_batch(self):
        with SQLiteStorage(':memory:') as storage:
            old, new = event(), event()
            storage.write_batch([old])
            with self.assertRaises(StorageError):
                storage.write_batch([new, dict(old, action='conflict')])
            self.assertIsNone(storage.read_event(new['event_id']))
            self.assertEqual(storage.read_event(old['event_id']), old)

    def test_uncertain_commit_retries_without_duplicate_rows(self):
        bus = EventBus()
        for _ in range(3): bus.publish(event())
        with SQLiteStorage(':memory:') as storage:
            class Uncertain:
                first = True
                def write_batch(self, records):
                    count = storage.write_batch(records)
                    if self.first:
                        self.first = False
                        raise OSError('Reply lost after commit')
                    return count
            collector = Collector(bus, Uncertain(), batch_size=2)
            with self.assertRaises(CollectorError): collector.drain_once()
            self.assertEqual(collector.stats()['pending'], 2)
            self.assertEqual(bus.stats()['depth'], 1)
            self.assertEqual(collector.drain(), 3)
            self.assertEqual(len(storage.read_events()), 3)
            self.assertEqual(collector.stats()['pending'], 0)

    def test_invalid_input_cannot_partially_persist(self):
        with SQLiteStorage(':memory:') as storage:
            with self.assertRaises(EventValidationError): storage.write_batch([event(), event(simulation='yes')])
            self.assertEqual(storage.read_events(), [])


if __name__ == '__main__': unittest.main()

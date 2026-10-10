"""Transfer queued telemetry to a transactional storage provider."""

from threading import RLock
from types import MappingProxyType

from core.event_bus import EventBus


class CollectorError(RuntimeError):
    """A batch could not be durably acknowledged."""


class Collector:
    """Keep failed batches in memory for retry before dequeuing more events.

    Storage providers implement write_batch(events), returning the count of newly
    inserted rows after commit. Identical retries must be idempotent. Only one
    collector should consume a bus. Pending events do not survive process exit;
    graceful shutdown must drain successfully before closing storage.
    """

    def __init__(self, bus: EventBus, storage, *, batch_size: int = 100) -> None:
        if not isinstance(bus, EventBus) or not callable(getattr(storage, 'write_batch', None)):
            raise ValueError('Collector requires an EventBus and storage provider')
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError('Batch size must be between 1 and 1000')
        self._bus, self._storage = bus, storage
        self._batch_size = batch_size
        self._pending = ()
        self._lock = RLock()
        self._acknowledged = 0
        self._inserted = 0
        self._failures = 0

    def drain_once(self) -> int:
        """Commit one batch, returning acknowledged record count, including retries."""
        with self._lock:
            if not self._pending:
                self._pending = self._bus.poll(limit=self._batch_size)
            if not self._pending:
                return 0
            try:
                inserted = self._storage.write_batch(self._pending)
                if type(inserted) is not int or not 0 <= inserted <= len(self._pending):
                    raise CollectorError('Storage returned an invalid commit result')
            except Exception:
                self._failures += 1
                raise CollectorError('Telemetry batch persistence failed; batch retained for retry') from None
            count = len(self._pending)
            self._acknowledged += count
            self._inserted += inserted
            self._pending = ()
            return count

    def drain(self, *, max_batches: int = 100) -> int:
        """Bound work per call, so continuous producers cannot prevent return."""
        if type(max_batches) is not int or max_batches <= 0:
            raise ValueError('Maximum batches must be a positive integer')
        total = 0
        for _ in range(max_batches):
            count = self.drain_once()
            total += count
            if not count:
                break
        return total

    def stats(self):
        with self._lock:
            return MappingProxyType({
                'pending': len(self._pending), 'acknowledged': self._acknowledged,
                'inserted': self._inserted, 'failures': self._failures,
            })

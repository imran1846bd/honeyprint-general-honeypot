"""Bounded nonblocking telemetry handoff without background workers or storage."""

from collections import deque
from threading import RLock
from types import MappingProxyType

from telemetry.normalizer import normalize_event, validate_event


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class EventBus:
    """Accept validated events in FIFO order and expose bounded batches.

    Publish never waits for queue capacity. A full queue drops the incoming event
    and returns False. Poll transfers responsibility to the consumer; it is not an
    acknowledgement of durable storage. No subscribers or persistence are run on
    the adapter's publishing thread. Validation itself still takes CPU time.
    """

    def __init__(self, *, capacity: int = 1024) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('Event capacity must be a positive integer')
        self._capacity = capacity
        self._queue = deque()
        self._lock = RLock()
        self._accepted = 0
        self._dropped = 0
        self._dequeued = 0

    def publish(self, event) -> bool:
        """Validate and detach a canonical event; return whether it was queued.

        Invalid input raises EventValidationError and is not counted as a capacity
        drop. Duplicate IDs are retained for future storage-level deduplication.
        """
        record = _freeze(validate_event(event))
        with self._lock:
            if len(self._queue) >= self._capacity:
                self._dropped += 1
                return False
            self._queue.append(record)
            self._accepted += 1
            return True

    def sink(self, *, instance_id: str, platform: str):
        """Return an adapter observation sink with framework-owned identity.

        The callback returns queue admission status. Existing adapters that ignore
        this return value must inspect bus statistics to detect capacity drops.
        """
        def emit(observation):
            return self.publish(normalize_event(
                observation, instance_id=instance_id, platform=platform,
            ))
        return emit

    def poll(self, *, limit: int = 100) -> tuple:
        """Remove at most limit events in FIFO order without waiting for input."""
        if type(limit) is not int or limit <= 0:
            raise ValueError('Poll limit must be a positive integer')
        with self._lock:
            batch = tuple(self._queue.popleft() for _ in range(min(limit, len(self._queue))))
            self._dequeued += len(batch)
            return batch

    def stats(self):
        """Return immutable counters and depth from a single lock snapshot."""
        with self._lock:
            return MappingProxyType({
                'capacity': self._capacity, 'depth': len(self._queue),
                'accepted': self._accepted, 'dropped': self._dropped,
                'dequeued': self._dequeued,
            })

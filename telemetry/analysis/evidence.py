"""Read committed SQLite evidence without creating or modifying its database."""

from pathlib import Path
import json
import sqlite3
from threading import RLock

from telemetry.normalizer import validate_event


class EvidenceReader:
    def __init__(self, path: Path):
        self.path = Path(path).resolve(strict=True)
        self._connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                           timeout=5, check_same_thread=False)
        self._connection.execute('PRAGMA query_only=ON')
        self._lock = RLock()

    def read_events(self, *, after_sequence=0, limit=100, instance_id=None, event_type=None):
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError('Evidence cursor must be nonnegative')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Evidence limit must be between 1 and 1000')
        query = 'SELECT sequence, payload FROM events WHERE sequence > ?'
        args = [after_sequence]
        for field, value in [('instance_id', instance_id)]:
            if value is not None:
                if not isinstance(value, str): raise ValueError('Evidence filter must be a string')
                query += f' AND {field} = ?'; args.append(value)
        # Event type filtering uses SQLite JSON extraction and never interpolates input.
        if event_type is not None:
            if not isinstance(event_type, str): raise ValueError('Event type filter must be a string')
            query += " AND json_extract(payload, '$.event_type') = ?"; args.append(event_type)
        query += ' ORDER BY sequence LIMIT ?'; args.append(limit)
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [{'sequence': sequence, 'event': validate_event(json.loads(payload))}
                for sequence, payload in rows]

    def close(self):
        with self._lock: self._connection.close()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

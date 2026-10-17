"""Transactional SQLite storage for normalized HoneyPrint events."""

import json
from pathlib import Path
import sqlite3
from threading import RLock

from telemetry.normalizer import validate_event


class StorageError(RuntimeError):
    """Event persistence or retrieval failed."""


class SQLiteStorage:
    """Commit batches atomically and deduplicate identical event retries.

    Conflicting payloads for one event ID fail the entire transaction. The caller
    owns this connection and must close it. Returned records are detached JSON
    dictionaries. No uploaded artifacts or arbitrary SQL are executed.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        try:
            self._connection.execute('PRAGMA synchronous=FULL')
            self._connection.execute('''CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                instance_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL
            )''')
            self._connection.execute('CREATE INDEX IF NOT EXISTS events_instance ON events(instance_id, sequence)')
            self._connection.commit()
        except Exception:
            self._connection.close()
            raise

    def _check_open(self):
        if self._closed:
            raise StorageError('Storage is closed')

    def write_batch(self, events) -> int:
        """Return new row count only after commit; validate before writing."""
        records = [validate_event(event) for event in events]
        payloads = [json.dumps(event, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=True, allow_nan=False) for event in records]
        with self._lock:
            self._check_open()
            inserted = 0
            try:
                with self._connection:
                    for event, payload in zip(records, payloads):
                        old = self._connection.execute(
                            'SELECT payload FROM events WHERE event_id = ?',
                            (event['event_id'],),
                        ).fetchone()
                        if old is not None:
                            if old[0] != payload:
                                raise StorageError('Conflicting payload for an existing event ID')
                            continue
                        self._connection.execute(
                            'INSERT INTO events(event_id, instance_id, timestamp, payload) VALUES (?, ?, ?, ?)',
                            (event['event_id'], event['instance_id'], event['timestamp'], payload),
                        )
                        inserted += 1
            except sqlite3.Error:
                raise StorageError('SQLite event transaction failed') from None
            return inserted

    def read_events(self, *, instance_id: str | None = None,
                    after_sequence: int = 0, limit: int = 100) -> list[dict]:
        """Read ordered rows, with sequence cursors for bounded pagination."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Read limit must be between 1 and 1000')
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError('Sequence cursor must be a nonnegative integer')
        if instance_id is not None and not isinstance(instance_id, str):
            raise ValueError('Instance filter must be a string')
        with self._lock:
            self._check_open()
            query = 'SELECT sequence, payload FROM events WHERE sequence > ?'
            args = [after_sequence]
            if instance_id is not None:
                query += ' AND instance_id = ?'
                args.append(instance_id)
            query += ' ORDER BY sequence LIMIT ?'
            args.append(limit)
            try:
                rows = self._connection.execute(query, args).fetchall()
                return [{'sequence': seq, 'event': validate_event(json.loads(payload))}
                        for seq, payload in rows]
            except (sqlite3.Error, ValueError):
                raise StorageError('Stored events could not be read') from None

    def read_event(self, event_id: str) -> dict | None:
        """Read one committed event by ID to verify durable acknowledgements."""
        if not isinstance(event_id, str):
            raise ValueError('Event ID must be a string')
        with self._lock:
            self._check_open()
            try:
                row = self._connection.execute(
                    'SELECT payload FROM events WHERE event_id = ?', (event_id,),
                ).fetchone()
                return validate_event(json.loads(row[0])) if row else None
            except (sqlite3.Error, ValueError):
                raise StorageError('Stored event could not be read') from None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *exc):
        self.close()

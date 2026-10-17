"""Persist derived rule findings and analysis cursors apart from raw evidence."""

import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from uuid import UUID


_FIELDS = {'finding_id', 'event_id', 'instance_id', 'timestamp', 'rule_id',
           'rule_version', 'severity', 'message'}


def validate_finding(finding):
    if not isinstance(finding, dict) or set(finding) != _FIELDS:
        raise ValueError('Finding fields do not match the analysis schema')
    if any(not isinstance(v, str) or not v or len(v) > 512 for v in finding.values()):
        raise ValueError('Finding fields must be bounded strings')
    if not re.fullmatch(r'[a-f0-9]{64}', finding['finding_id']):
        raise ValueError('Finding identity must be a SHA256 digest')
    UUID(finding['event_id'])
    if finding['severity'] not in ('low', 'medium', 'high'):
        raise ValueError('Finding severity is unsupported')
    return dict(finding)


class AlertStore:
    def __init__(self, path: Path):
        self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute('PRAGMA synchronous=FULL')
            self._connection.execute('''CREATE TABLE IF NOT EXISTS findings (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_id TEXT NOT NULL UNIQUE, instance_id TEXT NOT NULL,
                severity TEXT NOT NULL, payload TEXT NOT NULL)''')
            self._connection.execute('''CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY, sequence INTEGER NOT NULL)''')

    def cursor(self, source: str) -> int:
        with self._lock:
            row = self._connection.execute('SELECT sequence FROM cursors WHERE source = ?', (source,)).fetchone()
            return row[0] if row else 0

    def write_batch(self, findings, *, source: str, cursor: int) -> int:
        if not isinstance(source, str) or not source or type(cursor) is not int or cursor < 0:
            raise ValueError('Analysis source and cursor are invalid')
        records = [validate_finding(finding) for finding in findings]
        inserted = 0
        with self._lock, self._connection:
            for record in records:
                payload = json.dumps(record, sort_keys=True, separators=(',', ':'))
                previous = self._connection.execute('SELECT payload FROM findings WHERE finding_id = ?',
                                                    (record['finding_id'],)).fetchone()
                if previous:
                    if previous[0] != payload: raise ValueError('Conflicting finding retry')
                    continue
                self._connection.execute('INSERT INTO findings(finding_id,instance_id,severity,payload) VALUES (?,?,?,?)',
                                         (record['finding_id'], record['instance_id'], record['severity'], payload))
                inserted += 1
            self._connection.execute('''INSERT INTO cursors(source, sequence) VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET sequence = MAX(sequence, excluded.sequence)''', (source, cursor))
        return inserted

    def read_findings(self, *, limit=100, after_sequence=0, instance_id=None, severity=None):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after_sequence) is not int or after_sequence < 0:
            raise ValueError('Finding pagination is invalid')
        query = 'SELECT sequence,payload FROM findings WHERE sequence > ?'
        args = [after_sequence]
        for field, value in [('instance_id', instance_id), ('severity', severity)]:
            if value is not None:
                if not isinstance(value, str): raise ValueError('Finding filter must be a string')
                query += f' AND {field} = ?'; args.append(value)
        query += ' ORDER BY sequence LIMIT ?'; args.append(limit)
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [{'sequence': sequence, 'finding': validate_finding(json.loads(payload))}
                for sequence, payload in rows]

    def close(self):
        with self._lock: self._connection.close()
    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

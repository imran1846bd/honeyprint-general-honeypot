"""Local research audit storage; independent of the printer connection."""
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DB = ROOT / 'monitoring' / 'activity.sqlite3'
LOCK = threading.Lock()
SECRET = re.compile(r'password|passwd|secret|token|authorization|cookie|api.?key|csrf', re.I)


def clean(value, depth=0):
    if depth > 8:
        return '[depth limit]'
    if isinstance(value, dict):
        return {str(k)[:200]: '[redacted]' if SECRET.search(str(k)) else clean(v, depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [clean(v, depth + 1) for v in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:8000]


def connect(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
        source TEXT NOT NULL, action TEXT NOT NULL, status INTEGER,
        details TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS activity_kind ON activity(kind, id);
    CREATE INDEX IF NOT EXISTS activity_source ON activity(source, id);
    CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return conn


def record(kind, action, source='', status=None, details=None):
    try:
        with LOCK:
            conn = connect()
            try:
                conn.execute('INSERT INTO activity(ts,kind,source,action,status,details) VALUES(?,?,?,?,?,?)',
                    (datetime.now(timezone.utc).isoformat(), kind, str(source)[:500], str(action)[:2000],
                     status, json.dumps(clean(details or {}), ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logging.getLogger(__name__).exception('Audit write failed; printer operation is continuing')


def heartbeat():
    try:
        with LOCK:
            conn = connect()
            try:
                conn.execute('INSERT OR REPLACE INTO health VALUES(?,?)',
                             ('heartbeat', datetime.now(timezone.utc).isoformat()))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logging.getLogger(__name__).exception('Audit heartbeat failed')

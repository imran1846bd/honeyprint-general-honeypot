"""Read committed SQLite evidence without creating or modifying its database."""

from pathlib import Path
import json
import sqlite3
from threading import RLock

from telemetry.normalizer import validate_event


class EvidenceReader:
    def __init__(self, path: Path, *, timeout=5):
        self.path = Path(path).resolve(strict=True)
        self._connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                           timeout=timeout, check_same_thread=False)
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

    def read_latest_events(self, *, before_sequence=0, limit=100, instance_id=None,
                           event_type=None, interactions_only=False):
        if type(before_sequence) is not int or before_sequence < 0:
            raise ValueError('Evidence cursor must be nonnegative')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Evidence limit must be between 1 and 1000')
        query = 'SELECT sequence, payload FROM events WHERE 1=1'
        args = []
        if before_sequence:
            query += ' AND sequence < ?'; args.append(before_sequence)
        if instance_id is not None:
            if not isinstance(instance_id, str): raise ValueError('Evidence filter must be a string')
            query += ' AND instance_id = ?'; args.append(instance_id)
        if event_type is not None:
            if not isinstance(event_type, str): raise ValueError('Event type filter must be a string')
            query += " AND json_extract(payload, '$.event_type') = ?"; args.append(event_type)
        if interactions_only:
            query += ''' AND (
                json_extract(payload, '$.event_type') IN (
                    'ui.interaction','printer.command','network.connection',
                    'manufacturing.stage',
                    'ssh.auth','ssh.command','ssh.upload',
                    'telnet.auth','telnet.command','telnet.upload',
                    'mqtt.auth','mqtt.subscribe','mqtt.command','mqtt.upload',
                    'file.artifact','printer.job')
                OR (json_extract(payload, '$.event_type') = 'http.request' AND (
                    (json_extract(payload, '$.action') IN ('POST','PUT','PATCH','DELETE')
                     AND json_extract(payload, '$.metadata.route') != '/honeyprint/interactions')
                    OR (json_extract(payload, '$.metadata.status') >= 400
                        AND COALESCE(json_extract(payload, '$.metadata.route'),'')
                            NOT IN ('frontend','asset'))))
                OR (json_extract(payload, '$.event_type') = 'api.request' AND
                    json_extract(payload, '$.metadata.method') NOT IN (
                        'server.info','printer.info','printer.objects.query',
                        'printer.objects.subscribe','server.files.list',
                        'server.files.get_directory','server.temperature_store',
                        'server.gcode_store','machine.proc_stats',
                        'printer.objects.list','server.webcams.list',
                        'server.announcements.list','server.history.list',
                        'server.history.totals','server.job_queue.status',
                        'server.database.list','server.database.get_item',
                        'server.database.post_item',
                        'server.config','machine.system_info',
                        'server.connection.identify'))
            ) AND NOT (json_extract(payload, '$.event_type') = 'printer.job'
                       AND json_extract(payload, '$.action') = 'progress')
              AND NOT (json_extract(payload, '$.event_type') = 'printer.command'
                       AND json_extract(payload, '$.source') IN ('firmware','gcode'))'''
        query += ' ORDER BY sequence DESC LIMIT ?'; args.append(limit)
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [{'sequence': sequence, 'event': validate_event(json.loads(payload))}
                for sequence, payload in rows]

    def print_activity(self, *, instance_id=None, limit=10):
        """Summarise streamed print lines per instance and file (newest first)."""
        query = '''SELECT instance_id,
                COALESCE(json_extract(payload, '$.metadata.filename'), ''),
                json_extract(payload, '$.source'), COUNT(*), MAX(sequence),
                MAX(json_extract(payload, '$.timestamp'))
            FROM events WHERE json_extract(payload, '$.event_type') = 'printer.command'
                AND json_extract(payload, '$.source') IN ('firmware','gcode')'''
        args = []
        if instance_id is not None:
            query += ' AND instance_id = ?'; args.append(instance_id)
        query += ' GROUP BY 1, 2, 3 ORDER BY 5 DESC LIMIT ?'; args.append(limit)
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        return [{'instance_id': instance, 'filename': filename, 'source': source,
                 'lines': count, 'last_sequence': last, 'last_timestamp': timestamp}
                for instance, filename, source, count, last, timestamp in rows]

    def instances(self):
        with self._lock:
            return [row[0] for row in self._connection.execute(
                'SELECT DISTINCT instance_id FROM events ORDER BY 1').fetchall()]

    def close(self):
        with self._lock: self._connection.close()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

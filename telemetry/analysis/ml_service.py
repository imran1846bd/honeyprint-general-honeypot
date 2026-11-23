"""Advisory inference over committed evidence, stored separately from raw logs."""

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import time
from zipfile import ZipFile

from dashboard.classification import describe_event, is_print_stream
from dashboard.taxonomy import BENIGN, lookup, to_label
from telemetry.analysis.evidence import EvidenceReader


CATEGORICAL = ('source', 'event_type', 'action', 'outcome', 'route_family', 'command', 'gcode')
NEW_CATEGORICAL = CATEGORICAL + ('method',)
NUMERIC = ('http_status', 'command_length', 'target_c', 'parameter_x', 'parameter_y',
           'parameter_z', 'parameter_e', 'parameter_f', 'parameter_s', 'payload_bytes',
           'artifact_prior_versions', 'recent_404_60s', 'recent_auth_reject_60s')
# v3 schema: adds the command intent token and the number of distinct account names
# rejected in the last 60 s (password spraying). v1/v2 models keep working.
V3_CATEGORICAL = NEW_CATEGORICAL + ('intent',)
V3_NUMERIC = NUMERIC + ('recent_auth_users_60s',)
FEATURES_V3 = list(V3_CATEGORICAL + V3_NUMERIC)
# v4 schema: v3 without 'source'. The source mostly names the platform, and the model
# used it as a shortcut (every Klipper web request looked routine), so it is left out.
FEATURES_V4 = [name for name in FEATURES_V3 if name != 'source']
DEFAULT_ACCOUNTS = frozenset(('admin', 'root', 'pi', 'user', 'ubuntu', 'octoprint', 'klipper', 'mks',
                              'guest', 'support', 'default', 'printer', 'administrator', 'test'))
SUPPORTED = frozenset(('ssh.auth', 'http.request', 'ssh.command', 'ssh.upload',
                       'file.artifact', 'ui.interaction', 'mqtt.auth', 'mqtt.publish',
                       'mqtt.command', 'telnet.auth', 'printer.job', 'printer.command'))


def load_model(archive):
    """Load only an explicitly selected, trusted local training archive."""
    import joblib
    with ZipFile(archive) as package:
        names = {'model_metadata.json', 'attack_subsection.joblib'}
        if not names <= set(package.namelist()):
            raise ValueError('Model archive is missing metadata or attack model')
        if any(package.getinfo(name).file_size > 20_000_000 for name in names):
            raise ValueError('Model archive member is too large')
        metadata = json.loads(package.read('model_metadata.json'))
        if metadata.get('features') not in (list(CATEGORICAL + NUMERIC),
                                            list(NEW_CATEGORICAL + NUMERIC), FEATURES_V3, FEATURES_V4):
            raise ValueError('Model feature schema differs from live inference')
        if not 0.5 <= float(metadata['attack_review_gate']) <= 1:
            raise ValueError('Model confidence gate is invalid')
        with tempfile.TemporaryDirectory(prefix='honeyprint-ml-') as temp:
            path = Path(temp) / 'attack_subsection.joblib'
            path.write_bytes(package.read('attack_subsection.joblib'))
            model = joblib.load(path)
    return model, metadata, sha256(Path(archive).read_bytes()).hexdigest()[:16] + ':policy3'


class FeatureState:
    def __init__(self):
        self.artifacts = Counter()
        self.failures = defaultdict(deque)
        self.denied = defaultdict(deque)
        self.denied_users = defaultdict(deque)

    def transform(self, event):
        metadata = event.get('metadata') or {}
        instance = event['instance_id']
        now = datetime.fromisoformat(event['timestamp'])
        for window in (self.failures[instance], self.denied[instance]):
            while window and now - window[0] > timedelta(seconds=60):
                window.popleft()
        status = metadata.get('status')
        if event['event_type'] == 'http.request' and status == 404:
            self.failures[instance].append(now)
        auth = event['event_type'] in ('ssh.auth', 'telnet.auth', 'mqtt.auth', 'auth.login')
        user = str(metadata.get('username') or '').lower()[:64]
        users = self.denied_users[instance]
        while users and now - users[0][0] > timedelta(seconds=60):
            users.popleft()
        if auth and event['outcome'] == 'rejected':
            self.denied[instance].append(now)
            users.append((now, user))
        intent = metadata.get('intent', '')
        if auth and not intent:
            intent = 'default_account' if user in DEFAULT_ACCOUNTS else ''
        if event['event_type'] == 'mqtt.subscribe' and not intent:
            # Only the scope of the topic filter is kept, never the filter itself.
            own = f'honeyprint/{instance}/'
            intent = '' if str(metadata.get('topic_filter') or '').startswith(own) else 'foreign_topic'
        filename = metadata.get('filename')
        kind = event['event_type']
        upload = kind in ('file.artifact', 'ssh.upload', 'telnet.upload', 'mqtt.upload')
        key = (instance, filename, kind) if isinstance(filename, str) and upload else None
        # Without a filename (e.g. Klipper uploads carry only a content hash) no earlier
        # version can be known, so the upload counts as a first version.
        prior = self.artifacts[key] if key else (0 if upload else math.nan)
        if key and event['action'] in ('upload', 'add') and event['outcome'] != 'rejected':
            self.artifacts[key] += 1
        route = str(metadata.get('route', ''))
        family = ('api' if route.startswith('/api/') else
                  'printer' if route.startswith('/printer/') else
                  'server' if route.startswith('/server/') else
                  'unknown' if route else '')
        values = {'source': event['source'], 'event_type': kind, 'action': event['action'],
                  'outcome': event['outcome'], 'route_family': family,
                  'command': metadata.get('command', ''), 'gcode': metadata.get('gcode', ''),
                  'method': metadata.get('method', ''), 'intent': intent,
                  'http_status': status if type(status) is int else math.nan,
                  'command_length': metadata.get('length'), 'target_c': metadata.get('target_c'),
                  'parameter_x': metadata.get('parameter_x'), 'parameter_y': metadata.get('parameter_y'),
                  'parameter_z': metadata.get('parameter_z'), 'parameter_e': metadata.get('parameter_e'),
                  'parameter_f': metadata.get('parameter_f'), 'parameter_s': metadata.get('parameter_s'),
                  'payload_bytes': metadata.get('payload_bytes'), 'artifact_prior_versions': prior,
                  'recent_404_60s': len(self.failures[instance]),
                  'recent_auth_reject_60s': len(self.denied[instance]),
                  'recent_auth_users_60s': len({name for _, name in users})}
        for key in V3_CATEGORICAL:
            values[key] = str(values[key] if values[key] is not None else '')
        for key in V3_NUMERIC:
            try: values[key] = float(values[key])
            except (TypeError, ValueError): values[key] = math.nan
        return values


def brute_force_label(values):
    """Repetition decides the Brute Force sub-technique (same rule as the dataset builder)."""
    users = values.get('recent_auth_users_60s')
    if isinstance(users, float) and users == users and users >= 3:
        return 'T1110.003'
    return 'T1110.001' if values['recent_auth_reject_60s'] >= 3 else 'T1110'


def open_store(path):
    db = sqlite3.connect(path, timeout=5)
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS ml_predictions (
            event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, instance_id TEXT NOT NULL,
            section TEXT NOT NULL, subsection TEXT NOT NULL, confidence REAL NOT NULL,
            status TEXT NOT NULL, am_stage TEXT NOT NULL, model_id TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS ml_predictions_sequence ON ml_predictions(sequence)')
        db.execute('CREATE TABLE IF NOT EXISTS ml_control (key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        db.execute("INSERT OR IGNORE INTO ml_control VALUES ('generation', 0)")
    return db


def warm_start(evidence, state, recent=100):
    """Restore feature counters, then score only the newest stored events on launch."""
    with sqlite3.connect(evidence.path.resolve().as_uri() + '?mode=ro', uri=True) as source:
        highwater = source.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
        cursor = max(0, highwater - recent)
        for (payload,) in source.execute('''SELECT payload FROM events WHERE sequence <= ?
                AND json_extract(payload, '$.event_type') IN
                ('file.artifact','ssh.upload','telnet.upload','mqtt.upload',
                 'http.request','ssh.auth','telnet.auth','mqtt.auth')
                ORDER BY sequence''', (cursor,)):
            state.transform(json.loads(payload))
    return cursor


def process(evidence, db, model, metadata, model_id, state, cursor, limit=100):
    import numpy as np
    import pandas as pd
    rows = evidence.read_events(after_sequence=cursor, limit=limit)
    pending = []
    scored = []
    trained_types = set(metadata.get('trained_event_types', SUPPORTED))
    for row in rows:
        event = row['event']
        values = state.transform(event)
        if event['event_type'] == 'framework.lifecycle' or is_print_stream(event):
            # Streamed job lines are summarised by the dashboard, not scored one by one.
            continue
        if event['event_type'] not in trained_types:
            pending.append((event['event_id'], row['sequence'], event['instance_id'], 'Unscored',
                            'unscored', 0.0, 'unsupported_event_type',
                            describe_event(event)['process_mapping'], model_id))
        else:
            scored.append((row, values))
    if scored:
        frame = pd.DataFrame([values for _, values in scored], columns=metadata['features'])
        probabilities = np.asarray(model.predict_proba(frame))
        classes = np.asarray(model.classes_)
        for (row, values), scores in zip(scored, probabilities):
            event = row['event']
            index = int(scores.argmax())
            confidence = float(scores[index])
            # Every stored label is a MITRE ATT&CK ID or BENIGN (older models are translated).
            label = to_label(str(classes[index]), values.get('command', '')) or str(classes[index])
            status = 'benign_like' if label == BENIGN else 'mitre_mapped'
            if confidence < float(metadata['attack_review_gate']):
                status = 'low_confidence'
            # Repetition decides the sub-technique; a single event keeps the parent technique.
            if status != 'low_confidence':
                if label == 'T1595.003' and values['recent_404_60s'] < 3:
                    label, status = 'T1595', 'insufficient_sequence'
                if label in ('T1110', 'T1110.001', 'T1110.003'):
                    label = brute_force_label(values)
                    if label == 'T1110':
                        status = 'insufficient_sequence'
            section = lookup(label)['section']
            subsection = label
            pending.append((event['event_id'], row['sequence'], event['instance_id'], section,
                            subsection, confidence, status, describe_event(event)['process_mapping'], model_id))
    if pending:
        with db:
            db.executemany('''INSERT INTO ml_predictions VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET section=excluded.section,
                subsection=excluded.subsection,confidence=excluded.confidence,
                status=excluded.status,am_stage=excluded.am_stage,model_id=excluded.model_id''', pending)
    return rows[-1]['sequence'] if rows else cursor, len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--model-zip', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0.5)
    args = parser.parse_args(argv)
    model, metadata, model_id = load_model(args.model_zip)
    db = open_store(args.predictions)
    try:
        with EvidenceReader(args.database) as evidence:
            state = FeatureState()
            cursor = warm_start(evidence, state)
            backfill_state = FeatureState()
            backfill_cursor = 0
            with sqlite3.connect(evidence.path.resolve().as_uri() + '?mode=ro', uri=True) as source:
                backfill_target = source.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
            generation = db.execute("SELECT value FROM ml_control WHERE key='generation'").fetchone()[0]
            while True:
                current = db.execute("SELECT value FROM ml_control WHERE key='generation'").fetchone()[0]
                if current != generation:
                    generation, state, cursor = current, FeatureState(), 0
                    backfill_state, backfill_cursor, backfill_target = FeatureState(), 0, 0
                    with db: db.execute('DELETE FROM ml_predictions')
                cursor, count = process(evidence, db, model, metadata, model_id, state, cursor)
                if backfill_cursor < backfill_target:
                    backfill_cursor, _ = process(evidence, db, model, metadata, model_id,
                                                 backfill_state, backfill_cursor, limit=500)
                if not count and backfill_cursor >= backfill_target:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())

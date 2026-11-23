"""Build the v3 HoneyPrint training CSV.

Starts from the scenario-labelled ``training_ready.csv`` (v1/v2 pilot) and adds
observed events from one or more evidence databases for the event types the
earlier model never saw: printer commands, Telnet commands/uploads, print-job
lifecycle, MQTT subscriptions, OctoPrint API interactions, UI actions and normal
read-only HTTP/RPC traffic.

Added rows are labelled by the transparent single-event rules in
``dashboard/classification.py`` (``attack_label_status=rule_weak_label``).
They teach the model what normal and clearly out-of-envelope activity looks like
for those event types; they are not independent proof of attacker intent.
Features are computed with the live ``FeatureState`` so training matches
inference exactly.

Example:
    python research/build_dataset_v3.py \
        --base .honeyprint/datasets/local-pilot/training_ready.csv \
        --evidence .honeyprint/research-dataset/events.sqlite3 .honeyprint/quickstart/events.sqlite3 \
        --output .honeyprint/datasets/v3/training_v3.csv
"""

import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.classification import is_print_stream, rule_label, process_stage
from dashboard.taxonomy import BENIGN, lookup
from telemetry.analysis.ml_service import CATEGORICAL, NUMERIC, FeatureState


# Event types whose rows are added from observed evidence.
ADDED_TYPES = frozenset((
    'printer.command', 'telnet.command', 'telnet.upload', 'telnet.auth', 'ssh.command',
    'ssh.auth', 'mqtt.subscribe', 'mqtt.command', 'mqtt.auth', 'mqtt.publish',
    'api.interaction', 'api.request', 'http.request', 'printer.job', 'ui.interaction',
    'file.artifact', 'auth.login'))
# Only uncommon or previously unseen types may add benign rows freely; for types
# the pilot already covers, observed rows are added only when the rule flags them.
WELL_COVERED = frozenset(('ssh.command', 'ssh.auth', 'mqtt.command', 'mqtt.auth',
                          'file.artifact', 'http.request', 'api.request'))
FEATURES = list(CATEGORICAL) + ['method'] + list(NUMERIC)


def read_base(path):
    with open(path, newline='') as source:
        reader = csv.DictReader(source)
        columns, rows = list(reader.fieldnames or ()), list(reader)
    required = {'event_id', 'event_type', 'attack_section', 'attack_subsection', 'run_group', 'split'}
    if not required <= set(columns):
        raise ValueError(f'{path} is not a HoneyPrint training CSV')
    if 'method' not in columns:
        methods = {}
        labelled = Path(path).with_name('labeled_events.csv')
        if labelled.is_file():
            with labelled.open(newline='') as source:
                for row in csv.DictReader(source):
                    methods[row['event_id']] = str(json.loads(row['metadata_json']).get('method', ''))
        for row in rows:
            row['method'] = methods.get(row['event_id'], '')
        columns.insert(columns.index('route_family') + 1, 'method')
    for extra in ('dataset_source',):
        if extra not in columns:
            columns.append(extra)
    for row in rows:
        row.setdefault('dataset_source', 'scenario_pilot')
        row['dataset_source'] = row['dataset_source'] or 'scenario_pilot'
    return columns, rows


def split_for(group, test_share):
    bucket = int(sha256(group.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 'test' if bucket < test_share else 'train'


def harvest(evidence_path, existing, columns, *, cap, benign_cap, test_share, counts):
    rows = []
    state, starts = FeatureState(), {}
    with sqlite3.connect(f'file:{Path(evidence_path).resolve()}?mode=ro', uri=True) as db:
        for sequence, payload in db.execute('SELECT sequence,payload FROM events ORDER BY sequence'):
            event = json.loads(payload)
            instance = event['instance_id']
            kind = event['event_type']
            if kind == 'framework.lifecycle' and event['action'] == 'start':
                starts[instance] = sequence
            if is_print_stream(event):
                continue
            values = state.transform(event)
            if (kind not in ADDED_TYPES or event['event_id'] in existing):
                continue
            subsection = rule_label(event)
            section = lookup(subsection)['section']
            if subsection == BENIGN and kind in WELL_COVERED:
                continue
            group = f"{Path(evidence_path).parent.name}:{instance}:{starts.get(instance, 0)}"
            key = (group, kind, event['action'], subsection)
            limit = benign_cap if subsection == BENIGN else cap
            if counts[key] >= limit:
                continue
            counts[key] += 1
            record = {column: '' for column in columns}
            record.update(event_id=event['event_id'], platform=event['platform'],
                          session_id=event.get('session_id') or '', run_group=group,
                          split=split_for(group, test_share),
                          label='benign' if subsection == BENIGN else 'attack_rule',
                          attack_section=section, attack_subsection=subsection,
                          attack_label_status='rule_weak_label',
                          am_stage=process_stage(event), am_stage_mapping_status='reference_rule',
                          dataset_source='observed_rule_' + Path(evidence_path).parent.name)
            for key_name in FEATURES:
                value = values.get(key_name, '')
                record[key_name] = '' if isinstance(value, float) and value != value else value
            rows.append(record)
            existing.add(event['event_id'])
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', type=Path, required=True, help='Scenario-labelled training_ready.csv')
    parser.add_argument('--evidence', type=Path, nargs='+', required=True, help='events.sqlite3 files')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cap', type=int, default=60,
                        help='Max suspicious rows per run group/event/action/label (default 60)')
    parser.add_argument('--benign-cap', type=int, default=40,
                        help='Max benign rows per run group/event/action (default 40)')
    parser.add_argument('--test-share', type=float, default=0.25,
                        help='Share of added run groups held out for testing (default 0.25)')
    args = parser.parse_args(argv)
    columns, rows = read_base(args.base)
    for column in FEATURES:
        if column not in columns:
            columns.append(column)
    existing = {row['event_id'] for row in rows}
    base_types = Counter(row['event_type'] for row in rows)
    global WELL_COVERED
    WELL_COVERED = frozenset(kind for kind, n in base_types.items() if n >= 300)
    counts, added = Counter(), []
    for path in args.evidence:
        if not path.is_file():
            parser.error(f'Evidence database not found: {path}')
        added.extend(harvest(path, existing, columns, cap=args.cap, benign_cap=args.benign_cap,
                             test_share=args.test_share, counts=counts))
    rows.extend(added)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        'output': str(args.output), 'total_rows': len(rows), 'added_rows': len(added),
        'added_by_event_type': dict(Counter(r['event_type'] for r in added).most_common()),
        'added_by_label': dict(Counter(r['attack_subsection'] for r in added).most_common()),
        'labels_total': dict(Counter(r['attack_subsection'] for r in rows).most_common()),
        'event_types_total': dict(Counter(r['event_type'] for r in rows).most_common()),
        'splits': dict(Counter(r['split'] for r in rows)),
        'platforms': dict(Counter(r['platform'] for r in rows)),
        'label_note': ('Added rows use transparent rule labels (rule_weak_label). '
                       'Scenario rows keep their controlled-scenario labels.'),
    }
    args.output.with_name(args.output.stem + '_summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()

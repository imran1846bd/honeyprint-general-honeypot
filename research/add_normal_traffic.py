"""Append conservative, observed normal UI traffic to an existing training CSV.

Only allowlisted read-only Klipper/Moonraker requests and successful status
polls are selected. This labels observed normal operation, not arbitrary traffic.
"""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry.analysis.ml_service import FeatureState


READ_ONLY_METHODS = frozenset({
    'server.info', 'printer.info', 'printer.objects.query',
    'printer.objects.subscribe', 'printer.objects.list',
    'server.files.list', 'server.files.get_directory',
    'server.temperature_store', 'server.gcode_store',
    'server.webcams.list', 'server.announcements.list',
    'server.history.list', 'server.history.totals',
    'server.job_queue.status', 'server.database.list',
    'server.database.get_item', 'server.config',
    'machine.system_info', 'server.connection.identify',
})
NORMAL_GET_ROUTES = frozenset({'/printer/objects/query', '/server/info',
                               '/printer/info', '/server/temperature_store'})


def make_dataset(source_csv, evidence_db, output_csv):
    with open(source_csv, newline='') as source:
        reader = csv.DictReader(source)
        columns, rows = list(reader.fieldnames or ()), list(reader)
    if not {'event_id', 'event_type', 'attack_subsection', 'run_group'} <= set(columns):
        raise ValueError('Input is not a HoneyPrint training_ready.csv')
    existing = {row['event_id'] for row in rows}
    labeled_path = Path(source_csv).with_name('labeled_events.csv')
    methods = {}
    if labeled_path.is_file():
        with labeled_path.open(newline='') as source:
            for row in csv.DictReader(source):
                methods[row['event_id']] = str(json.loads(row['metadata_json']).get('method', ''))
    for row in rows:
        row['method'] = methods.get(row['event_id'], '')
    if 'method' not in columns:
        columns.insert(columns.index('route_family') + 1, 'method')
    state, starts, caps = FeatureState(), {}, Counter()
    selected = Counter()
    with sqlite3.connect(f'file:{Path(evidence_db).resolve()}?mode=ro', uri=True) as db:
        for sequence, payload in db.execute('SELECT sequence,payload FROM events ORDER BY sequence'):
            event = json.loads(payload)
            instance = event['instance_id']
            if event['event_type'] == 'framework.lifecycle' and event['action'] == 'start':
                starts[instance] = sequence
            values = state.transform(event)
            if event['event_id'] in existing or event['platform'] != 'klipper' or instance not in starts:
                continue
            meta = event.get('metadata') or {}
            method = str(meta.get('method', ''))
            route = str(meta.get('route', ''))
            if event['event_type'] == 'api.request' and method in READ_ONLY_METHODS and event['outcome'] == 'observed':
                family = 'rpc:' + method
            elif (event['event_type'] == 'http.request' and event['action'] == 'GET' and
                  meta.get('status') == 200 and route in NORMAL_GET_ROUTES):
                family = 'get:' + route
            else:
                continue
            group = f'{instance}:{starts[instance]}'
            if caps[(group, family)] >= 30:
                continue
            caps[(group, family)] += 1
            record = {key: '' for key in columns}
            record.update(event_id=event['event_id'], platform=event['platform'],
                          session_id=event.get('session_id') or '', run_group=group,
                          split='train', label='benign', attack_section='Benign',
                          attack_subsection='None',
                          attack_label_status='observed_normal_read_only',
                          am_stage='', am_stage_mapping_status='unmapped')
            for key, value in values.items():
                if key in record:
                    record[key] = '' if isinstance(value, float) and value != value else value
            rows.append(record)
            selected[event['event_type']] += 1
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return {'total_rows': len(rows), 'added_normal_rows': dict(selected),
            'output': str(output_csv)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(make_dataset(args.input, args.evidence, args.output), indent=2))

"""Join a dashboard CSV export with controlled-scenario labels by event_id."""

import argparse
from collections import Counter
from collections import defaultdict, deque
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.labels.open(newline='') as source:
        reader = csv.DictReader(source)
        label_columns = list(reader.fieldnames or [])
        labels = list(reader)
    mitre_columns = ['mitre_section', 'mitre_tactic_id', 'mitre_subject',
                     'mitre_mapping_status', 'mitre_url']
    scenario_mappings = {}
    for label in labels:
        if label.get('mitre_section'):
            scenario_mappings[label['scenario']] = {key: label.get(key, '') for key in mitre_columns}
    for label in labels:
        if not label.get('mitre_section'):
            label.update(scenario_mappings.get(label['scenario'], {}))
        if label['scenario'] == 'stored_gcode_replacement':
            label['mitre_mapping_status'] = 'research_hypothesis_not_verified'
    if all(key in label_columns for key in mitre_columns):
        with args.labels.open('w', newline='') as destination:
            writer = csv.DictWriter(destination, fieldnames=label_columns)
            writer.writeheader()
            writer.writerows(labels)
    by_id = {row['event_id']: row for row in labels}
    if len(by_id) != len(labels):
        parser.error('Duplicate event IDs in the scenario labels')
    with args.evidence.open(newline='') as source:
        evidence = list(csv.DictReader(source))
    missing = set(by_id) - {row['event_id'] for row in evidence}
    if missing:
        parser.error(f'{len(missing)} labelled events are absent from the dashboard export')
    args.output.mkdir(parents=True, exist_ok=True)
    columns = list(evidence[0]) + ['scenario', 'label', 'mitre_technique',
                                   *mitre_columns, 'label_basis']
    joined = []
    features = []
    prior_artifacts = Counter()
    recent_failures = defaultdict(deque)
    recent_denied = defaultdict(deque)
    for row in evidence:
        metadata = json.loads(row['metadata_json'])
        now = datetime.fromisoformat(row['timestamp'])
        instance = row['instance_id']
        for window in (recent_failures[instance], recent_denied[instance]):
            while window and now - window[0] > timedelta(seconds=60):
                window.popleft()
        status = metadata.get('status')
        if row['event_type'] == 'http.request' and status == 404:
            recent_failures[instance].append(now)
        if row['event_type'] in ('ssh.auth', 'telnet.auth', 'mqtt.auth') and row['outcome'] == 'rejected':
            recent_denied[instance].append(now)
        filename = metadata.get('filename')
        upload_event = row['event_type'] in ('file.artifact', 'ssh.upload', 'telnet.upload', 'mqtt.upload')
        key = (instance, filename, row['event_type']) if isinstance(filename, str) and upload_event else None
        prior_versions = prior_artifacts[key] if key else ''
        if key and row['action'] in ('upload', 'add') and row['outcome'] != 'rejected':
            prior_artifacts[key] += 1
        label = by_id.get(row['event_id'])
        if label:
            joined.append({**row, 'scenario': label['scenario'], 'label': label['label'],
                           'mitre_technique': label['mitre_technique'],
                           **{key: label.get(key, '') for key in mitre_columns},
                           'label_basis': 'controlled_scenario_intent'})
            route = str(metadata.get('route', ''))
            route_family = ('api' if route.startswith('/api/') else
                            'printer' if route.startswith('/printer/') else
                            'server' if route.startswith('/server/') else
                            'unknown' if route else '')
            features.append({
                'event_id': row['event_id'], 'platform_group': row['platform'],
                'split': ('review_only' if label['scenario'] == 'emergency_stop' else
                          'holdout' if row['platform'] == 'klipper' else 'train'),
                'source': row['source'], 'event_type': row['event_type'],
                'action': row['action'], 'outcome': row['outcome'],
                'http_status': status if type(status) is int else '',
                'route_family': route_family,
                'command': metadata.get('command', ''),
                'command_length': metadata.get('length', ''),
                'gcode': metadata.get('gcode', ''),
                'target_c': metadata.get('target_c', ''),
                'parameter_x': metadata.get('parameter_x', ''),
                'parameter_y': metadata.get('parameter_y', ''),
                'parameter_z': metadata.get('parameter_z', ''),
                'parameter_e': metadata.get('parameter_e', ''),
                'parameter_f': metadata.get('parameter_f', ''),
                'parameter_s': metadata.get('parameter_s', ''),
                'payload_bytes': metadata.get('payload_bytes', ''),
                'artifact_prior_versions': prior_versions,
                'recent_404_60s': len(recent_failures[instance]),
                'recent_auth_reject_60s': len(recent_denied[instance]),
                'label': label['label'], 'mitre_technique': label['mitre_technique'],
                **{key: label.get(key, '') for key in mitre_columns},
                'scenario': label['scenario']})
    with (args.output / 'labeled_events.csv').open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(joined)
    with (args.output / 'ml_features.csv').open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=list(features[0]))
        writer.writeheader()
        writer.writerows(features)
    summary = {
        'raw_event_count': len(evidence),
        'labelled_event_count': len(joined),
        'unlabelled_event_count': len(evidence) - len(joined),
        'class_counts': dict(Counter(row['scenario'] for row in joined)),
        'training_split_counts': dict(Counter(row['split'] for row in features)),
        'by_platform_and_scenario': {
            f'{platform} / {scenario}': count for (platform, scenario), count in
            sorted(Counter((row['platform'], row['scenario']) for row in joined).items())},
        'label_basis': 'Controlled local scenario intent; not proof of malicious intent from any single event.',
        'model_note': 'Exclude event_id, platform_group, split and scenario from model features; split by platform or session, not random event rows.',
        'mitre_mapping': {
            'T1110.001': 'Password Guessing',
            'T1595.003': 'Wordlist Scanning',
            'T1565.001': 'Stored Data Manipulation (research hypothesis for repeated G-code replacement)'},
    }
    (args.output / 'dataset_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()

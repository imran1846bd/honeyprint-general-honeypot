"""Build the HoneyPrint ATT&CK Enterprise dataset (v9): every row labelled with a MITRE ATT&CK ID.

Every row gets ``mitre_label``: a MITRE ATT&CK technique / sub-technique ID
(ATT&CK Enterprise v20.1), plus its tactic (section), technique and
sub-technique names (subsection). There is no "review needed" or "benign" class:
a honeypot has no legitimate users, so routine use gets its closest technique
(status → T1082, files → T1083, upload → T1105, print command → T1059; see
dashboard.taxonomy.activity_technique). Such rows have label = 'routine'.

* Legacy labels (v1–v4) are translated with dashboard/taxonomy.py.
* Console probing is split per command: ls/find/pwd → T1083, ps → T1057,
  id/whoami → T1033, uname/env → T1082, cat → T1005, sudo → T1548.003, ...
* Repetition decides the sub-technique, exactly as the live worker does:
  fewer than 3 rejected logins in 60 s → T1110 (not T1110.001); fewer than
  3 not-found requests in 60 s → T1595 (not T1595.003).
* Setup steps of the print-interruption scenario are routine, not service stops.
* AM process mapping is recomputed from the component reference.

Example:
    ./.venv/bin/python research/build_attack_dataset.py \
        --base ~/Desktop/dataset/HoneyPrint_Dataset_v4/training_v4.csv \
        --output-dir ~/Desktop/dataset/HoneyPrint_ATTACK_Enterprise_v9

Add newly observed honeypot rows with --observed .honeyprint/datasets/v3/training_v3.csv
(produced by research/build_dataset_v3.py). --base can be a previous honeyprint_attack_v9.csv.
"""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.am_mapping import map_features
import sqlite3

from dashboard.classification import PRINTER_VERBS, _attack_label, is_print_stream
from dashboard.taxonomy import (ATTACK_VERSION, BENIGN, LABELS, activity_technique, lookup, to_label,
                                write_reference)
from telemetry.analysis.ml_service import FeatureState, brute_force_label

FEATURES = ['source', 'event_type', 'action', 'outcome', 'route_family', 'command', 'gcode', 'method', 'intent',
            'http_status', 'command_length', 'target_c', 'parameter_x', 'parameter_y', 'parameter_z',
            'parameter_e', 'parameter_f', 'parameter_s', 'payload_bytes', 'artifact_prior_versions',
            'recent_404_60s', 'recent_auth_reject_60s', 'recent_auth_users_60s']
IDS = ['event_id', 'platform', 'session_id', 'run_group', 'split', 'dataset_source']
LABEL_COLUMNS = ['label', 'mitre_label', 'attack_label_status', 'mitre_tactic_id', 'mitre_tactic',
                 'mitre_technique_id', 'mitre_technique', 'mitre_subtechnique_id', 'mitre_subtechnique',
                 'mitre_domain', 'mitre_url', 'severity']
AM_COLUMNS = ['component', 'honeypot_stage', 'honeypot_stage_name', 'am_stage', 'am_stage_name',
              'am_layer', 'am_element', 'am_process_mapping']
COLUMNS = IDS + FEATURES + LABEL_COLUMNS + AM_COLUMNS


def read(path):
    with open(path, newline='') as source:
        return list(csv.DictReader(source))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def is_interruption_setup(row):
    if row.get('attack_subsection') != 'Print job interruption':
        return False
    if row['event_type'] == 'printer.job' and row['action'] in ('start', 'resume'):
        return True
    return row['event_type'] == 'ssh.command' and row['command'] in ('print', 'start', 'resume', 'select')


def mitre_label(row):
    """MITRE label for a row: its attack technique, or the closest technique for routine use."""
    label = attack_label(row)
    return activity_technique(row) if label == BENIGN else label


def attack_label(row):
    """Attack-specific label, or BENIGN when the row is routine use."""
    if row.get('mitre_label') == BENIGN or row.get('label') == 'routine':
        # Routine rows from an earlier build keep their original meaning.
        return BENIGN
    if row.get('mitre_label'):
        label = to_label(row['mitre_label'], row.get('command', ''))
    elif is_interruption_setup(row):
        return BENIGN
    else:
        label = to_label(row.get('attack_subsection', ''), row.get('command', ''))
    if not label:
        raise ValueError(f"No MITRE mapping for label {row.get('attack_subsection')!r}")
    if label in ('T1110', 'T1110.001', 'T1110.003'):
        users = row.get('recent_auth_users_60s', '')
        label = brute_force_label({'recent_auth_reject_60s': number(row['recent_auth_reject_60s']),
                                   'recent_auth_users_60s': number(users) if users != '' else float('nan')})
    if label in ('T1595', 'T1595.003'):
        label = 'T1595.003' if number(row['recent_404_60s']) >= 3 else 'T1595'
    if label == 'T1565.001' and number(row.get('artifact_prior_versions')) < 1:
        # A first upload replaces nothing; it looks exactly like a normal upload.
        label = BENIGN
    return label


KEEP_CONSOLE = {BENIGN, 'None', 'T1565', 'T1041', 'T1489', 'Thermal setpoint manipulation',
                'Out-of-range motion request', 'Stored print-file readout', 'Print job interruption'}


def superseded(row, have_scenarios):
    """Old rows recorded before command intent existed; the attack scenarios replace them."""
    if not have_scenarios:
        return False
    if row.get('dataset_source') in ('scenario_mitre_gaps', 'scenario_attack', 'scenario_routine'):
        return True
    # Routine rows from a v9+ build carry a technique label but are still routine use.
    label = BENIGN if row.get('label') == 'routine' else (row.get('mitre_label') or row.get('attack_subsection', ''))
    if row['event_type'] in ('ssh.command', 'telnet.command') and not row.get('intent') \
            and not row.get('dataset_source', '').startswith('observed') and label not in KEEP_CONSOLE:
        return True
    return row['event_type'] == 'mqtt.command' and row['outcome'] == 'rejected' and not row.get('intent') \
        and not row.get('dataset_source', '').startswith('observed')


def scenario_rows(database, labels_csv):
    """Rows for events labelled by research/generate_attack_scenarios.py (features replayed live-style)."""
    labels = {row['event_id']: row for row in read(labels_csv)}
    runs = sorted({row['run_id'] for row in labels.values()})
    state, rows = FeatureState(), []
    with sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True) as db:
        for (payload,) in db.execute('SELECT payload FROM events ORDER BY sequence'):
            event = json.loads(payload)
            values = state.transform(event)
            label = labels.get(event['event_id'])
            if not label:
                continue
            verb = str(event['metadata'].get('command', '')).lower().split()[:1]
            if label['mitre_label'] == 'auth':
                # Logins follow the same rules as live scoring.
                if event['outcome'] == 'rejected':
                    mitre = brute_force_label(values)
                else:
                    mitre = 'T1078.001' if values.get('intent') == 'default_account' else BENIGN
            elif verb and verb[0] in PRINTER_VERBS and verb[0] != 'run':
                mitre = BENIGN
            else:
                mitre = to_label(label['mitre_label'])
            row = {key: ('' if isinstance(value, float) and value != value else value)
                   for key, value in values.items()}
            row.update(event_id=event['event_id'], platform=event['platform'],
                       session_id=event.get('session_id') or '', run_group=f"attack_scenarios:{label['run_id']}",
                       split='test' if label['run_id'] == runs[-1] else 'train',
                       dataset_source='scenario_attack', mitre_label=mitre,
                       attack_label_status='scenario_intent_mitre')
            rows.append(row)
    return rows


# Interaction types the model is trained on; sessions and printer telemetry are left to the rules.
OBSERVED_TYPES = frozenset((
    'ssh.auth', 'telnet.auth', 'mqtt.auth', 'auth.login', 'ssh.command', 'telnet.command', 'mqtt.command',
    'ssh.upload', 'telnet.upload', 'file.artifact', 'mqtt.subscribe', 'mqtt.publish', 'http.request',
    'api.request', 'api.interaction', 'ui.interaction', 'printer.command', 'printer.job'))


def observed_rows(database, cap):
    """Real interactions from a live evidence database, labelled by the dashboard rules.

    Features come from the live FeatureState, so training matches inference. At most
    ``cap`` rows per distinct (event type, action, outcome, route, status, method,
    command, label) keep routine polling from flooding the data. All rows are
    training rows; the held-out test stays on the controlled scenario runs.
    """
    state, counts, rows = FeatureState(), Counter(), []
    name = Path(database).parent.name
    with sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True) as db:
        for (payload,) in db.execute('SELECT payload FROM events ORDER BY sequence'):
            event = json.loads(payload)
            if event['event_type'] == 'framework.lifecycle' or is_print_stream(event):
                continue
            values = state.transform(event)
            if event['event_type'] not in OBSERVED_TYPES:
                continue
            label = _attack_label(event)
            key = tuple(str(values.get(k, '')) for k in ('event_type', 'action', 'outcome', 'route_family',
                                                         'http_status', 'method', 'command', 'intent')) + (label,)
            if counts[key] >= cap:
                continue
            counts[key] += 1
            row = {key: ('' if isinstance(value, float) and value != value else value)
                   for key, value in values.items()}
            row.update(event_id=event['event_id'], platform=event['platform'],
                       session_id=event.get('session_id') or '',
                       run_group=f"observed_live:{name}:{event['instance_id']}", split='train',
                       dataset_source=f'observed_live_{name}', mitre_label=label,
                       attack_label_status='rule_label_observed')
            rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', type=Path, required=True, help='training_v4.csv or a previous honeyprint_attack_v9.csv')
    parser.add_argument('--observed', type=Path, help='training_v3.csv with newly observed rows')
    parser.add_argument('--scenario-db', type=Path, action='append', default=[],
                        help='events.sqlite3 from run_attack_scenarios.sh or run_routine_sessions.sh (repeatable)')
    parser.add_argument('--scenario-labels', type=Path, action='append', default=[],
                        help='labels.csv for each --scenario-db, in the same order')
    parser.add_argument('--observed-db', type=Path, action='append', default=[],
                        help='Live evidence events.sqlite3 to add real interactions from (repeatable)')
    parser.add_argument('--observed-cap', type=int, default=40,
                        help='Max rows per distinct kind of observed interaction (default 40)')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--summary', type=Path, default=ROOT / '.honeyprint' / 'datasets' / 'v9' / 'dataset_summary.json')
    args = parser.parse_args(argv)

    if len(args.scenario_db) != len(args.scenario_labels):
        parser.error('Give one --scenario-labels for each --scenario-db')
    scenarios = list(zip(args.scenario_db, args.scenario_labels))
    rows = [row for row in read(args.base) if not superseded(row, bool(scenarios))
            and not (args.observed_db and row.get('dataset_source', '').startswith('observed_live_'))]
    seen = {row['event_id'] for row in rows}
    if args.observed:
        rows += [row for row in read(args.observed)
                 if row.get('dataset_source', '').startswith('observed_rule_') and row['event_id'] not in seen]
    for database in args.observed_db:
        found = [row for row in observed_rows(database, args.observed_cap) if row['event_id'] not in seen]
        seen.update(row['event_id'] for row in found)
        rows += found
    for database, labels in scenarios:
        source = 'scenario_routine' if 'routine' in Path(database).parent.name else 'scenario_attack'
        rows += [{**row, 'dataset_source': source, 'run_group': row['run_group'].replace('attack_scenarios', source)}
                 for row in scenario_rows(database, labels) if row['event_id'] not in seen]
    output, relabelled = [], Counter()
    for row in rows:
        routine = attack_label(row) == BENIGN
        label = mitre_label(row)
        if label == BENIGN:
            raise ValueError(f"Row {row.get('event_id')} has no technique (event type {row.get('event_type')})")
        old = row.get('mitre_label') or row.get('attack_subsection', '')
        if old != label:
            relabelled[f'{old} -> {label}'] += 1
        info = lookup(label)
        record = {key: row.get(key, '') for key in IDS + FEATURES}
        record['dataset_source'] = record['dataset_source'] or 'scenario'
        status = row.get('attack_label_status', '')
        if routine:
            status = 'routine_activity_technique'
        # label: 'routine' = normal use mapped to its closest technique; 'attack' = attack behaviour.
        record.update(label='routine' if routine else 'attack', mitre_label=label,
                      attack_label_status=status, mitre_tactic_id=info['tactic_id'],
                      mitre_tactic=info['section'], mitre_technique_id=info['technique_id'],
                      mitre_technique=info['technique'], mitre_subtechnique_id=info['subtechnique_id'],
                      mitre_subtechnique=info['subtechnique'], mitre_domain=info['mitre_domain'],
                      mitre_url=info['mitre_url'], severity=info['severity'])
        am = map_features(row)
        record.update({key: am[key] for key in AM_COLUMNS})
        output.append(record)

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'honeyprint_attack_v9.csv').open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=COLUMNS)
        writer.writeheader(); writer.writerows(output)
    write_reference(out / 'attack_taxonomy.csv')
    write_reference()

    counts = Counter(r['mitre_label'] for r in output)
    summary = {
        'attack_version': ATTACK_VERSION, 'total_rows': len(output),
        'labels': {label: {'rows': counts.get(label, 0), 'tactic': lookup(label)['section'],
                           'technique': lookup(label)['subsection']} for label in LABELS},
        'by_tactic': dict(Counter(r['mitre_tactic'] for r in output).most_common()),
        'relabelled': dict(relabelled.most_common()),
        'by_am_stage': dict(Counter(r['am_stage'] for r in output).most_common()),
        'by_platform': dict(Counter(r['platform'] for r in output).most_common()),
        'by_split': dict(Counter(r['split'] for r in output).most_common()),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ('total_rows', 'by_tactic', 'relabelled', 'by_split')}, indent=2))
    print('\nRows per MITRE label:')
    for label, item in summary['labels'].items():
        print(f"  {label:10} {item['rows']:>6}  {item['tactic']} → {item['technique']}")


if __name__ == '__main__':
    main()

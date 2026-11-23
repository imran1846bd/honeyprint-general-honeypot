"""Add honest attack/stage targets and run-disjoint splits to a local pilot export."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.classification import describe_event


def read_csv(path):
    with path.open(newline='') as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path, columns, rows):
    with path.open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    args = parser.parse_args()
    folder = args.dataset
    evidence_columns, evidence = read_csv(folder / 'honeyprint-evidence.csv')
    label_columns, labels = read_csv(folder / 'labels.csv')
    joined_columns, joined = read_csv(folder / 'labeled_events.csv')
    feature_columns, features = read_csv(folder / 'ml_features.csv')
    if len(labels) != len(joined) or len(labels) != len(features):
        parser.error('Labelled exports have different row counts')
    ids = [row['event_id'] for row in labels]
    if len(set(ids)) != len(ids) or set(ids) != {row['event_id'] for row in joined}:
        parser.error('Labelled event IDs are missing or duplicated')

    run_starts = defaultdict(list)
    for row in evidence:
        if row['event_type'] == 'framework.lifecycle' and row['action'] == 'start':
            run_starts[row['instance_id']].append(int(row['sequence']))
    for starts in run_starts.values():
        starts.sort()
    experiment_starts = {}
    for label in labels:
        if label.get('run_id'):
            key = (label['instance_id'], label['run_id'])
            experiment_starts[key] = min(experiment_starts.get(key, int(label['sequence'])),
                                         int(label['sequence']))

    def group_start(label):
        if label.get('run_id'):
            return experiment_starts[(label['instance_id'], label['run_id'])]
        starts = run_starts[label['instance_id']]
        index = bisect_right(starts, int(label['sequence'])) - 1
        if index < 0:
            parser.error(f'No lifecycle start precedes event {label["event_id"]}')
        return starts[index]

    newest_non_klipper = {}
    for label in labels:
        if label['platform'] != 'klipper':
            instance = label['instance_id']
            newest_non_klipper[instance] = max(newest_non_klipper.get(instance, 0),
                                               group_start(label))

    label_by_id = {row['event_id']: row for row in labels}
    feature_by_id = {row['event_id']: row for row in features}
    additions = ('attack_section', 'attack_subsection', 'attack_label_status',
                 'am_stage', 'am_stage_mapping_status', 'run_group', 'split')
    clean_features = [name for name in feature_columns if name not in (
        'event_id', 'platform_group', 'split', 'label', 'scenario', 'mitre_technique',
        'mitre_section', 'mitre_tactic_id', 'mitre_subject', 'mitre_mapping_status',
        'mitre_url', *additions)]
    training_columns = ['event_id', 'platform', 'session_id', 'run_group', 'split', *clean_features,
                        'label', 'attack_section', 'attack_subsection', 'attack_label_status',
                        'am_stage', 'am_stage_mapping_status']
    training = []
    for row in joined:
        event_id = row['event_id']
        label = label_by_id[event_id]
        metadata = json.loads(row['metadata_json'])
        event = {**row, 'metadata': metadata}
        mapping = describe_event(event)['process_mapping']
        stage = mapping.split(':', 1)[0].replace('AM Stage ', '') if mapping.startswith('AM Stage ') else ''
        start = group_start(label)
        run_group = f"{row['instance_id']}:{start}"
        first_replacement_upload = (label['scenario'] == 'stored_gcode_replacement' and
                                    str(feature_by_id[event_id].get('artifact_prior_versions', '')) == '0')
        split = ('review_only' if label['scenario'] == 'emergency_stop' or first_replacement_upload else
                 'test' if row['platform'] == 'klipper' else
                 'validation' if start == newest_non_klipper.get(row['instance_id']) else
                 'train')
        attack_section = ('Impact candidate / review needed'
                          if label['scenario'] == 'stored_gcode_replacement' else
                          label['mitre_section'])
        attack_subsection = ('Stored Data Manipulation (T1565.001 candidate)'
                             if label['scenario'] == 'stored_gcode_replacement' else
                             label['mitre_subject'] if attack_section != 'Benign' else 'None')
        attack_status = ('ambiguous_first_upload' if first_replacement_upload else
                         'research_hypothesis_not_verified' if label['scenario'] == 'stored_gcode_replacement' else
                         'scenario_intent_mitre_mapping' if label['mitre_technique'] else
                         'scenario_intent_am_specific' if attack_section != 'Benign' else
                         'scenario_intent_benign')
        extra = {'attack_section': attack_section, 'attack_subsection': attack_subsection,
                 'attack_label_status': attack_status, 'am_stage': stage,
                 'am_stage_mapping_status': 'reference_rule' if stage else 'unmapped',
                 'run_group': run_group, 'split': split}
        label.update(extra)
        row.update(extra)
        features_row = feature_by_id[event_id]
        features_row.update(extra)
        training.append({'event_id': event_id, 'platform': row['platform'],
                         'session_id': row.get('session_id', ''),
                         'run_group': run_group, 'split': split,
                         **{key: features_row.get(key, '') for key in clean_features},
                         'label': label['label'], **{key: extra[key] for key in additions[:5]}})

    for path, columns, rows in ((folder / 'labels.csv', label_columns, labels),
                                (folder / 'labeled_events.csv', joined_columns, joined),
                                (folder / 'ml_features.csv', feature_columns, features)):
        write_csv(path, list(dict.fromkeys([*columns, *additions])), rows)
    write_csv(folder / 'training_ready.csv', training_columns, training)
    summary_path = folder / 'dataset_summary.json'
    summary = json.loads(summary_path.read_text())
    summary['training_split_counts'] = dict(Counter(row['split'] for row in training))
    summary['am_stage_counts'] = dict(Counter(row['am_stage'] or 'unmapped' for row in training))
    summary['attack_section_counts'] = dict(Counter(row['attack_section'] for row in training))
    summary['training_note'] = ('Targets describe controlled scenario intent; AM stages are reference-rule '
                                'mappings, not manually verified attack localization. The split column is a '
                                'preset example; the training notebook evaluates all three adapters with '
                                'one platform held out per fold and separate run-level validation.')
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({key: summary[key] for key in ('labelled_event_count',
          'training_split_counts', 'am_stage_counts', 'attack_section_counts')}, indent=2))


if __name__ == '__main__':
    main()

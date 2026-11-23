"""Export all captured events with advisory predictions and reviewed ATT&CK IDs."""

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.classification import describe_event


def export(evidence, analysis, labels, output):
    with Path(labels).open(newline='') as source:
        label_by_id = {row['event_id']: row for row in csv.DictReader(source)}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ('sequence','event_id','timestamp','instance_id','platform','source',
               'event_type','action','outcome','metadata_json','label_origin',
               'attack_section','attack_subsection','ml_confidence','ml_status',
               'am_stage','mitre_suggested_id','mitre_mapping_status')
    subsection_ids = {
        'Active Scanning: Wordlist Scanning': 'T1595.003',
        'Brute Force: Password Guessing': 'T1110.001',
        'Stored Data Manipulation (T1565.001 candidate)': 'T1565.001',
    }
    count = 0
    with sqlite3.connect(f'file:{Path(evidence).resolve()}?mode=ro', uri=True) as raw, \
         sqlite3.connect(f'file:{Path(analysis).resolve()}?mode=ro', uri=True) as findings, \
         output.open('w', newline='') as destination:
        predictions = {row[0]: row[1:] for row in findings.execute(
            'SELECT event_id,section,subsection,confidence,status,am_stage FROM ml_predictions')}
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        for sequence, payload in raw.execute('SELECT sequence,payload FROM events ORDER BY sequence'):
            event = json.loads(payload)
            label = label_by_id.get(event['event_id'])
            prediction = predictions.get(event['event_id'])
            if label:
                section = label['attack_section']
                subsection = label['attack_subsection']
                technique = label.get('mitre_technique','')
                mapping = label.get('mitre_mapping_status','') or 'no_verified_mapping'
                origin = 'controlled_scenario_label'
                confidence = ''
                ml_status = ''
                stage = label.get('am_stage','') or describe_event(event)['process_mapping']
            elif prediction:
                section, subsection, confidence, ml_status, stage = prediction
                technique = subsection_ids.get(subsection, '')
                mapping = ('model_suggestion_requires_review' if technique else
                           'not_applicable' if section == 'Benign' else
                           'no_verified_mapping')
                origin = 'model_advisory'
            else:
                section, subsection, confidence, ml_status = 'Unknown', 'Not classified', '', ''
                stage = describe_event(event)['process_mapping']
                technique, mapping, origin = '', 'not_classified', 'raw_event'
            writer.writerow({'sequence': sequence, 'event_id': event['event_id'],
                'timestamp': event['timestamp'], 'instance_id': event['instance_id'],
                'platform': event['platform'], 'source': event['source'],
                'event_type': event['event_type'], 'action': event['action'],
                'outcome': event['outcome'],
                'metadata_json': json.dumps(event.get('metadata',{}),ensure_ascii=False,separators=(',',':')),
                'label_origin': origin, 'attack_section': section,
                'attack_subsection': subsection, 'ml_confidence': confidence,
                'ml_status': ml_status, 'am_stage': stage,
                'mitre_suggested_id': technique, 'mitre_mapping_status': mapping})
            count += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--analysis', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(export(args.evidence,args.analysis,args.labels,args.output))

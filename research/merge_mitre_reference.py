"""Join HoneyPrint scenario labels to pinned official Enterprise/ICS ATT&CK data."""

import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path


def external_ref(obj):
    return next((ref for ref in obj.get('external_references', ())
                 if ref.get('source_name') == 'mitre-attack' and ref.get('external_id')), None)


def catalog(paths):
    techniques = {}
    sources = []
    for domain, path in paths:
        raw = Path(path).read_bytes()
        objects = json.loads(raw)['objects']
        tactics = {}
        for obj in objects:
            if obj['type'] == 'x-mitre-tactic' and not obj.get('revoked') and not obj.get('x_mitre_deprecated'):
                ref = external_ref(obj)
                if ref:
                    tactics[obj['x_mitre_shortname']] = (ref['external_id'], obj['name'])
        count = 0
        for obj in objects:
            if obj['type'] != 'attack-pattern' or obj.get('revoked') or obj.get('x_mitre_deprecated'):
                continue
            ref = external_ref(obj)
            if not ref:
                continue
            phases = [tactics.get(phase['phase_name'], ('', phase['phase_name']))
                      for phase in obj.get('kill_chain_phases', ())]
            technique = {'domain': domain, 'technique_id': ref['external_id'],
                         'technique_name': obj['name'], 'technique_url': ref.get('url', ''),
                         'tactic_ids': ';'.join(dict.fromkeys(value[0] for value in phases if value[0])),
                         'tactic_names': ';'.join(dict.fromkeys(value[1] for value in phases)),
                         'is_subtechnique': bool(obj.get('x_mitre_is_subtechnique', False)),
                         'description': obj.get('description', '')}
            techniques[(domain, ref['external_id'])] = technique
            count += 1
        sources.append({'domain': domain, 'file': str(Path(path).resolve()),
                        'sha256': sha256(raw).hexdigest(), 'active_techniques': count,
                        'version': '20.1'})
    return techniques, sources


def merge(training, labels, paths, output):
    techniques, sources = catalog(paths)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with Path(labels).open(newline='') as source:
        labels_by_id = {row['event_id']: row for row in csv.DictReader(source)}
    with Path(training).open(newline='') as source:
        reader = csv.DictReader(source)
        training_columns = list(reader.fieldnames or ())
        rows = list(reader)
    mitre_columns = ['mitre_domain', 'mitre_scenario_technique_id',
                     'mitre_technique_name', 'mitre_tactic_ids', 'mitre_tactic_names',
                     'mitre_technique_url', 'mitre_mapping_status', 'mitre_label_basis']
    counts = Counter()
    for row in rows:
        label = labels_by_id.get(row['event_id'], {})
        candidate_id = label.get('mitre_technique', '')
        status = label.get('mitre_mapping_status', '')
        if row['label'] == 'benign':
            status = 'not_applicable'
        elif not candidate_id:
            status = 'no_verified_mapping'
        elif status == 'scenario_level_mapping':
            status = 'scenario_level_mapping_requires_review'
        elif status == 'research_hypothesis_not_verified':
            pass
        else:
            status = 'review_needed'
        matched = techniques.get(('enterprise', candidate_id)) or techniques.get(('ics', candidate_id))
        if candidate_id and not matched:
            raise ValueError(f'Label uses an unknown or inactive ATT&CK ID: {candidate_id}')
        row.update({'mitre_domain': matched['domain'] if matched else '',
                    'mitre_scenario_technique_id': candidate_id,
                    'mitre_technique_name': matched['technique_name'] if matched else '',
                    'mitre_tactic_ids': matched['tactic_ids'] if matched else '',
                    'mitre_tactic_names': matched['tactic_names'] if matched else '',
                    'mitre_technique_url': matched['technique_url'] if matched else '',
                    'mitre_mapping_status': status,
                    'mitre_label_basis': ('controlled_scenario_intent' if label else
                                          'allowlisted_normal_ui_traffic')})
        counts[status] += 1
    with (output / 'training_ready.csv').open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=training_columns + mitre_columns)
        writer.writeheader()
        writer.writerows(rows)
    with (output / 'mitre_techniques.csv').open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=list(next(iter(techniques.values()))))
        writer.writeheader()
        writer.writerows(sorted(techniques.values(), key=lambda item:(item['domain'], item['technique_id'])))
    review = Counter((row['attack_section'], row['attack_subsection']) for row in rows
                     if row['mitre_mapping_status'] in ('no_verified_mapping', 'research_hypothesis_not_verified'))
    with (output / 'mapping_review_queue.csv').open('w', newline='') as destination:
        writer = csv.writer(destination)
        writer.writerow(('attack_section', 'attack_subsection', 'row_count', 'review_reason'))
        for (section, subsection), count in sorted(review.items()):
            writer.writerow((section, subsection, count, 'Needs event-level analyst evidence before ATT&CK mapping'))
    summary = {'training_rows': len(rows), 'mapping_status_counts': dict(counts),
               'technique_reference_rows': len(techniques), 'sources': sources,
               'caveat': ('ATT&CK is a technique reference, not labelled HoneyPrint logs. '
                          'Scenario-level IDs are candidates for analyst review, not proof that every row used a technique.')}
    (output / 'mapping_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--enterprise', type=Path, required=True)
    parser.add_argument('--ics', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(merge(args.training, args.labels,
                           [('enterprise', args.enterprise), ('ics', args.ics)], args.output), indent=2))

"""Update HoneyPrint's ATT&CK reference from MITRE's official STIX data, and check its labels.

Source: https://github.com/mitre-attack/attack-stix-data/tree/master/enterprise-attack
(the data attack.mitre.org is built from). Writes, in configs/reference/:

* mitre_techniques.csv   every active Enterprise technique and sub-technique: name, URL,
                         tactics, description (ICS rows already in the file are kept)
* mitre_mitigations.csv  technique -> mitigations (M####) and how each one helps
* mitre_detections.csv   technique -> detection strategies (DET####)
* mitre_technique_users.csv  groups, campaigns and software that used HoneyPrint's techniques
                         (background only: never a claim that they attacked the honeypot)
* attack_version.txt     the ATT&CK version

Then every HoneyPrint label (dashboard/taxonomy.py LABELS) is checked: it must still exist,
not be deprecated or revoked (the replacement is named), and still sit under its tactic.

    ./.venv/bin/python research/update_attack_reference.py                 # latest version
    ./.venv/bin/python research/update_attack_reference.py --version 20.1  # a specific version
    ./.venv/bin/python research/update_attack_reference.py --source enterprise-attack.json --check-only
"""

import argparse
import csv
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REFERENCE = ROOT / 'configs' / 'reference'
BASE_URL = 'https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/'
TECHNIQUE_COLUMNS = ['domain', 'technique_id', 'technique_name', 'technique_url', 'tactic_ids',
                     'tactic_names', 'is_subtechnique', 'description']


def attack_id(obj):
    for ref in obj.get('external_references', ()):
        if ref.get('source_name') == 'mitre-attack' and ref.get('external_id'):
            return ref['external_id'], ref.get('url', '')
    return None, ''


def active(obj):
    return not obj.get('revoked') and not obj.get('x_mitre_deprecated')


def load(source, version):
    if source:
        return json.loads(Path(source).read_text(encoding='utf-8'))
    name = f'enterprise-attack-{version}.json' if version else 'enterprise-attack.json'
    print(f'Downloading {BASE_URL}{name}', flush=True)
    with urllib.request.urlopen(BASE_URL + name, timeout=300) as response:
        return json.load(response)


def extract(bundle):
    objects = bundle['objects']
    by_ref = {obj['id']: obj for obj in objects}
    collection = next(obj for obj in objects if obj['type'] == 'x-mitre-collection')
    tactics = {obj['x_mitre_shortname']: attack_id(obj)[0] + '\t' + obj['name']
               for obj in objects if obj['type'] == 'x-mitre-tactic' and active(obj)}
    techniques, status = [], {}
    for obj in objects:
        if obj['type'] != 'attack-pattern':
            continue
        ident, url = attack_id(obj)
        if not ident:
            continue
        status[ident] = obj
        if not active(obj):
            continue
        phases = [tactics[p['phase_name']].split('\t') for p in obj.get('kill_chain_phases', ())
                  if p.get('kill_chain_name') == 'mitre-attack' and p['phase_name'] in tactics]
        techniques.append({'domain': 'enterprise', 'technique_id': ident, 'technique_name': obj['name'],
                           'technique_url': url, 'tactic_ids': ';'.join(t[0] for t in phases),
                           'tactic_names': ';'.join(t[1] for t in phases),
                           'is_subtechnique': str(bool(obj.get('x_mitre_is_subtechnique'))),
                           'description': obj.get('description', '')})
    techniques.sort(key=lambda row: row['technique_id'])

    revoked_by, mitigations, detections, users = {}, [], [], []
    for rel in objects:
        if rel['type'] != 'relationship' or not active(rel):
            continue
        source, target = by_ref.get(rel['source_ref']), by_ref.get(rel['target_ref'])
        if not source or not target:
            continue
        kind = rel['relationship_type']
        if kind == 'revoked-by' and source['type'] == 'attack-pattern':
            revoked_by[attack_id(source)[0]] = attack_id(target)[0]
        if target['type'] != 'attack-pattern' or not active(target) or not active(source):
            continue
        technique = attack_id(target)[0]
        ident, url = attack_id(source)
        if kind == 'mitigates' and source['type'] == 'course-of-action':
            mitigations.append({'technique_id': technique, 'mitigation_id': ident, 'mitigation_name': source['name'],
                                'mitigation_url': url, 'how': ' '.join(rel.get('description', '').split())[:500]})
        elif kind == 'detects' and source['type'] == 'x-mitre-detection-strategy':
            detections.append({'technique_id': technique, 'detection_id': ident, 'detection_name': source['name'],
                               'detection_url': url})
        elif kind == 'uses' and source['type'] in ('intrusion-set', 'campaign', 'malware', 'tool'):
            users.append({'technique_id': technique, 'user_type': {'intrusion-set': 'group'}.get(source['type'], source['type']),
                          'user_id': ident, 'user_name': source['name'], 'user_url': url})
    key = lambda row: tuple(row.values())
    return {'version': collection.get('x_mitre_version', ''), 'techniques': techniques, 'status': status,
            'revoked_by': revoked_by, 'mitigations': sorted(mitigations, key=key),
            'detections': sorted(detections, key=key), 'users': sorted(users, key=key)}


def check_labels(data):
    """Problems with HoneyPrint's labels in this ATT&CK version (empty list = all good)."""
    from dashboard.taxonomy import ENTERPRISE_TACTICS, LABELS
    tactic_names = dict(ENTERPRISE_TACTICS)
    current = {row['technique_id']: row for row in data['techniques']}
    problems = []
    for label, (tactic_id, _, _) in sorted(LABELS.items()):
        row = current.get(label)
        if row is None:
            obj = data['status'].get(label)
            if obj is None:
                problems.append(f'{label}: no longer exists in ATT&CK')
            elif obj.get('revoked'):
                problems.append(f"{label} ({obj['name']}): revoked, replaced by {data['revoked_by'].get(label, '?')}")
            else:
                problems.append(f"{label} ({obj['name']}): deprecated by MITRE")
        elif tactic_id not in row['tactic_ids'].split(';'):
            problems.append(f"{label} ({row['technique_name']}): no longer under {tactic_names.get(tactic_id, tactic_id)}; "
                            f"now under {row['tactic_names']}")
    return problems


def write(path, rows, columns):
    with path.open('w', newline='', encoding='utf-8') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', help='Local enterprise-attack.json instead of downloading')
    parser.add_argument('--version', help='ATT&CK version to download, e.g. 20.1 (default: latest)')
    parser.add_argument('--output-dir', type=Path, default=REFERENCE)
    parser.add_argument('--check-only', action='store_true', help='Only check HoneyPrint labels; write nothing')
    args = parser.parse_args(argv)

    data = extract(load(args.source, args.version))
    print(f"ATT&CK Enterprise v{data['version']}: {len(data['techniques'])} active techniques, "
          f"{len(data['mitigations'])} mitigation links, {len(data['detections'])} detection links", flush=True)
    problems = check_labels(data)
    from dashboard.taxonomy import LABELS
    if problems:
        print(f'{len(problems)} HoneyPrint label(s) need attention:')
        for problem in problems:
            print('  -', problem)
    else:
        print(f'All {len(LABELS)} HoneyPrint labels are current ATT&CK techniques under their tactics.')
    if args.check_only:
        return 1 if problems else 0

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    existing = out / 'mitre_techniques.csv'
    others = []
    if existing.is_file():
        with existing.open(newline='', encoding='utf-8') as source:
            others = [row for row in csv.DictReader(source) if row['domain'] != 'enterprise']
    # Write to temporary files first, then replace, so a failure never leaves a half-written reference.
    staged = Path(tempfile.mkdtemp(prefix='attack-', dir=out))
    try:
        write(staged / 'mitre_techniques.csv', data['techniques'] + others, TECHNIQUE_COLUMNS)
        write(staged / 'mitre_mitigations.csv', data['mitigations'],
              ['technique_id', 'mitigation_id', 'mitigation_name', 'mitigation_url', 'how'])
        write(staged / 'mitre_detections.csv', data['detections'],
              ['technique_id', 'detection_id', 'detection_name', 'detection_url'])
        honeypot = {label.split('.')[0] for label in LABELS} | set(LABELS)
        write(staged / 'mitre_technique_users.csv', [row for row in data['users'] if row['technique_id'] in honeypot],
              ['technique_id', 'user_type', 'user_id', 'user_name', 'user_url'])
        (staged / 'attack_version.txt').write_text(data['version'] + '\n')
        for file in staged.iterdir():
            file.replace(out / file.name)
    finally:
        for file in staged.iterdir():
            file.unlink()
        staged.rmdir()
    print(f'Wrote ATT&CK v{data["version"]} reference to {out}')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())

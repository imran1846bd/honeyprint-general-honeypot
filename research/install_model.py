"""Validate a trained HoneyPrint model ZIP and make it the active dashboard model.

    ./.venv/bin/python research/install_model.py ~/Downloads/HoneyPrint_Model_v4.zip

Checks the archive with the same loader the live ML worker uses, confirms every
predicted label exists in dashboard/taxonomy.py, backs up the current
.honeyprint/models/active.zip to previous.zip and installs the new one. Restart
./run.sh afterwards; the worker then re-scores stored interactions.
Only install archives you trained yourself: .joblib files can execute code.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.taxonomy import to_label
from telemetry.analysis.ml_service import load_model

MODELS = ROOT / '.honeyprint' / 'models'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--dry-run', action='store_true', help='Validate only; do not install')
    args = parser.parse_args(argv)
    model, metadata, model_id = load_model(args.archive)
    labels = [str(label) for label in model.classes_]
    unknown = sorted(label for label in labels if not to_label(label))
    if unknown:
        parser.error(f'Model predicts labels with no MITRE mapping in dashboard/taxonomy.py: {unknown}')
    with ZipFile(args.archive) as package:
        metrics = json.loads(package.read('metrics.json')) if 'metrics.json' in package.namelist() else {}
    print(json.dumps({'model_id': model_id, 'model': metadata.get('attack_model'),
                      'dataset_version': metadata.get('dataset_version'),
                      'gate': metadata['attack_review_gate'], 'labels': labels,
                      'trained_event_types': metadata.get('trained_event_types'),
                      'share_above_gate': metrics.get('share_above_gate'),
                      'accuracy_above_gate': metrics.get('accuracy_above_gate'),
                      'routine_accuracy': metrics.get('routine_accuracy'),
                      'routine_raised_severity_rate': metrics.get('routine_raised_severity_rate'),
                      'attack_accuracy': metrics.get('attack_accuracy')}, indent=2))
    if args.dry_run:
        return
    MODELS.mkdir(parents=True, exist_ok=True)
    active = MODELS / 'active.zip'
    if active.is_file():
        shutil.copy2(active, MODELS / 'previous.zip')
        print('Backed up current model to', MODELS / 'previous.zip')
    shutil.copy2(args.archive, active)
    print('Installed', active, '- restart ./run.sh to use it.')


if __name__ == '__main__':
    main()

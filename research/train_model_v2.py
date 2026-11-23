"""Train a local advisory attack model from the reviewed HoneyPrint CSV."""

import argparse
import json
from pathlib import Path
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

import joblib
import pandas as pd
from catboost import CatBoostClassifier


CATEGORICAL = ['source', 'event_type', 'action', 'outcome', 'route_family',
               'command', 'gcode', 'method']
NUMERIC = ['http_status', 'command_length', 'target_c', 'parameter_x',
           'parameter_y', 'parameter_z', 'parameter_e', 'parameter_f',
           'parameter_s', 'payload_bytes', 'artifact_prior_versions',
           'recent_404_60s', 'recent_auth_reject_60s']


def train(dataset, output):
    data = pd.read_csv(dataset, keep_default_na=False)
    data = data[data['split'] != 'review_only'].copy()
    if data['event_id'].duplicated().any():
        raise ValueError('Training data contains duplicate event IDs')
    if len(data) < 1000 or not {'custom_lab', 'octoprint', 'klipper'} <= set(data['platform']):
        raise ValueError('Training data lacks required platform coverage')
    for key in CATEGORICAL:
        data[key] = data[key].fillna('').astype(str)
    for key in NUMERIC:
        data[key] = pd.to_numeric(data[key], errors='coerce')
    mapping = data.groupby('attack_subsection')['attack_section'].nunique()
    if mapping.max() != 1:
        raise ValueError('One subsection maps to multiple sections')
    sections = data[['attack_subsection', 'attack_section']].drop_duplicates().set_index('attack_subsection')['attack_section'].to_dict()
    model = CatBoostClassifier(iterations=350, depth=5, learning_rate=0.04,
        l2_leaf_reg=5, loss_function='MultiClass', auto_class_weights='Balanced',
        cat_features=CATEGORICAL, random_seed=42, verbose=False,
        allow_writing_files=False)
    model.fit(data[CATEGORICAL + NUMERIC], data['attack_subsection'].astype(str))
    statuses = {}
    for subsection, section in sections.items():
        statuses[subsection] = ('not_applicable' if section == 'Benign' else
            'research_hypothesis_not_verified' if 'candidate' in section.lower() else
            'no_verified_mitre_mapping' if section.startswith('AM-specific') else
            'scenario_level_mapping_requires_review')
    metadata = {'attack_model': 'CatBoostClassifier', 'features': CATEGORICAL + NUMERIC,
        'categorical': CATEGORICAL, 'numeric': NUMERIC,
        'trained_event_types': sorted(data['event_type'].unique()),
        'section_by_subsection': sections, 'attack_review_gate': 0.95,
        'mitre_status_by_subsection': statuses,
        'training_rows': len(data),
        'label_caveat': 'Controlled scenario intent and allowlisted normal UI traffic; not independently verified attack intent.'}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        model_path = Path(folder) / 'attack_subsection.joblib'
        joblib.dump(model, model_path)
        with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
            archive.write(model_path, 'attack_subsection.joblib')
            archive.writestr('model_metadata.json', json.dumps(metadata, indent=2))
    return {'output': str(output), 'training_rows': len(data),
            'trained_event_types': metadata['trained_event_types']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(train(args.dataset, args.output), indent=2))

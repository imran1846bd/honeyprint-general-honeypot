"""Write HoneyPrint_ATTACK_Training_v9.ipynb (run: python research/make_colab_notebook.py OUT.ipynb)."""

import json
import sys
from pathlib import Path

MD, CODE = 'markdown', 'code'
CELLS = [
(MD, """# HoneyPrint ATT&CK Enterprise v9 — classifier training (Google Colab)

**Run every cell from top to bottom** (Runtime → Run all works too).

1. When asked, upload **`honeyprint_attack_v9.csv`** and **`attack_taxonomy.csv`** from the `HoneyPrint_ATTACK_Enterprise_v9` folder.
2. The notebook compares Logistic Regression and CatBoost on held-out runs and on each held-out platform, picks the better model, and sets the confidence gate.
3. The last cell downloads **`HoneyPrint_ATTACK_Model_v9.zip`**. Send that ZIP back — it plugs straight into the HoneyPrint dashboard.

The model predicts a **MITRE ATT&CK Enterprise technique / sub-technique ID** (v20.1) for every interaction. A honeypot has no legitimate users, so routine use also gets its closest technique (status → T1082 System Information Discovery, upload → T1105 Ingress Tool Transfer, print command → T1059 Command and Scripting Interpreter); those rows have `label = routine`. The dashboard shows it under one of the 15 Enterprise tactics as *tactic → technique › sub-technique*, as on attack.mitre.org. The **AM process mapping** is not learned: the dashboard maps every event to a HoneyPrint component and its AM stage from the component reference table, so it stays exact for new event types."""),
(CODE, """# Same versions as the HoneyPrint project, so the saved model loads on the Mac.
%pip install -q numpy==2.5.5 pandas==3.1.1 scikit-learn==1.9.1 catboost==1.2.10 joblib==1.6.0 matplotlib"""),
(MD, "## Step 1 — Upload the dataset"),
(CODE, """from pathlib import Path
import os
try:
    from google.colab import files
    IN_COLAB = True
except ImportError:
    IN_COLAB = False
DATA_DIR = Path(os.environ.get('HONEYPRINT_DATA_DIR', '/content/honeyprint-data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
if IN_COLAB:
    uploaded = files.upload()
    for name, content in uploaded.items():
        (DATA_DIR / name).write_bytes(content)
for name in ('honeyprint_attack_v9.csv', 'attack_taxonomy.csv'):
    assert (DATA_DIR / name).is_file(), f'Upload {name} from HoneyPrint_ATTACK_Enterprise_v9'
print('Data folder:', DATA_DIR, sorted(p.name for p in DATA_DIR.iterdir()))"""),
(MD, "## Step 2 — Load and inspect"),
(CODE, """import json
from collections import defaultdict
import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

MIN_CLASS_ROWS = 10
df = pd.read_csv(DATA_DIR / 'honeyprint_attack_v9.csv', keep_default_na=False, dtype=str)
taxonomy = pd.read_csv(DATA_DIR / 'attack_taxonomy.csv', keep_default_na=False, dtype=str)
data = df[df['split'] != 'review_only'].copy()
assert data['event_id'].is_unique
counts = data['mitre_label'].value_counts()
RARE = sorted(counts[counts < MIN_CLASS_ROWS].index)
data = data[~data['mitre_label'].isin(RARE)].copy()
# octoprint-main is an OctoPrint variant; evaluate it with OctoPrint.
data['platform_group'] = data['platform'].replace({'octoprint-main': 'octoprint'})
print('Rows:', len(data), '| labels with too few rows (the dashboard rules cover them):', RARE)
print(data['mitre_tactic'].value_counts().to_string())
print()
print(pd.crosstab(data['mitre_label'] + '  ' + data['mitre_technique'], data['platform_group']).to_string())"""),
(CODE, """# Only observed features are model inputs. IDs, platform, run, split and every label column stay out.
# This order must match FEATURES_V4 in telemetry/analysis/ml_service.py.
# 'source' is left out on purpose: it mostly names the platform, and the model used it as a shortcut.
CATEGORICAL = ['event_type', 'action', 'outcome', 'route_family', 'command', 'gcode', 'method', 'intent']
NUMERIC = ['http_status', 'command_length', 'target_c', 'parameter_x', 'parameter_y',
           'parameter_z', 'parameter_e', 'parameter_f', 'parameter_s',
           'payload_bytes', 'artifact_prior_versions', 'recent_404_60s', 'recent_auth_reject_60s',
           'recent_auth_users_60s']
FEATURES = CATEGORICAL + NUMERIC
TARGET = 'mitre_label'
for col in CATEGORICAL: data[col] = data[col].fillna('').astype(str)
for col in NUMERIC: data[col] = pd.to_numeric(data[col], errors='coerce')

BASELINE = Pipeline([
    ('prepare', ColumnTransformer([
        ('category', OneHotEncoder(handle_unknown='ignore'), CATEGORICAL),
        ('number', Pipeline([('fill', SimpleImputer(strategy='median', keep_empty_features=True)),
                             ('scale', StandardScaler(with_mean=False))]), NUMERIC)])),
    ('classify', LogisticRegression(max_iter=3000, class_weight='balanced'))])

def make_model(name):
    if name == 'Logistic Regression':
        return clone(BASELINE)
    # One-hot categories keep CatBoost fast on a normal Colab CPU (about a minute per model).
    return CatBoostClassifier(iterations=500, depth=6, learning_rate=0.2, l2_leaf_reg=5,
                              loss_function='MultiClass', cat_features=CATEGORICAL,
                              one_hot_max_size=255, random_seed=42,
                              verbose=10, allow_writing_files=False)  # progress every 10 trees

def predict(model, X):
    proba = np.asarray(model.predict_proba(X))
    classes = np.asarray(model.classes_).astype(str)
    return classes[proba.argmax(axis=1)], proba.max(axis=1)

CANDIDATES = ['Logistic Regression', 'CatBoost']

# Progress: one bar step per trained model (held-out runs, each held-out platform, final model).
import time
try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None
TOTAL_FITS = len(CANDIDATES) * (1 + data['platform_group'].nunique()) + 1
STARTED = time.time()
bar = tqdm(total=TOTAL_FITS, desc='Training', unit='model') if tqdm else None
done_fits = 0

def fit(model, X, y, label):
    global done_fits
    print(f'\\n▶ [{done_fits + 1}/{TOTAL_FITS}] {label} — {len(X):,} rows')
    start = time.time()
    model.fit(X, y)
    done_fits += 1
    elapsed = time.time() - STARTED
    left = elapsed / done_fits * (TOTAL_FITS - done_fits)
    print(f'✔ {label} took {time.time() - start:.0f}s | total {elapsed / 60:.1f} min | about {left / 60:.1f} min left')
    if bar: bar.update(1)
    return model"""),
(MD, """## Step 3 — Held-out runs (no event from a test run is seen in training)"""),
(CODE, """train = data[data['split'] == 'train']
hold = data[data['split'].isin(['test', 'validation'])]
assert set(train['run_group']).isdisjoint(hold['run_group'])
results, correct_conf, fitted = [], {}, {}
for name in CANDIDATES:
    model = fit(make_model(name), train[FEATURES], train[TARGET], f'{name} (held-out runs)')
    guess, conf = predict(model, hold[FEATURES])
    truth = hold[TARGET].to_numpy()
    fitted[name] = (model, guess, conf)
    correct_conf[name] = conf[guess == truth]
    results.append({'model': name, 'accuracy': round(float((guess == truth).mean()), 4),
                    'macro_F1': round(f1_score(truth, guess, average='macro', zero_division=0), 4)})
run_table = pd.DataFrame(results)
print(run_table.to_string(index=False))"""),
(MD, """## Step 4 — Held-out platform (train on two platforms, test on the third)

Shows how well the model transfers to an adapter it has never seen. Labels that exist on only one platform cannot be learned in that fold; they are listed."""),
(CODE, """platform_rows = []
for held in sorted(data['platform_group'].unique()):
    tr, te = data[data['platform_group'] != held], data[data['platform_group'] == held]
    unseen = sorted(set(te[TARGET]) - set(tr[TARGET]))
    known = te[~te[TARGET].isin(unseen)]
    for name in CANDIDATES:
        model = fit(make_model(name), tr[FEATURES], tr[TARGET], f'{name} (without {held})')
        guess, _ = predict(model, known[FEATURES])
        platform_rows.append({'held_out_platform': held, 'model': name,
                              'macro_F1_on_learnable_labels': round(f1_score(known[TARGET], guess, average='macro', zero_division=0), 4),
                              'labels_never_seen_in_training': ', '.join(unseen)})
platform_table = pd.DataFrame(platform_rows)
print(platform_table.to_string(index=False))"""),
(MD, "## Step 5 — Choose the model and confidence gate"),
(CODE, """score = (run_table.set_index('model')['macro_F1']
         + platform_table.groupby('model')['macro_F1_on_learnable_labels'].mean()) / 2
SELECTED = score.idxmax()
# Gate = 10th percentile of confidence on correct held-out predictions, bounded to [0.60, 0.95].
GATE = float(np.clip(np.quantile(correct_conf[SELECTED], 0.10), 0.60, 0.95))
model, guess, conf = fitted[SELECTED]
truth = hold[TARGET].to_numpy()
above = conf >= GATE
print('Combined score:', score.round(4).to_dict())
print('Selected:', SELECTED, '| gate:', round(GATE, 3))
print(f'{above.mean():.1%} of held-out events pass the gate; accuracy there {(guess[above] == truth[above]).mean():.4f}')
print()
print(classification_report(truth, guess, zero_division=0))"""),
(CODE, """# Routine use: is it given its own technique, and is it ever raised to a more severe one?
RANK = {'info': 0, 'low': 1, 'medium': 2, 'high': 3}
severity = taxonomy.set_index('label')['severity'].map(RANK).to_dict()
routine = hold['label'].to_numpy() == 'routine'
raised = above & (np.vectorize(lambda g: severity.get(g, 0))(guess) > np.vectorize(lambda t: severity.get(t, 0))(truth))
print(f'Routine events: {routine.sum()} | correct technique: {(guess[routine] == truth[routine]).mean():.2%}')
print(f'Routine events raised to a more severe technique (after gate): {raised[routine].mean():.2%}')
print(f'Attack events: {(~routine).sum()} | correct technique: {(guess[~routine] == truth[~routine]).mean():.2%}')
per_type = (pd.DataFrame({'event_type': hold['event_type'].to_numpy(), 'ok': guess == truth})
            .groupby('event_type')['ok'].agg(['mean', 'count']).rename(columns={'mean': 'accuracy'}))
print(per_type.sort_values('accuracy').round(3).to_string())"""),
(CODE, """import matplotlib.pyplot as plt
labels = sorted(set(truth) | set(guess))
matrix = confusion_matrix(truth, guess, labels=labels)
fig, ax = plt.subplots(figsize=(11, 9))
ax.imshow(np.log1p(matrix), cmap='Blues')
short = [l[:28] for l in labels]
ax.set_xticks(range(len(labels)), short, rotation=70, ha='right', fontsize=8)
ax.set_yticks(range(len(labels)), short, fontsize=8)
for i in range(len(labels)):
    for j in range(len(labels)):
        if matrix[i, j]: ax.text(j, i, matrix[i, j], ha='center', va='center', fontsize=7)
ax.set_xlabel('Predicted'); ax.set_ylabel('True'); ax.set_title(f'Held-out runs — {SELECTED}')
plt.tight_layout()
MODEL_DIR = DATA_DIR / 'HoneyPrint_ATTACK_Model_v9'
MODEL_DIR.mkdir(exist_ok=True)
fig.savefig(MODEL_DIR / 'confusion_matrix.png', dpi=130)
plt.show()"""),
(MD, "## Step 6 — Final model on all rows, then save"),
(CODE, """final = fit(make_model(SELECTED), data[FEATURES], data[TARGET], f'{SELECTED} (final, all rows)')
if bar: bar.close()
print(f'Training finished in {(time.time() - STARTED) / 60:.1f} min')
section_by_subsection = (data[['mitre_label', 'mitre_tactic']].drop_duplicates()
                         .set_index('mitre_label')['mitre_tactic'].to_dict())
assert data.groupby('mitre_label')['mitre_tactic'].nunique().max() <= 1
tax = taxonomy.set_index('label')

def explain(example):
    row = pd.DataFrame([{**{c: str(example.get(c, '')) for c in CATEGORICAL},
                         **{c: example.get(c, np.nan) for c in NUMERIC}}])
    for c in NUMERIC: row[c] = pd.to_numeric(row[c], errors='coerce')
    label, confidence = predict(final, row[FEATURES])
    label, confidence = label[0], float(confidence[0])
    info = tax.loc[label] if label in tax.index else {}
    return {'mitre_label': label, 'confidence': round(confidence, 3),
            'above_gate': confidence >= GATE, 'tactic': info.get('section', ''),
            'technique': info.get('subsection', '')}

print(explain({'source': 'ssh', 'event_type': 'ssh.auth', 'action': 'password', 'outcome': 'rejected', 'recent_auth_reject_60s': 5}))
print(explain({'source': 'ssh', 'event_type': 'ssh.command', 'action': 'execute', 'outcome': 'observed', 'command': 'heat', 'target_c': 420}))
print(explain({'source': 'klipper', 'event_type': 'printer.command', 'action': 'submit', 'outcome': 'observed', 'gcode': 'G28'}))
print(explain({'source': 'telnet', 'event_type': 'telnet.command', 'action': 'execute', 'outcome': 'observed', 'command': 'netstat'}))
print(explain({'source': 'ssh', 'event_type': 'ssh.command', 'action': 'execute', 'outcome': 'observed', 'command': 'cat', 'intent': 'read_shadow'}))"""),
(CODE, """joblib.dump(final, MODEL_DIR / 'attack_subsection.joblib')
metadata = {
    'attack_model': SELECTED, 'dataset_version': 9, 'label_scheme': 'mitre_attack_v20.1', 'features': FEATURES,
    'categorical': CATEGORICAL, 'numeric': NUMERIC,
    'trained_event_types': sorted(data['event_type'].unique()),
    'section_by_subsection': section_by_subsection, 'attack_review_gate': GATE,
    'taxonomy': taxonomy.set_index('label')[
        ['section', 'tactic_id', 'technique_id', 'technique', 'subtechnique_id', 'subtechnique',
         'mitre_domain', 'mitre_url', 'severity']].to_dict(orient='index'),
    'skipped_rare_labels': RARE, 'training_rows': int(len(data)),
    'rows_by_label': data[TARGET].value_counts().to_dict(),
    'label_caveat': ('Labels are MITRE ATT&CK v20.1 IDs from controlled scenarios and transparent rules; '
                     'a single event is an indicator, not proof of an adversary.')}
metrics = {'run_holdout': run_table.to_dict(orient='records'),
           'platform_holdout': platform_table.to_dict(orient='records'),
           'selected_model': SELECTED, 'gate': GATE,
           'share_above_gate': float(above.mean()),
           'accuracy_above_gate': float((guess[above] == truth[above]).mean()),
           'routine_accuracy': float((guess[routine] == truth[routine]).mean()),
           'routine_raised_severity_rate': float(raised[routine].mean()),
           'attack_accuracy': float((guess[~routine] == truth[~routine]).mean()),
           'per_class': {k: v for k, v in classification_report(truth, guess, output_dict=True, zero_division=0).items()
                         if isinstance(v, dict)}}
(MODEL_DIR / 'model_metadata.json').write_text(json.dumps(metadata, indent=2))
(MODEL_DIR / 'metrics.json').write_text(json.dumps(metrics, indent=2, default=float))
(MODEL_DIR / 'attack_taxonomy.csv').write_bytes((DATA_DIR / 'attack_taxonomy.csv').read_bytes())
import shutil
archive = shutil.make_archive(str(DATA_DIR / 'HoneyPrint_ATTACK_Model_v9'), 'zip', root_dir=MODEL_DIR)
print('Saved', archive, sorted(p.name for p in MODEL_DIR.iterdir()))"""),
(MD, "## Step 7 — Download and send back `HoneyPrint_ATTACK_Model_v9.zip`"),
(CODE, """if IN_COLAB:
    files.download(archive)"""),
(MD, """### Notes
* Held-out scores come from controlled local scenarios. They show the pipeline works; they do not prove real-world detection.
* Below the confidence gate the dashboard shows the transparent rule's MITRE label instead.
* Labels are MITRE ATT&CK v20.1 IDs. One event is an indicator; repetition (e.g. 3+ failed logins) decides the sub-technique.
* Never load a `.joblib` file you did not create yourself."""),
]


def notebook():
    cells = []
    for kind, text in CELLS:
        lines = text.splitlines(keepends=True)
        cell = {'cell_type': kind, 'metadata': {}, 'source': lines}
        if kind == CODE:
            cell.update(execution_count=None, outputs=[])
        cells.append(cell)
    return {'cells': cells, 'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
                                         'language_info': {'name': 'python'}, 'colab': {'provenance': []}},
            'nbformat': 4, 'nbformat_minor': 5}


def code_cells():
    """Code for a local test run (skips %pip lines)."""
    return '\n\n'.join('\n'.join(l for l in text.splitlines() if not l.startswith('%'))
                       for kind, text in CELLS if kind == CODE)


if __name__ == '__main__':
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'HoneyPrint_ATTACK_Training_v9.ipynb')
    target.write_text(json.dumps(notebook(), indent=1))
    print('Wrote', target)

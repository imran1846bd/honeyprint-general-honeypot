# HoneyPrint local research dataset

This experiment ran only against isolated HoneyPrint listeners on `127.0.0.1`.
The existing quickstart runtime and its evidence were left intact. The same
evidence database collected custom lab, OctoPrint and Klipper observations in
separate runs. The analyst dashboard can read it at `http://127.0.0.1:8210/`;
its **Download all logs (CSV)** action exports the complete raw evidence.

The dataset files are under `.honeyprint/datasets/local-pilot/`, with a visible
copy in `/Users/macairm3/Desktop/dataset/HoneyPrint/`:

| File | Purpose |
| --- | --- |
| `honeyprint-evidence.csv` | Exact CSV downloaded from the analyst dashboard. |
| `labels.csv` | Scenario labels keyed by `event_id`. |
| `labeled_events.csv` | Raw evidence joined to labels by `event_id`; only labelled rows. |
| `ml_features.csv` | Initial event and short-history features with train/holdout designation. |
| `dataset_summary.json` | Counts and experiment limitations. |

## What was simulated

| Scenario | Scenario label | ATT&CK mapping |
| --- | --- | --- |
| Routine UI, console, MQTT and G-code uploads | Benign | None |
| Repeated file-path probes | Attack | [T1595.003 Wordlist Scanning](https://attack.mitre.org/techniques/T1595/003/) |
| Repeated rejected SSH, Telnet and MQTT passwords | Attack | [T1110.001 Password Guessing](https://attack.mitre.org/techniques/T1110/001/) |
| Replacing the same stored G-code file repeatedly | Attack intent | [T1565.001 Stored Data Manipulation](https://attack.mitre.org/techniques/T1565/001/) hypothesis |
| Routine printer control | Benign | None |
| High or invalid temperature setpoints | AM-specific attack intent | No exact ATT&CK technique asserted |
| Out-of-range virtual motion requests | AM-specific attack intent | No exact ATT&CK technique asserted |
| Invalid or rejected G-code uploads | AM-specific attack intent | No exact ATT&CK technique asserted |
| Host-shell command probes against the SSH decoy | Attempted misuse | No host shell was executed |
| Repeated reads of stored print jobs | Collection intent | No exfiltration claim |
| Unsupported MQTT printer commands | Attempted misuse | No exact ATT&CK technique asserted |
| Repeated print cancellation, custom lab | AM-specific interruption | No exact ATT&CK technique asserted |
| One virtual emergency stop, custom lab | AM-specific interruption | Review-only singleton, not a trainable class |

These are **controlled scenario-intent labels**. A single 404, failed login or
file upload does not prove an adversary used an ATT&CK technique. In particular,
routine and adversarial uploads can look identical in one event. Use the
`artifact_prior_versions` and time-window fields to study behavior across
events; review the original evidence before making incident claims.

The dashboard export includes platform background events and lifecycle records.
Those records are left **unlabelled**, rather than assigned a guessed class.
The joined dataset contains 10,420 labelled events from 32,927 raw records; 22,507
records remain unlabelled. The scenarios were chosen partly from the
`Final_list` sheet of the supplied `AM_Attack_SOK.xlsx`, especially its
G-code/toolpath, unauthorized file access, malformed-input, temperature,
motion, and production-sabotage entries. Sensor spoofing, audio/vibration
side channels, firmware replacement and physical part-quality effects were
not generated because this deployment lacks the observations needed to label
them honestly. These synthetic events form a pilot dataset, not a
representative real-world sample.

## Training notes

Run `research/prepare_training_dataset.py --dataset .honeyprint/datasets/local-pilot`
after exporting evidence and labels. It writes `training_ready.csv` and adds
`attack_section`, `attack_subsection`, `am_stage`, mapping status and `run_group`
to the labelled files. New generated scenarios carry a `run_id` so repeated
experiments remain separate groups. The two emergency-stop rows and 72 first
uploads in replacement scenarios are `review_only`: the first upload is not
yet a replacement. T1565.001 is marked as a research hypothesis and the model
target says "Impact candidate / review needed." The `split` column is an
example preset; `HoneyPrint_Model_Training.ipynb` instead holds out each of
custom lab, OctoPrint and Klipper in turn, with newest runs of the other two
reserved for validation. It compares Logistic Regression and CatBoost by
validation macro F1 before fitting the selected model on all three adapters.
No run crosses train, validation and held-out test within a fold.
`training_ready.csv` separates predictors from targets; exclude its event ID,
platform, run group and split fields from model inputs. Its stage
numbers come from reference rules, not manually verified attack localization.
Unmapped rows have a blank AM stage. MITRE mappings are scenario-level research
labels, not proof that an individual event is malicious. Collect fresh runs
on different days before making generalization claims.

The export computes `artifact_prior_versions` separately for each observed
upload channel and filename. This distinguishes a first upload from repeated
replacement without using a run-specific filename as a model feature. The
training CSV excludes IDs, platform, run group, scenario names and MITRE
fields from predictors. It still describes scripted local interactions; it
does not contain all possible browser actions or real-world attack families.
The dashboard CSV now includes opaque `session_id` values when the adapter
provides them, so an analyst can reconstruct captured console sessions.
Session ID is never a model predictor. Browser capture covers supported clicks
and selected input changes; a missing UI event must not be treated as proof
that no interaction occurred.

The `research/generate_local_dataset.py` and
`research/generate_extended_dataset.py` clients perform the local scenarios
against one already-running isolated HoneyPrint instance. It reads a synthetic
password from `HONEYPRINT_RESEARCH_PASSWORD`, records event IDs from the
instance's SQLite database, and appends `labels.csv`. It never reads or writes
the normal quickstart database. `research/export_labeled_dataset.py` joins
those labels to a fresh dashboard CSV export and reports missing IDs.

During the high-setpoint scenario, the simulated Klipper MCU shut down and
Moonraker lost the ready connection. That is an observed **virtual runtime**
outcome, not evidence that the same request would damage a physical printer.
The remaining Klipper scenarios were collected after an isolated restart.

To reopen the read-only dashboard after it has stopped, from the project root:

```sh
PYTHONPATH=. ./.venv/bin/python -m dashboard.server \
  --database .honeyprint/research-dataset/events.sqlite3 \
  --alerts .honeyprint/research-dataset/analysis.sqlite3 --port 8210
```

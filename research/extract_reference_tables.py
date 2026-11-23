"""Convert the HoneyPrint reference spreadsheets into small CSV tables.

* honey3dprint_.xlsx  -> configs/reference/component_am_mapping.csv
  (honeypot stage, component, AM process-stage mapping, justification, purpose)
* AM_Attack_SOK.xlsx  -> configs/reference/am_attack_literature.csv
  (systematisation-of-knowledge paper list used to cite AM attack categories)

Needs pandas + openpyxl (only for this one-off conversion):
    python research/extract_reference_tables.py --components honey3dprint_.xlsx --sok AM_Attack_SOK.xlsx
"""

import argparse
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[1] / 'configs' / 'reference'
STAGE_NAMES = {1: 'Exposure', 2: 'Access & Interaction', 3: 'Firmware Emulation',
               4: 'Physical Behavior Simulation', 5: 'Data Capture & Analysis'}


def components(path):
    rows = []
    for sheet, frame in pd.read_excel(path, sheet_name=None, header=None).items():
        stage = int(sheet.split()[1])
        header = frame.index[frame[0].astype(str).str.strip() == 'Component'][0]
        for _, row in frame.iloc[header + 1:].iterrows():
            if pd.isna(row[0]):
                continue
            mapping = str(row[1]).strip()
            primary = mapping.split(';')[0].strip()
            parts = [part.strip() for part in primary.split('>')]
            rows.append({
                'honeypot_stage': stage, 'honeypot_stage_name': STAGE_NAMES[stage],
                'component': str(row[0]).strip(), 'am_process_mapping': mapping,
                'primary_am_stage': parts[0] if len(parts) == 3 else 'N/A',
                'primary_am_layer': parts[1] if len(parts) == 3 else '',
                'primary_am_element': parts[2] if len(parts) == 3 else primary,
                'justification': str(row[2]).strip(), 'purpose': str(row[4]).strip(),
                'output_state_change': str(row[5]).strip()})
    return pd.DataFrame(rows)


def literature(path):
    frame = pd.read_excel(path, sheet_name='Final_list')
    frame = frame.rename(columns={'no': 'paper_no', 'Paper Name': 'paper', 'Where When Published': 'venue',
                                  'DOI': 'doi', 'Attacks Mentioned': 'attacks_mentioned',
                                  'Lifecycle stage': 'lifecycle_stage', 'Purdue Stage': 'purdue_level',
                                  'Threat Model': 'threat_model'})
    keep = ['paper_no', 'paper', 'venue', 'doi', 'attacks_mentioned', 'lifecycle_stage',
            'purdue_level', 'threat_model']
    return frame[keep].dropna(subset=['paper'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--components', type=Path, required=True)
    parser.add_argument('--sok', type=Path, required=True)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    table = components(args.components)
    table.to_csv(OUT / 'component_am_mapping.csv', index=False)
    papers = literature(args.sok)
    papers.to_csv(OUT / 'am_attack_literature.csv', index=False)
    print(f'{len(table)} components, {len(papers)} papers -> {OUT}')

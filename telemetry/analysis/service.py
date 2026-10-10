"""Incrementally derive local rule alerts from committed read-only evidence."""

import argparse
from hashlib import sha256
import math
from pathlib import Path
import sqlite3
import time

from telemetry.analysis.alerts import AlertStore
from telemetry.analysis.evidence import EvidenceReader
from telemetry.analysis.rules import classify_event, RULE_VERSION


class Analyzer:
    def __init__(self, evidence: EvidenceReader, alerts: AlertStore):
        self.evidence, self.alerts = evidence, alerts

    def process_once(self, *, limit=100):
        first = self.evidence.read_events(limit=1)
        if not first: return {'processed': 0, 'findings': 0}
        source = sha256(f'{self.evidence.path}:{first[0]["event"]["event_id"]}:{RULE_VERSION}'.encode()).hexdigest()
        cursor = self.alerts.cursor(source)
        rows = self.evidence.read_events(after_sequence=cursor, limit=limit)
        if not rows: return {'processed': 0, 'findings': 0}
        findings = [finding for row in rows for finding in classify_event(row['event'])]
        count = self.alerts.write_batch(findings, source=source, cursor=rows[-1]['sequence'])
        return {'processed': len(rows), 'findings': count}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Generate local rule findings without changing raw evidence.')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--alerts', type=Path, required=True)
    parser.add_argument('--watch', action='store_true', help='Continuously inspect new committed events')
    parser.add_argument('--interval', type=float, default=0.5)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 0.1 <= args.interval <= 60:
        parser.error('Interval must be between 0.1 and 60 seconds')
    try:
        if args.database.resolve() == args.alerts.resolve():
            raise ValueError('Raw evidence and derived alerts must use different files')
        args.alerts.parent.mkdir(parents=True, exist_ok=True)
        with EvidenceReader(args.database) as evidence, AlertStore(args.alerts) as alerts:
            analyzer = Analyzer(evidence, alerts)
            total = 0
            while True:
                result = analyzer.process_once()
                total += result['processed']
                if result['findings']:
                    print(f"Local alert: {result['findings']} new rule finding(s). Review context before classifying an attack.", flush=True)
                if not args.watch and result['processed'] == 0:
                    print(f'Analysis completed: {total} event(s) processed.')
                    return 0
                if args.watch and result['processed'] == 0:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print('Analysis failed. Check database paths and validated evidence records.')
        return 1


if __name__ == '__main__': raise SystemExit(main())

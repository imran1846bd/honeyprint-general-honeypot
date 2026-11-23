"""Incrementally derive local rule alerts from committed read-only evidence."""

import argparse
from collections import defaultdict, deque
from datetime import datetime
from hashlib import sha256
import math
from pathlib import Path
import sqlite3
import time

from telemetry.analysis.alerts import AlertStore
from telemetry.analysis.evidence import EvidenceReader
from telemetry.analysis.rules import classify_event, RULE_VERSION


SCAN_WINDOW_SECONDS = 60
SCAN_MIN_PORTS = 2


class Analyzer:
    def __init__(self, evidence: EvidenceReader, alerts: AlertStore):
        self.evidence, self.alerts = evidence, alerts
        self.last_high_count = 0
        # (instance, peer) -> recent (time, port) probes, for spotting port scans across services.
        self.probes = defaultdict(deque)
        self.reported = {}

    def port_scan(self, event):
        """A finding when one address probes several honeypot ports within a minute (T1046)."""
        if event.get('event_type') != 'network.connection':
            return ()
        metadata = event.get('metadata') or {}
        peer, port = metadata.get('peer'), metadata.get('port')
        try:
            now = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
        except (KeyError, ValueError):
            return ()
        key = (event.get('instance_id'), peer)
        window = self.probes[key]
        window.append((now, port))
        while window and now - window[0][0] > SCAN_WINDOW_SECONDS:
            window.popleft()
        ports = sorted({str(p) for _, p in window})
        if len(ports) < SCAN_MIN_PORTS or now - self.reported.get(key, -1e18) < SCAN_WINDOW_SECONDS:
            return ()
        self.reported[key] = now
        return ({'finding_id': sha256(f"{event['event_id']}:port_scan:{RULE_VERSION}".encode()).hexdigest(),
                 'event_id': event['event_id'], 'instance_id': event['instance_id'], 'timestamp': event['timestamp'],
                 'rule_id': 'port_scan', 'rule_version': RULE_VERSION, 'severity': 'medium',
                 'message': f"{peer} probed {len(ports)} honeypot ports within {SCAN_WINDOW_SECONDS} s ({', '.join(ports)}): "
                            'port scan, ATT&CK T1046 Network Service Discovery.'},)

    def process_once(self, *, limit=100):
        self.last_high_count = 0
        first = self.evidence.read_events(limit=1)
        if not first: return {'processed': 0, 'findings': 0}
        source = sha256(f'{self.evidence.path}:{first[0]["event"]["event_id"]}:{RULE_VERSION}'.encode()).hexdigest()
        cursor = self.alerts.cursor(source)
        rows = self.evidence.read_events(after_sequence=cursor, limit=limit)
        if not rows: return {'processed': 0, 'findings': 0}
        findings = [finding for row in rows for finding in (*classify_event(row['event']), *self.port_scan(row['event']))]
        self.last_high_count = sum(finding['severity'] == 'high' for finding in findings)
        count = self.alerts.write_batch(findings, source=source, cursor=rows[-1]['sequence'])
        return {'processed': len(rows), 'findings': count}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Generate local rule findings without changing raw evidence.')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--alerts', type=Path, required=True)
    parser.add_argument('--watch', action='store_true', help='Continuously inspect new committed events')
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--sqlite-timeout', type=float, default=5)
    args = parser.parse_args(argv)
    from core.runtime_settings import resolve_settings
    try: resolve_settings({'analysis_batch_size': args.batch_size, 'sqlite_busy_timeout_seconds': args.sqlite_timeout})
    except ValueError as error: parser.error(str(error))
    if not math.isfinite(args.interval) or not 0.1 <= args.interval <= 60:
        parser.error('Interval must be between 0.1 and 60 seconds')
    try:
        if args.database.resolve() == args.alerts.resolve():
            raise ValueError('Raw evidence and derived alerts must use different files')
        args.alerts.parent.mkdir(parents=True, exist_ok=True)
        with EvidenceReader(args.database, timeout=args.sqlite_timeout) as evidence, AlertStore(args.alerts, timeout=args.sqlite_timeout) as alerts:
            analyzer = Analyzer(evidence, alerts)
            total = 0
            while True:
                result = analyzer.process_once(limit=args.batch_size)
                total += result['processed']
                # The dashboard retains every finding. Keep routine browser 404s
                # out of the live terminal, while still showing high-severity alerts.
                if result['findings'] and (not args.watch or analyzer.last_high_count):
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

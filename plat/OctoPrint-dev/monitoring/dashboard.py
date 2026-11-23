#!/usr/bin/env python3
"""Read-only local audit dashboard, separate from the OctoPrint UI."""
import argparse
import csv
import io
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audit_store', ROOT / 'src/octoprint/plugins/honeypot_audit/store.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)
HTML = Path(__file__).with_name('dashboard.html')


def query(params):
    clauses, args = [], []
    for field in ('kind', 'source'):
        value = params.get(field, [''])[0]
        if value:
            clauses.append(field + ' = ?'); args.append(value)
    search = params.get('q', [''])[0][:300]
    if search:
        clauses.append('(action LIKE ? OR details LIKE ? OR source LIKE ?)')
        args.extend(['%' + search + '%'] * 3)
    before = params.get('before', [''])[0]
    if before:
        clauses.append('id < ?'); args.append(int(before))
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), args


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, body, mime='application/json', status=200, download=False):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="activity.jsonl"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Loopback listener plus Host validation prevents cross-site DNS rebinding.
        host = self.headers.get('Host', '')
        if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            return self.respond(b'{"error":"Invalid host"}', status=403)
        url = urlsplit(self.path)
        if url.path == '/':
            return self.respond(HTML.read_bytes(), 'text/html; charset=utf-8')
        if url.path not in ('/api/activity', '/api/stats', '/api/export'):
            return self.respond(b'{"error":"Not found"}', status=404)
        conn = store.connect()
        try:
            if url.path == '/api/stats':
                counts = dict(conn.execute('SELECT kind,count(*) FROM activity GROUP BY kind').fetchall())
                peers = conn.execute("SELECT count(DISTINCT source) FROM activity WHERE kind='http' AND source!=''").fetchone()[0]
                row = conn.execute("SELECT value FROM health WHERE key='heartbeat'").fetchone()
                beat = row[0] if row else None
                active = bool(beat and (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 20)
                result = {'counts': counts, 'peers': peers, 'heartbeat': beat, 'collector_active': active,
                          'database_bytes': store.DB.stat().st_size, 'database': str(store.DB)}
            else:
                params = parse_qs(url.query)
                if url.path == '/api/export':
                    # Exports include all matches, independent of table pagination.
                    params.pop('before', None)
                where, args = query(params)
                if url.path == '/api/export':
                    fmt = params.get('format', ['jsonl'])[0]
                    if fmt not in ('jsonl', 'csv'):
                        return self.respond(b'{"error":"Unsupported export format"}', status=400)
                    filename = 'activity-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.' + fmt
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8' if fmt == 'csv' else 'application/x-ndjson; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    fields = ['id', 'ts', 'kind', 'source', 'user', 'request_id', 'attribution', 'action', 'status', 'details']
                    buffer = io.StringIO(newline='')
                    writer = csv.writer(buffer)
                    def csv_row(values):
                        # Keep spreadsheet applications from executing attacker-controlled formulas.
                        safe = []
                        for value in values:
                            text = '' if value is None else str(value)
                            if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
                                text = "'" + text
                            safe.append(text)
                        buffer.seek(0); buffer.truncate(0)
                        writer.writerow(safe)
                        self.wfile.write(buffer.getvalue().encode('utf-8'))
                    if fmt == 'csv':
                        self.wfile.write(b'\xef\xbb\xbf')
                        csv_row(fields)
                    for row in conn.execute('SELECT * FROM activity' + where + ' ORDER BY id DESC', args):
                        item = dict(row)
                        parsed = json.loads(item['details'])
                        actor = parsed.get('actor') or {}
                        item['user'] = actor.get('user') or parsed.get('user') or parsed.get('username', '')
                        item['request_id'] = actor.get('request_id') or parsed.get('request_id', '')
                        item['attribution'] = actor.get('attribution') or parsed.get('attribution', '')
                        if fmt == 'csv':
                            csv_row([item[field] for field in fields])
                        else:
                            item['details'] = json.loads(item['details'])
                            self.wfile.write((json.dumps(item, ensure_ascii=False) + '\n').encode())
                    return
                rows = conn.execute('SELECT * FROM activity' + where + ' ORDER BY id DESC LIMIT 200', args)
                result = []
                for row in rows:
                    item = dict(row); item['details'] = json.loads(item['details']); result.append(item)
            self.respond(json.dumps(result, ensure_ascii=False).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, sqlite3.Error):
            self.respond(b'{"error":"Invalid query or database unavailable"}', status=400)
        finally:
            conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5002)
    args = parser.parse_args()
    conn = store.connect(); conn.close()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Audit dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

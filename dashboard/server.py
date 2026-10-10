"""Loopback-only, read-only evidence dashboard and bounded JSON query API."""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from telemetry.analysis.alerts import validate_finding
from telemetry.analysis.evidence import EvidenceReader


_STYLE = '''body{margin:0;background:#f4f6f8;color:#17212b;font:15px system-ui,sans-serif}
main{max-width:1300px;margin:40px auto;padding:0 24px}h1{font-size:30px;margin-bottom:8px}
.sub{color:#55616e}form{display:flex;gap:12px;flex-wrap:wrap;background:white;padding:20px;border-radius:8px;margin:24px 0}
label{display:flex;flex-direction:column;gap:5px}input,button{font:inherit;padding:9px;border:1px solid #bcc6cf;border-radius:4px}
button{background:#214f77;color:white;cursor:pointer}table{width:100%;border-collapse:collapse;background:white;margin-bottom:24px}
th,td{text-align:left;padding:12px;border-bottom:1px solid #dde3e8;vertical-align:top}th{background:#e8eef3}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-width:420px}a{color:#214f77}.table-wrap{overflow:auto}
.tag{display:inline-block;background:#e8eef3;padding:5px 10px;border-radius:4px;margin:8px 8px 8px 0}'''


def read_findings(path, *, instance_id=None):
    if path is None: return []
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.execute('PRAGMA query_only=ON')
        query = 'SELECT sequence,payload FROM findings'
        args = []
        if instance_id:
            query += ' WHERE instance_id = ?'; args.append(instance_id)
        query += ' ORDER BY sequence DESC LIMIT 100'
        return [{'sequence': seq, 'finding': validate_finding(json.loads(payload))}
                for seq, payload in connection.execute(query, args).fetchall()]
    finally: connection.close()


def render_page(rows, findings, *, instance_id='', event_type='', cursor=0):
    def text(value): return escape(str(value), quote=True)
    body = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HoneyPrint Evidence</title><style>{_STYLE}</style></head><body><main>',
            '<h1>HoneyPrint Evidence</h1><p class="sub">Browse captured interactions and derived rule findings.</p>',
            '<span class="tag">Read only</span><span class="tag">Local SQLite evidence</span>',
            '<form method="get"><label>Instance<input name="instance" value="'+text(instance_id)+'" placeholder="All instances"></label>',
            '<label>Event type<input name="type" value="'+text(event_type)+'" placeholder="All event types"></label>',
            '<button type="submit">Search</button></form>',
            f'<h2>Captured events</h2><p class="sub">Showing {len(rows)} records after sequence {cursor}.</p>',
            '<div class="table-wrap"><table><thead><tr><th>Sequence</th><th>UTC time</th><th>Instance</th><th>Type</th><th>Action</th><th>Outcome</th><th>Details</th></tr></thead><tbody>']
    for row in rows:
        e = row['event']
        details = text(json.dumps(e.get('metadata', {}), indent=2, ensure_ascii=False))
        body.append('<tr>' + ''.join('<td>'+text(v)+'</td>' for v in
                    [row['sequence'],e['timestamp'],e['instance_id'],e['event_type'],e['action'],e['outcome']])
                    + '<td><details><summary>Metadata</summary><pre>'+details+'</pre></details></td></tr>')
    body.append('</tbody></table></div>')
    if len(rows) == 100:
        query = urlencode({'instance': instance_id, 'type': event_type, 'after': rows[-1]['sequence']})
        body.append('<p><a href="/?'+text(query)+'">Next 100 records</a></p>')
    body.append('<h2>Rule findings</h2><p class="sub">Indicators require context and do not confirm an attack. Latest 100 findings for this instance filter.</p><div class="table-wrap"><table><thead><tr><th>Instance</th><th>Severity</th><th>Rule</th><th>Observation</th></tr></thead><tbody>')
    for row in findings:
        f = row['finding']
        body.append('<tr>'+''.join('<td>'+text(f[k])+'</td>' for k in ('instance_id','severity','rule_id','message'))+'</tr>')
    body.append('</tbody></table></div></main></body></html>')
    return ''.join(body)


def create_server(database: Path, *, alerts: Path | None = None, port=8090):
    database = Path(database).resolve(strict=True)
    alerts = Path(alerts).resolve(strict=True) if alerts else None
    class Handler(BaseHTTPRequestHandler):
        def setup(self): super().setup(); self.connection.settimeout(2)
        def log_message(self, *args): pass
        def respond(self, status, content, content_type='application/json'):
            body = json.dumps(content).encode() if content_type == 'application/json' else content.encode()
            self.send_response(status); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            try:
                route = urlsplit(self.path)
                if route.path not in ('/', '/api/events', '/api/alerts'):
                    self.respond(404, {'error': 'unknown endpoint'}); return
                query = parse_qs(route.query, max_num_fields=8)
                if set(query) - {'instance', 'type', 'after'}: raise ValueError()
                instance = query.get('instance', [''])[0]
                kind = query.get('type', [''])[0]
                cursor = int(query.get('after', ['0'])[0])
                if len(instance) > 256 or len(kind) > 256 or cursor < 0: raise ValueError()
                with EvidenceReader(database) as reader:
                    rows = reader.read_events(after_sequence=cursor, limit=100,
                                              instance_id=instance or None, event_type=kind or None)
                findings = read_findings(alerts, instance_id=instance or None)
                if route.path == '/api/events': self.respond(200, rows)
                elif route.path == '/api/alerts': self.respond(200, findings)
                else: self.respond(200, render_page(rows, findings, instance_id=instance,
                                                    event_type=kind, cursor=cursor), 'text/html; charset=utf-8')
            except (ValueError, OverflowError): self.respond(400, {'error': 'invalid query or evidence'})
            except (OSError, sqlite3.Error): self.respond(503, {'error': 'evidence unavailable'})
        def do_POST(self): self.respond(405, {'error': 'dashboard is read only'})
    return HTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Browse HoneyPrint SQLite evidence on loopback.')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--alerts', type=Path)
    parser.add_argument('--port', type=int, default=8090)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535: parser.error('Port must be between 1 and 65535')
    try:
        with create_server(args.database, alerts=args.alerts, port=args.port) as server:
            print(f'Evidence dashboard: http://127.0.0.1:{args.port}', flush=True)
            try: server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt: pass
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print('Dashboard failed. Check existing evidence paths and the local port.')
        return 1


if __name__ == '__main__': raise SystemExit(main())

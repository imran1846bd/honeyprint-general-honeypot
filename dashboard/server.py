"""Loopback evidence dashboard with bounded reads, CSV export and log clearing."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from core.quiet_http import QuietThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from dashboard.classification import BENIGN, describe_event, describe_rows, merge_prediction
from dashboard.taxonomy import ENTERPRISE_TACTICS, LABELS, enterprise_matrix
from telemetry.analysis.alerts import validate_finding
from telemetry.analysis.evidence import EvidenceReader


_STYLE = '''
:root{--bg:#f4f6f8;--panel:#fff;--ink:#17212b;--muted:#5b6773;--line:#dde3e8;--head:#eef2f6;
--accent:#214f77;--info-bg:#e3f4ea;--info:#17643a;--low-bg:#fff4d6;--low:#7a5600;
--medium-bg:#ffe6d1;--medium:#8a3c00;--high-bg:#fde0e0;--high:#9b1c1c;--ml:#3b3f99;--ml-bg:#e7e8fb}
@media (prefers-color-scheme:dark){:root{--bg:#11161c;--panel:#1a2129;--ink:#e6edf3;--muted:#9aa7b4;
--line:#2c3640;--head:#212a33;--accent:#7fb5e6;--info-bg:#173626;--info:#8fdcae;--low-bg:#3a3110;
--low:#f0cf72;--medium-bg:#3f2614;--medium:#f6ad74;--high-bg:#431c1c;--high:#f59b9b;--ml:#b9bcff;--ml-bg:#272a52}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1440px;margin:0 auto;padding:24px 16px 48px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 10px}
.sub{color:var(--muted);margin:4px 0}.top{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:flex-end}
.tag{display:inline-block;background:var(--head);padding:4px 10px;border-radius:999px;font-size:12px;margin:6px 6px 0 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card b{display:block;font-size:26px;margin-top:2px}.card>span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-top:12px}
.bar{display:flex;align-items:center;gap:8px;margin:6px 0}.bar i{display:block;height:10px;border-radius:5px;background:var(--accent);min-width:2px}
.bar .lbl{width:170px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar .n{color:var(--muted);font-variant-numeric:tabular-nums}
form.filters{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;background:var(--panel);border:1px solid var(--line);padding:14px;border-radius:10px;margin:18px 0 0}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
input,select,button{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}
.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:12px}.actions a{color:var(--accent)}
.actions form{display:flex;gap:8px;align-items:center}.danger{background:transparent;color:var(--high);border-color:var(--high)}
.table-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--head);font-size:12px;position:sticky;top:0}tr.sev-high td:first-child{box-shadow:inset 3px 0 var(--high)}
tr.sev-medium td:first-child{box-shadow:inset 3px 0 var(--medium)}tr.sev-low td:first-child{box-shadow:inset 3px 0 var(--low)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-info{background:var(--info-bg);color:var(--info)}.b-low{background:var(--low-bg);color:var(--low)}
.b-medium{background:var(--medium-bg);color:var(--medium)}.b-high{background:var(--high-bg);color:var(--high)}
.basis{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;background:var(--head);color:var(--muted);margin-left:4px}
.basis.ml{background:var(--ml-bg);color:var(--ml)}.tech{margin-top:3px}.note{color:var(--muted);font-size:12px}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}.time{white-space:nowrap}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-width:420px;margin:6px 0 0}summary{cursor:pointer;color:var(--accent)}
.empty{padding:18px;color:var(--muted)}
.matrix{display:grid;grid-template-columns:repeat(15,minmax(150px,1fr));gap:6px;overflow-x:auto;padding-bottom:6px;margin-top:8px;align-items:start}
.tac{border:1px solid var(--line);border-radius:8px;background:var(--panel);font-size:11px}
.tac .th{padding:8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--head);border-radius:8px 8px 0 0}
.tac.hit .th{box-shadow:inset 0 3px var(--high)}
.tac a.tn{display:block;font-weight:700;font-size:12px;color:var(--ink);text-decoration:none}
.tac .tc{font-size:18px;font-weight:700}.tac .sub{color:var(--muted)}
.tq{padding:4px 8px;border-top:1px solid var(--line);line-height:1.3}.tq a{color:var(--ink);text-decoration:none}
.tq.no{opacity:.38}.tq.ok a{color:var(--accent)}.tq.hit{background:var(--high-bg)}.tq.hit a{color:var(--high);font-weight:700}
.tq .n{float:right;font-weight:700;color:var(--high)}.tq details{margin:2px 0 0}.tq summary{font-size:10px;color:var(--muted)}
.tq .s{padding:2px 0 2px 8px}.tq .s.hit{color:var(--high);font-weight:700}
.legend span{display:inline-block;margin-right:14px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
.mtools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px}
.mtools button{padding:4px 10px;font-size:12px}
.wide{grid-column:1/-1}'''

_SCRIPT = '''(() => {
  const $ = id => document.getElementById(id);
  const eventsBody = $('events-body'), status = $('live-status');
  const clearForm = $('clear-logs');
  if (clearForm) clearForm.addEventListener('submit', event => {
    if (!window.confirm('Delete captured logs and analysis findings? This cannot be undone.')) event.preventDefault();
  });
  const params = new URLSearchParams(location.search);
  const query = new URLSearchParams();
  for (const key of ['instance', 'type', 'view']) if (params.get(key)) query.set(key, params.get(key));
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls;
    if (text != null) n.textContent = String(text); return n; };
  const td = (row, value, cls) => { const c = el('td', cls, value == null ? '' : value); row.appendChild(c); return c; };
  const brief = event => { const m = event.metadata || {};
    return [m.control, m.filename, m.command, m.method, m.gcode, m.route, m.topic, m.topic_filter, m.status]
      .filter(v => v != null && v !== '').map(String).join(' · ').slice(0, 160) || 'Metadata'; };
  const localTime = ts => { const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString(); };
  function attackLevels(v) {
    if (!v.label || v.label === 'BENIGN' || !v.tactic_id) return [];
    const levels = [[null, v.section, v.tactic_id, 'https://attack.mitre.org/tactics/' + v.tactic_id],
                    ['tech', v.technique, v.technique_id, 'https://attack.mitre.org/techniques/' + v.technique_id]];
    if (v.subtechnique_id) levels.push(['tech', v.subtechnique, v.subtechnique_id, v.mitre_url]);
    return levels;
  }
  function verdictCell(row, item) {
    const v = item.verdict || {}, c = el('td');
    // Only the ATT&CK hierarchy, each level linked: tactic → technique → sub-technique.
    for (const [cls, name, id, url] of attackLevels(v)) {
      const line = el('div', cls), a = el('a', null, name + ' (' + id + ')');
      a.href = url; a.target = '_blank'; a.rel = 'noopener';
      line.appendChild(a); c.appendChild(line);
    }
    row.appendChild(c);
  }
  function renderEvents(items) {
    const frag = document.createDocumentFragment();
    for (const item of items) {
      const e = item.event, v = item.verdict || {}, row = el('tr');
      row.className = 'sev-' + (v.severity || 'info');
      td(row, item.sequence, 'mono'); td(row, localTime(e.timestamp), 'time');
      td(row, e.instance_id); td(row, e.source);
      td(row, e.event_type, 'mono'); td(row, e.action + ' / ' + e.outcome);
      verdictCell(row, item);
      const am = item.am || {}, ac = td(row, item.process_mapping);
      if (am.component) ac.appendChild(el('div', 'note', 'Component: ' + am.component +
        (am.honeypot_stage ? ' (HoneyPrint stage ' + am.honeypot_stage + ')' : '')));
      const c = el('td'), d = el('details');
      d.append(el('summary', null, brief(e)), el('pre', null, JSON.stringify(e.metadata || {}, null, 2)));
      c.appendChild(d); row.appendChild(c); frag.appendChild(row);
    }
    if (!items.length) { const r = el('tr'), c = el('td', 'empty', 'No interactions yet for this filter.');
      c.colSpan = 9; r.appendChild(c); frag.appendChild(r); }
    eventsBody.replaceChildren(frag);
  }
  function bars(target, counts) {
    const box = $(target); if (!box) return;
    const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
    const max = Math.max(1, ...entries.map(e => e[1])), frag = document.createDocumentFragment();
    for (const [name, n] of entries) {
      const b = el('div', 'bar'); b.appendChild(el('span', 'lbl', name)); b.title = name;
      const i = el('i'); i.style.width = Math.round(160 * n / max) + 'px';
      b.append(i, el('span', 'n', n)); frag.appendChild(b);
    }
    if (!entries.length) frag.appendChild(el('div', 'note', 'No data yet.'));
    box.replaceChildren(frag);
  }
  let layout = null, lastSummary = null, showAll = false;
  const toggle = $('matrix-toggle');
  if (toggle) toggle.addEventListener('click', () => {
    showAll = !showAll; toggle.textContent = showAll ? 'Show detected only' : 'Show all techniques';
    if (lastSummary) renderMatrix(lastSummary);
  });
  fetch('/api/matrix').then(r => r.ok && r.json()).then(m => { layout = m; if (lastSummary) renderMatrix(lastSummary); }).catch(() => {});
  const link = (cls, text, url) => { const a = el('a', cls, text); a.href = url; a.target = '_blank'; a.rel = 'noopener'; return a; };
  function renderMatrix(s) {
    lastSummary = s;
    const box = $('matrix'); if (!box || !layout) return;
    const frag = document.createDocumentFragment();
    for (const column of layout) {
      const counts = (s.matrix || {})[column.tactic] || {};
      const total = Object.values(counts).reduce((a, b) => a + b, 0);
      const col = el('div', 'tac' + (total ? ' hit' : ''));
      const head = el('div', 'th');
      head.append(link('tn', column.tactic, column.url),
        el('div', 'sub', column.tactic_id + ' · ' + column.techniques.length + ' techniques'), el('div', 'tc', total));
      col.appendChild(head);
      for (const t of column.techniques) {
        const subHits = t.subs.filter(x => counts[x.id]);
        const n = (counts[t.id] || 0) + subHits.reduce((a, x) => a + counts[x.id], 0);
        if (!showAll && !n) continue;
        const item = el('div', 'tq ' + (n ? 'hit' : t.observable ? 'ok' : 'no'));
        if (n) item.appendChild(el('span', 'n', n));
        item.appendChild(link(null, t.name + (t.subs.length ? ' (' + t.subs.length + ')' : ''), t.url));
        if (t.subs.length && (showAll || subHits.length)) {
          const d = el('details'); d.open = subHits.length > 0;
          d.appendChild(el('summary', null, t.id + ' · sub-techniques'));
          for (const x of t.subs) {
            if (!showAll && !counts[x.id]) continue;
            const line = el('div', 's' + (counts[x.id] ? ' hit' : ''));
            line.append(link(null, x.name, x.url), document.createTextNode(counts[x.id] ? ' (' + counts[x.id] + ')' : ''));
            d.appendChild(line);
          }
          item.appendChild(d);
        }
        col.appendChild(item);
      }
      frag.appendChild(col);
    }
    box.replaceChildren(frag);
  }
  function renderSummary(s) {
    for (const [id, v] of [['c-total', s.total_events], ['c-inter', s.interactions],
        ['c-susp', s.suspicious], ['c-high', s.high], ['c-stream', s.print_stream_lines],
        ['c-find', s.findings_total]]) { const n = $(id); if (n) n.textContent = (v ?? 0).toLocaleString(); }
    renderMatrix(s); bars('by-technique', s.by_technique);
    bars('by-channel', s.by_channel); bars('by-stage', s.by_stage); bars('by-component', s.by_component);
    const fb = $('findings-body');
    if (fb) { const frag = document.createDocumentFragment();
      for (const f of s.findings || []) { const r = el('tr');
        const c = el('td'); c.appendChild(el('span', 'badge b-' + f.severity, f.severity)); r.appendChild(c);
        td(r, f.rule_id, 'mono'); td(r, f.message); td(r, f.count); td(r, f.instances.join(', '));
        td(r, localTime(f.latest), 'time'); frag.appendChild(r); }
      if (!(s.findings || []).length) { const r = el('tr'), c = el('td', 'empty', 'No rule findings.'); c.colSpan = 6; r.appendChild(c); frag.appendChild(r); }
      fb.replaceChildren(frag); }
    const pb = $('print-body');
    if (pb) { const frag = document.createDocumentFragment();
      for (const p of s.print_activity || []) { const r = el('tr');
        td(r, p.instance_id); td(r, p.filename || '(serial stream)'); td(r, p.source);
        td(r, Number(p.lines).toLocaleString()); td(r, localTime(p.last_timestamp), 'time'); frag.appendChild(r); }
      if (!(s.print_activity || []).length) { const r = el('tr'), c = el('td', 'empty', 'No print streaming recorded.'); c.colSpan = 5; r.appendChild(c); frag.appendChild(r); }
      pb.replaceChildren(frag); }
    const m = $('model-status'); if (m) m.textContent = s.model;
  }
  let busy = false, tick = 0;
  async function refresh() {
    if (busy) return; busy = true;
    try {
      const response = await fetch('/api/live?' + query, {cache: 'no-store'});
      if (!response.ok) throw new Error('Evidence request failed');
      renderEvents((await response.json()).events);
      if (tick++ % 3 === 0) {
        const s = await fetch('/api/summary?' + query, {cache: 'no-store'});
        if (s.ok) renderSummary(await s.json());
      }
      status.textContent = 'Live · updated ' + new Date().toLocaleTimeString();
    } catch (_) {
      status.textContent = 'Update unavailable · retrying';
    } finally { busy = false; }
  }
  if (!params.has('before')) { refresh(); setInterval(refresh, 3000); }
  else fetch('/api/summary?' + query).then(r => r.ok && r.json()).then(s => s && renderSummary(s)).catch(() => {});
})();'''

VIEWS = ('all', 'suspicious')


def read_findings(path, *, instance_id=None, limit=100, timeout=5):
    if path is None: return []
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=timeout)
    try:
        connection.execute('PRAGMA query_only=ON')
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='findings'").fetchone():
            return []
        query = 'SELECT sequence,payload FROM findings'
        args = []
        if instance_id:
            query += ' WHERE instance_id = ?'; args.append(instance_id)
        query += ' ORDER BY sequence DESC LIMIT ?'; args.append(limit)
        return [{'sequence': seq, 'finding': validate_finding(json.loads(payload))}
                for seq, payload in connection.execute(query, args).fetchall()]
    finally: connection.close()


def group_findings(path, *, instance_id=None, timeout=5, scan=5000):
    """Group the most recent findings by rule so repeats do not flood the page."""
    groups = {}
    for row in read_findings(path, instance_id=instance_id, limit=scan, timeout=timeout):
        f = row['finding']
        g = groups.setdefault(f['rule_id'], {'rule_id': f['rule_id'], 'severity': f['severity'],
                                             'message': f['message'], 'count': 0,
                                             'instances': set(), 'latest': f['timestamp']})
        g['count'] += 1; g['instances'].add(f['instance_id'])
        g['latest'] = max(g['latest'], f['timestamp'])
    order = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}
    return sorted(({**g, 'instances': sorted(g['instances'])} for g in groups.values()),
                  key=lambda g: (order.get(g['severity'], 4), -g['count']))


def add_predictions(rows, path, *, timeout=5):
    if not path or not rows:
        return rows
    ids = [row['event']['event_id'] for row in rows]
    predictions = {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=timeout)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ml_predictions'").fetchone():
            return rows
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ','.join('?' for _ in chunk)
            predictions.update(
                {event_id: (section, subsection, confidence, status)
                 for event_id, section, subsection, confidence, status in connection.execute(
                     'SELECT event_id,section,subsection,confidence,status '
                     f'FROM ml_predictions WHERE event_id IN ({placeholders})', chunk)})
    return [merge_prediction(row, *predictions[row['event']['event_id']])
            if row['event']['event_id'] in predictions else row for row in rows]


def matrix_counts(rows):
    """{tactic: {technique or sub-technique ID: count}} for the 15 Enterprise tactics."""
    counts = {name: {} for _, name in ENTERPRISE_TACTICS}
    for row in rows:
        v = row['verdict']
        if v['section'] in counts and v['mitre']:
            column = counts[v['section']]
            column[v['mitre']] = column.get(v['mitre'], 0) + 1
    return counts


def matrix_layout():
    """Static ATT&CK Enterprise layout: every tactic, technique and sub-technique."""
    observable = {(LABELS[label][0], label) for label in LABELS if label != BENIGN}
    layout = []
    for column in enterprise_matrix():
        layout.append({**column, 'techniques': [
            {**t, 'observable': any((column['tactic_id'], i) in observable
                                    for i in [t['id'], *(sub['id'] for sub in t['subs'])])}
            for t in column['techniques']]})
    return layout


def summarize(rows):
    by_category, by_technique, by_channel, by_stage, by_component = (Counter() for _ in range(5))
    suspicious = high = 0  # suspicious = mapped to an ATT&CK technique
    for row in rows:
        v = row['verdict']
        by_category[v['section']] += 1
        by_channel[row['event'].get('source', '')] += 1
        by_stage[row['process_mapping'].split(':')[0]] += 1
        by_component[(row.get('am') or {}).get('component', 'Unknown')] += 1
        if v['label'] != BENIGN:
            suspicious += 1
            by_technique[f"{v['mitre']} {v['mitre_name']}"] += 1
            high += v['severity'] == 'high'
    return {'interactions': len(rows), 'suspicious': suspicious, 'high': high,
            'matrix': matrix_counts(rows),
            'by_category': dict(by_category), 'by_technique': dict(by_technique),
            'by_channel': dict(by_channel), 'by_stage': dict(by_stage), 'by_component': dict(by_component)}


def _options(values, selected, blank):
    out = [f'<option value="">{escape(blank)}</option>']
    values = list(values)
    if selected and selected not in values:
        values.append(selected)
    for value in values:
        mark = ' selected' if value == selected else ''
        out.append(f'<option value="{escape(value, quote=True)}"{mark}>{escape(value)}</option>')
    return ''.join(out)


def _verdict_html(row):
    v = row.get('verdict') or describe_event(row['event'])['verdict']
    text = lambda value: escape(str(value), quote=True)
    html = ''
    if v['label'] != BENIGN and v.get('tactic_id'):
        # Only the ATT&CK hierarchy, each level linked: tactic → technique → sub-technique.
        levels = [('', v['section'], v['tactic_id'], 'https://attack.mitre.org/tactics/' + v['tactic_id']),
                  (' class="tech"', v['technique'], v['technique_id'],
                   'https://attack.mitre.org/techniques/' + v['technique_id'])]
        if v.get('subtechnique_id'):
            levels.append((' class="tech"', v['subtechnique'], v['subtechnique_id'], v['mitre_url']))
        html = ''.join(f'<div{cls}><a href="{text(url)}" target="_blank" rel="noopener">{text(name)} ({text(ident)})</a></div>'
                       for cls, name, ident, url in levels)
    return html, v['severity']


def render_page(rows, findings, *, instance_id='', event_type='', cursor=0, page_size=100,
                action_token=None, view='all', instances=(), model_status=''):
    rows = [row if 'verdict' in row else {**describe_event(row['event']), **row} for row in rows]
    def text(value): return escape(str(value), quote=True)
    def brief(event):
        metadata = event.get('metadata') or {}
        values = [metadata.get(key) for key in ('control', 'filename', 'command', 'method', 'gcode',
                                                'route', 'topic', 'topic_filter', 'status')]
        return (' · '.join(str(value) for value in values if value is not None and value != '')[:160]
                or 'Metadata')
    card = lambda key, label: f'<div class="card"><span>{label}</span><b id="{key}">–</b></div>'
    body = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HoneyPrint Evidence</title><style>{_STYLE}</style></head><body><main>',
            '<div class="top"><div><h1>HoneyPrint Interactions</h1><p class="sub">Access and control activity across the honeypot. '
            'Streamed print lines and background polls are summarised separately.</p>',
            '<span class="tag">Local SQLite evidence</span>',
            '<span class="tag" id="live-status">'+('Live · updates every 3 seconds' if cursor == 0 else 'Older events · static page')+'</span>',
            '<span class="tag" id="model-status">'+text(model_status or 'Rules only')+'</span></div></div>',
            '<div class="cards">', card('c-total', 'Stored events'), card('c-inter', 'Recent interactions'),
            card('c-susp', 'Mapped to ATT&amp;CK'), card('c-high', 'High severity'), card('c-stream', 'Print stream lines'),
            card('c-find', 'Rule findings'), '</div>',
            '<p class="note">Counts cover the latest 1000 interactions for the current filter.</p>',
            '<div class="card" style="margin-top:12px"><span>MITRE ATT&amp;CK Enterprise matrix (v20.1) · latest 1000 interactions</span>'
            '<div class="mtools"><button type="button" id="matrix-toggle">Show all techniques</button>'
            '<span class="note legend"><span><i style="background:var(--high-bg);border:1px solid var(--high)"></i>detected</span>'
            '<span><i style="background:var(--accent)"></i>detectable by HoneyPrint</span>'
            '<span><i style="background:var(--line)"></i>not observable on a printer honeypot</span></span></div>'
            '<div class="matrix" id="matrix"></div></div>',
            '<div class="grid2">',
            '<div class="card"><span>ATT&amp;CK techniques</span><div id="by-technique"></div></div>',
            '<div class="card"><span>Channel</span><div id="by-channel"></div></div>',
            '<div class="card"><span>AM process stage</span><div id="by-stage"></div></div>',
            '<div class="card"><span>HoneyPrint component</span><div id="by-component"></div></div></div>',
            '<form class="filters" method="get">'
            '<label>Instance<select name="instance">'+_options(instances, instance_id, 'All instances')+'</select></label>',
            '<label>Event type<input name="type" value="'+text(event_type)+'" placeholder="e.g. ssh.auth"></label>',
            '<label>Show<select name="view">'+''.join(
                f'<option value="{v}"{" selected" if v == view else ""}>{"All interactions" if v == "all" else "Medium and high severity"}</option>'
                for v in VIEWS)+'</select></label>',
            '<button type="submit">Apply</button></form>',
            '<div class="actions"><a href="/api/logs/download">Download all logs (CSV)</a>']
    if action_token is not None:
        body.append('<form id="clear-logs" method="post" action="/api/logs/clear">'
                    '<input type="hidden" name="token" value="'+text(action_token)+'">'
                    '<button class="danger" type="submit">Clear captured logs</button>'
                    '<span class="note">Deletes captured events and findings; lifecycle markers remain.</span></form>')
    body.append('</div>')
    body.extend([f'<h2>Interactions</h2><p class="sub">Showing {len(rows)} most recent interactions'+
                 (f' before sequence {cursor}' if cursor else '')+'. Classifications are indicators for review, not confirmed attacks. '
                 'Section = MITRE ATT&amp;CK tactic; below it the technique › sub-technique and its ID. <b>ML</b> = trained model; <b>Rule</b> = transparent rule used when the model is not confident enough or has not seen that event type.</p>',
                 '<div class="table-wrap"><table><thead><tr><th>Seq</th><th>Time</th><th>Instance</th><th>Channel</th><th>Event</th><th>Action / outcome</th><th>Classification</th><th>AM process stage</th><th>Details</th></tr></thead><tbody id="events-body">'])
    for row in rows:
        e = row['event']
        verdict, severity = _verdict_html(row)
        details = text(json.dumps(e.get('metadata', {}), indent=2, ensure_ascii=False))
        body.append(f'<tr class="sev-{text(severity)}">' + ''.join(
            '<td>'+text(v)+'</td>' for v in
            [row['sequence'], e['timestamp'], e['instance_id'], e.get('source', ''), e['event_type'],
             f"{e['action']} / {e['outcome']}"])
            + '<td>'+verdict+'</td><td>'+text(row['process_mapping'])
            + ('<div class="note">Component: '+text(row['am']['component'])+'</div>' if row.get('am') else '')+'</td>'
            + '<td><details><summary>'+text(brief(e))+'</summary><pre>'+details+'</pre></details></td></tr>')
    if not rows:
        body.append('<tr><td class="empty" colspan="9">No interactions yet for this filter.</td></tr>')
    body.append('</tbody></table></div>')
    if len(rows) == page_size:
        query = urlencode({'instance': instance_id, 'type': event_type, 'view': view,
                           'before': rows[-1]['sequence']})
        body.append('<p><a href="/?'+text(query)+'">Next '+str(page_size)+' records</a></p>')
    body.append('<h2>Rule findings</h2><p class="sub">Grouped by rule. Indicators require context and do not confirm an attack.</p>'
                '<div class="table-wrap"><table><thead><tr><th>Severity</th><th>Rule</th><th>Observation</th><th>Count</th><th>Instances</th><th>Latest</th></tr></thead><tbody id="findings-body">')
    for row in findings[:20]:
        f = row['finding']
        body.append('<tr>'+''.join('<td>'+text(f[k])+'</td>' for k in ('severity','rule_id','message'))
                    +'<td>1</td><td>'+text(f['instance_id'])+'</td><td>'+text(f['timestamp'])+'</td></tr>')
    body.append('</tbody></table></div>')
    body.append('<h2>Print activity</h2><p class="sub">G-code lines streamed to the printer during jobs. They are normal execution traffic, so they are counted here instead of listed above.</p>'
                '<div class="table-wrap"><table><thead><tr><th>Instance</th><th>File</th><th>Source</th><th>Lines</th><th>Last line</th></tr></thead><tbody id="print-body"></tbody></table></div>')
    body.append('</main><script src="/dashboard.js" defer></script></body></html>')
    return ''.join(body)


def clear_logs(database, alerts=None, *, timeout=5):
    """Remove captured evidence while preserving lifecycle readiness markers."""
    with closing(sqlite3.connect(database, timeout=timeout)) as connection:
        if alerts is not None:
            connection.execute('ATTACH DATABASE ? AS analysis', (str(alerts),))
        with connection:
            if alerts is not None:
                if connection.execute("SELECT 1 FROM analysis.sqlite_master WHERE type='table' AND name='findings'").fetchone():
                    connection.execute('DELETE FROM analysis.findings')
                if connection.execute("SELECT 1 FROM analysis.sqlite_master WHERE type='table' AND name='ml_predictions'").fetchone():
                    connection.execute('DELETE FROM analysis.ml_predictions')
                if connection.execute("SELECT 1 FROM analysis.sqlite_master WHERE type='table' AND name='ml_control'").fetchone():
                    connection.execute("UPDATE analysis.ml_control SET value=value+1 WHERE key='generation'")
            cursor = connection.execute("DELETE FROM events WHERE json_extract(payload, '$.event_type') != 'framework.lifecycle'")
            return cursor.rowcount


def model_label(predictions, timeout=5):
    if not predictions:
        return 'Rules only (no ML model configured)'
    try:
        with closing(sqlite3.connect(Path(predictions).resolve().as_uri() + '?mode=ro', uri=True,
                                     timeout=timeout)) as connection:
            row = connection.execute('SELECT model_id, COUNT(*) FROM ml_predictions '
                                     'GROUP BY model_id ORDER BY MAX(sequence) DESC LIMIT 1').fetchone()
    except sqlite3.Error:
        row = None
    return f'ML model {row[0].split(":")[0][:8]} + rules' if row else 'ML model loading · rules active'


def create_server(database: Path, *, alerts: Path | None = None, predictions: Path | None = None,
                  port=8090, page_size=100, request_timeout=2, sqlite_timeout=5):
    from core.runtime_settings import resolve_settings
    resolve_settings({'dashboard_page_size': page_size, 'dashboard_request_timeout_seconds': request_timeout,
                      'sqlite_busy_timeout_seconds': sqlite_timeout})
    database = Path(database).resolve(strict=True)
    alerts = Path(alerts).resolve(strict=True) if alerts else None
    predictions = Path(predictions).resolve(strict=True) if predictions else None
    action_token = secrets.token_urlsafe(32)
    slow_cache, slow_lock = {}, Lock()

    def cached(key, ttl, compute):
        """Cache full-table aggregates so live refreshes stay cheap."""
        with slow_lock:
            hit = slow_cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = compute()
        with slow_lock:
            slow_cache[key] = (time.monotonic(), value)
        return value

    def interactions(reader, *, before, instance, kind, view, limit):
        if view != 'suspicious':
            rows = reader.read_latest_events(before_sequence=before, limit=limit,
                                             instance_id=instance or None, event_type=kind or None,
                                             interactions_only=True)
            return add_predictions(describe_rows(rows), predictions, timeout=sqlite_timeout)
        selected, cursor = [], before
        for _ in range(5):  # bounded scan for suspicious rows
            rows = reader.read_latest_events(before_sequence=cursor, limit=1000,
                                             instance_id=instance or None, event_type=kind or None,
                                             interactions_only=True)
            if not rows: break
            merged = add_predictions(describe_rows(rows), predictions, timeout=sqlite_timeout)
            selected.extend(row for row in merged if row['verdict']['severity'] in ('medium', 'high'))
            cursor = rows[-1]['sequence']
            if len(selected) >= limit or cursor <= 1: break
        return selected[:limit]

    class Handler(BaseHTTPRequestHandler):
        def setup(self): super().setup(); self.connection.settimeout(request_timeout)
        def log_message(self, *args): pass
        def respond(self, status, content, content_type='application/json'):
            body = json.dumps(content).encode() if content_type == 'application/json' else content.encode()
            self.send_response(status); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            try:
                route = urlsplit(self.path)
                if route.path == '/api/logs/download':
                    if route.query: raise ValueError()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="honeyprint-evidence.csv"')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=sqlite_timeout)) as connection:
                        writer_buffer = StringIO()
                        writer = csv.writer(writer_buffer)
                        writer.writerow(('sequence','event_id','timestamp','instance_id','platform','source','session_id',
                                         'event_type','action','outcome','simulation','metadata_json',
                                         'attack_classification','process_mapping','ml_confidence','ml_status',
                                         'category','severity','classification_basis',
                                         'mitre_candidate','mitre_name','mitre_domain','mitre_tactics',
                                         'component','honeypot_stage','am_stage','am_layer','am_element'))
                        self.wfile.write(writer_buffer.getvalue().encode('utf-8'))
                        cursor = connection.execute('SELECT sequence,payload FROM events ORDER BY sequence')
                        while batch := cursor.fetchmany(500):
                            rows = describe_rows([{'sequence': sequence, 'event': json.loads(payload)}
                                                  for sequence, payload in batch])
                            rows = add_predictions(rows, predictions, timeout=sqlite_timeout)
                            writer_buffer.seek(0); writer_buffer.truncate(0)
                            for row in rows:
                                event = row['event']
                                prediction = row.get('ml_prediction', {})
                                verdict = row['verdict']
                                writer.writerow([row['sequence'],*[event.get(key) for key in
                                    ('event_id','timestamp','instance_id','platform','source','session_id',
                                     'event_type','action','outcome','simulation')],
                                    json.dumps(event.get('metadata', {}), ensure_ascii=False, separators=(',', ':')),
                                    row['attack_classification'],row['process_mapping'],
                                    prediction.get('confidence', ''),prediction.get('status', ''),
                                    verdict['category'], verdict['severity'], verdict['basis'],
                                    verdict.get('mitre', ''), verdict.get('mitre_name', ''),
                                    verdict.get('mitre_domain', ''), verdict.get('mitre_tactics', ''),
                                    *[row['am'].get(key, '') for key in
                                      ('component', 'honeypot_stage', 'am_stage', 'am_layer', 'am_element')]])
                            self.wfile.write(writer_buffer.getvalue().encode('utf-8'))
                    return
                if route.path == '/health':
                    with EvidenceReader(database, timeout=sqlite_timeout) as reader:
                        reader.read_events(limit=1)
                    self.respond(200, {'ready': True}); return
                if route.path == '/api/matrix':
                    self.respond(200, cached(('layout',), 3600, matrix_layout)); return
                if route.path == '/dashboard.js':
                    self.respond(200, _SCRIPT, 'application/javascript; charset=utf-8'); return
                if route.path not in ('/', '/api/events', '/api/alerts', '/api/live', '/api/summary'):
                    self.respond(404, {'error': 'unknown endpoint'}); return
                query = parse_qs(route.query, max_num_fields=8)
                if set(query) - {'instance', 'type', 'after', 'before', 'view'}: raise ValueError()
                instance = query.get('instance', [''])[0]
                kind = query.get('type', [''])[0]
                view = query.get('view', ['all'])[0] or 'all'
                cursor = int(query.get('after', ['0'])[0])
                before = int(query.get('before', ['0'])[0])
                if (len(instance) > 256 or len(kind) > 256 or cursor < 0 or before < 0
                        or view not in VIEWS): raise ValueError()
                with EvidenceReader(database, timeout=sqlite_timeout) as reader:
                    if route.path == '/api/summary':
                        rows = interactions(reader, before=0, instance=instance, kind=kind,
                                            view='all', limit=1000)
                        summary = summarize(rows)
                        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True,
                                                     timeout=sqlite_timeout)) as connection:
                            summary['total_events'] = connection.execute(
                                'SELECT COUNT(*) FROM events').fetchone()[0]
                        activity = cached(('print', instance), 30,
                                          lambda: reader.print_activity(instance_id=instance or None))
                        summary['print_activity'] = activity
                        summary['print_stream_lines'] = sum(item['lines'] for item in activity)
                        grouped = cached(('findings', instance), 10,
                                         lambda: group_findings(alerts, instance_id=instance or None,
                                                                timeout=sqlite_timeout))
                        summary['findings'] = grouped
                        summary['findings_total'] = sum(item['count'] for item in grouped)
                        summary['model'] = cached(('model',), 30, lambda: model_label(predictions, sqlite_timeout))
                        self.respond(200, summary); return
                    if route.path == '/api/events':
                        rows = add_predictions(describe_rows(reader.read_events(
                            after_sequence=cursor, limit=page_size,
                            instance_id=instance or None, event_type=kind or None)),
                            predictions, timeout=sqlite_timeout)
                    else:
                        rows = interactions(reader, before=before, instance=instance, kind=kind,
                                            view=view, limit=page_size)
                    instances = reader.instances() if route.path == '/' else ()
                findings = (read_findings(alerts, instance_id=instance or None, limit=page_size,
                                          timeout=sqlite_timeout) if rows or route.path == '/api/alerts' else [])
                if route.path == '/api/events': self.respond(200, rows)
                elif route.path == '/api/alerts': self.respond(200, findings)
                elif route.path == '/api/live': self.respond(200, {'events': rows, 'findings': findings})
                else: self.respond(200, render_page(rows, findings, instance_id=instance,
                                                    event_type=kind, cursor=before, page_size=page_size,
                                                    action_token=action_token, view=view,
                                                    instances=instances,
                                                    model_status=model_label(predictions, sqlite_timeout)),
                                   'text/html; charset=utf-8')
            except (ValueError, OverflowError): self.respond(400, {'error': 'invalid query or evidence'})
            except (OSError, sqlite3.Error): self.respond(503, {'error': 'evidence unavailable'})
        def do_POST(self):
            if self.path != '/api/logs/clear':
                self.respond(405, {'error': 'unsupported dashboard action'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if (not 1 <= length <= 512 or self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'
                        or self.headers.get('Origin') not in (None, *(f'http://{name}:{self.server.server_port}'
                                                                     for name in ('127.0.0.1', 'localhost', '[::1]')))):
                    raise ValueError()
                form = parse_qs(self.rfile.read(length).decode('ascii'), strict_parsing=True)
                if set(form) != {'token'} or len(form['token']) != 1:
                    raise ValueError()
                if not secrets.compare_digest(form['token'][0], action_token):
                    # The page was loaded before the dashboard restarted; its token is out of date.
                    self.respond(409, {'error': 'this page is out of date; reload the dashboard and try again'})
                    return
                clear_logs(database, alerts or predictions, timeout=sqlite_timeout)
                with slow_lock: slow_cache.clear()
                self.send_response(303); self.send_header('Location', '/')
                self.send_header('Content-Length', '0'); self.end_headers()
            except (ValueError, UnicodeError): self.respond(403, {'error': 'clear request rejected'})
            except (OSError, sqlite3.Error): self.respond(503, {'error': 'log storage unavailable'})
    # Threaded: a scanner holding a connection open must not freeze the dashboard.
    return QuietThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Browse HoneyPrint SQLite evidence on loopback.')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--alerts', type=Path)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--page-size', type=int, default=100)
    parser.add_argument('--request-timeout', type=float, default=2)
    parser.add_argument('--sqlite-timeout', type=float, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535: parser.error('Port must be between 1 and 65535')
    try:
        with create_server(args.database, alerts=args.alerts, predictions=args.predictions,
                           port=args.port, page_size=args.page_size,
                           request_timeout=args.request_timeout, sqlite_timeout=args.sqlite_timeout) as server:
            print(f'Evidence dashboard: http://127.0.0.1:{args.port}', flush=True)
            try: server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt: pass
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print('Dashboard failed. Check existing evidence paths and the local port.')
        return 1


if __name__ == '__main__': raise SystemExit(main())

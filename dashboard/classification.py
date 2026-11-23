"""Rule-based MITRE ATT&CK classification for HoneyPrint events.

Every interaction gets a MITRE ATT&CK label (technique or sub-technique ID).
A honeypot has no legitimate users, so routine use gets its closest technique
(dashboard.taxonomy.activity_technique). Only printer telemetry that no one sent
(state polls, plugin state) has no label.
The trained model uses the same labels; these rules are used when no model is
loaded, when the model's confidence is below its gate, or for event types the
model was not trained on. A single event is an indicator, not proof of intent.
"""

import math

from dashboard.am_mapping import map_event
from dashboard.taxonomy import (BENIGN, DEFAULT_ACCOUNTS, GCODE_TECHNIQUE, MQTT_TECHNIQUE,
                                activity_technique, lookup, route_family, shell_technique, to_label)

# Default safe envelope; matches the common .env template (220 x 220 x 250 mm,
# hotend 0..280 C, bed 0..130 C). Small tolerance allows homing offsets.
LIMITS = {'x': (-5.0, 225.0), 'y': (-5.0, 225.0), 'z': (-1.0, 255.0)}
HOTEND_MAX_C = 280.0
BED_MAX_C = 130.0
# Status routes a logged-out web UI polls; a 403 there is normal behaviour.
UI_POLL_ROUTES = ('/api/job', '/api/printer', '/api/settings', '/api/connection',
                  '/api/files', '/api/version', '/api/login', '/api/currentuser',
                  '/api/printerprofiles', '/api/system', '/api/plugin')

AUTH = ('ssh.auth', 'telnet.auth', 'mqtt.auth')
CONSOLE_COMMANDS = ('ssh.command', 'telnet.command', 'mqtt.command')
UPLOADS = ('ssh.upload', 'telnet.upload', 'mqtt.upload', 'file.artifact')
PRINTER_VERBS = frozenset((
    'help', 'status', 'temperature', 'temp', 'position', 'job', 'connect', 'print',
    'start', 'pause', 'resume', 'cancel', 'home', 'move', 'heat', 'bed', 'cooldown',
    'run', 'files', 'show', 'upload', 'select', 'exit', 'quit', 'logout', 'preheat',
    'g0', 'g1', 'g28', 'g90', 'g91', 'g92', 'm82', 'm83', 'm84', 'm104', 'm105',
    'm106', 'm107', 'm109', 'm112', 'm114', 'm115', 'm140', 'm190', 'm117'))
READ_ONLY_RPC = frozenset((
    'server.info', 'printer.info', 'printer.objects.query', 'printer.objects.subscribe',
    'printer.objects.list', 'server.files.list', 'server.files.get_directory',
    'server.files.metadata', 'server.temperature_store', 'server.gcode_store',
    'server.webcams.list', 'server.announcements.list', 'server.history.list',
    'server.history.totals', 'server.job_queue.status', 'server.database.list',
    'server.database.get_item', 'server.database.post_item', 'server.config',
    'machine.system_info', 'machine.proc_stats', 'server.connection.identify'))


def is_print_stream(event):
    """Lines streamed to the printer while a job runs, and periodic polls.

    They are normal job execution, not operator interaction, and can number in
    the hundreds of thousands, so the dashboard summarises them instead of
    listing each line.
    """
    kind = event.get('event_type')
    if kind == 'printer.command':
        return event.get('source') in ('firmware', 'gcode')
    if kind == 'printer.state':
        return event.get('action') in ('print_line', 'poll')
    return kind == 'printer.job' and event.get('action') == 'progress'


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _gcode_of(event, metadata):
    gcode = str(metadata.get('gcode') or '').upper()
    if gcode:
        return gcode
    command = str(metadata.get('command') or '').strip().split(maxsplit=1)
    return command[0].upper() if command and command[0][:1].upper() in 'GM' else ''


def _tamper(event, metadata):
    """MITRE label for out-of-envelope printer requests, or None."""
    gcode = _gcode_of(event, metadata)
    command = str(metadata.get('command') or '').lower().split(maxsplit=1)
    verb = command[0] if command else ''
    if gcode == 'M112' or str(metadata.get('method', '')) == 'printer.emergency_stop':
        return 'T1529'
    target = _number(metadata.get('target_c'))
    if target is None and gcode in ('M104', 'M109', 'M140', 'M190'):
        target = _number(metadata.get('parameter_s'))
    if target is not None:
        bed = gcode in ('M140', 'M190') or verb == 'bed'
        if target < 0 or target > (BED_MAX_C if bed else HOTEND_MAX_C):
            return 'T1565'
    if gcode in ('G0', 'G1', 'G92') or verb == 'move':
        for axis, (low, high) in LIMITS.items():
            value = _number(metadata.get('parameter_' + axis))
            if value is not None and not low <= value <= high:
                return 'T1565'
    return None


def _routine(event):
    metadata = event.get('metadata') or {}
    return activity_technique({
        'event_type': event.get('event_type', ''), 'action': event.get('action', ''),
        'command': metadata.get('command', ''), 'method': metadata.get('method', ''),
        'route_family': route_family(metadata.get('route') or metadata.get('path'))})


# Scanner tools by User-Agent. Directory brute-forcers are Wordlist Scanning;
# the rest probe software and versions: Vulnerability Scanning.
WORDLIST_TOOLS = ('gobuster', 'dirbuster', 'dirb/', 'ffuf', 'feroxbuster', 'wfuzz')
SCANNER_TOOLS = ('nmap', 'nikto', 'sqlmap', 'nuclei', 'wpscan', 'masscan', 'zgrab', 'openvas', 'nessus',
                 'acunetix', 'qualys', 'whatweb', 'httpx', 'burp', 'zap/', 'arachni', 'skipfish', 'w3af')


def scanner_tool(user_agent):
    text = str(user_agent or '').lower()
    if any(tool in text for tool in WORDLIST_TOOLS):
        return 'T1595.003'
    if any(tool in text for tool in SCANNER_TOOLS):
        return 'T1595.002'
    return ''


def rule_label(event):
    """Return the MITRE ATT&CK label from transparent single-event rules."""
    label = _attack_label(event)
    return _routine(event) if label == BENIGN else label


def _attack_label(event):
    """Attack-specific label, or BENIGN when the event is routine use."""
    kind = event.get('event_type', '')
    outcome = event.get('outcome', '')
    action = str(event.get('action', '')).lower()
    metadata = event.get('metadata') or {}
    status = metadata.get('status')
    route = str(metadata.get('route') or metadata.get('path') or '')
    method = str(metadata.get('method') or '')

    if kind in AUTH or kind == 'auth.login':
        if outcome == 'rejected':
            return 'T1110'
        user = str(metadata.get('username') or '').lower()
        return 'T1078.001' if user in DEFAULT_ACCOUNTS else BENIGN

    tamper = _tamper(event, metadata)
    if tamper and kind in ('printer.command', 'api.request', 'http.request') + CONSOLE_COMMANDS:
        return tamper

    gcode = _gcode_of(event, metadata)
    if kind in CONSOLE_COMMANDS:
        command = str(metadata.get('command') or event.get('action') or '').lower().split(maxsplit=1)
        verb = command[0] if command else ''
        if kind == 'mqtt.command':
            if outcome == 'rejected':
                return MQTT_TECHNIQUE.get(verb, 'T1059')
            if gcode in GCODE_TECHNIQUE:
                return GCODE_TECHNIQUE[gcode]
            return 'T1489' if verb == 'cancel' else BENIGN
        if verb and verb not in PRINTER_VERBS:
            return shell_technique(verb, metadata.get('intent', ''))
        if gcode in GCODE_TECHNIQUE:
            return GCODE_TECHNIQUE[gcode]
        if outcome == 'rejected':
            return 'T1059'
        return 'T1489' if verb == 'cancel' else BENIGN

    if kind in UPLOADS:
        return 'T1203' if outcome == 'rejected' else BENIGN

    if kind == 'printer.job':
        return 'T1489' if action in ('cancel', 'cancelled', 'failed', 'error', 'abort') else BENIGN

    if kind == 'printer.command':
        if outcome == 'rejected':
            return 'T1059'
        # M115 is routine for web UIs, so only firmware-changing codes count here.
        return GCODE_TECHNIQUE.get(gcode, BENIGN) if gcode in ('M997', 'M117', 'M502', 'M500') else BENIGN

    if kind == 'api.request':
        if outcome == 'rejected':
            # A refused RPC call: host control (reboot/shutdown) or another blocked command.
            if any(word in method for word in ('reboot', 'shutdown', 'restart')):
                return 'T1529'
            # Listing a protected folder (Mainsail asks for config/logs/docs) is file discovery.
            return 'T1083' if method in ('server.files.get_directory', 'server.files.list',
                                         'server.files.metadata') else 'T1059'
        return 'T1489' if method.startswith('printer.print.cancel') else BENIGN

    if kind == 'network.connection':
        # Connected and left: a port scan. Sent non-protocol data: a service / version probe.
        return 'T1595.002' if outcome == 'invalid' else 'T1595.001'

    if kind == 'mqtt.subscribe':
        # Subscribing outside this printer's own topic tree enumerates the broker.
        topic = str(metadata.get('topic_filter') or '')
        own = f"honeyprint/{event.get('instance_id', '')}/"
        return BENIGN if topic.startswith(own) and outcome != 'rejected' else 'T1018'

    if kind in ('http.request', 'api.interaction'):
        if action == 'invalid':
            return 'T1595.002'  # not even valid HTTP: a service probe
        tool = scanner_tool(metadata.get('user_agent'))
        if tool:
            return tool
        if status == 404 and route not in ('frontend', 'asset'):
            return 'T1595'
        if status in (401, 403):
            if action in ('post', 'put', 'patch', 'delete'):
                return 'T1190'
            return BENIGN if route.startswith(UI_POLL_ROUTES) else 'T1595'
        if isinstance(status, int) and status >= 400 and route not in ('frontend', 'asset'):
            return 'T1595'
        return BENIGN

    if kind == 'ui.interaction':
        control = str(metadata.get('control') or '').lower()
        if 'emergency' in control:
            return 'T1529'
        return 'T1489' if 'cancel' in control else BENIGN
    return BENIGN


def am_detail(event):
    """Component and AM process-stage mapping (see dashboard/am_mapping.py)."""
    return map_event(event)


def process_stage(event):
    return am_detail(event)['label']


def _verdict(label, basis, confidence=None, status='', note=''):
    info = lookup(to_label(label) or BENIGN)
    return {**info, 'basis': basis, 'confidence': confidence, 'status': status, 'note': note}


def _text(verdict):
    if verdict['label'] == BENIGN:
        return ''
    return f"{verdict['section']} → {verdict['subsection']} ({verdict['mitre']})"


def describe_event(event):
    verdict = _verdict(rule_label(event), 'rule')
    am = am_detail(event)
    return {'attack_classification': _text(verdict) + ' [rule]', 'process_mapping': am['label'],
            'am': am, 'verdict': verdict}


def describe_rows(rows):
    return [{**describe_event(row['event']), **row} for row in rows]


# ML statuses where the transparent rule result is shown instead.
ML_FALLBACK = frozenset(('unsupported_event_type', 'low_confidence'))


def merge_prediction(row, section, subsection, confidence, status):
    """Combine a stored ML prediction with the rule verdict for display."""
    rule = row.get('verdict') or describe_event(row['event'])['verdict']
    ml = {'section': section, 'subsection': subsection, 'confidence': confidence, 'status': status}
    label = to_label(subsection)
    if label == BENIGN and rule['label'] != BENIGN and status not in ML_FALLBACK:
        # The model can add detections from sequences; it never hides a rule detection.
        verdict = dict(rule, basis='rule', status=status,
                       note=f'Rule technique kept (model gave none, {confidence:.2f})')
    elif label and str(rule['label']).startswith(label + '.') and status not in ML_FALLBACK:
        # The rule found the specific sub-technique of the model's parent technique
        # (e.g. model T1595, rule T1595.002 from a scanner tool): show the more specific one.
        verdict = dict(rule, basis='rule', status=status,
                       note=f'Sub-technique from rule (model: {label}, {confidence:.2f})')
    elif status in ML_FALLBACK or not label:
        note = (f'Model confidence {confidence:.2f} below gate; rule used' if status == 'low_confidence'
                else 'Event type not in model training; rule used')
        verdict = dict(rule, basis='rule', status=status, note=note)
    else:
        note = ('Single event: parent technique shown until it repeats'
                if status == 'insufficient_sequence' else '')
        verdict = _verdict(label, 'ml', confidence, status, note)
    suffix = (f" [ML {verdict['confidence']:.2f}]" if verdict['basis'] == 'ml' else ' [rule]')
    return {**row, 'verdict': verdict, 'ml_prediction': ml,
            'attack_classification': _text(verdict) + suffix}

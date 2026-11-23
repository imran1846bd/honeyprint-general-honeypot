"""Map each observed event to a HoneyPrint component and its AM process stage.

The component -> AM process-stage table comes from the HoneyPrint component
reference (configs/reference/component_am_mapping.csv, extracted from
honey3dprint_.xlsx). The event -> component step below is a transparent rule
set, so new interaction types map consistently without retraining.
"""

import csv
from functools import lru_cache
from pathlib import Path

TABLE = Path(__file__).resolve().parents[1] / 'configs' / 'reference' / 'component_am_mapping.csv'
# Stage names exactly as in the AM Process Flow diagram (Software / Hardware / Network).
AM_STAGE_NAMES = {
    'Stage 1': 'Job Intake & Digital Authorization', 'Stage 2': 'Build Preparation: Slicing & Simulation',
    'Stage 3': 'Machine Preparation & Build Execution', 'Stage 4': 'Post-processing & Finishing',
    'Stage 5': 'Inspection, Certification & Feedback', 'N/A': 'No direct AM process counterpart'}

# Every element of the AM Process Flow diagram (AM_Process_Flow_Software_Hardware_Network.drawio).
# The component table may only map to these; tests/test_dashboard_classification.py checks it.
AM_FLOW = {
    'Stage 1': {'Software': ('CAD/CAM & design viewers', 'PLM/PDM or print-management', 'Authentication / authorization'),
                'Hardware': ('Engineering workstation', 'Operator terminal / HMI', 'Data & coordination server'),
                'Network': ('Enterprise LAN / WAN / VPN', 'File, API, or cloud transfer', 'Identity & access channels')},
    'Stage 2': {'Software': ('Mesh repair & orientation', 'Slicer + support generation', 'Simulation / verification'),
                'Hardware': ('Engineering / build workstation', 'Job-package storage'),
                'Network': ('Enterprise LAN / file server', 'Cloud / API synchronization', 'Transfer to printer network')},
    'Stage 3': {'Software': ('Printer firmware & motion control', 'Build-execution software / HMI', 'In-situ monitoring & analytics'),
                'Hardware': ('AM machine / 3D printer', 'Printer controller (MCU / SoC)', 'Cameras, thermal & process sensors'),
                'Network': ('OT / industrial network', 'Ethernet / Wi-Fi / USB links', 'Monitoring & logging channels')},
    'Stage 4': {'Software': ('Post-process recipes / control', 'Scheduling & work instructions', 'Traceability / process logging'),
                'Hardware': ('Support-removal tools', 'Wash / cure / furnace equipment', 'CNC & surface-finishing systems'),
                'Network': ('OT network & device links', 'MES / workstation synchronization', 'Process-data transfer')},
    'Stage 5': {'Software': ('Metrology / NDE analysis', 'Quality & certification software', 'MES / PLM feedback systems'),
                'Hardware': ('CMM / 3D scanner', 'Microscope / NDE equipment', 'Mechanical test systems'),
                'Network': ('QC data upload / database', 'Enterprise OT feedback path', 'Reporting / API exchange')},
}

# WebSocket / JSON-RPC methods carry no URL route, so they are mapped by method.
RPC_COMPONENT = {
    'server.history.': 'Session Timeline',                                  # job history -> traceability
    'server.temperature_store': 'Runtime Telemetry (States, Sensors, Events)',
    'server.gcode_store': 'Runtime Telemetry (States, Sensors, Events)',
    'machine.proc_stats': 'Runtime Telemetry (States, Sensors, Events)',
    'server.database.': 'State Management',                                # saved interface state
    'server.connection.identify': 'Session Control',
    'server.webcams.': 'Sensor Emulation',                                  # cameras
    'server.config': 'Host Configuration',
    'server.announcements.': 'REST API',
    'printer.restart': 'Power & Failure Model (Normal / Faulty behavior)',
    'printer.firmware_restart': 'Power & Failure Model (Normal / Faulty behavior)',
}

SHELL_VERBS = frozenset(('ls', 'cat', 'env', 'find', 'id', 'ps', 'uname', 'whoami', 'wget', 'curl',
                         'sh', 'bash', 'pwd', 'cd', 'echo', 'netstat', 'ifconfig', 'ip', 'chmod',
                         'rm', 'busybox', 'nc', 'python', 'tftp', 'history', 'sudo', 'su'))
THERMAL = frozenset(('M104', 'M109', 'M140', 'M190', 'M105', 'M106', 'M107', 'M303'))
MOTION = frozenset(('G0', 'G1', 'G2', 'G3', 'G28', 'G29', 'G90', 'G91', 'M114', 'M84', 'M17', 'M18',
                    'M201', 'M203', 'M204', 'M205'))
EXTRUSION = frozenset(('G92', 'M82', 'M83', 'M221', 'G10', 'G11'))
FILE_VERBS = frozenset(('files', 'show', 'select', 'upload', 'ls_gcode', 'delete'))
JOB_VERBS = frozenset(('print', 'start', 'pause', 'resume', 'cancel', 'job', 'abort'))
STATE_VERBS = frozenset(('status', 'temperature', 'temp', 'position', 'connect', 'help'))


@lru_cache(maxsize=1)
def components():
    with TABLE.open(newline='') as source:
        return {row['component']: row for row in csv.DictReader(source)}


def component_for(kind, action='', outcome='', command='', gcode='', method='', route='', status=None):
    """Return the HoneyPrint component that handled this kind of event."""
    kind, action = str(kind or ''), str(action or '').lower()
    verb = str(command or '').strip().lower().split(maxsplit=1)
    verb = verb[0] if verb else ''
    gcode = str(gcode or '').upper()
    if not gcode and verb[:1] in ('g', 'm') and verb[1:].isdigit():
        gcode = verb.upper()
    method, route = str(method or ''), str(route or '').lower()

    if kind.endswith('.auth') or kind == 'auth.login':
        return 'Authentication'
    if kind.endswith('.session'):
        return 'Session Control'
    if kind == 'websocket.connection':
        return 'WebSocket'
    if kind == 'network.connection':
        return 'Network Simulation'  # a bare TCP connection to one of the printer's ports
    if kind in ('ssh.upload', 'telnet.upload', 'mqtt.upload'):
        return 'File Management'
    if kind == 'file.artifact':
        return 'G-code & File Artifacts (Uploaded / Generated)'
    if kind in ('printer.job', 'manufacturing.stage'):
        return 'Job Control'
    if kind == 'printer.state':
        if action in ('shutdown', 'firmware_restart', 'recovered'):
            return 'Power & Failure Model (Normal / Faulty behavior)'
        return 'Printer State'
    if kind in ('platform.event', 'audit.started'):
        return 'Runtime Telemetry (States, Sensors, Events)'
    if kind == 'mqtt.subscribe':
        return 'Runtime Telemetry (States, Sensors, Events)'
    if kind == 'ui.interaction':
        return 'Web UI'

    if kind in ('ssh.command', 'telnet.command', 'mqtt.command', 'mqtt.publish', 'printer.command'):
        if verb in SHELL_VERBS:
            return 'Other Services'
        if gcode == 'M112':
            return 'Safety Limits'
        if gcode == 'M115':
            return 'Firmware Identity'
        if gcode in THERMAL or verb in ('heat', 'bed', 'cooldown', 'preheat'):
            return 'Thermal Model (Hotend, Bed)'
        if gcode in EXTRUSION:
            return 'Extrusion Model (Material flow)'
        if gcode in MOTION or verb in ('move', 'home'):
            return 'Motion Model (X/Y/Z, Stepper)'
        if verb in FILE_VERBS or action in FILE_VERBS:
            return 'File Management'
        if verb in JOB_VERBS or action in JOB_VERBS:
            return 'Job Control'
        if verb in STATE_VERBS or action in STATE_VERBS:
            return 'Printer State'
        if gcode:
            return 'G-code Parser'
        return 'Command Handler'

    if kind in ('api.request', 'http.request', 'api.interaction'):
        if any(part in route for part in ('login', 'auth', 'session', 'access/')) or method.startswith('access.'):
            return 'Authentication'
        if method.startswith('server.files.') or any(part in route for part in ('file', 'upload', 'gcodes')):
            return 'File Management'
        if method.startswith('printer.print.') or any(part in route for part in ('/api/job', 'job_queue')) \
                or method.startswith('server.job_queue.'):
            return 'Job Control'
        if method == 'printer.emergency_stop':
            return 'Safety Limits'
        # Same lookup for the HTTP form of a call: /server/history/list -> server.history.list
        name = method or (route.strip('/').replace('/', '.') if route.startswith(('/server/', '/machine/', '/printer/')) else '')
        for prefix, component in RPC_COMPONENT.items():
            if name == prefix or name.startswith(prefix if prefix.endswith('.') else prefix + '.'):
                return component
        if route == '/websocket':
            return 'WebSocket'
        if method.startswith('printer.gcode.') or '/printer/gcode' in route or '/api/printer/command' in route:
            return 'Command Handler'
        if method.startswith('printer.objects.') or '/printer/objects' in route or route.startswith('/api/printer'):
            return 'Printer State'
        if method.startswith(('server.info', 'printer.info', 'machine.')) or 'version' in route or route.endswith('/info'):
            return 'Firmware Identity'
        if route in ('frontend', 'asset'):
            return 'Web UI'
        if status == 404 or route in ('other', 'unknown', ''):
            return 'Access Logging'
        return 'REST API'
    return 'Access Evidence'


# A file travelling to the printer (upload) is the Network transfer element of the
# component's mapping; storing, listing or reading a file is the Hardware storage element.
TRANSFER = 'Stage 2 > Network > Transfer to printer network'
UPLOAD_KINDS = ('file.artifact', 'ssh.upload', 'telnet.upload', 'mqtt.upload')


def is_transfer(kind, action='', route=''):
    kind, action, route = str(kind or ''), str(action or '').lower(), str(route or '').lower()
    return ((kind in UPLOAD_KINDS and action in ('upload', 'add'))
            or (kind == 'http.request' and action in ('post', 'put') and 'upload' in route)
            or (kind == 'ui.interaction' and action in ('file_select', 'file_drop')))


def describe(component, prefer=''):
    """AM fields for a component; prefer picks one of its listed mappings (default: the primary)."""
    row = components().get(component)
    if not row:
        return {'component': component, 'honeypot_stage': '', 'honeypot_stage_name': '',
                'am_stage': 'N/A', 'am_stage_name': AM_STAGE_NAMES['N/A'], 'am_layer': '',
                'am_element': '', 'am_process_mapping': 'N/A', 'label': 'N/A (no direct AM process action)'}
    stage, layer, element = row['primary_am_stage'], row['primary_am_layer'], row['primary_am_element']
    listed = [part.strip() for part in row['am_process_mapping'].split(';')]
    if prefer and prefer in listed:
        stage, layer, element = (part.strip() for part in prefer.split('>', 2))
    label = (f"AM {stage}: {AM_STAGE_NAMES.get(stage, '')} › {layer} › {element}"
             if stage != 'N/A' else 'N/A (no direct AM process action)')
    return {'component': component, 'honeypot_stage': int(row['honeypot_stage']),
            'honeypot_stage_name': row['honeypot_stage_name'], 'am_stage': stage,
            'am_stage_name': AM_STAGE_NAMES.get(stage, ''), 'am_layer': layer,
            'am_element': element, 'am_process_mapping': row['am_process_mapping'],
            'label': label}


def map_event(event):
    metadata = event.get('metadata') or {}
    route = (metadata.get('route_group') if metadata.get('route') == 'other' else metadata.get('route')) \
        or metadata.get('path') or ''
    command = metadata.get('command') or (event.get('action') if event.get('event_type') == 'mqtt.command' else '')
    component = component_for(event.get('event_type'), event.get('action'), event.get('outcome'),
                              command, metadata.get('gcode'), metadata.get('method'), route,
                              metadata.get('status'))
    if event.get('event_type') == 'ui.interaction' and event.get('action') in ('file_select', 'file_drop'):
        component = 'File Management'  # choosing a file to send is file handling, not page browsing
    return describe(component, TRANSFER if is_transfer(event.get('event_type'), event.get('action'), route) else '')


def map_features(row):
    """Same mapping for a training-CSV row (features only, no raw metadata)."""
    status = row.get('http_status')
    try:
        status = int(float(status)) if status not in (None, '') else None
    except ValueError:
        status = None
    route = row.get('route') or row.get('route_family')
    component = component_for(row.get('event_type'), row.get('action'), row.get('outcome'),
                              row.get('command'), row.get('gcode'), row.get('method'), route, status)
    return describe(component, TRANSFER if is_transfer(row.get('event_type'), row.get('action'), route) else '')

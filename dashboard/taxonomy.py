"""MITRE ATT&CK classification shared by rules, dataset builder, model and dashboard.

Every class label is a MITRE ATT&CK Enterprise technique or sub-technique ID
(v20.1). A honeypot has no legitimate users, so routine printer use is also
mapped to its closest technique (status read → T1082, upload → T1105, print
command → T1059); see activity_technique(). Section = one of the 15 Enterprise
tactics, subsection = technique, and sub-technique when one applies, matching
https://attack.mitre.org/ (e.g. Reconnaissance → Active Scanning → Wordlist Scanning).

Names, tactics and URLs are read from configs/reference/mitre_techniques.csv,
which was extracted from the official ATT&CK STIX bundles, so nothing is typed by hand.
"""

import csv
from functools import lru_cache
from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[1] / 'configs' / 'reference'
TECHNIQUES = REFERENCE / 'mitre_techniques.csv'
TABLE = REFERENCE / 'attack_taxonomy.csv'
ATTACK_VERSION = '20.1'

# Internal value for printer telemetry that no one sent (state polls, plugin
# state). It is never a model class and never shown on the dashboard.
BENIGN = 'BENIGN'

# The 15 Enterprise ATT&CK tactics, in matrix order (https://attack.mitre.org/).
# These are the only sections the dashboard uses.
ENTERPRISE_TACTICS = (
    ('TA0043', 'Reconnaissance'), ('TA0042', 'Resource Development'), ('TA0001', 'Initial Access'),
    ('TA0002', 'Execution'), ('TA0003', 'Persistence'), ('TA0004', 'Privilege Escalation'),
    ('TA0005', 'Stealth'), ('TA0112', 'Defense Impairment'), ('TA0006', 'Credential Access'),
    ('TA0007', 'Discovery'), ('TA0008', 'Lateral Movement'), ('TA0009', 'Collection'),
    ('TA0011', 'Command and Control'), ('TA0010', 'Exfiltration'), ('TA0040', 'Impact'))
TACTIC_ORDER = [name for _, name in ENTERPRISE_TACTICS]

# label: (Enterprise tactic ID, severity, HoneyPrint behaviour)
# Every label is an Enterprise technique listed under that tactic on attack.mitre.org
# and observable through the honeypot's HTTP, SSH, Telnet, MQTT or G-code interfaces.
LABELS = {
    # Reconnaissance
    'T1595': ('TA0043', 'low', 'Web interface request or a failed / rejected endpoint request'),
    'T1595.001': ('TA0043', 'low', 'Port scan: connection to a printer port closed without a request'),
    'T1595.002': ('TA0043', 'medium', 'Service / vulnerability probe (nmap -sV, malformed request, scanner tool)'),
    'T1595.003': ('TA0043', 'low', 'Repeated requests for non-existent paths'),
    'T1592.003': ('TA0043', 'low', 'Firmware identity query from a console (M115, version)'),
    # Initial Access
    'T1190': ('TA0001', 'medium', 'Unauthenticated write request to the printer API'),
    'T1078': ('TA0001', 'low', 'Successful login with valid credentials'),
    'T1078.001': ('TA0001', 'medium', 'Login with a default account name (admin, root, pi, ...)'),
    # Execution
    'T1059': ('TA0002', 'low', 'Command sent to the printer interpreter (console, G-code, API, web UI)'),
    'T1059.004': ('TA0002', 'medium', 'Unix shell command on the printer console'),
    'T1059.006': ('TA0002', 'medium', 'Python interpreter on the printer console'),
    'T1203': ('TA0002', 'high', 'Malformed or rejected G-code file uploaded to the printer'),
    # Persistence
    'T1053.003': ('TA0003', 'high', 'Cron job listing or creation (crontab, at)'),
    'T1098': ('TA0003', 'high', 'Account or admin change requested over MQTT'),
    'T1098.004': ('TA0003', 'high', 'SSH key added to authorized_keys'),
    'T1136.001': ('TA0003', 'high', 'Local account creation (useradd, adduser)'),
    'T1543.002': ('TA0003', 'high', 'Systemd service created or enabled'),
    'T1546.004': ('TA0003', 'high', 'Shell start-up file modified (.bashrc, .profile)'),
    # Privilege Escalation
    'T1548.001': ('TA0004', 'high', 'setuid / setgid bit set (chmod u+s)'),
    'T1548.003': ('TA0004', 'high', 'sudo attempt on the printer console'),
    'T1078.003': ('TA0004', 'high', 'Switch to another local user (su)'),
    # Stealth
    'T1070.003': ('TA0005', 'high', 'Command history cleared'),
    'T1070.004': ('TA0005', 'medium', 'File deletion on the console'),
    'T1140': ('TA0005', 'medium', 'Decoding hidden payloads (base64 -d, xxd -r)'),
    'T1564.001': ('TA0005', 'medium', 'Hidden file or directory created'),
    # Defense Impairment
    'T1222.002': ('TA0112', 'medium', 'File permission or owner change (chmod, chown)'),
    'T1685': ('TA0112', 'high', 'Printer safety or monitoring disabled (unsafe mode)'),
    'T1685.004': ('TA0112', 'high', 'Linux audit / SELinux disabled'),
    'T1685.006': ('TA0112', 'high', 'System logs cleared'),
    'T1686': ('TA0112', 'high', 'Firewall rules flushed or disabled'),
    'T1690': ('TA0112', 'high', 'Command history logging disabled (unset HISTFILE)'),
    # Credential Access
    'T1110': ('TA0006', 'medium', 'Rejected login'),
    'T1110.001': ('TA0006', 'medium', 'Repeated rejected logins for one account'),
    'T1110.003': ('TA0006', 'high', 'Rejected logins across many account names'),
    'T1003.008': ('TA0006', 'high', 'Reading /etc/shadow'),
    'T1552.001': ('TA0006', 'high', 'Searching files for passwords or tokens'),
    'T1552.003': ('TA0006', 'high', 'Reading shell history'),
    'T1552.004': ('TA0006', 'high', 'Reading SSH private keys'),
    'T1040': ('TA0006', 'high', 'Packet capture (tcpdump, tshark)'),
    # Discovery
    'T1083': ('TA0007', 'low', 'File or directory listing (files, ls, find, file API)'),
    'T1057': ('TA0007', 'medium', 'Process listing (ps, top, process statistics API)'),
    'T1033': ('TA0007', 'medium', 'User identity check (id, whoami, who)'),
    'T1082': ('TA0007', 'low', 'System or printer information (status, temperature, uname, info API)'),
    'T1016': ('TA0007', 'medium', 'Network configuration (ifconfig, ip, route)'),
    'T1016.001': ('TA0007', 'medium', 'Internet connectivity check (ping, traceroute)'),
    'T1049': ('TA0007', 'medium', 'Network connections (netstat, ss)'),
    'T1018': ('TA0007', 'low', 'Remote system enumeration (arp, MQTT wildcard subscribe)'),
    'T1046': ('TA0007', 'high', 'Port / service scan (nmap, nc -z)'),
    'T1007': ('TA0007', 'medium', 'Service listing (systemctl list-units)'),
    'T1124': ('TA0007', 'low', 'System time check (date, timedatectl)'),
    'T1087.001': ('TA0007', 'medium', 'Local account listing (/etc/passwd)'),
    'T1069.001': ('TA0007', 'medium', 'Local group listing (groups, /etc/group)'),
    'T1518': ('TA0007', 'medium', 'Installed software listing (dpkg, pip)'),
    'T1201': ('TA0007', 'medium', 'Password policy check (chage, login.defs)'),
    'T1614.001': ('TA0007', 'low', 'System language check (locale)'),
    'T1120': ('TA0007', 'medium', 'Peripheral / serial device listing (lsusb, dmesg, webcam list)'),
    'T1652': ('TA0007', 'medium', 'Kernel driver listing (lsmod)'),
    'T1497.001': ('TA0007', 'medium', 'Virtual machine / sandbox check (systemd-detect-virt)'),
    # Lateral Movement
    'T1021': ('TA0008', 'low', 'Telnet session to the printer'),
    'T1021.004': ('TA0008', 'medium', 'SSH session to the printer, or SSH from it to another host'),
    # Collection
    'T1005': ('TA0009', 'low', 'Reading local files or stored data (cat, job history, database)'),
    'T1560.001': ('TA0009', 'medium', 'Archiving files (tar, zip)'),
    'T1074.001': ('TA0009', 'medium', 'Copying files to a staging folder (/tmp)'),
    'T1119': ('TA0009', 'low', 'Subscription to the printer telemetry topics (MQTT)'),
    # Command and Control
    'T1071': ('TA0011', 'low', 'MQTT or WebSocket session to the printer'),
    'T1105': ('TA0011', 'medium', 'File transferred to the printer (G-code upload, wget, curl)'),
    'T1572': ('TA0011', 'high', 'Tunnel set up (ssh -R, socat, ngrok)'),
    # Exfiltration
    'T1041': ('TA0010', 'high', 'Repeated readout of stored print files (G-code IP) over the session'),
    'T1048.003': ('TA0010', 'high', 'File upload to an outside host (curl -T, nc, ftp put)'),
    # Impact
    'T1565': ('TA0040', 'high', 'Temperature, motion or settings outside the printer safe envelope'),
    'T1565.001': ('TA0040', 'high', 'Repeated replacement of the same stored G-code file'),
    'T1489': ('TA0040', 'medium', 'Print job or printer service stopped'),
    'T1529': ('TA0040', 'high', 'Emergency stop (M112), reboot or shutdown'),
    'T1485': ('TA0040', 'high', 'Print files or data deleted (rm -rf, delete all)'),
    'T1561.001': ('TA0040', 'high', 'Storage wiped or formatted (dd, mkfs, format)'),
    'T1495': ('TA0040', 'high', 'Firmware flash / update attempt (M997, flash)'),
    'T1491.001': ('TA0040', 'medium', 'Printer display message changed (M117)'),
    'T1486': ('TA0040', 'high', 'Files encrypted (openssl enc, gpg)'),
    'T1496.001': ('TA0040', 'high', 'Crypto-miner started on the printer host'),
    'T1531': ('TA0040', 'high', 'Account password changed or account removed'),
}

INTENT_TECHNIQUE = {
    'pipe_shell': 'T1105', 'decode': 'T1140', 'history_clear': 'T1070.003', 'history_disable': 'T1690',
    'write_authorized_keys': 'T1098.004', 'write_shell_rc': 'T1546.004', 'setuid': 'T1548.001',
    'read_shadow': 'T1003.008', 'read_passwd': 'T1087.001', 'read_group': 'T1069.001',
    'read_history': 'T1552.003', 'read_ssh_key': 'T1552.004', 'read_login_defs': 'T1201',
    'read_proc_cpu': 'T1082', 'secret_search': 'T1552.001', 'upload_out': 'T1048.003',
    'tunnel': 'T1572', 'firewall_off': 'T1686', 'audit_off': 'T1685.004', 'log_clear': 'T1685.006',
    'service_enable': 'T1543.002', 'service_stop': 'T1489', 'service_list': 'T1007',
    'remote_host': 'T1021.004', 'remote_url': 'T1105', 'external_ping': 'T1016.001',
    'stage_tmp': 'T1074.001', 'recursive_delete': 'T1485', 'hidden_path': 'T1564.001',
    'encrypt': 'T1486', 'port_scan': 'T1046',
}

MQTT_TECHNIQUE = {
    'reboot': 'T1529', 'shutdown': 'T1529', 'restart': 'T1529', 'poweroff': 'T1529', 'halt': 'T1529',
    'firmware': 'T1495', 'flash': 'T1495', 'update': 'T1495', 'upgrade': 'T1495',
    'delete': 'T1485', 'remove': 'T1485', 'rm': 'T1485', 'erase': 'T1485',
    'format': 'T1561.001', 'wipe': 'T1561.001',
    'set': 'T1098', 'admin': 'T1098', 'user': 'T1098', 'passwd': 'T1098', 'password': 'T1098',
    'execute': 'T1059.004', 'exec': 'T1059.004', 'shell': 'T1059.004', 'sh': 'T1059.004', 'system': 'T1059.004',
    'unsafe': 'T1685', 'disable': 'T1685', 'debug': 'T1685',
}

GCODE_TECHNIQUE = {'M997': 'T1495', 'M117': 'T1491.001', 'M502': 'T1565', 'M500': 'T1565', 'M115': 'T1592.003'}

DEFAULT_ACCOUNTS = frozenset(('admin', 'root', 'pi', 'user', 'ubuntu', 'octoprint', 'klipper', 'mks',
                              'guest', 'support', 'default', 'printer', 'administrator', 'test'))

SHELL_TECHNIQUE = {
    **dict.fromkeys(('ls', 'dir', 'tree', 'pwd', 'find', 'locate', 'stat', 'file'), 'T1083'),
    **dict.fromkeys(('ps', 'top', 'pgrep', 'htop'), 'T1057'),
    **dict.fromkeys(('id', 'whoami', 'who', 'w', 'last', 'users', 'logname'), 'T1033'),
    **dict.fromkeys(('uname', 'hostname', 'uptime', 'env', 'printenv', 'df', 'free', 'lscpu', 'hostnamectl'), 'T1082'),
    **dict.fromkeys(('ifconfig', 'ip', 'route', 'iwconfig', 'nmcli'), 'T1016'),
    **dict.fromkeys(('ping', 'traceroute', 'tracepath', 'mtr'), 'T1016.001'),
    **dict.fromkeys(('netstat', 'ss', 'lsof'), 'T1049'),
    **dict.fromkeys(('arp', 'avahi-browse'), 'T1018'),
    **dict.fromkeys(('nmap', 'masscan'), 'T1046'),
    **dict.fromkeys(('date', 'timedatectl'), 'T1124'),
    **dict.fromkeys(('groups',), 'T1069.001'),
    **dict.fromkeys(('getent',), 'T1087.001'),
    **dict.fromkeys(('dpkg', 'rpm', 'apt', 'apt-get', 'opkg', 'pip', 'pip3', 'snap'), 'T1518'),
    **dict.fromkeys(('chage',), 'T1201'),
    **dict.fromkeys(('locale',), 'T1614.001'),
    **dict.fromkeys(('lsusb', 'dmesg', 'lspci'), 'T1120'),
    **dict.fromkeys(('lsmod', 'modinfo'), 'T1652'),
    **dict.fromkeys(('systemd-detect-virt', 'dmidecode', 'virt-what'), 'T1497.001'),
    **dict.fromkeys(('cat', 'head', 'tail', 'less', 'more', 'strings', 'xxd', 'hexdump'), 'T1005'),
    **dict.fromkeys(('tar', 'zip', 'gzip', '7z', 'bzip2', 'xz'), 'T1560.001'),
    **dict.fromkeys(('cp', 'mv', 'rsync'), 'T1074.001'),
    **dict.fromkeys(('rm', 'unlink', 'shred'), 'T1070.004'),
    **dict.fromkeys(('chmod', 'chown', 'chattr'), 'T1222.002'),
    **dict.fromkeys(('sudo',), 'T1548.003'),
    **dict.fromkeys(('su',), 'T1078.003'),
    **dict.fromkeys(('crontab', 'at'), 'T1053.003'),
    **dict.fromkeys(('useradd', 'adduser'), 'T1136.001'),
    **dict.fromkeys(('passwd', 'chpasswd', 'deluser', 'userdel', 'usermod'), 'T1531'),
    **dict.fromkeys(('systemctl', 'service'), 'T1007'),
    **dict.fromkeys(('kill', 'killall', 'pkill'), 'T1489'),
    **dict.fromkeys(('reboot', 'shutdown', 'halt', 'poweroff', 'init'), 'T1529'),
    **dict.fromkeys(('dd', 'mkfs', 'wipefs'), 'T1561.001'),
    **dict.fromkeys(('flashrom', 'avrdude', 'dfu-util'), 'T1495'),
    **dict.fromkeys(('xmrig', 'minerd', 'cpuminer'), 'T1496.001'),
    **dict.fromkeys(('tcpdump', 'tshark'), 'T1040'),
    **dict.fromkeys(('wget', 'curl', 'tftp', 'scp', 'ftp'), 'T1105'),
    **dict.fromkeys(('ssh',), 'T1021.004'),
    **dict.fromkeys(('socat', 'ngrok', 'chisel'), 'T1572'),
    **dict.fromkeys(('python', 'python3'), 'T1059.006'),
    **dict.fromkeys(('openssl', 'gpg'), 'T1486'),
    **dict.fromkeys(('base64',), 'T1140'),
    **dict.fromkeys(('history',), 'T1552.003'),
}

# Earlier labels (v1–v4 strings and v5 ICS IDs) mapped to Enterprise labels.
LEGACY = {
    'Active Scanning: Wordlist Scanning': 'T1595.003',
    'Brute Force: Password Guessing': 'T1110.001', 'Unauthorised request': 'T1190',
    'Broker topic enumeration': 'T1018', 'Stored print-file readout': 'T1041',
    'Stored Data Manipulation (T1565.001 candidate)': 'T1565.001',
    'Malformed G-code upload': 'T1203', 'Thermal setpoint manipulation': 'T1565',
    'Out-of-range motion request': 'T1565', 'Print job interruption': 'T1489',
    'Virtual emergency stop': 'T1529', 'Unsupported MQTT printer command': 'T1059',
    'Rejected printer command': 'T1059', 'SSH shell command probing': 'T1059.004',
    'Single failed route; review context': 'T1595',
    'Unspecified failed route; review context': 'T1595',
    'Single failed login; review context': 'T1110',
    'T0816': 'T1529', 'T0826': 'T1489', 'T0821': 'T1203', 'T0888': 'T1018', 'T0882': 'T1041',
    'T0836': 'T1565', 'T0831': 'T1565', 'T1692.001': 'T1059',
}


# Closest technique for routine use of the printer. Each choice follows the
# technique's ATT&CK v20.1 description (configs/reference/mitre_techniques.csv).
SYSTEM_EVENTS = frozenset(('platform.event', 'printer.state', 'audit.started', 'manufacturing.stage'))
LOGINS = ('ssh.auth', 'telnet.auth', 'mqtt.auth', 'auth.login')
FILE_TRANSFERS = ('ssh.upload', 'telnet.upload', 'mqtt.upload', 'file.artifact')
CONSOLES = ('ssh.command', 'telnet.command', 'mqtt.command')
SESSION_TECHNIQUE = {'ssh.session': 'T1021.004', 'telnet.session': 'T1021',
                     'mqtt.session': 'T1071', 'websocket.connection': 'T1071'}
INFO_VERBS = frozenset(('status', 'temperature', 'temp', 'position', 'job', 'help', 'version', 'info'))
FILE_VERBS = frozenset(('files', 'gcodes', 'show', 'list'))
RPC_TECHNIQUE = {
    **dict.fromkeys(('server.info', 'printer.info', 'printer.objects.query', 'printer.objects.subscribe',
                     'printer.objects.list', 'machine.system_info', 'server.config',
                     'server.connection.identify', 'server.job_queue.status',
                     'server.announcements.list'), 'T1082'),
    **dict.fromkeys(('server.files.list', 'server.files.get_directory', 'server.files.metadata'), 'T1083'),
    **dict.fromkeys(('server.history.list', 'server.history.totals', 'server.gcode_store',
                     'server.temperature_store', 'server.database.list', 'server.database.get_item'), 'T1005'),
    'machine.proc_stats': 'T1057', 'server.webcams.list': 'T1120',
}
API_ACTION_TECHNIQUE = {'files_list': 'T1083', 'job_status': 'T1082', 'printer_state': 'T1082',
                        'version_check': 'T1082'}


def activity_technique(features):
    """MITRE label for routine activity, from an event's model features.

    ``features`` needs event_type, action and, where present, command, method
    and route_family (as built by telemetry/analysis/ml_service.FeatureState).
    Returns BENIGN only for printer telemetry that no one sent.
    """
    kind = str(features.get('event_type') or '')
    action = str(features.get('action') or '').lower()
    words = str(features.get('command') or '').lower().split(maxsplit=1)
    verb = words[0] if words else ''
    if kind in SYSTEM_EVENTS or (kind == 'printer.job' and action == 'progress'):
        return BENIGN
    if kind in LOGINS:
        return 'T1078'
    if kind in FILE_TRANSFERS:
        return 'T1105'
    if kind in SESSION_TECHNIQUE:
        return SESSION_TECHNIQUE[kind]
    if kind == 'mqtt.subscribe':
        return 'T1119'
    if kind == 'network.connection':
        return 'T1595'  # connected and left without a request: port scan / service probe
    if kind in CONSOLES:
        verb = verb or action
        if verb == 'upload':
            return 'T1105'
        if verb in INFO_VERBS:
            return 'T1082'
        if verb in FILE_VERBS:
            return 'T1083'
        return 'T1059'
    if kind == 'api.request':
        return RPC_TECHNIQUE.get(str(features.get('method') or ''), 'T1059')
    if kind == 'api.interaction':
        return API_ACTION_TECHNIQUE.get(action, 'T1059')
    if kind == 'http.request':
        if action == 'get':
            return 'T1082' if features.get('route_family') in ('printer', 'server', 'api') else 'T1595'
        return 'T1059'
    if kind == 'ui.interaction':
        if action in ('page_view', 'navigate', 'page_leave'):
            return 'T1082'  # viewing the printer's web pages
        if action in ('file_select', 'file_drop'):
            return 'T1105'  # choosing a file to send to the printer
        return 'T1059'
    # printer.command, printer.job, mqtt.publish
    return 'T1059'


def route_family(route):
    route = str(route or '')
    return ('api' if route.startswith('/api/') else 'printer' if route.startswith('/printer/') else
            'server' if route.startswith('/server/') else 'unknown' if route else '')


def shell_technique(command, intent=''):
    """Enterprise technique for a console command: argument intent first, then the verb."""
    if intent in INTENT_TECHNIQUE:
        return INTENT_TECHNIQUE[intent]
    verb = str(command or '').strip().lower().split(maxsplit=1)
    return SHELL_TECHNIQUE.get(verb[0] if verb else '', 'T1059.004')


def to_label(old_label, command=''):
    """Convert a legacy label (or an existing MITRE label) to a MITRE label.

    BENIGN / 'None' (older datasets and models) stay BENIGN: routine activity whose
    technique is chosen from the event itself by activity_technique().
    """
    if old_label in (BENIGN, 'None'):
        return BENIGN
    if old_label in LABELS:
        return old_label
    if old_label == 'SSH shell command probing':
        return shell_technique(command)
    return LEGACY.get(old_label, '')


@lru_cache(maxsize=1)
def techniques():
    with TECHNIQUES.open(newline='') as source:
        return {row['technique_id']: row for row in csv.DictReader(source)}


def tactics():
    return dict(ENTERPRISE_TACTICS)


@lru_cache(maxsize=None)
def lookup(label, _section=''):
    """Display fields for a label: tactic → technique → sub-technique."""
    label = to_label(label) or label
    if label not in LABELS:
        # Printer telemetry that no one sent: no ATT&CK fields, nothing to show.
        return {'label': BENIGN, 'section': '', 'category': '', 'tactic_id': '',
                'technique_id': '', 'technique': '', 'subtechnique_id': '',
                'subtechnique': '', 'subsection': '', 'mitre': '', 'mitre_name': '',
                'mitre_domain': '', 'mitre_tactics': '', 'mitre_url': '', 'severity': 'info',
                'behaviour': ''}
    tactic_id, severity, behaviour = LABELS[label]
    ref = techniques()[label]
    parent_id = label.split('.')[0]
    parent = techniques()[parent_id]
    sub = label if '.' in label else ''
    tactic = tactics()[tactic_id]
    subsection = parent['technique_name'] + (f" › {ref['technique_name']}" if sub else '')
    return {'label': label, 'section': tactic, 'category': tactic, 'tactic_id': tactic_id,
            'technique_id': parent_id, 'technique': parent['technique_name'],
            'subtechnique_id': sub, 'subtechnique': ref['technique_name'] if sub else '',
            'subsection': subsection, 'mitre': label, 'mitre_name': ref['technique_name'],
            'mitre_domain': 'Enterprise',
            'mitre_tactics': tactic, 'mitre_url': ref['technique_url'], 'severity': severity,
            'behaviour': behaviour}


@lru_cache(maxsize=1)
def enterprise_matrix():
    """All 15 Enterprise tactics with every technique and sub-technique, in ATT&CK order."""
    rows = [r for r in techniques().values() if r['domain'] == 'enterprise']
    subs = {}
    for r in rows:
        if r['is_subtechnique'] == 'True':
            subs.setdefault(r['technique_id'].split('.')[0], []).append(
                {'id': r['technique_id'], 'name': r['technique_name'], 'url': r['technique_url']})
    matrix = []
    for tid, name in ENTERPRISE_TACTICS:
        parents = sorted((r for r in rows if r['is_subtechnique'] != 'True'
                          and name in r['tactic_names'].split(';')), key=lambda r: r['technique_name'])
        matrix.append({'tactic_id': tid, 'tactic': name, 'url': f'https://attack.mitre.org/tactics/{tid}/',
                       'techniques': [{'id': r['technique_id'], 'name': r['technique_name'],
                                       'url': r['technique_url'],
                                       'subs': sorted(subs.get(r['technique_id'], []), key=lambda x: x['id'])}
                                      for r in parents]})
    return matrix


def section_of(label):
    return lookup(label)['section']


def write_reference(path=TABLE):
    columns = ['label', 'section', 'tactic_id', 'technique_id', 'technique', 'subtechnique_id',
               'subtechnique', 'subsection', 'mitre_domain', 'mitre_url', 'severity', 'behaviour']
    with Path(path).open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(lookup(label) for label in LABELS)
    return path

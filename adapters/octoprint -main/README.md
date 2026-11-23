# HoneyPrint: A Network-based Additive Manufacturing Honeypot Framework

HoneyPrint is a **high-interaction honeypot framework** that realistically simulates a complete networked 3D printer environment. It is designed to attract, deceive, and analyze adversaries who target additive manufacturing (AM) infrastructure over the Internet.

HoneyPrint is directly inspired by **HoneySat** (Lopez-Morales et al., NDSS 2026), the first satellite honeypot framework. We replicate HoneySat's proven architecture --- ground segment simulations, space segment simulations, physics-aware telemetry, and modular personality system --- and adapt every component to the domain of FDM 3D printing.

To the best of our knowledge, **no AM-specific honeypot exists in the literature**, making HoneyPrint the first dedicated honeypot for additive manufacturing systems.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Architecture Overview](#2-architecture-overview)
3. [Component Details](#3-component-details)
4. [HoneySat-to-HoneyPrint Mapping](#4-honeysat-to-honeyprint-mapping)
5. [Printer Personalities](#5-printer-personalities)
6. [Supported TTPs](#6-supported-ttps)
7. [Requirements](#7-requirements)
8. [Installation & Setup](#8-installation--setup)
9. [Running HoneyPrint](#9-running-honeyprint)
10. [Configuration](#10-configuration)
11. [Interacting with the Honeypot](#11-interacting-with-the-honeypot)
12. [Logging & Data Collection](#12-logging--data-collection)
13. [Deployment Recommendations](#13-deployment-recommendations)
14. [Project Structure](#14-project-structure)
15. [Extending HoneyPrint](#15-extending-honeyprint)

---

## 1. Motivation

Networked 3D printers represent a growing and largely unprotected attack surface. Thousands of OctoPrint instances are publicly exposed on the Internet (Shodan scans consistently find 3,000--6,000+ instances), many with default or no authentication. Documented attacks against AM systems include:

- **G-code injection / print sabotage** -- modifying G-code to introduce internal voids, alter dimensions, or weaken structural integrity while parts appear visually normal (Belikovetsky et al., "dr0wned", 2017).
- **IP theft** -- exfiltrating STL/G-code design files to enable counterfeiting.
- **Thermal attacks** -- overriding safety limits to overheat components, risking fire.
- **Ransomware** -- the manufacturing sector is a top ransomware target; 3D printer control systems are no exception.
- **Reconnaissance** -- attackers probe firmware versions (`M115`), file listings, and webcam feeds to map AM infrastructure.

Despite these threats, **there is no existing honeypot specifically designed for additive manufacturing**. Existing ICS honeypots (Conpot, HoneyPLC, HoneyICS) simulate industrial protocols like Modbus and S7, but none simulate the OctoPrint API, G-code command processing, or the thermal/kinematic physics of a 3D printer.

HoneyPrint fills this gap by providing a framework that:

1. **Captures rich interaction data** from adversaries targeting AM systems (Design Objective DO-1).
2. **Provides deception** by generating physics-backed telemetry that is indistinguishable from a real printer (DO-2).
3. **Is extensible and customizable** to simulate different printer models, firmware, and management ecosystems (DO-3).

---

## 2. Architecture Overview

HoneyPrint's architecture directly mirrors HoneySat's two-segment design (Fig. 2 in the HoneySat paper):

```
                                    HoneyPrint's Host
                                +--------------------------+
  Adversary's Host              |                          |
  +---------------+             | Ground Segment Sims      | Space Segment Sims
  |               |    (1)      | +--------------------+   | +--------------------+
  | Malicious     |------------>| | Exposed Network    |   | | Firmware Runtime   |
  | G-code /      |    (12)     | | Protocols          |   | | (Marlin Emulator)  |
  | Commands      |<------------| | (Web, Telnet, API) |   | |                    |
  |               |             | +--------+-----------+   | +--------+-----------+
  +---------------+             |          |               |          |
                                |     (2)  v               |     (4)  |
                                | +--------+-----------+   | +--------v-----------+
                                | | Management SW      |   | | Printer Services   |
                                | | (OctoPrint Sim)    |   | | (G-code Handler,   |
                                | +--------+-----------+   | |  SD Card, Temp     |
                                |          |               | |  Control)          |
                                |     (3)  v               | +--------+-----------+
                                | +--------+-----------+   |          |
                                | | Network Simulator  |   |     (6)  v
         +----------+           | +--------+-----------+   | +--------+-----------+
         | Log      |           |          |               | | Printer Simulator  |
         | Database |<----------+----------+               | | (Thermal, Motion,  |
         | (MongoDB)|           |                          | |  Extrusion, Power) |
         +----------+           |                          | +--------------------+
                                +--------------------------+
```

**Ground Segment** (operator/management side):
- Exposed Network Protocols (Web interface on port 80, Telnet on port 23)
- Management Software (OctoPrint-compatible REST API + WebSocket)
- Network Simulator (simulates network conditions, analogous to HoneySat's Radio Simulator)
- Ground Configuration (YAML-based settings)
- Logging Repository (MongoDB with JSON file fallback)

**Space Segment** (the simulated printer):
- Firmware Runtime (Marlin firmware emulation processing G-code "telecommands")
- Printer Services (G-code handler, SD card, temperature control)
- Printer Simulator (5 physics simulations: thermal, motion, extrusion, power, print job)
- Printer Personality (configurable printer profiles)

---

## 3. Component Details

### 3.1 Printer Simulator (`honeyprint/printer_simulator/`)

The Printer Simulator is the physics engine at the core of HoneyPrint, directly analogous to HoneySat's Satellite Simulator. It runs six interdependent simulations that communicate with each other to produce coherent, realistic sensor data in real time.

**Thermal Simulation** (`thermal_sim.py`):
- Models PID-controlled heating for the hotend and heated bed.
- Heat dissipation follows Newton's law of cooling: `Q_loss = h * (T - T_ambient) * C_thermal`.
- Part cooling fan affects hotend cooling rate.
- Gaussian noise is added to temperature readings to simulate real thermistor behavior.
- PID parameters are loaded from the printer personality profile, with different values for each printer model.
- Example: Ender-3 V2 hotend has `thermal_mass=4.5 J/K`, `heating_power=40W`, `cooling_coeff=0.08 W/K`, while the enclosed Bambu X1C has `thermal_mass=3.2 J/K`, `heating_power=65W`, `cooling_coeff=0.05 W/K` (enclosed printers cool slower).

**Motion Simulation** (`motion_sim.py`):
- Tracks the position of all four axes (X, Y, Z, E) in both absolute and relative modes.
- Enforces build volume boundaries from the printer personality.
- Simulates homing sequences with realistic timing.
- Calculates move duration from feedrate for realistic timing.
- Tracks layer changes based on Z-axis movement.

**Extrusion Simulation** (`extrusion_sim.py`):
- Tracks total filament extruded (length, volume, and weight).
- Enforces cold extrusion prevention (minimum extrusion temperature of 170C, matching Marlin's `EXTRUDE_MINTEMP`).
- Calculates material consumption based on filament cross-section and material density.
- Supports multiple filament types (PLA: 1.24 g/cm3, ABS: 1.04, PETG: 1.27, etc.).
- Tracks retraction state and flow rate adjustments.

**Power Simulation** (`power_sim.py`):
- Calculates electrical power draw from all subsystems: heaters, stepper motors, fans, and control electronics.
- Provides realistic current/voltage readings (e.g., 24V supply, 40W hotend heater, 200W bed heater).
- Tracks cumulative energy consumption in watt-hours.
- Motor power distinguishes between active movement and holding current.

**Print Job Simulation** (`print_job_sim.py`):
- Manages print job lifecycle: idle, printing, paused, cancelling, completed, error.
- Can execute uploaded G-code files line by line.
- Can simulate a "fake print" with configurable duration, layers, and filament usage for deception (so adversaries find a printer mid-print).
- Tracks progress, elapsed time, estimated remaining time, and layer count.
- Maintains a job history archive.

**Simulator Coordinator** (`simulator.py`):
- Orchestrates all simulation modules in an async update loop.
- Provides a unified interface for the firmware runtime to query sensor data and send commands.
- The power simulation queries thermal state; the extrusion simulation checks hotend temperature; simulations are interdependent just like in a real printer.

### 3.2 Firmware Runtime (`honeyprint/firmware/`)

Analogous to HoneySat's Flight Software Runtime (SUCHAI FS). This is the bridge between the exposed protocols and the Printer Simulator.

**G-code Handler** (`gcode_handler.py`):
- Parses G-code command strings into structured `GCodeCommand` objects.
- Handles standard G/M codes, parameters (X/Y/Z/E/F/S/P), inline comments, line numbers, and checksums.
- Analogous to HoneySat's TC (telecommand) parser.

**Marlin Emulator** (`marlin_emu.py`):
- Emulates Marlin 2.x firmware command processing with **50+ G-code and M-code commands**.
- Each command interacts with the Printer Simulator for physics-backed responses.
- Supported command categories:
  - **Movement**: G0/G1 (linear move), G4 (dwell), G10/G11 (retract/unretract), G28 (home), G29 (auto bed leveling), G90/G91 (absolute/relative mode), G92 (set position)
  - **Temperature**: M104/M109 (set/wait hotend), M140/M190 (set/wait bed), M105 (report temps), M155 (auto-report)
  - **Fan**: M106/M107 (fan on/off)
  - **SD Card**: M20 (list files), M21 (init SD), M23 (select file), M24/M25 (start/pause print), M27 (print status), M28/M29 (write file), M30 (delete file)
  - **Information**: M31 (print time), M114 (position), M115 (firmware info with capabilities), M119 (endstop status), M503 (all settings)
  - **Control**: M17/M18/M84 (enable/disable motors), M82/M83 (extruder absolute/relative), M112 (emergency stop), M220/M221 (feedrate/flow percentage)
  - **EEPROM**: M500/M501/M502 (save/load/reset settings)
- Temperature reporting follows Marlin's exact format: `T:200.15 /200.00 B:60.32 /60.00 @:64 B@:127`
- M115 returns realistic capability flags matching Marlin 2.x (EEPROM, AUTOLEVEL, SDCARD, THERMAL_PROTECTION, etc.).
- M109/M190 (wait commands) send periodic temperature reports while heating, just like real Marlin.

**SD Card Simulation** (`sd_card.py`):
- Virtual file system pre-populated with realistic G-code files (benchy.gcode, calibration_cube.gcode, phone_stand_v2.gcode).
- Generated G-code includes realistic headers (slicer name/version, filament usage, estimated print time) and layer structure.
- Supports file upload, delete, select, and list operations.
- **This is a key attack surface** -- adversaries upload malicious G-code files, and all uploads are logged for analysis.

### 3.3 OctoPrint Web Simulation (`honeyprint/octoprint_sim/`)

Analogous to HoneySat's Ground Software (SUCHAI MCS). This is the **primary exposed attack surface** -- a realistic OctoPrint-compatible web interface.

**REST API** (`api_routes.py`):
- Implements the full OctoPrint API surface that attackers are most likely to interact with:
  - `GET /api/version` -- server version (OctoPrint 1.9.3)
  - `POST /api/login` -- authentication (credential harvesting)
  - `GET/POST /api/connection` -- printer connection state
  - `GET /api/printer` -- full printer state (temperatures, flags)
  - `POST /api/printer/command` -- send G-code commands
  - `GET/POST /api/printer/tool` -- hotend temperature control
  - `GET/POST /api/printer/bed` -- bed temperature control
  - `GET /api/job`, `POST /api/job` -- print job status and control (start/pause/cancel)
  - `GET /api/files`, `POST /api/files/{location}` -- file listing and upload
  - `DELETE /api/files/{location}/{filename}` -- file deletion
  - `GET /api/settings` -- OctoPrint settings (includes API key, serial port, webcam URL)
  - `GET/POST /api/system/commands` -- system commands (shutdown, reboot, restart)
  - `GET /api/printerprofiles` -- printer profile with build volume, capabilities

**WebSocket Handler** (`websocket_handler.py`):
- Real-time printer status updates over WebSocket (`/ws` and `/sockjs/websocket`).
- Sends `connected` message with version info on connection.
- Broadcasts `current` messages every 1 second with temperature data, printer state, job progress.
- Matches the real OctoPrint WebSocket wire format.

**Authentication** (`auth.py`):
- Supports all OctoPrint authentication methods: `X-Api-Key` header, `Authorization: Bearer` header, `apikey` query parameter, session cookies.
- Default credentials: `admin`/`admin` with API key `A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6`.
- All login attempts (successful and failed) are logged with username, password, IP, and user-agent.

**Web UI** (`templates/index.html`, `static/`):
- Full OctoPrint-like dark-themed web interface with:
  - Temperature display with real-time updates (hotend and bed, with preset buttons for PLA/ABS/PETG)
  - Temperature chart (canvas-based, scrolling history)
  - Control panel (X/Y/Z jog buttons, home, extrude/retract, fan speed slider, motor disable)
  - Terminal (send G-code commands directly, see Marlin responses)
  - File manager (upload, print, delete G-code files)
  - Connection panel (serial port, baudrate, printer profile)
  - State panel (printer state, job progress with progress bar, print time, remaining time)
  - Webcam placeholder
- Login page with OctoPrint styling.
- Footer includes "This computer system is for authorized use only" notice (as per HoneySat's ethical practice).

### 3.4 Telnet Server (`honeyprint/telnet_server/`)

Analogous to HoneySat's Telnet exposed protocol. Provides direct serial/terminal access to the Marlin firmware emulator.

- Listens on port 23 (configurable).
- On connection, sends a realistic Marlin boot banner:
  ```
  start
  echo:Marlin 2.1.2.1
  echo: Last Updated: 2025-12-01 | Author: (default config)
  echo:Compiled: Dec 01 2025
  echo: Free Memory: 2408  PlannerBufferBytes: 1232
  echo:V87 stored settings retrieved (628 bytes; crc 12345)
  ```
- Processes G-code commands line by line through the Marlin emulator.
- Supports multiple concurrent connections.
- All commands, responses, IPs, and timestamps are logged.
- Supports async firmware messages (e.g., auto temperature reports via M155).

### 3.5 Logging Repository (`honeyprint/logging_repo/`)

Analogous to HoneySat's MongoDB Logging Repository.

- **MongoDB** (primary) with **JSON file fallback** when MongoDB is unavailable.
- Six collections:
  - `web_interactions` -- all HTTP/API requests (path, method, IP, user-agent, data, timestamp)
  - `telnet_interactions` -- Telnet sessions (IP, commands, responses, timestamps)
  - `login_attempts` -- all login attempts (username, password, IP, success flag, timestamp)
  - `gcode_commands` -- every G-code command sent to the printer (command, source, IP, timestamp)
  - `file_uploads` -- uploaded files (filename, size, content hash, IP, timestamp)
  - `system_events` -- honeypot lifecycle events (start, stop, errors)
- Every log entry is automatically enriched with UTC timestamp and source IP.
- `get_stats()` returns per-collection document counts.
- MongoDB is internal to the Docker network (not exposed to adversaries).

### 3.6 Network Simulator (`honeyprint/network_sim/`)

Analogous to HoneySat's Radio Simulator. While HoneySat simulates orbital passes (the satellite is only reachable during passes over a ground station), HoneyPrint simulates realistic network conditions for a 3D printer:

- **Schedule-based availability** -- the printer can go online/offline on a schedule (e.g., only available during business hours 08:00-18:00), simulating real printer usage patterns.
- **Latency injection** -- configurable base latency + jitter to simulate remote/cloud-connected printers.
- **Packet loss** -- probabilistic packet drop simulation.
- Manual override (force online/offline) for testing.
- Background async task evaluates the schedule every 30 seconds.

### 3.7 Printer Personality (`honeyprint/personality/`)

Analogous to HoneySat's Satellite Personality. A central configuration that defines all printer-specific parameters.

Four built-in profiles with **realistic specifications matching the real printers**:

| Profile | Printer | Firmware | Build Volume | Hotend Max | Bed Max | Enclosure |
|---------|---------|----------|-------------|-----------|---------|-----------|
| `ender3_v2` | Creality Ender-3 V2 | Marlin 2.1.2.1 | 220x220x250 | 260C | 100C | Open |
| `prusa_mk4` | Prusa MK4 | Prusa-Firmware-Buddy 5.1.2 | 250x210x220 | 290C | 120C | Open |
| `bambu_x1c` | Bambu Lab X1 Carbon | Bambu Firmware 01.07.00.00 | 256x256x256 | 300C | 110C | Enclosed |
| `voron_2_4` | VORON 2.4r2 350 | Klipper 0.12.0 | 350x350x340 | 300C | 120C | Enclosed |

Each profile includes differentiated thermal simulation parameters:
- **Open-frame printers** (Ender-3, Prusa) have higher cooling coefficients (heat dissipates faster).
- **Enclosed printers** (Bambu, Voron) have lower cooling coefficients and chamber thermal simulation.
- Thermal mass varies: larger beds have more thermal mass (Voron 350mm bed: 250 J/K vs Ender-3: 120 J/K).
- PID values are different per printer for realistic temperature control behavior.
- Heating power matches real specs (Ender-3: 40W hotend, 235W bed; Voron: 70W hotend, 750W AC bed).

### 3.8 Sensors (`honeyprint/sensors/`)

Analogous to HoneySat's Sensor Modules. Abstractions of 3D printer hardware sensors:

- **ThermistorSensor** -- reads from thermal simulation with noise, provides raw ADC values.
- **EndstopSensor** -- simulates limit switches (triggered/released state).
- **FilamentSensor** -- simulates filament runout detection.

### 3.9 Configuration (`honeyprint/config.py`, `config/honeyprint.yaml`)

Analogous to HoneySat's Ground Configuration. Centralized YAML-based configuration covering:

- Ground segment: ports, MongoDB URI, web credentials, API key, webcam settings.
- Printer personality: which profile to load.
- Simulation parameters: update intervals, ambient temperature, voltage, sensor noise.
- Logging: verbosity level, which protocols to log, PCAP capture toggle.
- Network simulator: latency, jitter, packet loss, schedule.
- Supports environment variable overrides with `HONEYPRINT_` prefix.

---

## 4. HoneySat-to-HoneyPrint Mapping

This table shows how every HoneySat component maps to its HoneyPrint equivalent:

| Segment | HoneySat Component | HoneyPrint Component | File(s) |
|---------|-------------------|---------------------|---------|
| Ground | Exposed Network Protocols (VNC, Telnet, Web, Raw) | Web (port 80), Telnet (port 23), API | `octoprint_sim/`, `telnet_server/` |
| Ground | Ground Software (SUCHAI MCS) | OctoPrint REST API + WebSocket | `api_routes.py`, `websocket_handler.py` |
| Ground | Radio Simulator | Network Simulator | `network_sim/simulator.py` |
| Ground | Ground Configuration | `config/honeyprint.yaml` | `config.py` |
| Ground | Logging Repository (MongoDB) | Logging Repository (MongoDB + JSON fallback) | `logging_repo/logger.py` |
| Space | Flight Software Runtime (SUCHAI FS) | Firmware Runtime (Marlin Emulator) | `firmware/marlin_emu.py` |
| Space | Flight Software Services | G-code Handler + SD Card + Services | `firmware/gcode_handler.py`, `sd_card.py` |
| Space | Satellite Simulator (6 sims) | Printer Simulator (5 sims) | `printer_simulator/` |
| Space | Satellite Personality | Printer Personality (4 profiles) | `personality/profiles/` |
| Space | Telecommands (TC) | G-code Commands | G-code is the AM equivalent of satellite TCs |
| Space | Telemetry (TM) | Temperature/Position/Status Reports | Marlin response strings are the AM equivalent of TM |
| -- | Orbital Simulation | Print Job Simulation | Tracks job progress instead of orbital position |
| -- | Power System Simulation | Power Simulation | Both track power consumption |
| -- | Thermal Simulation | Thermal Simulation | Both use physics-based models |
| -- | Magnetic Simulation | Extrusion Simulation | AM-specific: filament flow instead of magnetic field |
| -- | RGB Camera Simulation | Webcam Placeholder | Both simulate onboard imaging |
| -- | CSP / CCSDS Protocols | OctoPrint API / G-code Protocol | Domain-specific protocol ecosystems |
| -- | SPACE-SHIELD TTPs | MITRE ATT&CK for ICS TTPs | Security frameworks for TTP classification |
| Deploy | Docker Compose (4 containers) | Docker Compose (2 containers) | `docker-compose.yml`, `Dockerfile` |

---

## 5. Printer Personalities

Personalities are YAML files in `honeyprint/personality/profiles/` that define everything about the simulated printer. Switching profiles changes:

- Firmware identification string (what `M115` reports)
- Build volume and capabilities
- Temperature limits
- Thermal behavior (PID values, heating rates, cooling characteristics)
- Communication settings (serial port, baud rate)
- Supported features (auto bed leveling, filament sensor, heated chamber, etc.)

To create a custom personality, copy an existing profile and modify the values.

---

## 6. Supported TTPs

Mapping HoneyPrint's capabilities to MITRE ATT&CK for ICS:

| Tactic | Technique | HoneyPrint Support |
|--------|-----------|-------------------|
| Reconnaissance | System Service Discovery | `M115` firmware info, `/api/version`, `/api/settings` |
| Reconnaissance | Gather Victim Information | Web interface, file listing, printer profiles |
| Initial Access | Default Credentials | `admin`/`admin`, default API key |
| Initial Access | Exploit Public-Facing Application | OctoPrint API, Telnet |
| Execution | Unauthorized Command Message | G-code command processing via API/Telnet |
| Execution | Native API | OctoPrint REST API |
| Persistence | Modify Program | Upload malicious G-code files |
| Collection | Data from Information Repositories | Download G-code files (IP theft) |
| Collection | Adversary in the Middle | API key interception from settings |
| Command & Control | Standard Application Layer Protocol | HTTP API, WebSocket, Telnet |
| Impact | Manipulation of Control | Modify temperatures, movement, extrusion |
| Impact | Denial of Service | Emergency stop (`M112`), system restart commands |
| Impact | Loss of Control | Override temperature limits, disable safety |
| Impact | Data Manipulation | Modify uploaded G-code (print sabotage) |

---

## 7. Requirements

### Hardware
- 1 CPU core minimum (2 recommended)
- 512 MB RAM minimum
- 1 GB disk space

### Software
- **Docker** and **Docker Compose** (recommended deployment)
- OR **Python 3.12+** with virtual environment (local development)
- Most Linux distributions (tested on Ubuntu 24.04)

### Python Dependencies
```
aiohttp>=3.9.0          # Web framework
motor>=3.3.0            # Async MongoDB driver
pymongo>=4.6.0          # MongoDB client
pyyaml>=6.0.1           # Configuration
numpy>=1.26.0           # Physics calculations
```

---

## 8. Installation & Setup

### Option A: Docker Compose (Recommended)

This is the recommended deployment method, following HoneySat's containerized architecture.

```bash
# Clone the repository
git clone <repository-url> honeyprint
cd honeyprint

# Copy and customize environment
cp .env.example .env
# Edit .env to set your desired printer profile, credentials, ports

# Build and start
docker compose up --build -d

# Verify
docker compose logs -f honeyprint
```

### Option B: Local Development

```bash
# Clone
git clone <repository-url> honeyprint
cd honeyprint

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# (Optional) Start MongoDB
docker run -d --name honeyprint_mongo -p 27017:27017 mongo:7

# Run HoneyPrint
python -m honeyprint.main --web-port 8080 --telnet-port 2323
```

### Option C: Quick Start Script

```bash
# Default Ender-3 V2
./scripts/run_honeyprint.sh

# Specific profile
./scripts/run_honeyprint.sh prusa_mk4
./scripts/run_honeyprint.sh bambu_x1c
./scripts/run_honeyprint.sh voron_2_4
```

---

## 9. Running HoneyPrint

### Docker Compose

```bash
# Start the OctoPrint variant only
docker compose up -d honeyprint mongo

# Start the Klipper/Moonraker variant only
docker compose up -d honeyprint_klipper mongo

# Start both variants
docker compose up -d honeyprint honeyprint_klipper mongo

# View logs
docker compose logs -f honeyprint
docker compose logs -f honeyprint_klipper

# Stop
docker compose down

# Start with a specific profile
HONEYPRINT_PROFILE=prusa_mk4 HONEYPRINT_NAME="Original Prusa MK4" docker compose up -d

# Start with a fake print job running (for deception)
HONEYPRINT_FAKE_PRINT=true docker compose up -d
```

The OctoPrint interface is exposed on `http://localhost:80`. The Klipper
variant exposes a Moonraker-compatible API and a Mainsail-style dashboard on
`http://localhost:7125`.

### Stored Data and Analyst Dashboard

Uploaded print files survive container restarts in the Docker volumes
`honeyprint_data` and `honeyprint_klipper_data`. HoneyPrint also keeps an
append-only upload history, including the original filename, stored filename,
size, content hash, and upload time.

The analyst dashboards are intentionally separate from the printer-facing
interfaces:

```text
http://localhost/analyst
http://localhost:7125/analyst
```

Set a private analyst key before starting the containers:

```bash
HONEYPRINT_ANALYST_KEY='replace-this-value' docker compose up -d honeyprint honeyprint_klipper mongo
```

The dashboard asks for this key before loading telemetry. Full interaction
records are stored in MongoDB. To inspect the available collections:

```bash
docker exec -it honeyprint_mongo mongosh honeyprint_logs
docker exec -it honeyprint_mongo mongosh honeyprint_klipper_logs
```

Useful collections include `file_uploads`, `print_sessions`, `alerts`,
`gcode_commands`, and `web_interactions`.

### Python CLI

```bash
# Basic usage
python -m honeyprint.main

# With options
python -m honeyprint.main \
    --profile prusa_mk4 \
    --web-port 8080 \
    --telnet-port 2323 \
    --fake-print

# Using the management script
python scripts/honeyprint.py octoprint "Ender-3 V2" "Berlin"
python scripts/honeyprint.py start octoprint "Prusa MK4" "New York"
python scripts/honeyprint.py stop
```

### Verifying Operation

Once running, you should see:

```
============================================================
  HoneyPrint - Additive Manufacturing Honeypot
  Printer: Ender-3 V2
  Firmware: Marlin 2.1.2.1
============================================================
  HoneyPrint is ACTIVE - Waiting for adversaries...
  Web:    http://0.0.0.0:80
  Telnet: telnet 0.0.0.0 23
============================================================
```

---

## 10. Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HONEYPRINT_PROFILE` | `ender3_v2` | Printer personality profile |
| `HONEYPRINT_NAME` | `Ender-3 V2` | Printer display name |
| `HONEYPRINT_WEB_PORT` | `80` | Web interface port |
| `HONEYPRINT_TELNET_PORT` | `23` | Telnet server port |
| `HONEYPRINT_USER` | `admin` | Web login username |
| `HONEYPRINT_PASS` | `admin` | Web login password |
| `HONEYPRINT_API_KEY` | `A1B2C3...` | OctoPrint API key |
| `HONEYPRINT_MONGODB_URI` | `mongodb://...` | MongoDB connection string |
| `HONEYPRINT_FAKE_PRINT` | `false` | Start with a fake print job |

### Configuration File (`config/honeyprint.yaml`)

```yaml
ground:
  web_port: 80
  telnet_port: 23
  web_auth_enabled: true
  web_username: "admin"
  web_password: "admin"
  api_key: "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"

printer:
  profile: "ender3_v2"

simulation:
  ambient_temperature: 22.0
  noise_stddev: 0.3

network:
  enabled: false
  latency_ms: 0
  packet_loss_percent: 0
```

---

## 11. Interacting with the Honeypot

These examples show how an adversary (or researcher) would interact with HoneyPrint.

### Web Interface

Open `http://<host>:80` in a browser. Log in with `admin`/`admin`. You will see a full OctoPrint interface with temperature controls, file manager, terminal, and control panel.

### REST API

```bash
# Get version
curl http://localhost:80/api/version

# Login
curl -X POST http://localhost:80/api/login \
  -H "Content-Type: application/json" \
  -d '{"user":"admin","pass":"admin"}'

# Get printer state
curl -H "X-Api-Key: A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" \
  http://localhost:80/api/printer

# Send G-code
curl -X POST -H "X-Api-Key: A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" \
  -H "Content-Type: application/json" \
  -d '{"commands":["G28","M105"]}' \
  http://localhost:80/api/printer/command

# Upload G-code file
curl -X POST -H "X-Api-Key: A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" \
  -F "file=@mypart.gcode" \
  http://localhost:80/api/files/local

# Set hotend temperature
curl -X POST -H "X-Api-Key: A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" \
  -H "Content-Type: application/json" \
  -d '{"command":"target","targets":{"tool0":210}}' \
  http://localhost:80/api/printer/tool

# Get settings (reveals API key, serial port, etc.)
curl -H "X-Api-Key: A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" \
  http://localhost:80/api/settings
```

### Telnet (Direct Serial Access)

```bash
telnet localhost 23
# You'll see the Marlin boot banner, then can send G-code:
M115          # Firmware info
M105          # Report temperatures
G28           # Home all axes
M20           # List SD card files
M114          # Report position
M503          # Report all settings
G1 X100 Y100 F3000  # Move print head
M104 S210     # Set hotend to 210C
M140 S60      # Set bed to 60C
```

---

## 12. Logging & Data Collection

### MongoDB Collections

When MongoDB is available, OctoPrint interactions are stored in
`honeyprint_logs`. Klipper/Moonraker interactions are stored separately in
`honeyprint_klipper_logs`.

```bash
# Connect to MongoDB (from Docker network)
docker exec -it honeyprint_mongo mongosh honeyprint_logs

# Connect to Klipper variant logs
docker exec -it honeyprint_mongo mongosh honeyprint_klipper_logs

# View login attempts
db.login_attempts.find()

# View G-code commands
db.gcode_commands.find()

# View uploaded files
db.file_uploads.find()

# Count interactions per collection
db.web_interactions.countDocuments()
db.telnet_interactions.countDocuments()
```

### JSON Fallback Logs

When MongoDB is unavailable, logs are written to `logs/` as JSON lines:

```
logs/
  web_interactions.jsonl
  telnet_interactions.jsonl
  login_attempts.jsonl
  gcode_commands.jsonl
  file_uploads.jsonl
  system_events.jsonl
```

### What Gets Logged

Every interaction is captured, including:

- **Login attempts**: username, password, IP, user-agent, success/failure
- **API requests**: method, path, headers, request body, IP
- **G-code commands**: every command sent via any protocol, with response
- **File uploads**: filename, size, content hash, content preview, IP
- **WebSocket connections**: connect/disconnect events, messages
- **Telnet sessions**: full command history per session
- **System commands**: shutdown/reboot/restart attempts

---

## 13. Deployment Recommendations

### Security Hardening

Following HoneySat's deployment model (Appendix A in the paper):

1. **Isolation**: Deploy on a dedicated server with no production applications.
2. **Network segmentation**: Place the honeypot on its own network segment.
3. **Firewall**: Only expose ports 80 (Web) and 23 (Telnet) externally. Keep MongoDB (27017) internal.
4. **Read-only filesystem**: The Docker container uses `tmpfs` for temporary files.
5. **Resource limits**: Docker Compose sets memory (512 MB) and CPU (1.0) limits.
6. **Non-root**: The container runs as a non-root `honeyprint` user.
7. **Legal notice**: The web interface footer includes "This computer system is for authorized use only."

### Realistic Deployment

For maximum deception, following HoneySat's Internet deployment strategy:

1. **IP geolocation**: Deploy in a region appropriate for the simulated printer (e.g., a Prusa MK4 in Czech Republic, Bambu X1C in China/US).
2. **DNS**: Optionally set up a hostname that suggests a maker space, university lab, or manufacturing facility.
3. **Fake print job**: Start with `HONEYPRINT_FAKE_PRINT=true` so the printer appears actively in use.
4. **Multiple instances**: Deploy different printer personalities on different IPs for broader coverage.

---

## 14. Project Structure

```
honeyprint/
├── README.md                           # This file
├── docker-compose.yml                  # Container orchestration
├── Dockerfile                          # Main service container
├── requirements.txt                    # Python dependencies
├── .env.example                        # Environment template
│
├── config/
│   └── honeyprint.yaml                 # Ground configuration
│
├── scripts/
│   ├── honeyprint.py                   # CLI management script
│   └── run_honeyprint.sh               # Quick-start script
│
└── honeyprint/                         # Main Python package
    ├── __init__.py                      # Package metadata
    ├── __main__.py                      # python -m entry point
    ├── main.py                         # Orchestrator (lifecycle management)
    ├── config.py                       # Configuration management
    │
    ├── printer_simulator/              # Physics engine (Space Segment core)
    │   ├── __init__.py
    │   ├── simulator.py                # Coordinator for all simulations
    │   ├── thermal_sim.py              # PID heating + Newton cooling
    │   ├── motion_sim.py               # Cartesian kinematics (X/Y/Z/E)
    │   ├── extrusion_sim.py            # Filament flow + material tracking
    │   ├── power_sim.py                # Electrical consumption
    │   └── print_job_sim.py            # Job progress + fake prints
    │
    ├── firmware/                       # Marlin firmware emulation
    │   ├── __init__.py
    │   ├── marlin_emu.py               # 50+ G/M-code command handlers
    │   ├── gcode_handler.py            # G-code parser
    │   └── sd_card.py                  # Virtual SD card with fake files
    │
    ├── octoprint_sim/                  # OctoPrint web simulation
    │   ├── __init__.py
    │   ├── app.py                      # aiohttp application factory
    │   ├── api_routes.py               # OctoPrint REST API (20+ endpoints)
    │   ├── websocket_handler.py        # Real-time WebSocket updates
    │   ├── auth.py                     # Authentication + credential logging
    │   ├── templates/
    │   │   ├── index.html              # Main OctoPrint UI (dark theme)
    │   │   └── login.html              # Login page
    │   └── static/
    │       ├── css/octoprint.css       # OctoPrint-like styling
    │       └── js/octoprint.js         # Frontend logic
    │
    ├── telnet_server/                  # Direct firmware access
    │   ├── __init__.py
    │   └── server.py                   # Asyncio Telnet server
    │
    ├── logging_repo/                   # Interaction database
    │   ├── __init__.py
    │   └── logger.py                   # MongoDB + JSON fallback logger
    │
    ├── network_sim/                    # Network condition simulation
    │   ├── __init__.py
    │   └── simulator.py                # Latency, packet loss, scheduling
    │
    ├── personality/                    # Printer profiles
    │   ├── __init__.py
    │   ├── manager.py                  # Profile loader
    │   └── profiles/
    │       ├── ender3_v2.yaml          # Creality Ender-3 V2
    │       ├── prusa_mk4.yaml          # Prusa MK4
    │       ├── bambu_x1c.yaml          # Bambu Lab X1 Carbon
    │       └── voron_2_4.yaml          # VORON 2.4r2
    │
    └── sensors/                        # Sensor abstractions
        ├── __init__.py
        └── thermistor.py               # Thermistor, endstop, filament sensors
```

**Total: 40 files** (35 Python, 4 YAML profiles, 1 HTML template set, CSS, JS, Docker, config)

---

## 15. Extending HoneyPrint

HoneyPrint is designed to be extensible, following HoneySat's design principle DP-3 (Modularity).

### Adding a New Printer Personality

1. Create a new YAML file in `honeyprint/personality/profiles/`:
   ```yaml
   printer:
     name: "My Printer"
     manufacturer: "..."
     firmware: "Marlin"
     firmware_version: "2.1.2.1"
   build_volume:
     x: 200
     y: 200
     z: 200
   temperature:
     max_temp_hotend: 250
     max_temp_bed: 80
   simulation:
     hotend:
       thermal_mass: 5.0
       heating_power: 40.0
       cooling_coeff: 0.1
   ```
2. Set `HONEYPRINT_PROFILE=my_printer` or update `config/honeyprint.yaml`.

### Adding a New Protocol Ecosystem

Following HoneySat's CCSDS case study (Section VI-E), you can add new management ecosystems:

- **Klipper/Moonraker**: Add a `moonraker_sim/` module implementing the Moonraker JSON-RPC API (port 7125) with endpoints like `printer.info`, `printer.objects.query`, `server.files.list`.
- **Duet Web Control**: Add a `duet_sim/` module implementing the RepRapFirmware HTTP API (`rr_model`, `rr_gcode`, `rr_upload`).
- **Bambu Lab Cloud**: Add an `mqtt_sim/` module implementing Bambu's MQTT protocol.

### Adding New Physics Simulations

Create a new simulation module in `printer_simulator/` and register it in `simulator.py`:

```python
# Example: vibration simulation for acoustic side-channel research
class VibrationSimulation:
    def update(self, motion_state, dt):
        # Generate vibration frequencies based on stepper motor movement
        ...
```

---

## Acknowledgments

This project is directly inspired by and based on the architecture of:

> E. Lopez-Morales, U. Planta, G. Marra, C. Gonzalez, J. Hopkins, M. Garoosi, E. Obreque, C. Rubio-Medrano, A. Abbasi. "HoneySat: A Network-based Satellite Honeypot Framework." *Network and Distributed System Security (NDSS) Symposium 2026*, San Diego, CA, USA.

We thank the HoneySat authors for their open-source framework and detailed documentation which made this adaptation possible.

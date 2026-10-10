# HoneyPrint - Generalized Additive Manufacturing Honeypot Framework

HoneyPrint is a proposed configuration-driven framework for additive manufacturing (AM) honeypot research. This README follows the design in *HoneyPrint Academic Framework: OctoPrint, Klipper and Custom* and maps it to the current repository structure.

**Implementation status:** the local OctoPrint prototype now supports validated profiles, pinned deployment recipes, managed Virtual Printer startup, passive audit capture, normalized events, bounded queuing, transactional SQLite storage, verified readiness and graceful stop/restart. Read-only attachment supports state polling. Klipper, manufacturing-stage plugins, custom adapter examples, threat classification, alerts and the dashboard remain future work. The architecture below remains the full target framework.

## 1. Research objective and design boundaries

One shared framework should instantiate supported platform implementations through registered adapters. Reusable responsibilities include schema validation, capability checking, instance identity, lifecycle management, normalized telemetry, persistent evidence storage and a read-only analysis dashboard. Platform adapters are responsible for preparing, starting or attaching to, and observing their software.

Configuration selects capabilities that have already been implemented. It cannot generate an OctoPrint API, a Klipper virtual MCU or a custom integration. New functionality requires coded and registered adapters or plugins.

- **Managed mode:** the framework owns the resources it starts.
- **Attach mode:** the framework observes externally managed software and preserves its processes and resources.

## 2. Generalized reference architecture

```text
+------------------------------------------------------------------------+
|                    User-defined YAML configuration                     |
|     Printer profile | Process stages | Platform | Sensors | Decoys     |
+------------------------------------------------------------------------+
                                    |
                                    v
+------------------------------------------------------------------------+
|                        Honeypot Framework Core                         |
| Load | Validate | Resolve dependencies | Registry | Build | Lifecycle  |
+------------------------------------------------------------------------+
                                    |
                                    v
+----------------------------------+  +----------------------------------+
|    Manufacturing Stage Engine    |  |   Service and Platform Engine    |
|     Intake | Prepare | Build     |  |       Real software | APIs       |
|      Post-process | Inspect      |  |      Firmware integrations       |
+----------------------------------+  +----------------------------------+
+----------------------------------+  +----------------------------------+
|    Printer Simulation Engine     |  |   Behavior and Security Engine   |
|     Thermal | Motion | Power     |  |     Authentication | Decoys      |
|             Sensors              |  |           Event rules            |
+----------------------------------+  +----------------------------------+
                                    |
                                    v
+------------------------------------------------------------------------+
|                      Configured honeypot instance                      |
|       Platform software + simulation + workflow + isolated state       |
+------------------------------------------------------------------------+
                                    |
                                    v
+------------------------------------------------------------------------+
|                Central logging, telemetry and analysis                 |
|     Normalized events | Persistent storage | Dashboard | Analysis      |
+------------------------------------------------------------------------+
```

The four engines are logical responsibilities; they do not require four separate processes. Observation uses a separate, non-blocking telemetry pipeline.

### AM reference-model mapping

| AM process stage | Framework representation | Illustrative observable activity |
| --- | --- | --- |
| Job intake and authorization | Workflow plugin and authorization model | Login, job submission, actor/session metadata |
| Build preparation | Artifact and preparation handlers | G-code uploads, file changes, profile changes |
| Build execution | Platform adapter and printer simulator | Start, pause, cancel, temperatures, printer status |
| Post-processing | Optional equipment plugins | Wash, cure and finishing state changes |
| Inspection and feedback | Optional quality/records plugins | Inspection queries, reports and approval events |

These are manufacturing lifecycle stages, not technical architecture layers. A deployment may enable a subset and must identify which stages are simulated and which are provided natively by the platform.

## 3. Common configuration and build procedure

The examples in the design sections illustrate the full target framework. For the implemented prototype, use `configs/instances/octoprint.yaml` and `configs/profiles/virtual_lab.yaml`; its stage list is empty because manufacturing-stage plugins are not implemented. Profile paths resolve relative to the selected configuration file.

```yaml
schema_version: 1
instances:
  - id: printer_01
    platform: octoprint       # octoprint | klipper | custom_lab
    deployment: managed      # managed | attach
    printer_profile: configs/profiles/printers/ender3.yaml
    simulation: virtual_printer
    services: {web_port: 8081}
    stages: [intake, preparation, execution]
telemetry:
  backend: sqlite
  capture_requests: true
  capture_commands: true
  capture_state_changes: true
```

Validation must check configuration against declared adapter capabilities and a version-pinned deployment recipe before launch. Unknown keys, port conflicts, incompatible simulation choices, missing adapters and unsupported dependencies must fail validation. Secrets should use environment references or a secret store.

| Phase | Required operation | Output |
| --- | --- | --- |
| Configure | Choose platform, quantity, profile, mode and services | Saved YAML and profile references |
| Validate | Check schema, adapter, recipe, ports, resources and capabilities | Resolved, redacted build plan |
| Prepare | Install or attach to approved runtime components | Prepared environment and setup report |
| Integrate | Connect simulation, instrumentation and collector | Connected instance |
| Launch and verify | Start in dependency order and verify persisted events | Health status and test evidence |
| Stop and restart | Respect ownership, drain queues and revalidate changes | Repeatable lifecycle result |

## 4. Scenario A: Real OctoPrint honeypot

```yaml
instances:
  - id: octo_01
    platform: octoprint
    deployment: managed
    printer_profile: configs/profiles/printers/ender3.yaml
    simulation: virtual_printer
    services: {web_port: 8081}
    instrumentation: {plugin: honeyprint_audit, http: true, gcode: true}
```

The intended OctoPrint adapter resolves a validated recipe, prepares the real server, configures its virtual printer/profile and installs a framework-owned audit bridge. OctoPrint's normal UI and API handle commands. The bridge passively emits observable request, authentication, file, G-code and state events. Clicks that do not produce server requests require separate frontend instrumentation.

Verification requires a working UI, supported virtual printer responses, an observed test command, an event persisted with instance ID `octo_01`, and correct stop/restart behavior. Virtual printer responses do not establish physical extrusion or measurement fidelity.

## 5. Scenario B: Real Klipper / Moonraker / Mainsail honeypot

```yaml
instances:
  - id: klipper_01
    platform: klipper
    deployment: managed
    printer_profile: configs/profiles/printers/voron.yaml
    simulation: virtual_mcu
    frontend: mainsail
    api: moonraker
    services: {web_port: 8082}
    instrumentation: {moonraker_events: true, gcode: true}
```

The intended adapter selects a tested recipe for the Klipper host, Moonraker API and Mainsail frontend. Klipper expects microcontroller communication, so a hardware-free deployment requires a compatible virtual MCU or validated simulation backend. A YAML flag alone does not provide that dependency. Backend readiness must be confirmed before the instance is reported healthy.

Moonraker WebSocket subscriptions, API monitoring and Klipper/G-code observation feed a bridge that normalizes platform-native records. Verification must check temperature/status coherence across Mainsail, Moonraker and simulated execution state. Without a validated MCU backend, validation must report an unsupported capability.

## 6. Scenario C: Researcher-defined custom honeypot

```yaml
instances:
  - id: custom_01
    platform: custom_lab
    adapter: lab_printer_v1
    deployment: managed
    printer_profile: configs/profiles/printers/custom_lab.yaml
    simulation: lab_simulator_v1
    stages: [intake, preparation, execution, postprocessing, inspection]
    services: {web_port: 8090}
    extensions: [quality_records, curing_station]
```

A researcher supplies an adapter with declared capabilities, a deployment recipe, an observation bridge and any required simulator or manufacturing-stage plugins. Registration allows the common core to validate and instantiate it while reusing telemetry, storage and dashboard contracts.

Untrusted YAML must not execute arbitrary Python paths or shell commands. Third-party plugins must be explicitly installed, reviewed, allowlisted and isolated.

### Proposed adapter contract

```python
class PlatformAdapter:
    platform_id: str

    def capabilities(self) -> dict: ...
    def validate(self, cfg, context) -> None: ...
    def prepare(self, plan) -> None: ...
    def launch_or_attach(self, plan) -> object: ...
    def health(self, instance) -> dict: ...
    def observe(self, instance, sink) -> None: ...
    def stop(self, instance, owned_only=True) -> None: ...
```

Separate extension contracts are planned for `PlatformAdapter`, `SimulatorProvider`, `ManufacturingStagePlugin`, `CameraProvider`, `StorageProvider` and `EventNormalizer`. Plugin manifests should declare identifier, API version, capabilities, supported runtime/platform versions, required permissions, health checks and test fixtures.

## 7. What changes when a YAML variable changes?

| Configuration change | Core response | Impact or limitation |
| --- | --- | --- |
| `platform: octoprint` to `klipper` | Select another registered adapter and recipe | Different software stack; compatible MCU simulation required |
| Printer profile changed | Validate and apply supported settings | Does not guarantee hardware-accurate physics |
| Post-processing stage added | Load a registered stage plugin | Requires implemented stage behavior |
| Simulation changed | Resolve a simulator provider | Reject incompatible platform/backend pairs |
| Instance count increased | Assign unique IDs, state, ports and volumes | Isolate resources and logs per instance |
| Instrumentation enabled | Install or attach approved observers | Full click capture requires UI hooks |
| `platform: custom_lab` selected | Resolve the registered custom adapter | Requires adapter code and validation tests |

## 8. Unified observation, storage and security

An illustrative normalized event:

```json
{
  "event_id": "uuid",
  "timestamp": "ISO-8601-UTC",
  "instance_id": "octo_01",
  "platform": "octoprint",
  "source": "api",
  "event_type": "printer.command",
  "session_id": "opaque-session-ref",
  "action": "start_print",
  "outcome": "accepted",
  "simulation": true
}
```

Possible event sources include API, web, plugin and firmware observations. The bridge should minimize interference and send redacted events through a bounded asynchronous queue. The collector should authenticate producers, validate event schemas, deduplicate where appropriate and write to storage. SQLite is the proposed first backend; MongoDB and JSON writers are later options.

Queue admission is not proof of durable persistence. End-to-end verification must read an event back from storage. Health reporting must expose dropped records, backpressure and instrumentation outages.

Planned deployment boundaries include isolated networks, explicitly approved exposed ports, blocked physical device passthrough, constrained container privileges and outbound network policy. Uploaded artifacts should be inspected and stored without automatic execution. Real credentials must remain separate from decoy accounts, and real passwords must not be stored in clear text. Attach mode must not stop or overwrite externally managed platform processes.

## 9. Repository layout

The academic document suggests logical directories such as `simulators/`, `manufacturing/` and `recipes/`. In the current repository, these map to `plugins/simulators/`, `plugins/manufacturing/` and the implemented `recipes/` directory. The earlier `deployment/recipes/` directory remains reserved. The Klipper and custom adapter directories are named `klipper_moonraker/` and `custom/` respectively; configuration platform IDs remain `klipper` and `custom_lab`.

```text
honeyprint-general-honeypot/
├── adapters/
│   ├── custom/
│   ├── klipper_moonraker/
│   ├── octoprint/
│   │   ├── adapter.py
│   │   └── audit_plugin.py
│   └── base_adapter.py
├── configs/
│   ├── instances/
│   │   ├── custom.yaml
│   │   ├── klipper.yaml
│   │   └── octoprint.yaml
│   ├── profiles/
│   │   ├── manufacturing/
│   │   ├── printers/
│   │   └── virtual_lab.yaml
│   └── framework.yaml
├── core/
│   ├── builtin_registry.py
│   ├── config_loader.py
│   ├── dependency_resolver.py
│   ├── event_bus.py
│   ├── instance_builder.py
│   ├── lifecycle_manager.py
│   ├── plugin_registry.py
│   ├── profile_loader.py
│   ├── runtime.py
│   ├── state_manager.py
│   └── validator.py
├── dashboard/
├── deployment/
│   ├── control.py
│   ├── docker/
│   ├── recipes/
│   └── environment_manager.py
├── examples/
├── plugins/
│   ├── custom/
│   ├── firmware/
│   ├── manufacturing/
│   │   ├── build_execution.py
│   │   ├── build_preparation.py
│   │   ├── inspection.py
│   │   ├── job_intake.py
│   │   └── post_processing.py
│   ├── security/
│   ├── services/
│   └── simulators/
│       ├── extrusion.py
│       ├── motion.py
│       ├── sensors.py
│       └── thermal.py
├── recipes/
│   └── octoprint.py
├── sdk/
├── telemetry/
│   ├── analysis/
│   ├── storage/
│   ├── collector.py
│   ├── normalizer.py
│   └── sqlite_storage.py
├── tests/
│   ├── test_lifecycle.py
│   ├── test_octoprint_integration.py
│   ├── test_planning.py
│   └── test_telemetry.py
├── .gitignore
├── README.md
├── requirements.txt
├── requirements-octoprint.txt
└── main.py
```

Empty directories are tracked with `.gitkeep`, omitted above.

## Getting started

```bash
git clone https://github.com/imran1846bd/honeyprint-general-honeypot.git
cd honeyprint-general-honeypot
```

Install the YAML dependency and run the shared configuration checks:

```bash
python -m pip install -r requirements.txt
python main.py validate configs/framework.yaml
python -m unittest discover -s tests -v
```

The default configuration has no instances. Built-in registrations are explicit Python factories; custom startup code can pass its own populated registry to `main()`. YAML cannot import or execute uploaded code.

### Run the local OctoPrint prototype

Use Python 3.10–3.13 on macOS or Linux. The verified runtime uses Python 3.13 and OctoPrint 1.11.8. Install OctoPrint into a separate environment; framework preparation checks its version and does not run pip automatically.

```bash
python3 -m venv .venv-octoprint
.venv-octoprint/bin/python -m pip install -r requirements-octoprint.txt
python main.py plan configs/instances/octoprint.yaml --octoprint-python .venv-octoprint/bin/python
python main.py run configs/instances/octoprint.yaml --octoprint-python .venv-octoprint/bin/python
```

The managed Web UI listens at `http://127.0.0.1:8081`. Research login credentials are generated in `.honeyprint/octo_01/credentials.json`, with private file permissions. Keep that runtime directory outside version control. Runtime data includes the owned OctoPrint configuration, its logs, and `.honeyprint/events.sqlite3`.

Stop from another terminal, or press Ctrl-C in the running terminal:

```bash
python main.py stop
```

The stop command acknowledges a shutdown request; the running process confirms final cleanup and telemetry drain. For a bounded smoke run, append `--duration 5`. For a separate runtime directory, pass the same `--runtime-dir` to run and stop. An exclusive instance lock prevents concurrent ownership of one basedir.

Readiness requires the pinned server version, the selected VIRTUAL connection and profile, a temperature response, a live audit observer with no reported drops/errors, and lifecycle evidence read back from SQLite. It does not establish physical printer fidelity. This prototype stays on loopback; public network exposure and container/device isolation remain separate deployment work.

The integration uses OctoPrint's documented [Virtual Printer](https://docs.octoprint.org/en/main/development/virtual_printer.html), [connection API](https://docs.octoprint.org/en/main/api/connection.html) and [plugin hooks](https://docs.octoprint.org/en/main/plugins/hooks.html). Audit hooks enqueue minimal records; a worker sends them over an authenticated local UDP channel. Raw command arguments, request bodies, headers and event payloads are omitted. UDP is best-effort; counters expose observed loss, but do not establish lossless capture. Common credential metadata keys are redacted; producers must handle secrets embedded in free text.

### Read-only attachment

Attachment requires an already operational OctoPrint 1.11.8 server on loopback with its Virtual Printer connected and the matching profile selected. Copy the managed example, change deployment to `attach`, and replace instrumentation with `state: true`. Supply an existing user API key through an environment variable and pass `--attach-api-key-env VARIABLE_NAME`. The framework polls state, never installs plugins or reconnects the external printer, and stop only detaches its observer. HTTP and G-code audit flags are rejected in attach mode because that coverage is not implemented.

### Verify persistence and integration

```bash
python -m unittest discover -s tests -v
HONEYPRINT_OCTOPRINT_PYTHON="$PWD/.venv-octoprint/bin/python" python -m unittest discover -s tests -v
```

The default suite skips the real-server test when the optional interpreter is absent. With it enabled, the test launches a real server, observes commands, reads SQLite events, confirms that attach preserves the external process/configuration, requests graceful stop and restarts the owned server. Collector failures retain the pending batch for retry; pending queue records are in memory and must drain before shutdown.

## Implementation sequence

1. Implemented: schemas, extension contracts, registry and capability validation.
2. Implemented: dependency planning, resource ownership, lifecycle and health reporting.
3. Implemented: pinned local OctoPrint recipe, Virtual Printer integration and audit bridge.
4. Implemented: normalized telemetry, SQLite persistence and event readback.
5. Implement Klipper integration only with a validated MCU simulation backend.
6. Add custom adapter examples, manufacturing plugins and read-only analysis.
7. Implemented for this prototype: contract tests and real OctoPrint attach, persistence, shutdown and restart checks. Extend coverage as further platforms are added.

## License and citation

No license or citation file has been added yet. License selection and author/publication details remain to be documented. Third-party OctoPrint, Klipper, Moonraker and Mainsail source code is not included.

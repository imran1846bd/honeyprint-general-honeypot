# HoneyPrint - Generalized AM Honeypot Framework

HoneyPrint is a research framework for studying interactions with honeypots in additive manufacturing (AM). Its planned design uses a shared core to configure and manage platform adapters, printer simulations, manufacturing workflows and a common telemetry pipeline.

**Current status: basic project structure with placeholder files.** The framework does not yet launch services, simulate printers, collect events or provide a working dashboard. Python and YAML files currently contain placeholder comments.

## Planned platform support

| Platform | Intended components | Implementation status |
| --- | --- | --- |
| OctoPrint | OctoPrint server, virtual printer and audit bridge | Planned |
| Klipper | Klipper host, Moonraker API, Mainsail interface and a validated virtual MCU | Planned |
| Custom | Researcher-defined adapters, services and simulators | Planned |

A configuration will select implemented and registered components. Platform support requires adapter code and a tested deployment recipe. A hardware-free Klipper deployment also requires a compatible, validated MCU simulation backend.

## Planned architecture

```text
Framework configuration + instance settings + profiles
                         |
             Configuration validation
                         |
       Registry + dependency resolution + instance builder
                         |
           Lifecycle and state management
                         |
         Platform adapters + simulation plugins
                         |
               Manufacturing workflows
                         |
      Event bus -> telemetry collector -> normalizer
                         |
                Storage and analysis
                         |
               Read-only dashboard
```

The manufacturing workflow is intended to cover job intake, build preparation, build execution, post-processing and inspection. Deployments may enable the stages supported by their registered components.

## Repository structure

```text
honeyprint-general-honeypot/
├── adapters/
│   ├── custom/
│   ├── klipper_moonraker/
│   ├── octoprint/
│   └── base_adapter.py
├── configs/
│   ├── instances/
│   │   ├── custom.yaml
│   │   ├── klipper.yaml
│   │   └── octoprint.yaml
│   ├── profiles/
│   │   ├── manufacturing/
│   │   └── printers/
│   └── framework.yaml
├── core/
│   ├── config_loader.py
│   ├── dependency_resolver.py
│   ├── event_bus.py
│   ├── instance_builder.py
│   ├── lifecycle_manager.py
│   ├── plugin_registry.py
│   ├── state_manager.py
│   └── validator.py
├── dashboard/
├── deployment/
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
├── sdk/
├── telemetry/
│   ├── analysis/
│   ├── storage/
│   ├── collector.py
│   └── normalizer.py
├── tests/
├── .gitignore
├── README.md
└── main.py
```

Empty directories are retained in Git using `.gitkeep` files, omitted from the tree above.

## Component responsibilities

| Directory | Planned responsibility |
| --- | --- |
| `configs/` | Framework settings, per-platform instance configuration, printer and manufacturing profiles |
| `core/` | Configuration loading, validation, plugin registration, dependency resolution, instance creation, lifecycle, state and event routing |
| `adapters/` | Shared adapter contract and OctoPrint, Klipper/Moonraker and custom integrations |
| `plugins/` | Service, firmware, simulation, manufacturing, security and custom extensions |
| `deployment/` | Version-pinned recipes, Docker assets and environment management |
| `telemetry/` | Event collection, normalization, persistent storage and analysis |
| `dashboard/` | Read-only event exploration and export |
| `sdk/` | Contracts and utilities for extension developers |
| `tests/` | Future contract, integration, lifecycle and isolation tests |
| `examples/` | Future usage examples |
| `main.py` | Future framework entry point |

## Illustrative configuration

The following example describes the intended format. It is not an executable deployment configuration, and the referenced printer profile has not yet been created.

```yaml
schema_version: 1
instances:
  - id: octo_01
    platform: octoprint
    deployment: managed
    printer_profile: configs/profiles/printers/ender3.yaml
    simulation: virtual_printer
    services:
      web_port: 8081
    stages:
      - intake
      - preparation
      - execution
telemetry:
  backend: sqlite
  capture_requests: true
  capture_commands: true
  capture_state_changes: true
```

## Getting started

Clone the repository to inspect the structure:

```bash
git clone https://github.com/imran1846bd/honeyprint-general-honeypot.git
cd honeyprint-general-honeypot
```

There is currently no installation procedure or working run command. Running `main.py` does not start a honeypot. Runtime requirements will be documented when the first implementation is available.

## Development roadmap

1. Define configuration schemas, extension contracts and adapter capabilities.
2. Implement configuration validation, registration and dependency planning.
3. Implement per-instance lifecycle, ownership and health reporting.
4. Add a tested OctoPrint integration with a virtual printer and observation bridge.
5. Add Klipper/Moonraker integration once a compatible MCU simulation backend is validated.
6. Implement normalized telemetry, persistent storage and event read-back verification.
7. Add manufacturing extensions, custom adapter examples and a read-only dashboard.
8. Add integration and isolation tests for supported deployments.

## Deployment design boundaries

The planned framework distinguishes managed resources from externally managed resources in attach mode. Attach mode must preserve existing platform processes. Validation should reject unsupported component combinations before launch. Telemetry should redact secrets and report dropped events or collection failures.

Research deployments should use isolated environments and simulated devices. Virtual printer state does not establish physical printing accuracy. These boundaries describe the intended implementation; enforcement has not yet been built.

## License and citation

No license or citation file has been added yet. License selection and author/publication details remain to be documented. Third-party OctoPrint, Klipper, Moonraker and Mainsail source code is not included in this repository.

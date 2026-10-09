# HoneyPrint — Generalized AM Honeypot Framework

**Status: research prototype scaffold; not a deployed or validated honeypot.**

HoneyPrint is a proposed **configuration-driven, extensible** framework for research on honeypots in additive manufacturing (AM). One core supports multiple platform adapters, per-instance lifecycle, AM workflow extensions and a unified observation pipeline. Planned use cases include **real OctoPrint + Virtual Printer**, **real Klipper + Moonraker + Mainsail + verified simulated MCU**, and **researcher-defined custom integrations**. Choosing a name in YAML does *not* install or emulate unsupported software.

## Architecture

```text
User YAML + profiles
       |
Config loader / validator / capabilities / build plan
       |
Core orchestrator + registered, reviewed adapter
       |
Platform software + supported simulation + AM stage plugins
       |
Passive instrumentation -> normalized events -> bounded collector
       |
Persistent evidence store -> read-only dashboard / experiments
```

## Current scaffold

The repository currently contains folders and placeholder files only. No executable CLI, configuration validation, deployment, simulation, telemetry collection, or tests have been implemented yet. YAML files are placeholders; the example below describes the intended configuration format.

```text
configs/       Framework and instance YAML, printer and manufacturing profiles
core/          Configuration, validation, registry, dependencies and lifecycle
adapters/      Base adapter, OctoPrint, Klipper/Moonraker and custom adapters
plugins/       Services, firmware, simulators, AM stages and security extensions
deployment/    Recipes, Docker assets and environment management
telemetry/     Collection, normalization, storage and analysis
dashboard/     Planned read-only dashboard
sdk/           Planned extension SDK
tests/         Future validation and integration tests
examples/      Future usage examples
main.py        Entry-point placeholder
```

Empty directories are tracked using `.gitkeep`. Python and YAML placeholders identify where implementation will be added.

## Basic YAML

```yaml
schema_version: 1
instances:
  - id: octo_01
    platform: octoprint
    deployment: managed
    printer_profile: profiles/ender3.yaml
    simulation: virtual_printer
    services: {web_port: 8081}
telemetry: {backend: sqlite, capture_commands: true}
```

## Development

Implementation, developer documentation, security documentation and contribution templates remain future work. No claim of production readiness, attack detection accuracy, device authenticity or Klipper simulation fidelity is made.

## License and citation

Choose an open-source license with the supervisor/institution **before public release** and add a reviewed citation file when author and publication details are available. The repository does not include third-party OctoPrint, Klipper, Moonraker or Mainsail source code.

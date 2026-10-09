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

See [GitHub setup](docs/github_setup.md), [architecture](docs/architecture.md), [research questions](docs/research_questions.md), and [roadmap](docs/roadmap.md). An academic design document can be added to `docs/papers/` after authors' review.

## What works in this starter

- CLI to **validate** YAML, show a **non-executing deployment plan**, and list registered adapters.
- Minimal registry, adapter contract, capability verification, port and ID checks, tests.
- Example configurations for OctoPrint, Klipper and a developer-owned custom adapter.
- Template for approved adapter development and normalized event schema.

**Not yet implemented:** actual software installation, Docker orchestration, OctoPrint instrumentation, Klipper virtual MCU, custom service execution, AM simulator, persistent collector and analysis dashboard. The starter intentionally fails closed rather than pretend to launch them.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
honeyprint adapters
honeyprint validate configs/examples/octoprint.yaml
honeyprint plan configs/examples/klipper.yaml
pytest -q
```

To implement OctoPrint or Klipper, first add a **verified, version-pinned deployment recipe** and integration tests under `deployment/recipes/`, then implement its adapter hooks. Never expose an unreviewed image or live device just because a YAML field requests it.

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

Read [developer guide](docs/developer_guide.md) and [security boundaries](SECURITY.md). Open issues via the supplied GitHub templates. Contributions are welcome after research-team review. No claim of production readiness, attack detection accuracy, device authenticity or Klipper simulation fidelity is made.

## License and citation

Choose an open-source license with the supervisor/institution **before public release** (see `LICENSE-TO-CHOOSE.md`). A `CITATION.cff.template` template is included and requires author/DOI review. The repository does not include third-party OctoPrint, Klipper, Moonraker or Mainsail source code.

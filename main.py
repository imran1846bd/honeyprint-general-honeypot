"""HoneyPrint CLI for configuration checks and local runtime control.

Custom startup code can pass an explicitly populated registry. YAML never imports
or executes uploaded code. Built-ins currently support local OctoPrint Virtual
Printer deployments and read-only state attachment.
"""

import argparse
from pathlib import Path
import signal
import sys
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from core.plugin_registry import PluginRegistry


def main(argv: Optional[Sequence[str]] = None, *,
         registry: Optional['PluginRegistry'] = None) -> int:
    parser = argparse.ArgumentParser(description='Validate and run HoneyPrint honeypot instances.')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'plan', 'run'):
        command = commands.add_parser(name)
        command.add_argument('config', help='YAML configuration path')
        command.add_argument('--runtime-dir', type=Path, default=Path('.honeyprint'))
        command.add_argument('--octoprint-python', type=Path,
                             help='Interpreter with requirements-octoprint.txt installed')
        command.add_argument('--custom-code', type=Path, help='Trusted Python HTTP honeypot entry file')
        command.add_argument('--custom-python', type=Path)
        command.add_argument('--custom-api-url', help='External HTTP API base URL')
        command.add_argument('--custom-health-path', default='/')
        command.add_argument('--attach-api-key-env', help='Environment variable containing the external API key')
        if name == 'run':
            command.add_argument('--duration', type=float, help='Stop after this many ready-runtime seconds')
    stop = commands.add_parser('stop', help='Request graceful shutdown of a running framework')
    stop.add_argument('--runtime-dir', type=Path, default=Path('.honeyprint'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'stop':
            from deployment.control import stop_running
            stop_running(args.runtime_dir)
            print('Graceful stop requested. The running process will report final cleanup status.')
            return 0
        from core.config_loader import load_config
        from core.builtin_registry import create_builtin_registry
        from core.validator import validate_config
        config_path = Path(args.config).absolute()
        config = load_config(config_path)
        active_registry = registry if registry is not None else create_builtin_registry(
            runtime_dir=args.runtime_dir, octoprint_python=args.octoprint_python,
            attach_api_key_env=args.attach_api_key_env, custom_code=args.custom_code,
            custom_python=args.custom_python, custom_health_path=args.custom_health_path, custom_api_url=args.custom_api_url, runtime_settings=config.get('runtime', {}),
        )
        validate_config(config, active_registry)
        if args.command == 'validate':
            print(f"Configuration checks passed: {len(config['instances'])} instance(s).")
            if not config['instances']:
                print('No instances are configured.')
            print('Deployment readiness has not been verified; no services were launched.')
            return 0
        if args.command == 'plan':
            from core.dependency_resolver import resolve_dependencies
            plan = resolve_dependencies(config, active_registry, base_dir=config_path.parent)
            print(f'Dependency checks passed: {len(plan.instances)} instance(s); no services launched.')
            for instance in plan.instances:
                print(f'{instance.instance_id}: {instance.config["platform"]} / {instance.config["deployment"]}')
            return 0
        from core.runtime import FrameworkRuntime
        app = FrameworkRuntime(config, active_registry, base_dir=config_path.parent,
                               runtime_dir=args.runtime_dir)
        # SIGTERM, Ctrl-C and the stop command all request owned cleanup.
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: app.stop_event.set())
        def ready(instance, report):
            platform = instance.plan.config['platform']
            port = instance.plan.config['services'].get('web_port')
            print(f'{instance.instance_id}: ready; platform={platform}; http://127.0.0.1:{port}', flush=True)
            if instance.deployment == 'managed' and platform == 'octoprint':
                print(f'Local research login: {args.runtime_dir.absolute() / instance.instance_id / "credentials.json"}', flush=True)
        try:
            result = app.run(duration=args.duration, on_ready=ready)
        finally:
            signal.signal(signal.SIGTERM, previous)
        print(f"Stopped cleanly; acknowledged {result['telemetry']['acknowledged']} telemetry records.")
        return 0
    except ModuleNotFoundError as exc:
        if exc.name != 'yaml':
            raise
        print('Missing PyYAML dependency. Install with: python -m pip install -r requirements.txt', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError):
        # Library APIs retain diagnostic causes; CLI never dumps secret-bearing input.
        print('Operation failed. Check configuration, pinned runtime and owned instance logs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""HoneyPrint command-line configuration validation.

Usage: python main.py validate configs/framework.yaml
Custom application startup code can pass an explicitly populated PluginRegistry
into main(). No adapters are registered by default while integrations remain
unimplemented. Successful validation does not establish deployment readiness.
"""

import argparse
import sys
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from core.plugin_registry import PluginRegistry


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    registry: Optional["PluginRegistry"] = None,
) -> int:
    """Return 0 for valid configuration and 1 for loading/validation failures.

    argparse reports usage errors with exit code 2. A supplied registry is used
    unchanged, allowing developer-owned startup code to register custom plugins.
    This command does not import uploaded code, invoke factories or launch services.
    """
    parser = argparse.ArgumentParser(
        description="Validate HoneyPrint configuration without launching services."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Check a YAML configuration")
    validate.add_argument("config", help="Path to the YAML configuration file")
    args = parser.parse_args(argv)

    # Parse help first so --help works even before dependencies are installed.
    try:
        from core.config_loader import ConfigLoadError, load_config
        from core.plugin_registry import PluginRegistry
        from core.validator import ConfigValidationError, validate_config
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        print(
            "Missing PyYAML dependency. Install with: "
            "python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    active_registry = registry if registry is not None else PluginRegistry()
    try:
        config = load_config(args.config)
        validate_config(config, active_registry)
    except (ConfigLoadError, ConfigValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    instance_count = len(config["instances"])
    print(f"Configuration checks passed: {instance_count} instance(s).")
    if instance_count == 0:
        print("No instances are configured.")
    print("Deployment readiness has not been verified; no services were launched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

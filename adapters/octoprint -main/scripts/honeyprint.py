#!/usr/bin/env python3
"""
HoneyPrint CLI - Manage HoneyPrint honeypot deployments.

Usage:
    python honeyprint.py [csp|octoprint] "{printer_name}" "{location}"
    python honeyprint.py start [csp|octoprint] "{printer_name}" "{location}"
    python honeyprint.py stop [csp|octoprint]

Analogous to HoneySat's evaluation/experiment-2/honeysat.py script.
"""

import sys
import os
import subprocess
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROFILES_DIR = PROJECT_ROOT / "honeyprint" / "personality" / "profiles"


def list_profiles():
    """List available printer personality profiles."""
    profiles = []
    if PROFILES_DIR.exists():
        for f in PROFILES_DIR.glob("*.yaml"):
            profiles.append(f.stem)
    return profiles


def generate_config(ecosystem: str, printer_name: str, location: str):
    """Generate a HoneyPrint configuration for a specific deployment."""
    # Map ecosystem to profile defaults
    ecosystem_defaults = {
        "octoprint": {
            "profile": "ender3_v2",
            "firmware": "Marlin",
            "web_title": "OctoPrint",
        },
        "csp": {
            "profile": "ender3_v2",
            "firmware": "Marlin",
            "web_title": "OctoPrint",
        },
        "klipper": {
            "profile": "voron_2_4",
            "firmware": "Klipper",
            "web_title": "Mainsail",
        },
        "bambu": {
            "profile": "bambu_x1c",
            "firmware": "Bambu",
            "web_title": "Bambu Studio",
        },
    }

    defaults = ecosystem_defaults.get(ecosystem, ecosystem_defaults["octoprint"])

    config = {
        "ground": {
            "web_port": 80,
            "telnet_port": 23,
            "web_title": defaults["web_title"],
            "web_auth_enabled": True,
            "web_username": "admin",
            "web_password": "admin",
        },
        "printer": {
            "profile": defaults["profile"],
            "name": printer_name,
            "firmware": defaults["firmware"],
        },
        "deployment": {
            "ecosystem": ecosystem,
            "location": location,
            "printer_name": printer_name,
        },
    }

    config_path = CONFIG_DIR / "honeyprint.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    print(f"Configuration generated: {config_path}")
    print(f"  Ecosystem: {ecosystem}")
    print(f"  Printer: {printer_name}")
    print(f"  Location: {location}")
    print(f"  Profile: {defaults['profile']}")
    return config_path


def start_services(ecosystem: str, printer_name: str, location: str):
    """Start HoneyPrint services via Docker Compose."""
    generate_config(ecosystem, printer_name, location)

    env = os.environ.copy()
    env["HONEYPRINT_NAME"] = printer_name

    print(f"\nStarting HoneyPrint ({printer_name} at {location})...")
    subprocess.run(
        ["docker", "compose", "up", "--build", "-d"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    print("\nHoneyPrint is running!")
    print(f"  Web interface: http://localhost:80")
    print(f"  Telnet: telnet localhost 23")
    print(f"  Logs: docker compose logs -f honeyprint")


def stop_services(ecosystem: str = None):
    """Stop HoneyPrint services."""
    print("Stopping HoneyPrint...")
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=str(PROJECT_ROOT),
    )
    print("HoneyPrint stopped.")


def print_usage():
    print("HoneyPrint - Additive Manufacturing Honeypot")
    print()
    print("Usage:")
    print("  python honeyprint.py [ecosystem] [printer_name] [location]")
    print("  python honeyprint.py start [ecosystem] [printer_name] [location]")
    print("  python honeyprint.py stop")
    print("  python honeyprint.py profiles")
    print()
    print("Ecosystems: octoprint, klipper, bambu")
    print()
    print("Examples:")
    print('  python honeyprint.py octoprint "Ender-3 V2" "Berlin"')
    print('  python honeyprint.py start octoprint "Prusa MK4" "New York"')
    print('  python honeyprint.py stop')
    print()
    print("Available profiles:")
    for p in list_profiles():
        print(f"  - {p}")


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return

    if args[0] == "profiles":
        print("Available printer profiles:")
        for p in list_profiles():
            print(f"  - {p}")
        return

    if args[0] == "stop":
        ecosystem = args[1] if len(args) > 1 else None
        stop_services(ecosystem)
        return

    if args[0] == "start":
        args = args[1:]

    if len(args) < 3:
        print("Error: Missing arguments.")
        print_usage()
        sys.exit(1)

    ecosystem = args[0]
    printer_name = args[1]
    location = args[2]

    if args == sys.argv[1:]:
        # Just generate config
        generate_config(ecosystem, printer_name, location)
    else:
        # Start services
        start_services(ecosystem, printer_name, location)


if __name__ == "__main__":
    main()

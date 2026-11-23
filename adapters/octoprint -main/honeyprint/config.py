"""
Configuration management for HoneyPrint.

Loads ground configuration (network settings, exposed protocols) and
printer personality (printer model, capabilities, physics parameters).
Analogous to HoneySat's Ground Configuration + Satellite Personality.
"""

import os
import yaml
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    # Ground Configuration (management/network side)
    "ground": {
        "web_port": 80,
        "api_port": 80,
        "telnet_port": 23,
        "vnc_port": 5901,
        "mqtt_port": 1883,
        "mongodb_uri": "mongodb://honeyprint_mongo:27017",
        "mongodb_db": "honeyprint_logs",
        "storage_dir": "data/octo",
        "web_title": "OctoPrint",
        "web_auth_enabled": True,
        "web_username": "admin",
        "web_password": "admin",
        "api_key": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
        "analyst_key": "change-me",
        "enable_cors": True,
        "enable_webcam": True,
        "webcam_url": "/webcam/?action=stream",
    },
    # Printer Personality defaults (overridden by profile)
    "printer": {
        "profile": "ender3_v2",
        "name": "Ender-3 V2",
        "manufacturer": "Creality",
        "firmware": "Marlin",
        "firmware_version": "2.1.2.1",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 115200,
        "build_volume": {"x": 220, "y": 220, "z": 250},
        "nozzle_diameter": 0.4,
        "filament_diameter": 1.75,
        "max_temp_hotend": 260,
        "max_temp_bed": 100,
        "num_extruders": 1,
        "heated_bed": True,
        "heated_chamber": False,
        "auto_bed_leveling": False,
        "filament_sensor": False,
        "sd_card": True,
        "lcd_display": True,
    },
    # Simulation parameters
    "simulation": {
        "thermal_update_interval": 1.0,
        "motion_update_interval": 0.1,
        "print_job_update_interval": 1.0,
        "ambient_temperature": 22.0,
        "voltage": 24.0,
        "noise_stddev": 0.3,
    },
    # Logging configuration
    "logging": {
        "level": "INFO",
        "log_telnet": True,
        "log_web": True,
        "log_api": True,
        "log_gcode": True,
        "log_network": True,
        "capture_pcap": True,
    },
    # Network simulator
    "network": {
        "enabled": False,
        "latency_ms": 0,
        "jitter_ms": 0,
        "packet_loss_percent": 0,
    },
}


class HoneyPrintConfig:
    """Central configuration manager for HoneyPrint."""

    def __init__(self, config_path: str | None = None,
                 personality_path: str | None = None):
        self._config = dict(DEFAULT_CONFIG)
        if config_path:
            self._load_yaml(config_path)
        if personality_path:
            self._load_personality(personality_path)
        self._apply_env_overrides()

    def _load_yaml(self, path: str) -> None:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data:
            self._deep_merge(self._config, data)

    def _load_personality(self, path: str) -> None:
        """Load a printer personality profile."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data:
            if "printer" in data:
                self._deep_merge(self._config["printer"], data["printer"])
            if "simulation" in data:
                self._deep_merge(self._config["simulation"], data["simulation"])
            # Personality profiles expose richer hardware sections while the
            # simulator consumes a compact printer dictionary.
            if "build_volume" in data:
                self._config["printer"]["build_volume"] = data["build_volume"]
            if "nozzle" in data:
                self._config["printer"]["nozzle_diameter"] = data["nozzle"].get(
                    "diameter", self._config["printer"]["nozzle_diameter"]
                )
            if "filament" in data:
                self._config["printer"]["filament_diameter"] = data["filament"].get(
                    "diameter", self._config["printer"]["filament_diameter"]
                )
            if "extruder" in data:
                self._config["printer"]["num_extruders"] = data["extruder"].get(
                    "num_extruders", self._config["printer"]["num_extruders"]
                )
            if "temperature" in data:
                for key in ("max_temp_hotend", "max_temp_bed"):
                    if key in data["temperature"]:
                        self._config["printer"][key] = data["temperature"][key]
            if "capabilities" in data:
                self._deep_merge(self._config["printer"], data["capabilities"])
            if "communication" in data:
                communication = data["communication"]
                for key in ("serial_port", "baud_rate"):
                    if key in communication:
                        self._config["printer"][key] = communication[key]

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides (HONEYPRINT_ prefix)."""
        env_map = {
            "HONEYPRINT_WEB_PORT": ("ground", "web_port", int),
            "HONEYPRINT_TELNET_PORT": ("ground", "telnet_port", int),
            "HONEYPRINT_VNC_PORT": ("ground", "vnc_port", int),
            "HONEYPRINT_MONGODB_URI": ("ground", "mongodb_uri", str),
            "HONEYPRINT_MONGODB_DB": ("ground", "mongodb_db", str),
            "HONEYPRINT_STORAGE_DIR": ("ground", "storage_dir", str),
            "HONEYPRINT_PRINTER_PROFILE": ("printer", "profile", str),
            "HONEYPRINT_PRINTER_NAME": ("printer", "name", str),
            "HONEYPRINT_API_KEY": ("ground", "api_key", str),
            "HONEYPRINT_ANALYST_KEY": ("ground", "analyst_key", str),
            "HONEYPRINT_WEB_USERNAME": ("ground", "web_username", str),
            "HONEYPRINT_WEB_PASSWORD": ("ground", "web_password", str),
        }
        for env_var, (section, key, cast) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                self._config[section][key] = cast(val)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                HoneyPrintConfig._deep_merge(base[k], v)
            else:
                base[k] = v

    @property
    def ground(self) -> dict:
        return self._config["ground"]

    @property
    def printer(self) -> dict:
        return self._config["printer"]

    @property
    def simulation(self) -> dict:
        return self._config["simulation"]

    @property
    def logging_config(self) -> dict:
        return self._config["logging"]

    @property
    def network(self) -> dict:
        return self._config["network"]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._config.get(section, {}).get(key, default)

    def to_dict(self) -> dict:
        return dict(self._config)


def load_config(config_dir: str | None = None) -> HoneyPrintConfig:
    """Load configuration from the standard config directory."""
    if config_dir is None:
        config_dir = os.environ.get(
            "HONEYPRINT_CONFIG_DIR",
            str(Path(__file__).parent.parent / "config")
        )
    config_path = os.path.join(config_dir, "honeyprint.yaml")
    personality_dir = os.path.join(
        str(Path(__file__).parent), "personality", "profiles"
    )
    cfg = HoneyPrintConfig(
        config_path=config_path if os.path.exists(config_path) else None
    )
    # Load printer personality profile
    profile = cfg.printer.get("profile", "ender3_v2")
    personality_path = os.path.join(personality_dir, f"{profile}.yaml")
    if os.path.exists(personality_path):
        cfg._load_personality(personality_path)
    return cfg

"""Shared telemetry collection and alert classification for HoneyPrint variants."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class TelemetryCollector:
    """Keep a bounded live analyst feed and forward records to the logger."""

    def __init__(self, variant: str, log_callback: Callable[[str, dict], None]):
        self.variant = variant
        self._log_callback = log_callback
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self.print_sessions: deque[dict[str, Any]] = deque(maxlen=200)

    def record(self, event_type: str, data: dict[str, Any]) -> None:
        record = {
            **data,
            "event_type": event_type,
            "honeypot_variant": self.variant,
            "timestamp": time.time(),
        }
        self.events.appendleft(record)
        if event_type.startswith("print_"):
            self.print_sessions.appendleft(record)
            self._log_callback("print_session", record.copy())
        else:
            self._log_callback("event", record.copy())

        command = str(data.get("command", ""))
        if command:
            self._record_command(command, data)

    def record_upload(self, data: dict[str, Any]) -> None:
        record = {
            **data,
            "honeypot_variant": self.variant,
            "timestamp": time.time(),
        }
        self.events.appendleft({"event_type": "file_upload", **record})
        self._log_callback("file_upload", record)

    def _record_command(self, command: str, context: dict[str, Any]) -> None:
        record = {
            "command": command,
            "source": context.get("source", "print_file"),
            "filename": context.get("filename"),
            "line": context.get("line"),
            "ip": context.get("ip"),
            "honeypot_variant": self.variant,
            "timestamp": time.time(),
        }
        self._log_callback("gcode", record)
        for alert in classify_command(command):
            alert_record = {**record, **alert}
            self.alerts.appendleft(alert_record)
            self._log_callback("alert", alert_record.copy())

    def summary(self, upload_history: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "counts": {
                "events": len(self.events),
                "alerts": len(self.alerts),
                "print_sessions": len(self.print_sessions),
                "uploads": len(upload_history),
            },
            "alerts": list(self.alerts)[:50],
            "print_sessions": list(self.print_sessions)[:100],
            "uploads": list(reversed(upload_history[-100:])),
            "events": list(self.events)[:100],
        }


def classify_command(command: str) -> list[dict[str, str]]:
    """Return analyst alerts for risky printer commands."""
    upper = command.upper()
    alerts: list[dict[str, str]] = []
    if re.search(r"\bM112\b", upper):
        alerts.append({"severity": "critical", "reason": "Emergency stop command"})
    if re.search(r"\b(M18|M84)\b", upper):
        alerts.append({"severity": "medium", "reason": "Motors disabled"})
    if "TURN_OFF_HEATERS" in upper:
        alerts.append({"severity": "medium", "reason": "All heaters disabled"})

    hotend = _extract_target(upper, r"\bM10[49]\b", "S")
    if hotend is None and "SET_HEATER_TEMPERATURE" in upper and "HEATER=EXTRUDER" in upper:
        hotend = _extract_named_target(upper)
    if hotend is not None and hotend >= 250:
        alerts.append({"severity": "high", "reason": f"High hotend target: {hotend:g} C"})

    bed = _extract_target(upper, r"\bM1(40|90)\b", "S")
    if bed is None and "SET_HEATER_TEMPERATURE" in upper and "HEATER=HEATER_BED" in upper:
        bed = _extract_named_target(upper)
    if bed is not None and bed >= 100:
        alerts.append({"severity": "high", "reason": f"High bed target: {bed:g} C"})
    return alerts


def _extract_target(command: str, prefix: str, parameter: str) -> float | None:
    if not re.search(prefix, command):
        return None
    match = re.search(rf"\b{parameter}(-?\d+(?:\.\d+)?)", command)
    return float(match.group(1)) if match else None


def _extract_named_target(command: str) -> float | None:
    match = re.search(r"\bTARGET=(-?\d+(?:\.\d+)?)", command)
    return float(match.group(1)) if match else None

"""
Motion Simulation - Models kinematics of the 3D printer motion system.

Analogous to HoneySat's Orbital/Rotation Simulation. Tracks the position
of the print head (X, Y, Z) and extruder (E), simulates movement timing
based on feedrate and acceleration, and provides realistic position data.
"""

import time
import math
from dataclasses import dataclass, field
from enum import Enum


class PrinterMode(Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"


@dataclass
class Axis:
    """State of a single motion axis."""
    name: str
    position: float = 0.0
    min_pos: float = 0.0
    max_pos: float = 220.0
    homed: bool = False
    max_feedrate: float = 200.0     # mm/s
    max_acceleration: float = 500.0  # mm/s^2
    steps_per_mm: float = 80.0


@dataclass
class MotionState:
    """Complete motion system state."""
    x: Axis = field(default_factory=lambda: Axis(
        "X", max_pos=220.0, max_feedrate=200.0,
        max_acceleration=500.0, steps_per_mm=80.0
    ))
    y: Axis = field(default_factory=lambda: Axis(
        "Y", max_pos=220.0, max_feedrate=200.0,
        max_acceleration=500.0, steps_per_mm=80.0
    ))
    z: Axis = field(default_factory=lambda: Axis(
        "Z", max_pos=250.0, max_feedrate=5.0,
        max_acceleration=100.0, steps_per_mm=400.0
    ))
    e: Axis = field(default_factory=lambda: Axis(
        "E", min_pos=-10000, max_pos=100000,
        max_feedrate=50.0, max_acceleration=5000.0,
        steps_per_mm=93.0
    ))
    feedrate: float = 60.0          # mm/s (F value / 60)
    position_mode: PrinterMode = PrinterMode.ABSOLUTE
    extruder_mode: PrinterMode = PrinterMode.ABSOLUTE
    is_moving: bool = False
    current_layer: int = 0
    layer_height: float = 0.2


class MotionSimulation:
    """
    Simulates 3D printer motion system.

    Models:
    - Cartesian kinematics (X, Y, Z, E axes)
    - Feedrate-based move timing
    - Homing sequences
    - Position tracking in absolute/relative modes
    - Build volume boundary enforcement
    """

    def __init__(self, build_volume: dict | None = None):
        self.state = MotionState()
        if build_volume:
            self.state.x.max_pos = build_volume.get("x", 220.0)
            self.state.y.max_pos = build_volume.get("y", 220.0)
            self.state.z.max_pos = build_volume.get("z", 250.0)
        self._move_queue: list[dict] = []
        self._move_start_time: float = 0
        self._move_duration: float = 0
        self._move_start_pos: dict = {}
        self._move_end_pos: dict = {}

    def get_position(self) -> dict:
        """Get current position of all axes."""
        return {
            "x": round(self.state.x.position, 3),
            "y": round(self.state.y.position, 3),
            "z": round(self.state.z.position, 3),
            "e": round(self.state.e.position, 3),
        }

    def move(self, x: float | None = None, y: float | None = None,
             z: float | None = None, e: float | None = None,
             feedrate: float | None = None) -> float:
        """
        Execute a move command. Returns estimated time in seconds.

        In absolute mode, coordinates are absolute positions.
        In relative mode, coordinates are relative offsets.
        """
        if feedrate is not None:
            self.state.feedrate = feedrate / 60.0  # Convert mm/min to mm/s

        # Calculate target positions
        targets = {}
        for axis_name, val, axis in [
            ("x", x, self.state.x),
            ("y", y, self.state.y),
            ("z", z, self.state.z),
        ]:
            if val is not None:
                if self.state.position_mode == PrinterMode.RELATIVE:
                    target = axis.position + val
                else:
                    target = val
                targets[axis_name] = max(axis.min_pos, min(axis.max_pos, target))

        if e is not None:
            if self.state.extruder_mode == PrinterMode.RELATIVE:
                targets["e"] = self.state.e.position + e
            else:
                targets["e"] = e

        # Calculate move distance and duration
        distance = 0.0
        for axis_name in ("x", "y", "z"):
            if axis_name in targets:
                axis = getattr(self.state, axis_name)
                d = targets[axis_name] - axis.position
                distance += d * d
        distance = math.sqrt(distance)

        # Duration based on feedrate
        speed = min(self.state.feedrate, 200.0)
        if speed <= 0:
            speed = 50.0
        duration = distance / speed if distance > 0 else 0.0

        # Apply positions immediately (simplified - no real-time interpolation)
        for axis_name, target in targets.items():
            axis = getattr(self.state, axis_name)
            axis.position = target

        # Track layer changes
        if "z" in targets and targets["z"] > 0:
            new_layer = int(targets["z"] / self.state.layer_height)
            if new_layer > self.state.current_layer:
                self.state.current_layer = new_layer

        return duration

    def home(self, axes: str = "XYZ") -> float:
        """
        Home specified axes. Returns estimated time.
        """
        duration = 0.0
        axes = axes.upper()
        if "X" in axes:
            self.state.x.position = 0.0
            self.state.x.homed = True
            duration += 5.0
        if "Y" in axes:
            self.state.y.position = 0.0
            self.state.y.homed = True
            duration += 5.0
        if "Z" in axes:
            self.state.z.position = 0.0
            self.state.z.homed = True
            duration += 8.0
        return duration

    def set_position(self, x: float | None = None, y: float | None = None,
                     z: float | None = None, e: float | None = None) -> None:
        """Set current position without movement (G92)."""
        if x is not None:
            self.state.x.position = x
        if y is not None:
            self.state.y.position = y
        if z is not None:
            self.state.z.position = z
        if e is not None:
            self.state.e.position = e

    def set_absolute_mode(self) -> None:
        self.state.position_mode = PrinterMode.ABSOLUTE

    def set_relative_mode(self) -> None:
        self.state.position_mode = PrinterMode.RELATIVE

    def set_extruder_absolute(self) -> None:
        self.state.extruder_mode = PrinterMode.ABSOLUTE

    def set_extruder_relative(self) -> None:
        self.state.extruder_mode = PrinterMode.RELATIVE

    def is_homed(self) -> bool:
        return (self.state.x.homed and
                self.state.y.homed and
                self.state.z.homed)

    def get_status(self) -> dict:
        return {
            "position": self.get_position(),
            "feedrate": round(self.state.feedrate * 60, 1),
            "mode": self.state.position_mode.value,
            "homed": {
                "x": self.state.x.homed,
                "y": self.state.y.homed,
                "z": self.state.z.homed,
            },
            "is_moving": self.state.is_moving,
            "current_layer": self.state.current_layer,
        }

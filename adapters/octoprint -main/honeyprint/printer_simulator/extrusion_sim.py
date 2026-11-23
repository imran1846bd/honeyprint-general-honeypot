"""
Extrusion Simulation - Models filament extrusion mechanics.

No direct HoneySat analogue (AM-specific). Simulates filament feed,
melt zone behavior, and extrusion volume tracking. Provides data for
realistic print progress and material consumption telemetry.
"""

import time
from dataclasses import dataclass


@dataclass
class ExtrusionState:
    """State of the extrusion system."""
    filament_diameter: float = 1.75         # mm
    nozzle_diameter: float = 0.4            # mm
    total_extruded_mm: float = 0.0          # Total filament length fed
    total_extruded_volume: float = 0.0      # mm^3
    flow_rate_percent: float = 100.0        # Flow rate multiplier
    retracted: bool = False
    retract_length: float = 0.0
    filament_loaded: bool = True
    filament_type: str = "PLA"
    spool_weight_g: float = 1000.0          # Full spool
    filament_used_g: float = 0.0
    # Material densities (g/cm^3)
    material_densities: dict = None

    def __post_init__(self):
        if self.material_densities is None:
            self.material_densities = {
                "PLA": 1.24,
                "ABS": 1.04,
                "PETG": 1.27,
                "TPU": 1.21,
                "Nylon": 1.14,
                "ASA": 1.07,
            }


class ExtrusionSimulation:
    """
    Simulates 3D printer extrusion system.

    Models:
    - Filament feed tracking
    - Volume flow calculation
    - Material consumption
    - Retraction state
    - Flow rate adjustments
    """

    def __init__(self, filament_diameter: float = 1.75,
                 nozzle_diameter: float = 0.4):
        self.state = ExtrusionState(
            filament_diameter=filament_diameter,
            nozzle_diameter=nozzle_diameter,
        )
        self._filament_area = 3.14159 * (filament_diameter / 2) ** 2

    def extrude(self, length_mm: float, hotend_temp: float = 200.0) -> bool:
        """
        Extrude filament. Returns True if extrusion is feasible.
        Requires hotend to be at sufficient temperature.
        """
        min_temp = self._get_min_extrude_temp()
        if hotend_temp < min_temp:
            return False  # Cold extrusion prevention

        if not self.state.filament_loaded:
            return False

        actual_length = length_mm * (self.state.flow_rate_percent / 100.0)
        self.state.total_extruded_mm += abs(actual_length)
        volume = abs(actual_length) * self._filament_area
        self.state.total_extruded_volume += volume

        # Track material weight
        density = self.state.material_densities.get(
            self.state.filament_type, 1.24
        )
        weight = volume * density / 1000.0  # mm^3 * g/cm^3 / 1000
        self.state.filament_used_g += weight

        # Track retraction
        if actual_length < 0:
            self.state.retracted = True
            self.state.retract_length += abs(actual_length)
        else:
            if self.state.retracted:
                self.state.retracted = False
                self.state.retract_length = 0.0

        return True

    def set_flow_rate(self, percent: float) -> None:
        """Set flow rate multiplier (M221)."""
        self.state.flow_rate_percent = max(10.0, min(300.0, percent))

    def load_filament(self, material: str = "PLA") -> None:
        self.state.filament_loaded = True
        self.state.filament_type = material

    def unload_filament(self) -> None:
        self.state.filament_loaded = False

    def get_remaining_filament_g(self) -> float:
        return max(0, self.state.spool_weight_g - self.state.filament_used_g)

    def _get_min_extrude_temp(self) -> float:
        """Minimum extrusion temperature (cold extrusion prevention)."""
        return 170.0  # Marlin default EXTRUDE_MINTEMP

    def get_status(self) -> dict:
        return {
            "filament_type": self.state.filament_type,
            "filament_loaded": self.state.filament_loaded,
            "total_extruded_mm": round(self.state.total_extruded_mm, 2),
            "total_volume_mm3": round(self.state.total_extruded_volume, 2),
            "filament_used_g": round(self.state.filament_used_g, 2),
            "filament_remaining_g": round(self.get_remaining_filament_g(), 2),
            "flow_rate_percent": self.state.flow_rate_percent,
            "retracted": self.state.retracted,
            "nozzle_diameter": self.state.nozzle_diameter,
        }

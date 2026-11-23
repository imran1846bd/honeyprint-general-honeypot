"""
Power Simulation - Models electrical power consumption of the printer.

Analogous to HoneySat's Power System Simulation. Tracks power draw from
heaters, motors, fans, and control electronics. Provides realistic power
telemetry data.
"""

from dataclasses import dataclass


@dataclass
class PowerState:
    """Electrical power state of the printer."""
    supply_voltage: float = 24.0            # V (typical for modern printers)
    supply_current: float = 0.0             # A (total draw)
    heater_power_w: float = 0.0             # Heaters total
    motor_power_w: float = 0.0              # Stepper motors total
    fan_power_w: float = 0.0                # All fans
    electronics_power_w: float = 8.0        # Control board, display, etc.
    total_power_w: float = 8.0
    total_energy_wh: float = 0.0            # Cumulative energy


class PowerSimulation:
    """
    Simulates 3D printer power consumption.

    Tracks power draw from all subsystems and provides realistic
    current/power readings.
    """

    def __init__(self, voltage: float = 24.0):
        self.state = PowerState(supply_voltage=voltage)
        self._heater_specs = {
            "hotend": 40.0,     # 40W heater cartridge
            "bed": 200.0,       # 200W heated bed (typical 220x220)
        }
        self._motor_power_per_axis = 2.5    # W per active stepper
        self._fan_max_power = 5.0           # W per fan

    def update(self, thermal_state: dict, motion_active: bool,
               fan_speed: int, dt: float) -> None:
        """Update power consumption based on current system state."""
        # Heater power
        self.state.heater_power_w = 0.0
        hotend_power_frac = thermal_state.get("hotend", {}).get("power", 0) / 127
        bed_power_frac = thermal_state.get("bed", {}).get("power", 0) / 127
        self.state.heater_power_w += (
            self._heater_specs["hotend"] * hotend_power_frac
        )
        self.state.heater_power_w += (
            self._heater_specs["bed"] * bed_power_frac
        )

        # Motor power
        if motion_active:
            self.state.motor_power_w = self._motor_power_per_axis * 4
        else:
            # Steppers still draw holding current
            self.state.motor_power_w = self._motor_power_per_axis * 4 * 0.3

        # Fan power
        self.state.fan_power_w = (fan_speed / 255.0) * self._fan_max_power

        # Total
        self.state.total_power_w = (
            self.state.heater_power_w +
            self.state.motor_power_w +
            self.state.fan_power_w +
            self.state.electronics_power_w
        )
        self.state.supply_current = (
            self.state.total_power_w / self.state.supply_voltage
        )

        # Accumulate energy
        self.state.total_energy_wh += self.state.total_power_w * dt / 3600.0

    def get_status(self) -> dict:
        return {
            "voltage": round(self.state.supply_voltage, 1),
            "current": round(self.state.supply_current, 2),
            "total_power_w": round(self.state.total_power_w, 1),
            "heater_power_w": round(self.state.heater_power_w, 1),
            "motor_power_w": round(self.state.motor_power_w, 1),
            "fan_power_w": round(self.state.fan_power_w, 1),
            "total_energy_wh": round(self.state.total_energy_wh, 2),
        }

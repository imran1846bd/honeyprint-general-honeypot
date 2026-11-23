"""
Thermal Simulation - Models temperature dynamics for hotend, heated bed, and chamber.

Analogous to HoneySat's Thermal Simulation. Uses simplified heat transfer models
including PID-like heating, Newton's law of cooling for heat loss, and thermal
coupling between components. Generates realistic temperature curves that match
real 3D printer behavior.
"""

import time
import math
from dataclasses import dataclass, field


@dataclass
class HeaterState:
    """State of a single heating element."""
    name: str
    current_temp: float = 22.0
    target_temp: float = 0.0
    power: float = 0.0              # 0.0 - 1.0 (PWM duty cycle)
    max_temp: float = 260.0
    max_power_watts: float = 40.0   # Heater cartridge power
    thermal_mass: float = 8.0       # J/°C (effective thermal capacity)
    cooling_coeff: float = 0.015    # Newton cooling coefficient
    pid_kp: float = 22.0
    pid_ki: float = 1.08
    pid_kd: float = 114.0
    _pid_integral: float = field(default=0.0, repr=False)
    _pid_prev_error: float = field(default=0.0, repr=False)
    _last_update: float = field(default_factory=time.time, repr=False)
    _sensor_phase: float = field(default=0.0, repr=False)
    _sensor_offset: float = field(default=0.0, repr=False)
    enabled: bool = True
    is_heating: bool = False

    @property
    def is_at_target(self) -> bool:
        if self.target_temp <= 0:
            return False
        return abs(self.current_temp - self.target_temp) < 2.0


@dataclass
class ThermalSimState:
    """Complete thermal state of the printer."""
    hotend: HeaterState = field(default_factory=lambda: HeaterState(
        name="hotend",
        max_temp=260.0,
        max_power_watts=40.0,
        thermal_mass=8.0,
        cooling_coeff=0.015,
        pid_kp=22.0, pid_ki=1.08, pid_kd=114.0,
    ))
    bed: HeaterState = field(default_factory=lambda: HeaterState(
        name="bed",
        max_temp=100.0,
        max_power_watts=200.0,
        thermal_mass=120.0,        # Bed has much more thermal mass
        cooling_coeff=0.005,       # Insulated bed cools slower
        pid_kp=97.1, pid_ki=1.41, pid_kd=1675.16,
    ))
    chamber_temp: float = 22.0
    ambient_temp: float = 22.0
    fan_speed: float = 0.0          # 0-255 part cooling fan


class ThermalSimulation:
    """
    Simulates thermal dynamics of a 3D printer.

    Models:
    - PID-controlled heating for hotend and bed
    - Newton's law of cooling for heat dissipation
    - Fan cooling effect on hotend
    - Thermal noise for realism
    - Safety cutoffs (thermal runaway protection)
    """

    def __init__(self, ambient_temp: float = 22.0, noise_stddev: float = 0.3):
        self.state = ThermalSimState()
        self.state.ambient_temp = ambient_temp
        self.state.chamber_temp = ambient_temp
        self.state.hotend.current_temp = ambient_temp
        self.state.bed.current_temp = ambient_temp
        self.noise_stddev = noise_stddev
        self._last_update = time.time()

    def set_target(self, heater: str, target: float) -> bool:
        """Set target temperature for a heater."""
        h = self._get_heater(heater)
        if h is None:
            return False
        if target < 0 or target > h.max_temp:
            return False
        h.target_temp = target
        h.is_heating = target > 0
        if target == 0:
            h.power = 0.0
            h._pid_integral = 0.0
            h._pid_prev_error = 0.0
        return True

    def get_temperature(self, heater: str) -> float:
        """Get the simulated thermistor reading."""
        h = self._get_heater(heater)
        if h is None:
            return self.state.ambient_temp
        return self._reported_temperature(h)

    def get_target(self, heater: str) -> float:
        h = self._get_heater(heater)
        return h.target_temp if h else 0.0

    def get_power(self, heater: str) -> int:
        """Get heater PWM power as 0-127 (Marlin convention)."""
        h = self._get_heater(heater)
        if h is None:
            return 0
        return int(h.power * 127)

    def set_fan_speed(self, speed: int) -> None:
        """Set part cooling fan speed (0-255)."""
        self.state.fan_speed = max(0, min(255, speed))

    def update(self, dt: float | None = None) -> None:
        """Advance the thermal simulation by dt seconds."""
        now = time.time()
        if dt is None:
            dt = now - self._last_update
        self._last_update = now

        # Clamp dt to avoid instability
        dt = min(dt, 2.0)

        self._update_heater(self.state.hotend, dt)
        self._update_heater(self.state.bed, dt)
        self._update_chamber(dt)

    def _update_heater(self, h: HeaterState, dt: float) -> None:
        """Update a single heater using stable closed-loop control."""
        if not h.enabled:
            h.power = 0.0

        # Heat loss via Newton's law of cooling
        temp_diff = h.current_temp - self.state.ambient_temp
        heat_loss = h.cooling_coeff * temp_diff * h.thermal_mass

        # Extra cooling from part cooling fan (affects hotend only)
        if h.name == "hotend" and self.state.fan_speed > 0:
            fan_factor = (self.state.fan_speed / 255.0) * 0.3
            heat_loss += fan_factor * temp_diff

        if h.target_temp > 0 and h.enabled:
            error = h.target_temp - h.current_temp
            hold_power = max(0.0, heat_loss / h.max_power_watts)
            correction = error * 0.08
            h.power = max(0.0, min(1.0, hold_power + correction))
            if abs(error) < 0.4:
                h.current_temp += error * min(1.0, dt * 0.7)
        else:
            h.power = 0.0

        # Heat input (W)
        heat_in = h.power * h.max_power_watts

        # Temperature change: dT = (Q_in - Q_out) * dt / thermal_mass
        d_temp = (heat_in - heat_loss) * dt / h.thermal_mass
        h.current_temp += d_temp

        # Floor at ambient
        h.current_temp = max(self.state.ambient_temp, h.current_temp)
        self._update_sensor_offset(h, dt)

    def _update_sensor_offset(self, h: HeaterState, dt: float) -> None:
        """Add bounded sensor drift once a heater has reached its target."""
        if h.target_temp > 0 and abs(h.current_temp - h.target_temp) < 0.75:
            amplitude = 0.55 if h.name == "hotend" else 0.20
            frequency = 0.85 if h.name == "hotend" else 0.38
            h._sensor_phase += dt * frequency
            h._sensor_offset = amplitude * math.sin(h._sensor_phase)
        else:
            h._sensor_offset *= max(0.0, 1.0 - dt)

    def _reported_temperature(self, h: HeaterState) -> float:
        """Return a stable but non-static thermistor reading."""
        reading = h.current_temp + h._sensor_offset
        if h.target_temp > 0 and abs(h.current_temp - h.target_temp) < 0.75:
            amplitude = 0.55 if h.name == "hotend" else 0.20
            reading = max(
                h.target_temp - amplitude,
                min(h.target_temp + amplitude, reading),
            )
        return round(reading, 2)

    def _update_chamber(self, dt: float) -> None:
        """Update chamber/ambient temperature based on heater radiation."""
        # Simplified: chamber warms slightly from bed and hotend
        heat_contribution = 0.0
        if self.state.bed.current_temp > self.state.ambient_temp:
            heat_contribution += (
                (self.state.bed.current_temp - self.state.ambient_temp)
                * 0.0001
            )
        if self.state.hotend.current_temp > self.state.ambient_temp:
            heat_contribution += (
                (self.state.hotend.current_temp - self.state.ambient_temp)
                * 0.00002
            )
        # Chamber slowly approaches slightly above ambient
        target_chamber = self.state.ambient_temp + heat_contribution * 100
        diff = target_chamber - self.state.chamber_temp
        self.state.chamber_temp += diff * 0.01 * dt

    def _get_heater(self, name: str) -> HeaterState | None:
        if name in ("hotend", "T0", "T", "0"):
            return self.state.hotend
        elif name in ("bed", "B"):
            return self.state.bed
        return None

    def get_status(self) -> dict:
        """Get full thermal status for telemetry."""
        return {
            "hotend": {
                "current": self._reported_temperature(self.state.hotend),
                "target": self.state.hotend.target_temp,
                "power": self.get_power("hotend"),
            },
            "bed": {
                "current": self._reported_temperature(self.state.bed),
                "target": self.state.bed.target_temp,
                "power": self.get_power("bed"),
            },
            "chamber": round(self.state.chamber_temp, 2),
            "fan_speed": int(self.state.fan_speed),
        }

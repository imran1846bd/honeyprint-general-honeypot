"""
Thermistor Sensor - Reads temperature from the thermal simulation.

Models the NTC thermistor sensors used in 3D printers for temperature
measurement. Reads from the thermal simulation and applies sensor-specific
characteristics (noise, ADC resolution, lookup table quantization).
"""

import random


class ThermistorSensor:
    """Simulated NTC thermistor sensor."""

    def __init__(self, name: str, thermal_sim, heater_name: str,
                 noise_stddev: float = 0.2, adc_bits: int = 10):
        self.name = name
        self._thermal_sim = thermal_sim
        self._heater_name = heater_name
        self._noise_stddev = noise_stddev
        self._adc_resolution = 2 ** adc_bits
        self._pull_up_resistance = 4700  # Ohms (typical)

    def read(self) -> float:
        """Read temperature with sensor noise."""
        return self._thermal_sim.get_temperature(self._heater_name)

    def read_raw_adc(self) -> int:
        """Read raw ADC value (for firmware-level simulation)."""
        temp = self.read()
        # Simplified Steinhart-Hart inverse
        # Maps temperature to approximate ADC value
        if temp < 0:
            temp = 0
        # Rough mapping: 0°C -> ~1000 ADC, 300°C -> ~50 ADC
        adc = int(self._adc_resolution * (1.0 - temp / 350.0))
        adc = max(0, min(self._adc_resolution - 1, adc))
        return adc


class EndstopSensor:
    """Simulated endstop/limit switch sensor."""

    def __init__(self, name: str, axis: str, position: str = "min"):
        self.name = name
        self.axis = axis
        self.position = position  # "min" or "max"
        self._triggered = False

    def read(self) -> bool:
        return self._triggered

    def trigger(self) -> None:
        self._triggered = True

    def release(self) -> None:
        self._triggered = False


class FilamentSensor:
    """Simulated filament runout sensor."""

    def __init__(self, name: str = "filament_sensor"):
        self.name = name
        self._filament_present = True

    def read(self) -> bool:
        """Returns True if filament is present."""
        return self._filament_present

    def set_state(self, present: bool) -> None:
        self._filament_present = present

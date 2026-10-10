"""Explicit deterministic sensor faults for reproducible simulation runs."""
from plugins.simulators.motion import number


class SensorModel:
    def __init__(self):
        self.filament_present = True
        self.thermistor_fault = None

    def set_fault(self, name, enabled):
        if name not in ('filament_runout', 'thermistor_open', 'thermistor_short') or type(enabled) is not bool:
            raise ValueError('Unknown sensor fault or invalid flag')
        if name == 'filament_runout':
            self.filament_present = not enabled
        elif enabled:
            self.thermistor_fault = name
        elif self.thermistor_fault == name:
            self.thermistor_fault = None

    def read(self, *, temperature, position):
        temperature = number(temperature, minimum=-50.0, maximum=1000.0)
        if set(position) != {'x', 'y', 'z'}:
            raise ValueError('Sensor position requires x, y, z')
        values = {axis: number(value) for axis, value in position.items()}
        return {'filament_present': self.filament_present,
                'temperature': temperature if self.thermistor_fault is None else None,
                'thermistor_fault': self.thermistor_fault,
                'endstops': {axis: value == 0 for axis, value in values.items()}}

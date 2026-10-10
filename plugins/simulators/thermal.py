"""Deterministic first-order heating and cooling for simulated heaters."""
import math
from plugins.simulators.motion import number


class ThermalModel:
    def __init__(self, *, ambient=20.0, maximum=300.0, time_constant=30.0):
        self.ambient = number(ambient, minimum=-50.0, maximum=100.0)
        self.maximum = number(maximum, minimum=self.ambient, maximum=1000.0)
        self.time_constant = number(time_constant, minimum=0.001, maximum=3600.0)
        self.temperature = self.ambient
        self.target = self.ambient
        self.powered = True

    def set_target(self, value):
        self.target = number(value, maximum=self.maximum)

    def step(self, seconds):
        seconds = number(seconds, maximum=3600.0)
        target = max(self.ambient, self.target) if self.powered else self.ambient
        self.temperature = target + (self.temperature - target) * math.exp(-seconds / self.time_constant)
        return self.temperature

    def shutdown(self):
        self.powered = False
        self.target = 0.0

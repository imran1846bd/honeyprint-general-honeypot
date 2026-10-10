"""Track filament consumption and retraction without physical extrusion."""
import math
from plugins.simulators.motion import number


class ExtrusionModel:
    def __init__(self, *, filament_diameter=1.75, minimum_temperature=170.0):
        self.diameter = number(filament_diameter, minimum=0.01, maximum=10.0)
        self.minimum_temperature = number(minimum_temperature, maximum=1000.0)
        self.position = 0.0
        self.consumed = 0.0
        self.volume = 0.0

    def move(self, value, *, temperature, relative=False, filament_present=True):
        value = number(value, minimum=-1e9)
        temperature = number(temperature, minimum=-50.0, maximum=1000.0)
        if type(relative) is not bool or type(filament_present) is not bool:
            raise ValueError('Extrusion flags require booleans')
        target = self.position + value if relative else value
        number(target, minimum=-1e9)
        delta = target - self.position
        if delta > 0 and (temperature < self.minimum_temperature or not filament_present):
            raise ValueError('Cold extrusion or missing filament')
        consumed = max(delta, 0.0)
        self.position = target
        self.consumed += consumed
        self.volume += consumed * math.pi * (self.diameter / 2.0) ** 2
        return {'position': self.position, 'consumed_mm': self.consumed, 'volume_mm3': self.volume}

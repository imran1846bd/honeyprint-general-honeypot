"""Bounded Cartesian motion with explicit time steps and no hardware access."""
import math


def number(value, *, minimum=0.0, maximum=1e9):
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError('Invalid finite simulation value')
    return float(value)


class MotionModel:
    def __init__(self, limits):
        if set(limits) != {'x', 'y', 'z'}:
            raise ValueError('Cartesian limits require x, y, z')
        self.limits = {axis: number(limit, minimum=0.001) for axis, limit in limits.items()}
        self.position = dict.fromkeys(self.limits, 0.0)
        self.target = dict(self.position)
        self.speed = 100.0

    def move(self, coordinates, *, feedrate=6000.0, relative=False):
        if not coordinates or not set(coordinates) <= set(self.limits) or type(relative) is not bool:
            raise ValueError('Invalid motion coordinates')
        speed = number(feedrate, minimum=0.001, maximum=600000.0) / 60.0
        target = dict(self.target)
        for axis, value in coordinates.items():
            value = number(value, minimum=-1e9)
            target[axis] = value + self.target[axis] if relative else value
            if not 0 <= target[axis] <= self.limits[axis]:
                raise ValueError('Motion exceeds printer volume')
        self.target, self.speed = target, speed

    def home(self):
        self.position = dict.fromkeys(self.limits, 0.0)
        self.target = dict(self.position)

    def step(self, seconds):
        seconds = number(seconds, maximum=3600.0)
        delta = {axis: self.target[axis] - value for axis, value in self.position.items()}
        distance = math.sqrt(sum(value * value for value in delta.values()))
        fraction = min(1.0, self.speed * seconds / distance) if distance else 1.0
        self.position = dict(self.target) if fraction == 1.0 else {
            axis: value + delta[axis] * fraction for axis, value in self.position.items()}
        return dict(self.position)

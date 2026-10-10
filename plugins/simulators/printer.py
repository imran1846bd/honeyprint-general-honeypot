"""Limited G-code interpreter joining deterministic physical models.

Commands mutate a detached candidate; rejected commands leave state unchanged.
This is a simulation subset, not a Klipper MCU or a complete firmware parser.
"""
from copy import deepcopy
import re
from plugins.simulators.motion import MotionModel, number
from plugins.simulators.thermal import ThermalModel
from plugins.simulators.extrusion import ExtrusionModel
from plugins.simulators.sensors import SensorModel


class PrinterSimulator:
    def __init__(self, profile):
        volume = profile['volume']
        self.motion = MotionModel(dict(x=volume['width'], y=volume['depth'], z=volume['height']))
        self.hotend = ThermalModel()
        self.bed = ThermalModel(maximum=130.0)
        self.extrusion = ExtrusionModel()
        self.sensors = SensorModel()
        self.heated_bed = profile['heated_bed']
        self.relative = False
        self.extrusion_relative = False
        self.powered = True

    def snapshot(self):
        return {'simulation': True, 'position': dict(self.motion.position),
                'target_position': dict(self.motion.target),
                'temperature': self.hotend.temperature, 'target_temperature': self.hotend.target,
                'bed_temperature': self.bed.temperature, 'bed_target': self.bed.target,
                'extrusion_position': self.extrusion.position,
                'consumed_mm': self.extrusion.consumed, 'volume_mm3': self.extrusion.volume,
                'powered': self.powered,
                'sensors': self.sensors.read(temperature=self.hotend.temperature, position=self.motion.position)}

    def step(self, seconds):
        seconds = number(seconds, maximum=3600.0)
        if self.powered:
            self.motion.step(seconds)
        self.hotend.step(seconds)
        self.bed.step(seconds)
        return self.snapshot()

    def command(self, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError('Invalid command')
        tokens = text.split(';', 1)[0].upper().split()
        if not tokens or not re.fullmatch(r'[GM][0-9]{1,3}', tokens[0]):
            raise ValueError('Invalid command code')
        params = {}
        for token in tokens[1:]:
            if not re.fullmatch(r'[XYZEFSP][+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', token) or token[0] in params:
                raise ValueError('Invalid command parameter')
            params[token[0]] = number(float(token[1:]), minimum=-1e9)
        candidate = deepcopy(self)
        response = candidate._execute(tokens[0], params)
        self.__dict__.update(candidate.__dict__)
        return {'response': response, 'simulation': True, 'gcode': tokens[0]}

    def _execute(self, code, params):
        if code == 'M105' and not params:
            return f'ok T:{self.hotend.temperature:.2f} /{self.hotend.target:.2f} B:{self.bed.temperature:.2f} /{self.bed.target:.2f}'
        if code == 'M112' and not params:
            self.powered = False
            self.hotend.shutdown(); self.bed.shutdown()
            self.motion.target = dict(self.motion.position)
            return 'ok emergency stop'
        if not self.powered:
            raise ValueError('Printer is stopped')
        if code in ('G90', 'G91', 'M82', 'M83', 'G28') and not params:
            if code == 'G28': self.motion.home()
            elif code in ('G90', 'G91'): self.relative = code == 'G91'
            else: self.extrusion_relative = code == 'M83'
        elif code in ('M104', 'M140') and set(params) == {'S'}:
            if code == 'M140' and not self.heated_bed: raise ValueError('No heated bed')
            (self.hotend if code == 'M104' else self.bed).set_target(params['S'])
        elif code in ('G0', 'G1') and params and set(params) <= set('XYZEF'):
            if self.sensors.thermistor_fault: raise ValueError('Thermistor fault')
            coordinates = {axis.lower(): value for axis, value in params.items() if axis in 'XYZ'}
            if coordinates:
                self.motion.move(coordinates, feedrate=params.get('F', self.motion.speed * 60), relative=self.relative)
            elif 'F' in params:
                self.motion.speed = number(params['F'], minimum=0.001, maximum=600000) / 60
            if 'E' in params:
                self.extrusion.move(params['E'], temperature=self.hotend.temperature,
                                    relative=self.extrusion_relative, filament_present=self.sensors.filament_present)
        elif code == 'G4' and set(params) in ({'P'}, {'S'}):
            self.step(params['S'] if 'S' in params else params['P'] / 1000)
        else:
            raise ValueError('Unsupported command or parameters')
        return 'ok'

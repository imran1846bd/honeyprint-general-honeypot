"""Attach deterministic thermistors to upstream Klipper's AVR serial emulator.

The AVR runs the real compiled MCU firmware. Heater outputs drive simplified
thermal models; analog voltages feed the MCU ADC. No physical devices are used.
"""
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/klipper/scripts')
sys.path.insert(0, '/opt/klipper/klippy')
import avrsim
import pysimulavr
from extras.thermistor import Thermistor


class ThermalPins(pysimulavr.PySimulationMember):
    def __init__(self, device):
        super().__init__()
        self.device = device
        self.clock = pysimulavr.SystemClock.Instance()
        self.temperatures = [20.0, 20.0]
        self.targets = [0.0, 0.0]
        self.last_target_read = 0.0
        self.last_step_at = time.monotonic()
        self.sources = []
        self.nets = []
        self.converter = Thermistor(4700.0, 0.0)
        self.converter.setup_coefficients(25.0, 100000.0, 150.0, 1641.9, 250.0, 226.15)
        for name in ('A7', 'A0'):
            source = pysimulavr.Pin(pysimulavr.Pin.ANALOG)
            source.SetAnalogValue(5.0 * self.converter.calc_adc(20.0))
            net = pysimulavr.Net(); net.Add(source); net.Add(device.GetPin(name))
            self.sources.append(source); self.nets.append(net)
        self.clock.Add(self)

    def DoStep(self, true_hw_step):
        now = time.monotonic()
        # The AVR emulator may run slower than wall time on a busy host. Use
        # elapsed wall time so a simulated preheat does not stall a print.
        dt = min(0.1, max(0.0, now - self.last_step_at))
        self.last_step_at = now
        if now - self.last_target_read >= 0.1:
            for index, name in enumerate(('extruder', 'heater_bed')):
                try:
                    self.targets[index] = float((Path('/data/thermal_targets') / name).read_text())
                except (OSError, ValueError):
                    pass
            self.last_target_read = now
        for index, (heater, rate, cooling) in enumerate((('B4', 100.0, 90.0), ('B3', 40.0, 120.0))):
            active = self.device.GetPin(heater).outState == pysimulavr.Pin.HIGH
            value = 20.0 + (self.temperatures[index] - 20.0) * math.exp(-dt / cooling)
            # The thermistor conversion reads the hotend slightly below this
            # simulated value. The two-degree headroom lets Klipper switch its
            # heater off near the requested target without large overshoot.
            ceiling = self.targets[index] + (2.0 if index == 0 else 0.5)
            if active and ceiling > value:
                value += min(rate * dt, ceiling - value)
            self.temperatures[index] = min(350.0, max(20.0, value))
            self.sources[index].SetAnalogValue(5.0 * self.converter.calc_adc(self.temperatures[index]))
        return 1000000


def main():
    factory = pysimulavr.AvrFactory.instance()
    retained = []
    class Factory:
        @staticmethod
        def instance(): return Factory()
        def makeDevice(self, name):
            device = factory.makeDevice(name)
            retained.append(ThermalPins(device))
            return device
    pysimulavr.AvrFactory = Factory
    avrsim.main()


if __name__ == '__main__': main()

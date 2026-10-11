"""Attach deterministic thermistors to upstream Klipper's AVR serial emulator.

The AVR runs the real compiled MCU firmware. Heater outputs drive simplified
thermal models; analog voltages feed the MCU ADC. No physical devices are used.
"""
import math
import sys

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
        dt = 0.001
        for index, (heater, rate, cooling) in enumerate((('B4', 15.0, 60.0), ('B3', 3.0, 120.0))):
            active = self.device.GetPin(heater).outState == pysimulavr.Pin.HIGH
            value = 20.0 + (self.temperatures[index] - 20.0) * math.exp(-dt / cooling)
            value += rate * dt if active else 0.0
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

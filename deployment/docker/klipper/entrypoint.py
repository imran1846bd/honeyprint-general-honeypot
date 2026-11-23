"""Supervise the real Klipper host, simulated AVR MCU and Moonraker in Docker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
try:
    from deployment.docker.klipper.settings import resolve_klipper_settings
except ModuleNotFoundError:  # The Docker image copies this module to /settings.py.
    from settings import resolve_klipper_settings


def printer_config(profile, settings=None):
    native = resolve_klipper_settings(settings)
    motion = native['motion']
    extruder = native['extruder']
    volume = profile['volume']
    for key in ('width','depth','height'):
        if type(volume[key]) not in (int,float) or not 0 < volume[key] <= 10000:
            raise ValueError('Invalid printer volume')
    if type(profile['heated_bed']) is not bool or type(profile['nozzle_diameter']) not in (int,float) or not 0 < profile['nozzle_diameter'] <= 10:
        raise ValueError('Invalid printer profile')
    sections=[]
    for axis,step,direction,endstop,dimension in (
        ('x','PA5','PA4','PB0','width'),('y','PA3','PA2','PB1','depth'),
        ('z','PC7','PC6','PB2','height')):
        stepper = native['steppers'][axis]
        sections.append(f'''[stepper_{axis}]
step_pin: {step}
dir_pin: {direction}
enable_pin: PA1
microsteps: {stepper['microsteps']}
rotation_distance: {stepper['rotation_distance']}
endstop_pin: ^{endstop}
position_endstop: 0
position_min: 0
position_max: {volume[dimension]}
''')
    sections.append(f'''[extruder]
step_pin: PC3
dir_pin: PC2
enable_pin: PA1
microsteps: {extruder['microsteps']}
rotation_distance: {extruder['rotation_distance']}
nozzle_diameter: {profile['nozzle_diameter']}
filament_diameter: {extruder['filament_diameter']}
heater_pin: PB4
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA7
control: watermark
max_delta: 0.25
min_temp: 0
min_extrude_temp: {extruder['min_extrude_temp']}
max_temp: {extruder['max_temp']}
''')
    if profile['heated_bed']:
        sections.append(f'''[heater_bed]
heater_pin: PB3
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA0
control: watermark
max_delta: 0.25
min_temp: 0
max_temp: {native['heated_bed']['max_temp']}
''')
    sections.append(f'''[mcu]
serial: /tmp/pseudoserial
[printer]
kinematics: cartesian
max_velocity: {motion['max_velocity']}
max_accel: {motion['max_accel']}
max_z_velocity: {motion['max_z_velocity']}
max_z_accel: {motion['max_z_accel']}
[virtual_sdcard]
path: /data/gcodes
[display_status]
[pause_resume]
[force_move]
enable_force_move: True
[homing_override]
gcode:
    SET_KINEMATIC_POSITION X=0 Y=0 Z=0
''')
    return '\n'.join(sections)


def main():
    root=Path('/data');root.mkdir(exist_ok=True)
    for name in ('logs','config','gcodes','thermal_targets'): (root/name).mkdir(exist_ok=True)
    profile=json.loads(os.environ['HONEYPRINT_PROFILE_JSON'])
    settings=json.loads(os.environ.get('HONEYPRINT_KLIPPER_SETTINGS_JSON', '{}'))
    (root/'config/printer.cfg').write_text(printer_config(profile, settings))
    (root/'config/moonraker.conf').write_text('''[server]
host: 0.0.0.0
port: 7125
klippy_uds_address: /tmp/klippy.sock
[machine]
provider: none
[authorization]
trusted_clients:
    127.0.0.0/8
    172.16.0.0/12
    192.168.0.0/16
cors_domains:
    http://127.0.0.1:*
[file_manager]
enable_object_processing: False
''')
    stopping=threading.Event()
    # SIGUSR1 = press the simulated board's reset button. Klipper's FIRMWARE_RESTART
    # cannot reset the emulated AVR (no DTR on a pty), so after an MCU shutdown the
    # HoneyPrint adapter asks for this reset and then restarts the Klipper host.
    reset=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopping.set())
    signal.signal(signal.SIGUSR1,lambda *_:reset.set())
    children=[];logs=[]
    try:
        # Simulate the firmware's 8 MHz oscillator at real-time pace.
        # This leaves host CPU headroom without skewing Klipper's time bases.
        commands=[('/opt/klippy-env/bin/python','/bridge.py','-s','8000000','-r','1','/opt/klipper/out/klipper.elf'),
                  ('/opt/klippy-env/bin/python','/opt/klipper/klippy/klippy.py',str(root/'config/printer.cfg'),'-a','/tmp/klippy.sock','-l',str(root/'logs/klippy.log')),
                  ('/opt/moonraker-env/bin/moonraker','-d',str(root),'-c',str(root/'config/moonraker.conf'),'-l',str(root/'logs/moonraker.log'))]
        for name,command in zip(('avr','host','api'),commands):
            log=(root/'logs'/f'{name}.console.log').open('ab');logs.append(log)
            children.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT))
        while not stopping.wait(.2):
            if reset.is_set():
                reset.clear()
                avr=children[0]
                if avr.poll() is None:
                    avr.terminate()
                    try: avr.wait(timeout=10)
                    except subprocess.TimeoutExpired: avr.kill();avr.wait(timeout=5)
                children[0]=subprocess.Popen(commands[0],stdout=logs[0],stderr=subprocess.STDOUT)
                continue
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A Klipper stack component stopped')
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in reversed(children):
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        for log in logs:log.close()


if __name__=='__main__':raise SystemExit(main())

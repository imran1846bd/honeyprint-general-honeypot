"""Supervise the real Klipper host, simulated AVR MCU and Moonraker in Docker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def printer_config(profile):
    volume = profile['volume']
    for key in ('width','depth','height'):
        if type(volume[key]) not in (int,float) or not 0 < volume[key] <= 10000:
            raise ValueError('Invalid printer volume')
    if type(profile['heated_bed']) is not bool or type(profile['nozzle_diameter']) not in (int,float) or not 0 < profile['nozzle_diameter'] <= 10:
        raise ValueError('Invalid printer profile')
    sections=[]
    for axis,step,direction,endstop,rotation,dimension in (
        ('x','PA5','PA4','PB0',40,'width'),('y','PA3','PA2','PB1',40,'depth'),
        ('z','PC7','PC6','PB2',8,'height')):
        sections.append(f'''[stepper_{axis}]
step_pin: {step}
dir_pin: {direction}
enable_pin: PA1
microsteps: 16
rotation_distance: {rotation}
endstop_pin: ^{endstop}
position_endstop: 0
position_min: 0
position_max: {volume[dimension]}
''')
    sections.append(f'''[extruder]
step_pin: PC3
dir_pin: PC2
enable_pin: PA1
microsteps: 16
rotation_distance: 33.5
nozzle_diameter: {profile['nozzle_diameter']}
filament_diameter: 1.75
heater_pin: PB4
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA7
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
min_temp: 0
min_extrude_temp: 170
max_temp: 280
''')
    if profile['heated_bed']:
        sections.append('''[heater_bed]
heater_pin: PB3
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA0
control: watermark
min_temp: 0
max_temp: 130
''')
    sections.append('''[mcu]
serial: /tmp/pseudoserial
[printer]
kinematics: cartesian
max_velocity: 200
max_accel: 1000
max_z_velocity: 20
max_z_accel: 30
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
    for name in ('logs','config','gcodes'): (root/name).mkdir(exist_ok=True)
    profile=json.loads(os.environ['HONEYPRINT_PROFILE_JSON'])
    (root/'config/printer.cfg').write_text(printer_config(profile))
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
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopping.set())
    children=[];logs=[]
    try:
        commands=[('/opt/klippy-env/bin/python','/bridge.py','-r','1','/opt/klipper/out/klipper.elf'),
                  ('/opt/klippy-env/bin/python','/opt/klipper/klippy/klippy.py',str(root/'config/printer.cfg'),'-a','/tmp/klippy.sock','-l',str(root/'logs/klippy.log')),
                  ('/opt/moonraker-env/bin/moonraker','-d',str(root),'-c',str(root/'config/moonraker.conf'),'-l',str(root/'logs/moonraker.log'))]
        for name,command in zip(('avr','host','api'),commands):
            log=(root/'logs'/f'{name}.console.log').open('ab');logs.append(log)
            children.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT))
        while not stopping.wait(.2):
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

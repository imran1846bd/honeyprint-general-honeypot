"""
Printer Simulator - Physics engine for realistic 3D printer simulation.

Analogous to HoneySat's Satellite Simulator. Simulates the physical processes
of a 3D printer including thermal dynamics, motion kinematics, extrusion,
and power consumption. Provides realistic sensor data to the firmware runtime.

Module types (mirroring HoneySat's Satellite Simulator architecture):
- Simulation Modules: thermal_sim, motion_sim, extrusion_sim, power_sim, print_job_sim
- Sensor Modules: thermistor, endstop, filament_sensor
- Subsystem State Modules: hotend, heated_bed, motion_system, cooling
- Printer State Module: printer_state (aggregates all subsystem states)
- Interface Module: interface between firmware and simulator
"""

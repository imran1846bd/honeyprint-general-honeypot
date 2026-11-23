"""
Firmware Runtime - Marlin firmware emulation layer.

Analogous to HoneySat's Flight Software Runtime + Flight Software Services.
Processes G-code commands (telecommands) and generates realistic Marlin
firmware responses (telemetry). This is the bridge between the exposed
protocols (Telnet, OctoPrint API) and the Printer Simulator.
"""

"""
Telnet Server - Exposed network protocol for direct firmware access.

Analogous to HoneySat's Telnet interface for satellite ground software.
Provides raw serial/terminal-style access to the Marlin firmware emulator,
allowing adversaries to send G-code commands directly as if connected
over a serial port to the 3D printer's controller board.
"""

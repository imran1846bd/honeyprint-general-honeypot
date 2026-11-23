"""
Network Simulator for HoneyPrint.

Analogous to HoneySat's Radio Simulator, this module simulates realistic
network conditions between the management interface and the emulated 3D
printer.  Where HoneySat models RF-link characteristics such as orbital
passes, signal attenuation, and Doppler shift, HoneyPrint's Network
Simulator models TCP/IP-level phenomena relevant to networked printers:
artificial latency (cloud-connected or remote printers), packet loss,
jitter, and scheduled online/offline transitions that mirror real-world
usage patterns (e.g., printers powered down outside business hours or
entering error states).
"""

from honeyprint.network_sim.simulator import NetworkSimulator

__all__ = ["NetworkSimulator"]

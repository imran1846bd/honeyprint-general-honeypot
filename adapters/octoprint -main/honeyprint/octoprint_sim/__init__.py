"""
OctoPrint Simulation - Simulates OctoPrint web management interface.

Analogous to HoneySat's Ground Software (SUCHAI MCS). This is the primary
exposed attack surface - a realistic OctoPrint-compatible web interface
with REST API and WebSocket support. Adversaries interact with this to
control the simulated printer, upload G-code, and gather intelligence.

Components:
- REST API (api_routes.py): OctoPrint-compatible API endpoints
- WebSocket (websocket_handler.py): Real-time printer status updates
- Web UI (templates/): Realistic OctoPrint-like web interface
- Authentication (auth.py): Login system with default credentials
"""

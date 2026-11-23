"""
OctoPrint WebSocket Simulation.

Implements the OctoPrint WebSocket protocol for real-time printer status
updates. Clients (including OctoPrint web UI, plugins, and tools like
OctoPrint-Monitor) connect here to receive live telemetry.

OctoPrint WebSocket protocol:
1. Client connects to /sockjs/websocket or /ws
2. Server sends a "connected" message with version info
3. Server sends periodic "current" messages with printer state
4. Server sends "event" messages for state changes

This is a key observation surface for the honeypot - WebSocket connections
indicate active monitoring by the adversary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from ..printer_simulator.simulator import PrinterSimulator

logger = logging.getLogger("honeyprint.websocket")


class OctoPrintWebSocket:
    """Manages WebSocket connections simulating the OctoPrint push API."""

    def __init__(self, simulator: PrinterSimulator, config: dict) -> None:
        self.simulator = simulator
        self.config = config
        self._clients: set[web.WebSocketResponse] = set()
        self._update_interval: float = config.get(
            "websocket", {},
        ).get("update_interval", 1.0)
        self._broadcast_task: asyncio.Task[None] | None = None
        self._log_callback: Any = None

        ground_cfg = config.get("ground", {})
        self._version = ground_cfg.get("octoprint_version", "1.9.3")
        self._api_key = ground_cfg.get(
            "api_key", "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
        )

    def set_log_callback(self, callback: Any) -> None:
        """Set a callback for logging honeypot interactions."""
        self._log_callback = callback

    def _log_interaction(
        self, action: str, remote: str, data: dict[str, Any] | None = None,
    ) -> None:
        """Log a WebSocket interaction for the honeypot."""
        if self._log_callback:
            self._log_callback("websocket_interaction", {
                "action": action,
                "ip": remote,
                "data": data,
                "timestamp": time.time(),
            })

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle an incoming WebSocket connection."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        remote = request.remote or "unknown"
        path = request.path
        logger.info(
            "WebSocket connection opened: %s from %s", path, remote,
        )
        self._log_interaction("ws_connect", remote, {
            "path": path,
            "user_agent": request.headers.get("User-Agent", ""),
        })

        self._clients.add(ws)

        try:
            # Send the initial "connected" message
            await self._send_connected(ws)

            # Start the broadcast loop if not already running
            self._ensure_broadcast_running()

            # Listen for incoming messages from the client
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_client_message(ws, msg.data, remote)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning(
                        "WebSocket error from %s: %s",
                        remote, ws.exception(),
                    )
        finally:
            self._clients.discard(ws)
            logger.info("WebSocket connection closed: %s", remote)
            self._log_interaction("ws_disconnect", remote)

        return ws

    async def _send_connected(self, ws: web.WebSocketResponse) -> None:
        """Send the OctoPrint 'connected' handshake message."""
        # Generate a deterministic-looking plugin hash
        plugin_hash = hashlib.md5(
            self._version.encode(), usedforsecurity=False,
        ).hexdigest()[:16]

        message = {
            "connected": {
                "version": self._version,
                "display_version": self._version,
                "apikey": self._api_key,
                "branch": "master",
                "plugin_hash": plugin_hash,
            },
        }
        await self._send_json(ws, message)

    async def _handle_client_message(
        self,
        ws: web.WebSocketResponse,
        data: str,
        remote: str,
    ) -> None:
        """Process a message received from a WebSocket client."""
        logger.debug("WebSocket message from %s: %s", remote, data[:200])
        self._log_interaction("ws_message", remote, {"payload": data[:500]})

        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return

        # OctoPrint clients may send auth or throttle messages
        if "auth" in parsed:
            logger.info(
                "WebSocket auth attempt from %s: %s",
                remote, parsed["auth"],
            )
            self._log_interaction("ws_auth", remote, {"auth": parsed["auth"]})
        elif "throttle" in parsed:
            # Client requesting a different update rate - acknowledge but ignore
            logger.debug(
                "WebSocket throttle request from %s: %s",
                remote, parsed["throttle"],
            )

    # ------------------------------------------------------------------
    # Broadcast loop
    # ------------------------------------------------------------------

    def _ensure_broadcast_running(self) -> None:
        """Start the periodic broadcast task if it is not already active."""
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def _broadcast_loop(self) -> None:
        """Periodically send 'current' state to all connected clients."""
        logger.debug("WebSocket broadcast loop started")
        try:
            while self._clients:
                try:
                    message = self._build_current_message()
                    await self._broadcast(message)
                except Exception:
                    logger.exception("Error building/broadcasting current message")
                await asyncio.sleep(self._update_interval)
        finally:
            logger.debug("WebSocket broadcast loop stopped (no clients)")

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to every connected client."""
        payload = json.dumps(message)
        stale: list[web.WebSocketResponse] = []

        for ws in self._clients:
            try:
                await ws.send_str(payload)
            except ConnectionResetError:
                stale.append(ws)
            except Exception:
                logger.debug("Failed to send to a WebSocket client", exc_info=True)
                stale.append(ws)

        for ws in stale:
            self._clients.discard(ws)

    async def send_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Send an OctoPrint event message to all connected clients."""
        message: dict[str, Any] = {
            "event": {
                "type": event_type,
                "payload": payload,
            },
        }
        await self._broadcast(message)

    # ------------------------------------------------------------------
    # Message builders
    # ------------------------------------------------------------------

    def _build_current_message(self) -> dict[str, Any]:
        """Build an OctoPrint 'current' message from the simulator state."""
        temps = self.simulator.get_temperatures()
        job_status = self.simulator.print_job.get_status()

        state_text, state_flags = self._resolve_state(job_status)

        temperature_data: dict[str, Any] = {
            "tool0": {
                "actual": round(temps["hotend"]["current"], 2),
                "target": round(temps["hotend"]["target"], 1),
                "offset": 0,
            },
            "bed": {
                "actual": round(temps["bed"]["current"], 2),
                "target": round(temps["bed"]["target"], 1),
                "offset": 0,
            },
        }

        progress_data: dict[str, Any] = {
            "completion": (
                job_status.get("progress", 0.0)
                if job_status.get("state") != "idle"
                else None
            ),
            "filepos": None,
            "printTime": (
                int(job_status.get("elapsed_time", 0))
                if job_status.get("state") != "idle"
                else None
            ),
            "printTimeLeft": (
                int(job_status.get("estimated_remaining", 0))
                if job_status.get("state") != "idle"
                else None
            ),
            "printTimeLeftOrigin": "estimate",
            "currentLine": job_status.get("current_line", 0),
            "totalLines": job_status.get("total_lines", 0),
            "currentCommand": job_status.get("current_command", ""),
            "currentLayer": job_status.get("current_layer", 0),
            "totalLayers": job_status.get("total_layers", 0),
        }

        job_data: dict[str, Any] = {
            "file": {
                "name": job_status.get("filename"),
                "display": job_status.get("filename"),
                "path": job_status.get("filename"),
                "type": "machinecode",
                "typePath": ["machinecode", "gcode"],
                "origin": "local",
                "size": None,
                "date": None,
            },
            "estimatedPrintTime": job_status.get("estimated_remaining"),
            "filament": {
                "tool0": {
                    "length": job_status.get("filament_used_mm", 0),
                    "volume": 0,
                },
            },
        }

        return {
            "current": {
                "state": {
                    "text": state_text,
                    "flags": state_flags,
                },
                "job": job_data,
                "progress": progress_data,
                "temps": [temperature_data],
                "logs": [],
                "messages": [],
                "serverTime": time.time(),
                "busyFiles": [],
                "offsets": {},
                "resends": {
                    "count": 0,
                    "transmitted": 0,
                    "ratio": 0,
                },
                "currentZ": self.simulator.get_position().get("z", 0.0),
            },
        }

    @staticmethod
    def _resolve_state(
        job_status: dict[str, Any],
    ) -> tuple[str, dict[str, bool]]:
        """Derive OctoPrint state text and flags from the job status."""
        state = job_status.get("state", "idle")

        flags: dict[str, bool] = {
            "operational": True,
            "paused": False,
            "printing": False,
            "pausing": False,
            "cancelling": False,
            "sdReady": True,
            "error": False,
            "ready": True,
            "closedOrError": False,
        }

        if state == "printing":
            text = "Printing"
            flags["printing"] = True
            flags["ready"] = False
        elif state == "paused":
            text = "Paused"
            flags["paused"] = True
        elif state == "cancelling":
            text = "Cancelling"
            flags["cancelling"] = True
            flags["ready"] = False
        elif state == "error":
            text = "Error"
            flags["error"] = True
            flags["ready"] = False
        else:
            text = "Operational"

        return text, flags

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_json(ws: web.WebSocketResponse, data: Any) -> None:
        """Serialize *data* to JSON and send over the WebSocket."""
        await ws.send_str(json.dumps(data))

    async def shutdown(self) -> None:
        """Close all WebSocket connections and cancel the broadcast task."""
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass

        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        logger.info("WebSocket handler shut down")


# ------------------------------------------------------------------
# Route registration
# ------------------------------------------------------------------

def setup_websocket_routes(app: web.Application) -> None:
    """Register OctoPrint WebSocket routes on the given aiohttp application.

    Expects ``app["websocket_handler"]`` to be an :class:`OctoPrintWebSocket`
    instance (set this up before calling).
    """
    handler: OctoPrintWebSocket = app["websocket_handler"]
    app.router.add_get("/sockjs/websocket", handler.handle_websocket)
    app.router.add_get("/ws", handler.handle_websocket)
    logger.info("WebSocket routes registered: /sockjs/websocket, /ws")

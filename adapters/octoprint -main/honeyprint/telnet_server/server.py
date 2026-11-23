"""
Telnet Server - Asyncio-based Telnet server exposing Marlin serial interface.

Listens for incoming Telnet connections and presents a Marlin firmware
serial console. Each connection receives a realistic boot banner and can
send G-code commands that are processed by the MarlinEmulator.

All interactions are logged for honeypot intelligence gathering.
"""

import asyncio
import logging
import time
from typing import Any, Callable

from ..firmware.marlin_emu import MarlinEmulator

logger = logging.getLogger("honeyprint.telnet")

_STARTUP_BANNER = (
    "start\r\n"
    "echo:Marlin 2.1.2.1\r\n"
    "echo: Last Updated: 2025-12-01 | Author: (default config)\r\n"
    "echo:Compiled: Dec 01 2025\r\n"
    "echo: Free Memory: 2408  PlannerBufferBytes: 1232\r\n"
    "echo:V87 stored settings retrieved (628 bytes; crc 12345)\r\n"
)


class TelnetServer:
    """
    Asyncio Telnet server presenting a Marlin serial interface.

    Accepts multiple concurrent connections, sends a Marlin boot banner,
    and relays G-code commands to the firmware emulator. All activity
    is logged via the provided callback for honeypot analysis.
    """

    def __init__(
        self,
        host: str,
        port: int,
        firmware: MarlinEmulator,
        log_callback: Callable[..., Any],
        config: dict[str, Any],
        telemetry: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.firmware = firmware
        self.log_callback = log_callback
        self.config = config
        self.telemetry = telemetry

        self._server: asyncio.Server | None = None
        self._active_connections: dict[str, asyncio.Task[None]] = {}
        self._connection_count: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connection_count(self) -> int:
        """Total number of connections received since server start."""
        return self._connection_count

    @property
    def active_ips(self) -> list[str]:
        """List of currently connected remote IPs (with port)."""
        return list(self._active_connections.keys())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening for Telnet connections."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info("Telnet server listening on %s", addrs)

    async def stop(self) -> None:
        """Gracefully shut down the server and all active connections."""
        if self._server is None:
            return

        logger.info("Telnet server shutting down")
        self._server.close()
        await self._server.wait_closed()

        # Cancel all outstanding client handler tasks.
        for peer, task in list(self._active_connections.items()):
            task.cancel()
            logger.debug("Cancelled connection handler for %s", peer)

        self._active_connections.clear()
        self._server = None
        logger.info("Telnet server stopped")

    # ------------------------------------------------------------------
    # Client handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single Telnet client session."""
        peer_info = writer.get_extra_info("peername")
        peer_str = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "unknown"
        connect_ts = time.time()

        self._connection_count += 1
        # Store the current task so it can be cancelled on shutdown.
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_connections[peer_str] = current_task

        logger.info("New Telnet connection from %s", peer_str)
        self._log_event(peer_str, "connect", "", connect_ts)

        try:
            # --- Send Marlin boot banner ---
            writer.write(_STARTUP_BANNER.encode("utf-8"))
            writer.write(b"ok\r\n")
            await writer.drain()

            # --- Register an async sender so the firmware can push
            #     unsolicited messages (e.g. temperature auto-reports). ---
            async def _async_sender(msg: str) -> None:
                try:
                    writer.write(f"{msg}\r\n".encode("utf-8"))
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass  # Client already gone; ignore.

            self.firmware.set_async_sender(_async_sender)

            # --- Command loop ---
            while True:
                data = await reader.readline()
                if not data:
                    # Client disconnected (EOF).
                    break

                line = data.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                cmd_ts = time.time()
                logger.info("[%s] >> %s", peer_str, line)
                self._log_event(peer_str, "command", line, cmd_ts)
                if self.telemetry:
                    self.telemetry.record("telnet_command", {
                        "command": line,
                        "source": "telnet",
                        "ip": peer_str,
                    })

                try:
                    response = await self.firmware.process_command(line)
                except Exception:
                    logger.exception(
                        "Firmware error processing command from %s: %s",
                        peer_str,
                        line,
                    )
                    response = "Error:command processing failed"

                if response:
                    out = f"{response}\r\n".encode("utf-8")
                    writer.write(out)
                    await writer.drain()
                    logger.debug("[%s] << %s", peer_str, response)

        except asyncio.CancelledError:
            logger.info("Connection to %s cancelled (server shutdown)", peer_str)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            logger.warning("Connection error with %s: %s", peer_str, exc)
        finally:
            disconnect_ts = time.time()
            self._active_connections.pop(peer_str, None)
            self._log_event(peer_str, "disconnect", "", disconnect_ts)
            logger.info(
                "Telnet connection from %s closed (duration %.1fs)",
                peer_str,
                disconnect_ts - connect_ts,
            )
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_event(
        self,
        peer: str,
        event_type: str,
        data: str,
        timestamp: float,
    ) -> None:
        """Forward an interaction event to the registered log callback."""
        try:
            self.log_callback(
                "telnet",
                {
                    "protocol": "telnet",
                    "peer": peer,
                    "event": event_type,
                    "data": data,
                    "timestamp": timestamp,
                }
            )
        except Exception:
            logger.exception("log_callback raised an exception")

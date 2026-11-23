"""
HoneyPrint Main Entry Point - Orchestrates all honeypot components.

Analogous to HoneySat's overall framework coordination. Initializes and
starts all components:
1. Printer Simulator (physics engine)
2. Firmware Runtime (Marlin emulator)
3. OctoPrint Web Interface (management software)
4. Telnet Server (direct serial access)
5. Logging Repository (interaction database)
6. Network Simulator (network conditions)
"""

import asyncio
import json
import math
import re
import logging
import signal
import sys
import os
import time

from .config import load_config, HoneyPrintConfig
from .printer_simulator.simulator import PrinterSimulator
from .firmware.marlin_emu import MarlinEmulator
from .octoprint_sim.app import create_web_app
from .telnet_server.server import TelnetServer
from .logging_repo.logger import HoneyPrintLogger, create_logger
from .telemetry import TelemetryCollector

logger = logging.getLogger("honeyprint")


def emit_framework_evidence(category, data):
    """Export bounded observations without recording credentials or payloads."""
    if category == "web_request":
        flag, source, kind = "requests", "api", "http.request"
        action, outcome = data["method"], "observed"
    elif category == "websocket_connect":
        flag, source, kind = "requests", "websocket", "websocket.connection"
        action, outcome = "connect", "observed"
    elif category in {"login_attempt", "login_success"}:
        flag, source, kind = "requests", "api", "auth.login"
        action = "login"
        outcome = "accepted" if data.get("success") else "rejected"
    elif category == "gcode":
        flag, source, kind = "commands", "gcode", "printer.command"
        match = re.match(r"^[GMT][0-9]{1,5}(?:\.[0-9]{1,3})?(?![0-9])",
                         str(data.get("command", "")), re.IGNORECASE)
        action, outcome = (match.group().upper() if match else "unknown"), "observed"
    elif category == "file_upload":
        flag, source, kind = "requests", "api", "file.upload"
        action, outcome = "upload", "accepted"
    elif category == "print_session":
        flag, source, kind = "state_changes", "printer", "printer.state"
        action, outcome = data["event_type"], "observed"
    elif category == "api_interaction":
        flag, source, kind = "requests", "api", "api.interaction"
        action, outcome = data["action"], "observed"
    else:
        return
    if os.environ.get(f"HONEYPRINT_CAPTURE_{flag.upper()}") != "1":
        return
    metadata = {}
    for key in ("ip", "path", "status", "elapsed_ms", "filename", "size",
                "content_hash", "progress", "state", "line"):
        value = data.get(key)
        if isinstance(value, str):
            metadata[key] = value[:512]
        elif type(value) in (int, bool) or type(value) is float and math.isfinite(value):
            metadata[key] = value
    print("HONEYPRINT_EVIDENCE " + json.dumps({
        "source": source, "event_type": kind, "action": action,
        "outcome": outcome, "simulation": True, "metadata": metadata,
    }), flush=True)


class HoneyPrint:
    """
    Main HoneyPrint orchestrator.

    Manages lifecycle of all honeypot components following the
    HoneySat architecture pattern of ground segment + space segment.
    """

    def __init__(self, config_dir: str | None = None):
        self.config = load_config(config_dir)
        self._setup_logging()

        # Initialize components
        config_dict = self.config.to_dict()

        # Logging Repository
        self.logger_repo = create_logger({
            "mongodb_uri": config_dict.get("ground", {}).get(
                "mongodb_uri", "mongodb://localhost:27017"
            ),
            "db_name": config_dict.get("ground", {}).get(
                "mongodb_db", "honeyprint_logs"
            ),
        })

        # Space Segment: Printer Simulator + Firmware
        self.simulator = PrinterSimulator(config_dict)
        self.firmware = MarlinEmulator(self.simulator, config_dict)
        self.telemetry = TelemetryCollector("octoprint", self._log_interaction)
        self.simulator.set_event_callback(self.telemetry.record)
        self.simulator.set_print_command_executor(
            self.firmware.process_print_command
        )

        # Ground Segment: Web Interface + Telnet
        self.web_app = create_web_app(
            simulator=self.simulator,
            firmware=self.firmware,
            config=config_dict,
            log_callback=self._log_interaction,
            telemetry=self.telemetry,
        )
        self.telnet_server = TelnetServer(
            host="0.0.0.0",
            port=self.config.ground.get("telnet_port", 23),
            firmware=self.firmware,
            log_callback=self._log_interaction,
            config=config_dict,
            telemetry=self.telemetry,
        )

        self._running = False
        self._web_runner = None

        logger.info(
            "HoneyPrint initialized - Printer: %s (%s)",
            self.config.printer.get("name", "Unknown"),
            self.config.printer.get("firmware", "Marlin"),
        )

    def _setup_logging(self) -> None:
        """Configure Python logging."""
        level = getattr(
            logging,
            self.config.logging_config.get("level", "INFO"),
            logging.INFO,
        )
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _log_interaction(self, category: str, data: dict) -> None:
        """Central logging callback for all components."""
        emit_framework_evidence(category, data)
        data = {key: value for key, value in data.items() if key != "password"}
        data["category"] = category
        data.setdefault("timestamp", time.time())

        # Log to Python logger
        logger.info("INTERACTION [%s]: %s",
                     category, {k: v for k, v in data.items()
                                if k != "content_preview"})

        # Log to MongoDB (async - fire and forget)
        if self.logger_repo:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.logger_repo.log(category, data))
            except RuntimeError:
                pass

    async def start(self) -> None:
        """Start all HoneyPrint components."""
        self._running = True
        logger.info("=" * 60)
        logger.info("  HoneyPrint - Additive Manufacturing Honeypot")
        logger.info("  Printer: %s", self.config.printer.get("name"))
        logger.info("  Firmware: %s %s",
                     self.config.printer.get("firmware"),
                     self.config.printer.get("firmware_version"))
        logger.info("=" * 60)

        # Connect logging repository
        try:
            await self.logger_repo.connect()
            logger.info("Logging repository connected")
        except Exception as e:
            logger.warning("MongoDB not available, using file logging: %s", e)

        await self.logger_repo.log_event("honeyprint_start", {
            "printer_name": self.config.printer.get("name"),
            "printer_profile": self.config.printer.get("profile"),
            "web_port": self.config.ground.get("web_port"),
            "telnet_port": self.config.ground.get("telnet_port"),
        })

        # Start simulator
        await self.simulator.start()
        logger.info("Printer simulator started")

        # Optionally start a fake print job for realism
        if os.environ.get("HONEYPRINT_FAKE_PRINT", "").lower() == "true":
            self.simulator.print_job.start_fake_print(
                filename="bracket_v3.gcode",
                total_time_minutes=120,
                total_layers=300,
                filament_mm=12000,
            )
            logger.info("Fake print job started for deception")

        # Start Telnet server
        await self.telnet_server.start()
        telnet_port = self.config.ground.get("telnet_port", 23)
        logger.info("Telnet server listening on port %d", telnet_port)

        # Start web server
        web_port = self.config.ground.get("web_port", 80)
        from aiohttp import web
        self._web_runner = web.AppRunner(self.web_app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, "0.0.0.0", web_port)
        await site.start()
        logger.info("Web interface listening on port %d", web_port)

        logger.info("=" * 60)
        logger.info("  HoneyPrint is ACTIVE - Waiting for adversaries...")
        logger.info("  Web:    http://0.0.0.0:%d", web_port)
        logger.info("  Telnet: telnet 0.0.0.0 %d", telnet_port)
        logger.info("=" * 60)

    async def stop(self) -> None:
        """Gracefully stop all components."""
        logger.info("Shutting down HoneyPrint...")
        self._running = False

        await self.logger_repo.log_event("honeyprint_stop", {})

        # Stop components in reverse order
        await self.telnet_server.stop()
        if self._web_runner:
            await self._web_runner.cleanup()
        await self.simulator.stop()
        await self.logger_repo.close()

        logger.info("HoneyPrint stopped")

    async def run_forever(self) -> None:
        """Start and run until interrupted."""
        await self.start()

        # Wait for shutdown signal
        stop_event = asyncio.Event()

        def signal_handler():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                pass  # Windows

        await stop_event.wait()
        await self.stop()


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="HoneyPrint - Additive Manufacturing Honeypot"
    )
    parser.add_argument(
        "--config-dir", "-c",
        help="Configuration directory path",
        default=None,
    )
    parser.add_argument(
        "--profile", "-p",
        help="Printer personality profile name",
        default=None,
    )
    parser.add_argument(
        "--web-port",
        type=int,
        help="Web interface port (default: 80)",
        default=None,
    )
    parser.add_argument(
        "--telnet-port",
        type=int,
        help="Telnet server port (default: 23)",
        default=None,
    )
    parser.add_argument(
        "--fake-print",
        action="store_true",
        help="Start with a fake print job for realism",
    )
    args = parser.parse_args()

    # Apply CLI overrides via env vars
    if args.profile:
        os.environ["HONEYPRINT_PRINTER_PROFILE"] = args.profile
    if args.web_port:
        os.environ["HONEYPRINT_WEB_PORT"] = str(args.web_port)
    if args.telnet_port:
        os.environ["HONEYPRINT_TELNET_PORT"] = str(args.telnet_port)
    if args.fake_print:
        os.environ["HONEYPRINT_FAKE_PRINT"] = "true"

    honeyprint = HoneyPrint(config_dir=args.config_dir)
    asyncio.run(honeyprint.run_forever())


if __name__ == "__main__":
    main()

"""Independent HoneyPrint_Klipper process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

from aiohttp import web

from .config import load_config
from .klipper_sim.app import create_moonraker_app
from .klipper_sim.firmware import KlipperEmulator
from .logging_repo.logger import create_logger
from .printer_simulator.simulator import PrinterSimulator
from .telemetry import TelemetryCollector

logger = logging.getLogger("honeyprint.klipper")


class HoneyPrintKlipper:
    """Run a Moonraker-style Klipper honeypot as an independent service."""

    def __init__(self, config_dir: str | None = None):
        self.config = load_config(config_dir)
        config = self.config.to_dict()
        config["ground"]["storage_dir"] = os.environ.get(
            "HONEYPRINT_STORAGE_DIR", "data/klipper"
        )
        logging.basicConfig(
            level=getattr(logging, self.config.logging_config.get("level", "INFO")),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.logger_repo = create_logger({
            "mongodb_uri": self.config.ground.get(
                "mongodb_uri", "mongodb://honeyprint_mongo:27017"
            ),
            "db_name": self.config.ground.get(
                "mongodb_db", "honeyprint_klipper_logs"
            ),
            "fallback_dir": "logs/klipper",
        })
        self.simulator = PrinterSimulator(config)
        self.firmware = KlipperEmulator(self.simulator, config)
        self.telemetry = TelemetryCollector("klipper", self._log_interaction)
        self.simulator.set_event_callback(self.telemetry.record)
        self.simulator.set_print_command_executor(self.firmware.process_print_command)
        self.web_app = create_moonraker_app(
            self.simulator,
            self.firmware,
            config,
            self._log_interaction,
            self.telemetry,
        )
        self._web_runner: web.AppRunner | None = None

    def _log_interaction(self, category: str, data: dict) -> None:
        data["honeypot_variant"] = "klipper"
        data.setdefault("timestamp", time.time())
        logger.info("INTERACTION [%s]: %s", category, data)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.logger_repo.log(category, data))
        except RuntimeError:
            pass

    async def start(self) -> None:
        await self.logger_repo.connect()
        await self.logger_repo.log_event("honeyprint_klipper_start", {
            "honeypot_variant": "klipper",
            "printer_name": self.config.printer.get("name"),
        })
        await self.simulator.start()
        self._web_runner = web.AppRunner(self.web_app)
        await self._web_runner.setup()
        port = self.config.ground.get("web_port", 7125)
        await web.TCPSite(self._web_runner, "0.0.0.0", port).start()
        logger.info("HoneyPrint_Klipper Moonraker surface listening on port %d", port)

    async def stop(self) -> None:
        await self.logger_repo.log_event("honeyprint_klipper_stop", {
            "honeypot_variant": "klipper",
        })
        if self._web_runner:
            await self._web_runner.cleanup()
        await self.simulator.stop()
        await self.logger_repo.close()

    async def run_forever(self) -> None:
        await self.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await stop_event.wait()
        await self.stop()


def main() -> None:
    asyncio.run(HoneyPrintKlipper().run_forever())


if __name__ == "__main__":
    main()

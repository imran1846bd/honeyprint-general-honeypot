"""Klipper-flavored command adapter backed by the shared printer simulator."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from ..firmware.marlin_emu import MarlinEmulator
from ..firmware.sd_card import SDCardSimulation, SDFile
from ..printer_simulator.simulator import PrinterSimulator


class VirtualSDCardSimulation(SDCardSimulation):
    """Klipper virtual SD storage keeps host-style filenames intact."""

    @staticmethod
    def _display_filename(filename: str) -> str:
        return filename


class KlipperEmulator:
    """Translate common Klipper extended commands into simulator actions."""

    def __init__(self, simulator: PrinterSimulator, config: dict):
        self.sim = simulator
        self.config = config
        self.sd = VirtualSDCardSimulation(
            storage_dir=config.get("ground", {}).get("storage_dir")
        )
        self._marlin = MarlinEmulator(simulator, config)
        self._marlin.sd = self.sd
        self._output_callbacks: list[Callable[[str], None]] = []

    def add_output_callback(self, callback: Callable[[str], None]) -> None:
        self._output_callbacks.append(callback)

    def _emit(self, message: str) -> None:
        for callback in self._output_callbacks:
            callback(message)

    async def process_script(self, script: str, source: str = "api") -> str:
        responses: list[str] = []
        for line in script.splitlines():
            line = line.strip()
            if not line:
                continue
            response = await self.process_command(line)
            self._emit(f"{line}\n{response}")
            responses.append(response)
        return "\n".join(responses) or "ok"

    async def process_print_command(self, line: str) -> str:
        if self._is_extended_command(line):
            return await self.process_command(line)
        return await self._marlin.process_print_command(line)

    async def process_command(self, line: str) -> str:
        line = line.strip()
        if not line:
            return "ok"

        command, params = self._parse_extended(line)
        if command == "SET_HEATER_TEMPERATURE":
            heater = params.get("HEATER", "extruder")
            target = float(params.get("TARGET", 0))
            if heater in {"heater_bed", "bed"}:
                self.sim.set_bed_temp(target)
            else:
                self.sim.set_hotend_temp(target)
            return "ok"
        if command == "TURN_OFF_HEATERS":
            self.sim.set_hotend_temp(0)
            self.sim.set_bed_temp(0)
            return "ok"
        if command == "SET_FAN_SPEED":
            speed = float(params.get("SPEED", 0))
            self.sim.set_fan_speed(round(max(0.0, min(1.0, speed)) * 255))
            return "ok"
        if command == "PAUSE":
            self.sim.pause_print()
            return "ok"
        if command == "RESUME":
            self.sim.resume_print()
            return "ok"
        if command in {"CANCEL_PRINT", "SDCARD_RESET_FILE"}:
            self.cancel_print()
            return "ok"
        if command == "SDCARD_PRINT_FILE":
            filename = params.get("FILENAME", "")
            return "ok" if self.start_print(filename) else "error: file not found"
        if command in {"FIRMWARE_RESTART", "RESTART"}:
            return "ok"
        if command == "M112":
            self.emergency_stop()
            return "ok"

        return await self._marlin.process_command(line)

    def start_print(self, filename: str) -> bool:
        file = self.sd.get_file(filename)
        if file is None:
            return False
        self.sd.select_file(file.name)
        return self.sim.start_print_file(file.name, file.content)

    def cancel_print(self) -> None:
        self.sim.cancel_print()
        self.sim.set_hotend_temp(0)
        self.sim.set_bed_temp(0)
        self.sim.set_fan_speed(0)

    def emergency_stop(self) -> None:
        self.cancel_print()
        self.sim.disable_motors()

    @staticmethod
    def _is_extended_command(line: str) -> bool:
        return bool(line and line[0].isalpha() and "_" in line.split()[0])

    @staticmethod
    def _parse_extended(line: str) -> tuple[str, dict[str, str]]:
        tokens = shlex.split(line)
        if not tokens:
            return "", {}
        params: dict[str, str] = {}
        for token in tokens[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                params[key.upper()] = value
        return tokens[0].upper(), params

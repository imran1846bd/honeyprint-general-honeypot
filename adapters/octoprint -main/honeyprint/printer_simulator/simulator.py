"""
Printer Simulator - Main coordinator for all physics simulations.

Analogous to HoneySat's Satellite Simulator (Fig. 3 in paper). Coordinates
all simulation modules (thermal, motion, extrusion, power, print job) and
provides a unified interface for the firmware runtime to query sensor data
and update printer state.

Architecture mirrors HoneySat's Satellite Simulator:
- Simulation Modules → thermal_sim, motion_sim, extrusion_sim, power_sim
- Sensor Modules → temperature sensors, endstops, filament sensor
- Subsystem States → hotend state, bed state, motion state
- Printer State → unified state aggregation
- Interface → methods called by firmware runtime
"""

import asyncio
import time
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .thermal_sim import ThermalSimulation
from .motion_sim import MotionSimulation
from .extrusion_sim import ExtrusionSimulation
from .power_sim import PowerSimulation
from .print_job_sim import PrintJobSimulation

logger = logging.getLogger("honeyprint.simulator")


class PrinterSimulator:
    """
    Central printer simulator coordinating all physics modules.

    This is the core of HoneyPrint's space segment equivalent.
    All sensor data flows through this class.
    """

    def __init__(self, config: dict):
        printer_cfg = config.get("printer", {})
        sim_cfg = config.get("simulation", {})

        # Initialize simulation modules
        self.thermal = ThermalSimulation(
            ambient_temp=sim_cfg.get("ambient_temperature", 22.0),
            noise_stddev=sim_cfg.get("noise_stddev", 0.3),
        )
        self.motion = MotionSimulation(
            build_volume=printer_cfg.get("build_volume", {})
        )
        self.extrusion = ExtrusionSimulation(
            filament_diameter=printer_cfg.get("filament_diameter", 1.75),
            nozzle_diameter=printer_cfg.get("nozzle_diameter", 0.4),
        )
        self.power = PowerSimulation(
            voltage=sim_cfg.get("voltage", 24.0),
        )
        self.print_job = PrintJobSimulation()

        # Configure thermal limits from printer personality
        self.thermal.state.hotend.max_temp = printer_cfg.get(
            "max_temp_hotend", 260
        )
        self.thermal.state.bed.max_temp = printer_cfg.get(
            "max_temp_bed", 100
        )
        if not printer_cfg.get("heated_bed", True):
            self.thermal.state.bed.enabled = False

        # Update intervals
        self._thermal_interval = sim_cfg.get("thermal_update_interval", 1.0)
        self._motion_interval = sim_cfg.get("motion_update_interval", 0.1)
        self._print_interval = sim_cfg.get("print_job_update_interval", 1.0)

        self._running = False
        self._last_update = time.time()
        self._print_command_executor: Callable[[str], Awaitable[str]] | None = None
        self._event_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._print_line_budget = 0.0
        self._print_lines_per_second = 0.0

        # Printer state flags
        self.motors_enabled = True
        self.sd_printing = False

        logger.info(
            "PrinterSimulator initialized: %s (build volume: %s)",
            printer_cfg.get("name", "Unknown"),
            printer_cfg.get("build_volume", {}),
        )

    async def start(self) -> None:
        """Start the simulation loop."""
        self._running = True
        logger.info("Printer simulator started")
        asyncio.create_task(self._simulation_loop())

    async def stop(self) -> None:
        """Stop the simulation loop."""
        self._running = False
        logger.info("Printer simulator stopped")

    async def _simulation_loop(self) -> None:
        """Main simulation update loop."""
        while self._running:
            try:
                now = time.time()
                dt = now - self._last_update
                self._last_update = now

                # Update all simulations
                self.thermal.update(dt)
                self.print_job.update(dt)
                await self._execute_print_commands(dt)
                self.power.update(
                    thermal_state=self.thermal.get_status(),
                    motion_active=self.motion.state.is_moving,
                    fan_speed=int(self.thermal.state.fan_speed),
                    dt=dt,
                )

                await asyncio.sleep(self._thermal_interval)
            except Exception as e:
                logger.error("Simulation loop error: %s", e)
                await asyncio.sleep(1.0)

    def set_print_command_executor(
        self,
        executor: Callable[[str], Awaitable[str]],
    ) -> None:
        self._print_command_executor = executor

    def set_event_callback(
        self,
        callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._event_callback = callback

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_callback:
            self._event_callback(event_type, data)

    def start_print_file(self, filename: str, gcode_content: str) -> bool:
        """Start a print, using firmware execution when available."""
        execute_with_firmware = self._print_command_executor is not None
        started = self.print_job.start_print(
            filename,
            gcode_content,
            auto_progress=not execute_with_firmware,
        )
        if not started:
            return False

        job = self.print_job.current_job
        # Give the first tick enough budget to execute startup commands quickly.
        self._print_line_budget = min(10.0, float(job.total_lines))
        if execute_with_firmware and job.total_lines > 0:
            self._print_lines_per_second = (
                job.total_lines / max(job.estimated_total_time, 1.0)
            )
        else:
            self._print_lines_per_second = 0.0
        self._emit("print_started", {
            "filename": filename,
            "total_lines": job.total_lines,
            "estimated_seconds": job.estimated_total_time,
        })
        return True

    def pause_print(self) -> bool:
        paused = self.print_job.pause()
        if paused:
            self._emit("print_paused", self.print_job.get_status())
        return paused

    def resume_print(self) -> bool:
        resumed = self.print_job.resume()
        if resumed:
            self._emit("print_resumed", self.print_job.get_status())
        return resumed

    def cancel_print(self) -> bool:
        status = self.print_job.get_status()
        cancelled = self.print_job.cancel()
        if cancelled:
            self._emit("print_cancelled", status)
        return cancelled

    async def _execute_print_commands(self, dt: float) -> None:
        if (
            self._print_command_executor is None
            or self.print_job.current_job.state.value != "printing"
        ):
            return

        self._print_line_budget += self._print_lines_per_second * dt
        lines_this_tick = min(int(self._print_line_budget), 50)
        if lines_this_tick <= 0:
            return
        self._print_line_budget -= lines_this_tick

        for _ in range(lines_this_tick):
            line = self.print_job.get_next_gcode_line()
            if line is None:
                self._emit("print_completed", self.print_job.get_status())
                break
            response = await self._print_command_executor(line)
            self._emit("print_line", {
                "filename": self.print_job.current_job.filename,
                "line": self.print_job.current_job.current_line,
                "total_lines": self.print_job.current_job.total_lines,
                "command": line,
                "response": response,
                "source": "print_file",
            })
            logger.debug("Print G-code executed: %s -> %s", line, response)
            if self.print_job.current_job.state.value != "printing":
                break

    # === Interface Methods (called by firmware runtime) ===

    def set_hotend_temp(self, temp: float) -> bool:
        return self.thermal.set_target("hotend", temp)

    def set_bed_temp(self, temp: float) -> bool:
        return self.thermal.set_target("bed", temp)

    def get_hotend_temp(self) -> float:
        return self.thermal.get_temperature("hotend")

    def get_bed_temp(self) -> float:
        return self.thermal.get_temperature("bed")

    def get_hotend_target(self) -> float:
        return self.thermal.get_target("hotend")

    def get_bed_target(self) -> float:
        return self.thermal.get_target("bed")

    def move(self, **kwargs) -> float:
        """Execute a move and handle extrusion."""
        e = kwargs.get("e")
        if e is not None:
            hotend_temp = self.thermal.state.hotend.current_temp
            self.extrusion.extrude(e, hotend_temp)
        return self.motion.move(**kwargs)

    def home(self, axes: str = "XYZ") -> float:
        return self.motion.home(axes)

    def set_fan_speed(self, speed: int) -> None:
        self.thermal.set_fan_speed(speed)

    def set_flow_rate(self, percent: float) -> None:
        self.extrusion.set_flow_rate(percent)

    def get_position(self) -> dict:
        return self.motion.get_position()

    def disable_motors(self) -> None:
        self.motors_enabled = False

    def enable_motors(self) -> None:
        self.motors_enabled = True

    # === Telemetry Interface ===

    def get_full_status(self) -> dict:
        """Get complete printer status for telemetry/API."""
        return {
            "thermal": self.thermal.get_status(),
            "motion": self.motion.get_status(),
            "extrusion": self.extrusion.get_status(),
            "power": self.power.get_status(),
            "print_job": self.print_job.get_status(),
            "motors_enabled": self.motors_enabled,
        }

    def get_temperatures(self) -> dict:
        """Quick temperature readout."""
        return self.thermal.get_status()

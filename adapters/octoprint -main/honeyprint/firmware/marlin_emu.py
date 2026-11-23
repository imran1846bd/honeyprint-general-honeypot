"""
Marlin Firmware Emulator - Emulates Marlin firmware G-code responses.

Analogous to HoneySat's Flight Software Services (SUCHAI FS). This is the
core component that processes G-code telecommands and generates realistic
Marlin firmware responses. It bridges the exposed protocols with the
Printer Simulator, providing high-interaction honeypot capabilities.

Supports the most common Marlin G-code commands that an adversary would
use to interact with a 3D printer, including:
- Movement (G0/G1), Homing (G28), Bed Leveling (G29)
- Temperature control (M104/M109/M140/M190)
- Fan control (M106/M107)
- SD card operations (M20-M26)
- Printer information (M115, M503)
- Emergency stop (M112)
"""

import time
import logging
import asyncio
from typing import Callable

from ..printer_simulator.simulator import PrinterSimulator
from .gcode_handler import parse_gcode, GCodeCommand
from .sd_card import SDCardSimulation

logger = logging.getLogger("honeyprint.firmware")


class MarlinEmulator:
    """
    Emulates Marlin 2.x firmware command processing.

    Processes G-code commands and returns Marlin-format responses.
    Connected to the PrinterSimulator for physics-backed responses.
    """

    def __init__(self, simulator: PrinterSimulator, config: dict):
        self.sim = simulator
        self.config = config
        self.sd = SDCardSimulation(
            storage_dir=config.get("ground", {}).get("storage_dir")
        )

        printer_cfg = config.get("printer", {})
        self.printer_name = printer_cfg.get("name", "Ender-3 V2")
        self.firmware_version = printer_cfg.get(
            "firmware_version", "2.1.2.1"
        )
        self.protocol_version = "1.0"
        self.machine_type = printer_cfg.get("name", "Ender-3 V2")
        self.uuid = "12345678-1234-1234-1234-123456789abc"
        self.extruder_count = printer_cfg.get("num_extruders", 1)

        # State
        self._echo_enabled = True
        self._busy = False
        self._sd_printing = False

        # Callback for sending async messages (temp reports, etc.)
        self._async_sender: Callable | None = None

        # Auto temperature reporting
        self._auto_report_temp = False
        self._auto_report_interval = 4  # seconds
        self._auto_report_task: asyncio.Task | None = None

        # Command dispatch table
        self._g_commands = {
            0: self._cmd_g0_g1,     # Rapid move
            1: self._cmd_g0_g1,     # Linear move
            4: self._cmd_g4,        # Dwell
            10: self._cmd_g10,      # Retract
            11: self._cmd_g11,      # Unretract
            28: self._cmd_g28,      # Home
            29: self._cmd_g29,      # Bed leveling
            90: self._cmd_g90,      # Absolute mode
            91: self._cmd_g91,      # Relative mode
            92: self._cmd_g92,      # Set position
        }
        self._m_commands = {
            17: self._cmd_m17,      # Enable steppers
            18: self._cmd_m18,      # Disable steppers
            20: self._cmd_m20,      # List SD card
            21: self._cmd_m21,      # Init SD card
            23: self._cmd_m23,      # Select SD file
            24: self._cmd_m24,      # Start/resume SD print
            25: self._cmd_m25,      # Pause SD print
            26: self._cmd_m26,      # Set SD position
            27: self._cmd_m27,      # Report SD print status
            28: self._cmd_m28,      # Start SD write
            29: self._cmd_m29,      # Stop SD write
            30: self._cmd_m30,      # Delete SD file
            31: self._cmd_m31,      # Print time
            82: self._cmd_m82,      # Extruder absolute
            83: self._cmd_m83,      # Extruder relative
            84: self._cmd_m18,      # Disable steppers (alias)
            104: self._cmd_m104,    # Set hotend temp
            105: self._cmd_m105,    # Report temps
            106: self._cmd_m106,    # Fan on
            107: self._cmd_m107,    # Fan off
            108: self._cmd_m108,    # Break and continue
            109: self._cmd_m109,    # Wait for hotend temp
            110: self._cmd_m110,    # Set line number
            112: self._cmd_m112,    # Emergency stop
            114: self._cmd_m114,    # Report position
            115: self._cmd_m115,    # Firmware info
            117: self._cmd_m117,    # Set LCD message
            118: self._cmd_m118,    # Serial print
            119: self._cmd_m119,    # Endstop status
            140: self._cmd_m140,    # Set bed temp
            155: self._cmd_m155,    # Auto-report temperatures
            190: self._cmd_m190,    # Wait for bed temp
            200: self._cmd_m200,    # Set filament diameter
            201: self._cmd_m201,    # Set max acceleration
            203: self._cmd_m203,    # Set max feedrate
            204: self._cmd_m204,    # Set acceleration
            205: self._cmd_m205,    # Set jerk
            220: self._cmd_m220,    # Set feedrate percentage
            221: self._cmd_m221,    # Set flow percentage
            301: self._cmd_m301,    # Set PID
            400: self._cmd_m400,    # Wait for moves
            500: self._cmd_m500,    # Save settings
            501: self._cmd_m501,    # Load settings
            502: self._cmd_m502,    # Reset settings
            503: self._cmd_m503,    # Report settings
        }

    def set_async_sender(self, sender: Callable) -> None:
        """Set callback for sending unsolicited messages."""
        self._async_sender = sender

    async def process_command(self, line: str) -> str:
        """
        Process a G-code command line and return the response.

        This is the main entry point - analogous to HoneySat's TC processing.
        """
        line = line.strip()
        if not line:
            return ""

        cmd = parse_gcode(line)
        if cmd is None:
            return "ok"

        logger.debug("Processing: %s", cmd.raw)

        try:
            if cmd.letter == "G" and cmd.code in self._g_commands:
                response = await self._g_commands[cmd.code](cmd)
            elif cmd.letter == "M" and cmd.code in self._m_commands:
                response = await self._m_commands[cmd.code](cmd)
            elif cmd.letter == "T":
                response = self._cmd_tool_change(cmd)
            else:
                response = f"echo:Unknown command: \"{cmd.raw}\"\nok"
            return response
        except Exception as e:
            logger.error("Error processing '%s': %s", line, e)
            return "ok"

    async def process_print_command(self, line: str) -> str:
        """
        Process a command from an active print without blocking the simulator.

        Temperature wait commands still set targets, but the wait itself is
        skipped so the thermal simulation loop can continue to heat/cool.
        """
        line = line.strip()
        if not line:
            return ""

        cmd = parse_gcode(line)
        if cmd is None:
            return "ok"

        if cmd.letter == "M" and cmd.code == 109:
            temp = cmd.get_float("S", cmd.get_float("R", 0))
            self.sim.set_hotend_temp(temp)
            return "ok"
        if cmd.letter == "M" and cmd.code == 190:
            temp = cmd.get_float("S", cmd.get_float("R", 0))
            self.sim.set_bed_temp(temp)
            return "ok"
        if cmd.letter == "G" and cmd.code == 4:
            return "ok"

        return await self.process_command(line)

    # === G-code Command Handlers ===

    async def _cmd_g0_g1(self, cmd: GCodeCommand) -> str:
        """G0/G1 - Linear move."""
        kwargs = {}
        if cmd.has_param("X"):
            kwargs["x"] = cmd.get_float("X")
        if cmd.has_param("Y"):
            kwargs["y"] = cmd.get_float("Y")
        if cmd.has_param("Z"):
            kwargs["z"] = cmd.get_float("Z")
        if cmd.has_param("E"):
            kwargs["e"] = cmd.get_float("E")
        if cmd.has_param("F"):
            kwargs["feedrate"] = cmd.get_float("F")
        self.sim.move(**kwargs)
        return "ok"

    async def _cmd_g4(self, cmd: GCodeCommand) -> str:
        """G4 - Dwell/pause."""
        ms = cmd.get_float("P", 0)
        sec = cmd.get_float("S", 0)
        delay = max(ms / 1000.0, sec)
        if delay > 0:
            await asyncio.sleep(min(delay, 30))  # Cap at 30s
        return "ok"

    async def _cmd_g10(self, cmd: GCodeCommand) -> str:
        """G10 - Retract."""
        self.sim.move(e=-1.0)
        return "ok"

    async def _cmd_g11(self, cmd: GCodeCommand) -> str:
        """G11 - Unretract."""
        self.sim.move(e=1.0)
        return "ok"

    async def _cmd_g28(self, cmd: GCodeCommand) -> str:
        """G28 - Home axes."""
        axes = ""
        if cmd.has_param("X"):
            axes += "X"
        if cmd.has_param("Y"):
            axes += "Y"
        if cmd.has_param("Z"):
            axes += "Z"
        if not axes:
            axes = "XYZ"
        self.sim.home(axes)
        return "ok"

    async def _cmd_g29(self, cmd: GCodeCommand) -> str:
        """G29 - Auto bed leveling."""
        await asyncio.sleep(0.5)  # Simulate probing time
        return (
            "Bilinear Leveling Grid:\n"
            "      0      1      2\n"
            " 0 +0.025 -0.012 +0.008\n"
            " 1 -0.005 +0.003 -0.018\n"
            " 2 +0.015 -0.008 +0.022\n"
            "ok"
        )

    async def _cmd_g90(self, cmd: GCodeCommand) -> str:
        self.sim.motion.set_absolute_mode()
        return "ok"

    async def _cmd_g91(self, cmd: GCodeCommand) -> str:
        self.sim.motion.set_relative_mode()
        return "ok"

    async def _cmd_g92(self, cmd: GCodeCommand) -> str:
        """G92 - Set position."""
        kwargs = {}
        for axis in "XYZE":
            if cmd.has_param(axis):
                kwargs[axis.lower()] = cmd.get_float(axis)
        self.sim.motion.set_position(**kwargs)
        return "ok"

    # === M-code Command Handlers ===

    async def _cmd_m17(self, cmd: GCodeCommand) -> str:
        self.sim.enable_motors()
        return "ok"

    async def _cmd_m18(self, cmd: GCodeCommand) -> str:
        self.sim.disable_motors()
        return "ok"

    async def _cmd_m20(self, cmd: GCodeCommand) -> str:
        """M20 - List SD card files."""
        files = self.sd.list_files()
        lines = ["Begin file list"]
        for f in files:
            lines.append(f"{f['name']} {f['size']}")
        lines.append("End file list")
        lines.append("ok")
        return "\n".join(lines)

    async def _cmd_m21(self, cmd: GCodeCommand) -> str:
        return "SD card ok\nok"

    async def _cmd_m23(self, cmd: GCodeCommand) -> str:
        """M23 - Select SD file."""
        filename = cmd.params.get("_string", "")
        if not filename:
            # Try to get filename from raw command
            parts = cmd.raw.split(None, 1)
            filename = parts[1] if len(parts) > 1 else ""
        if self.sd.select_file(filename):
            f = self.sd.get_selected_file()
            return (
                f"echo:Now doing file: {f.name}\n"
                f"File opened: {f.name} Size: {f.size}\n"
                "File selected\n"
                "ok"
            )
        return "echo:open failed, File: not found.\nok"

    async def _cmd_m24(self, cmd: GCodeCommand) -> str:
        """M24 - Start/resume SD print."""
        f = self.sd.get_selected_file()
        if f:
            self.sim.start_print_file(f.name, f.content)
            self._sd_printing = True
            return "ok"
        return "echo:No file selected\nok"

    async def _cmd_m25(self, cmd: GCodeCommand) -> str:
        self.sim.print_job.pause()
        return "ok"

    async def _cmd_m26(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m27(self, cmd: GCodeCommand) -> str:
        """M27 - Report SD print status."""
        job = self.sim.print_job.current_job
        if self._sd_printing:
            return (
                f"SD printing byte "
                f"{int(job.progress * job.file_size / 100)}/"
                f"{job.file_size}\n"
                "ok"
            )
        return "Not SD printing\nok"

    async def _cmd_m28(self, cmd: GCodeCommand) -> str:
        return "Writing to file\nok"

    async def _cmd_m29(self, cmd: GCodeCommand) -> str:
        return "Done saving file.\nok"

    async def _cmd_m30(self, cmd: GCodeCommand) -> str:
        """M30 - Delete SD file."""
        parts = cmd.raw.split(None, 1)
        filename = parts[1] if len(parts) > 1 else ""
        if self.sd.delete_file(filename):
            return f"File deleted: {filename}\nok"
        return "Deletion failed.\nok"

    async def _cmd_m31(self, cmd: GCodeCommand) -> str:
        """M31 - Print time."""
        elapsed = self.sim.print_job.current_job.elapsed_time
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        return f"echo:Print time: {h}h {m}m {s}s\nok"

    async def _cmd_m82(self, cmd: GCodeCommand) -> str:
        self.sim.motion.set_extruder_absolute()
        return "ok"

    async def _cmd_m83(self, cmd: GCodeCommand) -> str:
        self.sim.motion.set_extruder_relative()
        return "ok"

    async def _cmd_m104(self, cmd: GCodeCommand) -> str:
        """M104 - Set hotend temperature."""
        temp = cmd.get_float("S", 0)
        self.sim.set_hotend_temp(temp)
        return "ok"

    async def _cmd_m105(self, cmd: GCodeCommand) -> str:
        """M105 - Report temperatures (key telemetry)."""
        return self._format_temp_report() + "\nok"

    async def _cmd_m106(self, cmd: GCodeCommand) -> str:
        """M106 - Fan on."""
        speed = cmd.get_int("S", 255)
        self.sim.set_fan_speed(speed)
        return "ok"

    async def _cmd_m107(self, cmd: GCodeCommand) -> str:
        self.sim.set_fan_speed(0)
        return "ok"

    async def _cmd_m108(self, cmd: GCodeCommand) -> str:
        self._busy = False
        return "ok"

    async def _cmd_m109(self, cmd: GCodeCommand) -> str:
        """M109 - Wait for hotend temperature."""
        temp = cmd.get_float("S", cmd.get_float("R", 0))
        self.sim.set_hotend_temp(temp)
        # Simulate waiting (send periodic temp reports)
        for _ in range(30):
            current = self.sim.get_hotend_temp()
            if abs(current - temp) < 2.0:
                break
            if self._async_sender:
                self._async_sender(self._format_temp_report())
            await asyncio.sleep(1)
        return self._format_temp_report() + "\nok"

    async def _cmd_m110(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m112(self, cmd: GCodeCommand) -> str:
        """M112 - Emergency stop."""
        self.sim.set_hotend_temp(0)
        self.sim.set_bed_temp(0)
        self.sim.set_fan_speed(0)
        self.sim.disable_motors()
        self.sim.cancel_print()
        return "echo:Emergency Stop!\nok"

    async def _cmd_m114(self, cmd: GCodeCommand) -> str:
        """M114 - Report position."""
        pos = self.sim.get_position()
        return (
            f"X:{pos['x']:.4f} Y:{pos['y']:.4f} "
            f"Z:{pos['z']:.4f} E:{pos['e']:.4f} "
            f"Count X:{int(pos['x']*80)} "
            f"Y:{int(pos['y']*80)} "
            f"Z:{int(pos['z']*400)}\n"
            "ok"
        )

    async def _cmd_m115(self, cmd: GCodeCommand) -> str:
        """M115 - Firmware info."""
        return (
            f"FIRMWARE_NAME:Marlin {self.firmware_version} "
            f"SOURCE_CODE_URL:https://github.com/MarlinFirmware/Marlin "
            f"PROTOCOL_VERSION:{self.protocol_version} "
            f"MACHINE_TYPE:{self.machine_type} "
            f"EXTRUDER_COUNT:{self.extruder_count} "
            f"UUID:{self.uuid}\n"
            "Cap:SERIAL_XON_XOFF:0\n"
            "Cap:BINARY_FILE_TRANSFER:0\n"
            "Cap:EEPROM:1\n"
            "Cap:VOLUMETRIC:1\n"
            "Cap:AUTOREPORT_TEMP:1\n"
            "Cap:PROGRESS:0\n"
            "Cap:PRINT_JOB:1\n"
            "Cap:AUTOLEVEL:1\n"
            "Cap:RUNOUT:0\n"
            "Cap:Z_PROBE:1\n"
            "Cap:LEVELING_DATA:1\n"
            "Cap:BUILD_PERCENT:1\n"
            "Cap:SOFTWARE_POWER:0\n"
            "Cap:TOGGLE_LIGHTS:0\n"
            "Cap:CASE_LIGHT_BRIGHTNESS:0\n"
            "Cap:EMERGENCY_PARSER:1\n"
            "Cap:HOST_ACTION_COMMANDS:1\n"
            "Cap:PROMPT_SUPPORT:0\n"
            "Cap:SDCARD:1\n"
            "Cap:AUTOREPORT_SD_STATUS:0\n"
            "Cap:LONG_FILENAME:1\n"
            "Cap:THERMAL_PROTECTION:1\n"
            "Cap:MOTION_MODES:1\n"
            "Cap:ARCS:1\n"
            "Cap:BABYSTEPPING:1\n"
            "Cap:CHAMBER_TEMPERATURE:0\n"
            "ok"
        )

    async def _cmd_m117(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m118(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m119(self, cmd: GCodeCommand) -> str:
        """M119 - Endstop status."""
        return (
            "Reporting endstop status\n"
            "x_min: open\n"
            "y_min: open\n"
            "z_min: open\n"
            "ok"
        )

    async def _cmd_m140(self, cmd: GCodeCommand) -> str:
        """M140 - Set bed temperature."""
        temp = cmd.get_float("S", 0)
        self.sim.set_bed_temp(temp)
        return "ok"

    async def _cmd_m155(self, cmd: GCodeCommand) -> str:
        """M155 - Auto-report temperatures."""
        interval = cmd.get_int("S", 0)
        if interval > 0:
            self._auto_report_temp = True
            self._auto_report_interval = interval
            if self._auto_report_task:
                self._auto_report_task.cancel()
            self._auto_report_task = asyncio.create_task(
                self._auto_temp_report_loop()
            )
        else:
            self._auto_report_temp = False
            if self._auto_report_task:
                self._auto_report_task.cancel()
                self._auto_report_task = None
        return "ok"

    async def _cmd_m190(self, cmd: GCodeCommand) -> str:
        """M190 - Wait for bed temperature."""
        temp = cmd.get_float("S", cmd.get_float("R", 0))
        self.sim.set_bed_temp(temp)
        for _ in range(60):
            current = self.sim.get_bed_temp()
            if abs(current - temp) < 2.0:
                break
            if self._async_sender:
                self._async_sender(self._format_temp_report())
            await asyncio.sleep(1)
        return self._format_temp_report() + "\nok"

    async def _cmd_m200(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m201(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m203(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m204(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m205(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m220(self, cmd: GCodeCommand) -> str:
        """M220 - Set feedrate percentage."""
        return "ok"

    async def _cmd_m221(self, cmd: GCodeCommand) -> str:
        """M221 - Set flow percentage."""
        percent = cmd.get_float("S", 100)
        self.sim.set_flow_rate(percent)
        return "ok"

    async def _cmd_m301(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m400(self, cmd: GCodeCommand) -> str:
        return "ok"

    async def _cmd_m500(self, cmd: GCodeCommand) -> str:
        return "echo:Settings Saved (0 bytes; crc -1)\nok"

    async def _cmd_m501(self, cmd: GCodeCommand) -> str:
        return "echo:Settings Loaded (0 bytes; crc -1)\nok"

    async def _cmd_m502(self, cmd: GCodeCommand) -> str:
        return "echo:Hardcoded Default Settings Loaded\nok"

    async def _cmd_m503(self, cmd: GCodeCommand) -> str:
        """M503 - Report all settings."""
        pos = self.sim.get_position()
        return (
            "echo:; Linear Units:\n"
            "echo:  G21 ; (mm)\n"
            "echo:\n"
            "echo:; Temperature Units:\n"
            "echo:  M149 C ; Units in Celsius\n"
            "echo:\n"
            "echo:; Steps per unit:\n"
            "echo:  M92 X80.00 Y80.00 Z400.00 E93.00\n"
            "echo:\n"
            "echo:; Max feedrates (units/s):\n"
            "echo:  M203 X200.00 Y200.00 Z5.00 E25.00\n"
            "echo:\n"
            "echo:; Max Acceleration (units/s2):\n"
            "echo:  M201 X500.00 Y500.00 Z100.00 E5000.00\n"
            "echo:\n"
            "echo:; Acceleration (printing, retract, travel):\n"
            "echo:  M204 P500.00 R1000.00 T500.00\n"
            "echo:\n"
            "echo:; Advanced:\n"
            "echo:  M205 B20000.00 S0.00 T0.00 "
            "X8.00 Y8.00 Z0.40 E5.00\n"
            "echo:\n"
            "echo:; Home offset:\n"
            "echo:  M206 X0.00 Y0.00 Z0.00\n"
            "echo:\n"
            "echo:; PID Settings:\n"
            "echo:  M301 P22.20 I1.08 D114.00\n"
            "echo:\n"
            "echo:; Bed PID Settings:\n"
            "echo:  M304 P97.10 I1.41 D1675.16\n"
            "ok"
        )

    def _cmd_tool_change(self, cmd: GCodeCommand) -> str:
        """T0/T1 - Tool change."""
        return "ok"

    # === Helper Methods ===

    def _format_temp_report(self) -> str:
        """Format temperature report in Marlin style."""
        hotend_temp = self.sim.get_hotend_temp()
        hotend_target = self.sim.get_hotend_target()
        bed_temp = self.sim.get_bed_temp()
        bed_target = self.sim.get_bed_target()
        hotend_power = self.sim.thermal.get_power("hotend")
        bed_power = self.sim.thermal.get_power("bed")

        return (
            f" T:{hotend_temp:.2f} /{hotend_target:.2f} "
            f"B:{bed_temp:.2f} /{bed_target:.2f} "
            f"@:{hotend_power} "
            f"B@:{bed_power}"
        )

    async def _auto_temp_report_loop(self) -> None:
        """Send periodic temperature reports."""
        while self._auto_report_temp:
            if self._async_sender:
                self._async_sender(self._format_temp_report())
            await asyncio.sleep(self._auto_report_interval)

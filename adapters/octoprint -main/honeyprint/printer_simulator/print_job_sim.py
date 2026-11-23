"""
Print Job Simulation - Simulates an active print job's progression.

Analogous to HoneySat's Payload Simulation. Manages G-code file playback,
print progress tracking, estimated time remaining, and layer progression.
Provides the appearance of an active or completed print to adversaries.
"""

import time
import os
from dataclasses import dataclass, field
from enum import Enum


class PrintJobState(Enum):
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class PrintJob:
    """Represents an active or recent print job."""
    filename: str = ""
    filepath: str = ""
    file_size: int = 0
    state: PrintJobState = PrintJobState.IDLE
    progress: float = 0.0              # 0.0 - 100.0
    current_line: int = 0
    total_lines: int = 0
    current_layer: int = 0
    total_layers: int = 0
    start_time: float = 0.0
    elapsed_time: float = 0.0
    estimated_total_time: float = 0.0
    estimated_remaining: float = 0.0
    filament_used_mm: float = 0.0
    estimated_filament_mm: float = 0.0


class PrintJobSimulation:
    """
    Simulates print job execution.

    Can either:
    1. Simulate a fake ongoing print (for deception)
    2. Process uploaded G-code files line by line
    3. Track print progress from firmware G-code execution
    """

    def __init__(self):
        self.current_job = PrintJob()
        self.job_history: list[dict] = []
        self._gcode_lines: list[str] = []
        self._auto_progress = False
        self._progress_rate = 0.0  # percent per second

    def start_fake_print(self, filename: str = "benchy.gcode",
                         total_time_minutes: float = 45.0,
                         total_layers: int = 150,
                         filament_mm: float = 5000.0) -> None:
        """Start a simulated print job for deception purposes."""
        self.current_job = PrintJob(
            filename=filename,
            file_size=int(filament_mm * 12),  # Rough estimate
            state=PrintJobState.PRINTING,
            total_layers=total_layers,
            start_time=time.time(),
            estimated_total_time=total_time_minutes * 60,
            estimated_filament_mm=filament_mm,
        )
        self._auto_progress = True
        self._progress_rate = 100.0 / (total_time_minutes * 60)

    def start_print(
        self,
        filename: str,
        gcode_content: str,
        auto_progress: bool = True,
    ) -> bool:
        """Start printing from G-code (uploaded or selected from SD)."""
        lines = gcode_content.strip().split("\n")
        gcode_lines = [
            l.strip() for l in lines
            if l.strip() and not l.strip().startswith(";")
        ]
        if not gcode_lines:
            return False

        # Estimate layers from Z moves and ;LAYER markers
        total_layers = 0
        for line in lines:
            stripped = line.strip()
            if (";LAYER:" in stripped.upper() or
                    ";LAYER_CHANGE" in stripped.upper()):
                total_layers += 1
            elif stripped.startswith("G1") and " Z" in stripped:
                total_layers += 1

        # Estimate filament from E values
        total_filament = 0.0
        for line in gcode_lines:
            if "E" in line:
                try:
                    e_part = [p for p in line.split() if p.startswith("E")]
                    if e_part:
                        total_filament = max(
                            total_filament, float(e_part[0][1:])
                        )
                except (ValueError, IndexError):
                    pass

        # Estimate print time: parse slicer header, otherwise default by lines
        estimated_seconds = self._estimate_print_time(gcode_content, gcode_lines)

        self._gcode_lines = gcode_lines
        self.current_job = PrintJob(
            filename=filename,
            file_size=len(gcode_content),
            state=PrintJobState.PRINTING,
            total_lines=len(gcode_lines),
            total_layers=max(total_layers, 1),
            start_time=time.time(),
            elapsed_time=0.0,
            estimated_total_time=estimated_seconds,
            estimated_remaining=estimated_seconds,
            estimated_filament_mm=total_filament,
        )
        # Auto-progress is used when no firmware executor is driving the file.
        self._auto_progress = auto_progress
        self._progress_rate = 100.0 / max(estimated_seconds, 1.0)
        return True

    @staticmethod
    def _estimate_print_time(content: str, gcode_lines: list[str]) -> float:
        """Best-effort print-time estimate in seconds."""
        # Common slicer comment formats
        import re
        patterns = [
            r";\s*estimated printing time.*?=\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
            r";\s*TIME:\s*(\d+)",  # Cura: total seconds
            r";\s*total estimated time:\s*(\d+):(\d+):(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, content, re.IGNORECASE)
            if not m:
                continue
            groups = [g for g in m.groups() if g is not None]
            if len(groups) == 1:
                return float(groups[0])
            if len(groups) == 3:
                h, mn, s = (int(g or 0) for g in m.groups())
                return float(h * 3600 + mn * 60 + s)
            if len(groups) == 2:
                h, mn = (int(g or 0) for g in groups)
                return float(h * 3600 + mn * 60)
        # Fallback: ~10 lines/sec for a believable pace
        return max(60.0, len(gcode_lines) / 10.0)

    def get_next_gcode_line(self) -> str | None:
        """Get the next G-code line to execute."""
        if (self.current_job.state != PrintJobState.PRINTING or
                not self._gcode_lines):
            return None
        if self.current_job.current_line >= len(self._gcode_lines):
            self._complete_print()
            return None
        line = self._gcode_lines[self.current_job.current_line]
        self.current_job.current_line += 1
        self._update_progress()
        return line

    def pause(self) -> bool:
        if self.current_job.state == PrintJobState.PRINTING:
            self.current_job.state = PrintJobState.PAUSED
            return True
        return False

    def resume(self) -> bool:
        if self.current_job.state == PrintJobState.PAUSED:
            self.current_job.state = PrintJobState.PRINTING
            return True
        return False

    def cancel(self) -> bool:
        if self.current_job.state in (
            PrintJobState.PRINTING, PrintJobState.PAUSED
        ):
            self.current_job.state = PrintJobState.CANCELLING
            self._archive_job()
            self.current_job = PrintJob()
            return True
        return False

    def clear(self) -> None:
        self.current_job = PrintJob()
        self._gcode_lines = []
        self._auto_progress = False
        self._progress_rate = 0.0

    def update(self, dt: float) -> None:
        """Update print job simulation."""
        if self.current_job.state == PrintJobState.PRINTING:
            # Accumulate active print time only — pauses don't tick.
            self.current_job.elapsed_time += dt
            if self._auto_progress:
                self.current_job.progress = min(
                    99.9,
                    self.current_job.progress + self._progress_rate * dt
                )
                self.current_job.current_layer = int(
                    self.current_job.total_layers
                    * self.current_job.progress / 100.0
                )
                if self.current_job.total_lines > 0:
                    if self.current_job.progress <= 0:
                        self.current_job.current_line = 0
                    else:
                        self.current_job.current_line = min(
                            self.current_job.total_lines,
                            max(
                                1,
                                int(
                                    self.current_job.total_lines
                                    * self.current_job.progress / 100.0
                                ),
                            ),
                        )
                if self.current_job.estimated_total_time > 0:
                    if self._auto_progress:
                        self.current_job.estimated_remaining = max(
                            0,
                            self.current_job.estimated_total_time
                            - self.current_job.elapsed_time
                        )
                    else:
                        self.current_job.estimated_remaining = max(
                            0,
                            self.current_job.estimated_total_time
                            * (100.0 - self.current_job.progress) / 100.0
                        )
                # Auto-complete
                if self.current_job.progress >= 99.9:
                    self._complete_print()

    def _update_progress(self) -> None:
        if self.current_job.total_lines > 0:
            self.current_job.progress = (
                self.current_job.current_line
                / self.current_job.total_lines * 100.0
            )

    def _complete_print(self) -> None:
        self.current_job.state = PrintJobState.COMPLETED
        self.current_job.progress = 100.0
        self._auto_progress = False
        self._archive_job()

    def _archive_job(self) -> None:
        self.job_history.append({
            "filename": self.current_job.filename,
            "state": self.current_job.state.value,
            "elapsed_time": self.current_job.elapsed_time,
            "progress": self.current_job.progress,
            "timestamp": time.time(),
        })

    def get_status(self) -> dict:
        job = self.current_job
        current_command = ""
        if 0 < job.current_line <= len(self._gcode_lines):
            current_command = self._gcode_lines[job.current_line - 1]
        return {
            "state": job.state.value,
            "filename": job.filename,
            "progress": round(job.progress, 1),
            "current_line": job.current_line,
            "total_lines": job.total_lines,
            "current_command": current_command,
            "current_layer": job.current_layer,
            "total_layers": job.total_layers,
            "elapsed_time": round(job.elapsed_time, 0),
            "estimated_remaining": round(job.estimated_remaining, 0),
            "filament_used_mm": round(job.filament_used_mm, 1),
        }

    @property
    def is_printing(self) -> bool:
        return self.current_job.state in (
            PrintJobState.PRINTING, PrintJobState.PAUSED
        )

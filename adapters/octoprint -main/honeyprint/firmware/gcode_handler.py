"""
G-code Handler - Parses and routes G-code commands.

Analogous to HoneySat's TC (Telecommand) processing. Parses G-code strings
into structured commands and routes them to the appropriate firmware service.
"""

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class GCodeCommand:
    """Parsed G-code command."""
    raw: str
    letter: str = ""        # G, M, T, etc.
    code: int = -1          # Numeric code
    params: dict = None     # Parameter dict (X, Y, Z, E, F, S, P, etc.)
    comment: str = ""
    line_number: int = -1
    checksum: int = -1

    def __post_init__(self):
        if self.params is None:
            self.params = {}

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self.params.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        return int(float(self.params.get(key, default)))

    def has_param(self, key: str) -> bool:
        return key in self.params

    @property
    def full_code(self) -> str:
        return f"{self.letter}{self.code}"


def parse_gcode(line: str) -> GCodeCommand | None:
    """
    Parse a G-code line into a GCodeCommand.

    Handles:
    - Standard G/M codes: G0, G1, M104, etc.
    - Parameters: X10.5 Y20.3 Z0.2 E1.5 F3000
    - Comments: ; this is a comment
    - Line numbers: N123 G1 X10 *45
    - Checksums
    """
    raw = line.strip()
    if not raw or raw.startswith(";"):
        return None

    cmd = GCodeCommand(raw=raw)

    # Strip inline comment
    if ";" in raw:
        parts = raw.split(";", 1)
        raw = parts[0].strip()
        cmd.comment = parts[1].strip()

    # Handle line number
    if raw.upper().startswith("N"):
        match = re.match(r"N(\d+)\s+", raw, re.IGNORECASE)
        if match:
            cmd.line_number = int(match.group(1))
            raw = raw[match.end():]

    # Handle checksum
    if "*" in raw:
        parts = raw.rsplit("*", 1)
        raw = parts[0].strip()
        try:
            cmd.checksum = int(parts[1])
        except ValueError:
            pass

    # Parse the command letter and code
    tokens = raw.split()
    if not tokens:
        return None

    main_token = tokens[0].upper()
    match = re.match(r"([GMTD])(\d+\.?\d*)", main_token)
    if match:
        cmd.letter = match.group(1)
        cmd.code = int(float(match.group(2)))
    else:
        # Could be a special command or text
        cmd.letter = ""
        cmd.code = -1
        cmd.params["_text"] = raw
        return cmd

    # Parse parameters
    for token in tokens[1:]:
        token = token.strip()
        if not token:
            continue
        if token[0].isalpha():
            key = token[0].upper()
            try:
                value = float(token[1:]) if len(token) > 1 else 0.0
                cmd.params[key] = value
            except ValueError:
                cmd.params[key] = token[1:]
        elif token.startswith('"') or token.startswith("'"):
            # String parameter
            cmd.params["_string"] = token.strip("\"'")

    return cmd

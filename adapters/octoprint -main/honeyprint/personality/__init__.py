"""Printer personality system for Honeyprint.

Manages printer personality profiles that define the simulated printer's
identity, capabilities, and behavioral characteristics. Each profile
corresponds to a real-world 3D printer model with realistic specifications.
"""

from honeyprint.personality.manager import PersonalityManager

__all__ = ["PersonalityManager"]

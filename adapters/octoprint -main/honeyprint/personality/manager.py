"""Personality manager for loading and managing printer profiles."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent / "profiles"


class PersonalityManager:
    """Loads and manages 3D printer personality profiles from YAML files.

    Profiles define the simulated printer's identity, hardware capabilities,
    thermal characteristics, and firmware behavior. This is analogous to
    HoneySat's Satellite Personality system.
    """

    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        self._profiles_dir = Path(profiles_dir) if profiles_dir else PROFILES_DIR
        self._cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[str]:
        """Return the names of all available printer profiles.

        Profile names are derived from the YAML filename (without extension).
        """
        if not self._profiles_dir.is_dir():
            logger.warning("Profiles directory does not exist: %s", self._profiles_dir)
            return []

        return sorted(
            p.stem for p in self._profiles_dir.glob("*.yaml")
        )

    def load_profile(self, name: str) -> dict[str, Any]:
        """Load a printer personality profile by name.

        Parameters
        ----------
        name:
            Profile name (e.g. ``"ender3_v2"``).  Must match a YAML file
            inside the profiles directory.

        Returns
        -------
        dict:
            The full, merged configuration dictionary for the profile.

        Raises
        ------
        FileNotFoundError:
            If no profile with the given name exists.
        ValueError:
            If the profile YAML is malformed or empty.
        """
        if name in self._cache:
            return self._cache[name]

        profile_path = self._profiles_dir / f"{name}.yaml"

        if not profile_path.is_file():
            available = self.list_profiles()
            raise FileNotFoundError(
                f"Profile '{name}' not found. Available profiles: {available}"
            )

        logger.info("Loading printer profile: %s", name)

        with open(profile_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict):
            raise ValueError(
                f"Profile '{name}' is malformed: expected a YAML mapping, "
                f"got {type(data).__name__}"
            )

        # Inject metadata so consumers always know which profile is active.
        data.setdefault("_meta", {})
        data["_meta"]["profile_name"] = name
        data["_meta"]["profile_path"] = str(profile_path)

        self._cache[name] = data
        return data

    def reload_profile(self, name: str) -> dict[str, Any]:
        """Force-reload a profile, bypassing the cache."""
        self._cache.pop(name, None)
        return self.load_profile(name)

    def clear_cache(self) -> None:
        """Clear all cached profiles."""
        self._cache.clear()

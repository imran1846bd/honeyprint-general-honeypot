"""Core network-condition simulator for HoneyPrint."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, time as dt_time
from typing import Any

logger = logging.getLogger("honeyprint.network")


def _parse_time(value: str) -> dt_time:
    """Parse an ``"HH:MM"`` string into a :class:`datetime.time`."""
    parts = value.strip().split(":")
    return dt_time(int(parts[0]), int(parts[1]))


class NetworkSimulator:
    """Simulate network conditions between the management network and the printer.

    This component is the HoneyPrint analogue of HoneySat's Radio Simulator.
    Instead of modelling RF-link dynamics (orbital passes, signal fade, Doppler
    shift), it models the TCP/IP-level realities of networked 3D printers:

    * **Latency & jitter** -- cloud-connected or VPN-tunnelled printers often
      exhibit higher and more variable round-trip times than LAN-attached ones.
    * **Packet loss** -- lossy Wi-Fi links or congested subnets can drop
      packets, causing retransmissions visible to an attacker.
    * **Scheduled availability** -- real printers are powered off at night,
      enter error/cooldown states, or are physically disconnected.  A
      configurable schedule makes the honeypot go offline during defined
      windows, just as HoneySat simulates orbital pass windows.

    Parameters
    ----------
    config:
        Dictionary with the following optional keys:

        * ``enabled`` (bool) -- master switch; when *False* the simulator is
          transparent (no latency, no loss, always online).  Default ``True``.
        * ``latency_ms`` (float) -- base one-way latency in milliseconds.
          Default ``0``.
        * ``jitter_ms`` (float) -- maximum random jitter added to the base
          latency (uniform distribution).  Default ``0``.
        * ``packet_loss_percent`` (float) -- probability (0--100) that any
          given packet is dropped.  Default ``0``.
        * ``schedule`` (list[dict]) -- list of *online* windows.  Each entry
          is a dict with ``start`` and ``end`` keys in ``"HH:MM"`` format
          (24-hour, local time).  When the list is non-empty the printer is
          considered **offline** outside every listed window.  An empty list
          (the default) means "always online".
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}

        self._enabled: bool = cfg.get("enabled", True)
        self._latency_ms: float = float(cfg.get("latency_ms", 0))
        self._jitter_ms: float = float(cfg.get("jitter_ms", 0))
        self._packet_loss_pct: float = float(cfg.get("packet_loss_percent", 0))

        # Parse schedule entries into (start, end) tuples of datetime.time.
        raw_schedule: list[dict[str, str]] = cfg.get("schedule", [])
        self._schedule: list[tuple[dt_time, dt_time]] = [
            (_parse_time(entry["start"]), _parse_time(entry["end"]))
            for entry in raw_schedule
        ]

        # Manual override: force the printer offline/online regardless of
        # schedule.  ``None`` means "follow the schedule".
        self._force_online: bool | None = None

        # Background task handle for periodic schedule evaluation.
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

        # Cached online state (updated by the background task).
        self._online: bool = True

        logger.info(
            "NetworkSimulator initialised  enabled=%s  latency=%g ms  "
            "jitter=%g ms  loss=%g%%  schedule_windows=%d",
            self._enabled,
            self._latency_ms,
            self._jitter_ms,
            self._packet_loss_pct,
            len(self._schedule),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background schedule-evaluation loop."""
        if self._running:
            logger.warning("NetworkSimulator.start() called while already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("NetworkSimulator started")

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("NetworkSimulator stopped")

    # ------------------------------------------------------------------
    # Public query interface
    # ------------------------------------------------------------------

    def is_printer_online(self) -> bool:
        """Return whether the printer is currently reachable.

        When the simulator is disabled the printer is always online.
        Otherwise the result is determined by the configured schedule (or
        a manual override set via :meth:`set_online`/:meth:`set_offline`).
        """
        if not self._enabled:
            return True
        if self._force_online is not None:
            return self._force_online
        return self._online

    def apply_latency(self) -> float:
        """Return an artificial delay in **seconds** to apply before responding.

        The value combines the configured base latency with uniform random
        jitter.  Returns ``0.0`` when the simulator is disabled.
        """
        if not self._enabled:
            return 0.0
        jitter = random.uniform(0, self._jitter_ms) if self._jitter_ms > 0 else 0.0
        delay_s = (self._latency_ms + jitter) / 1000.0
        return delay_s

    def should_drop_packet(self) -> bool:
        """Return *True* if the current packet should be silently dropped.

        Uses the configured ``packet_loss_percent`` value.  Always returns
        *False* when the simulator is disabled.
        """
        if not self._enabled:
            return False
        if self._packet_loss_pct <= 0:
            return False
        return random.uniform(0, 100) < self._packet_loss_pct

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of the current network-simulation state."""
        return {
            "enabled": self._enabled,
            "online": self.is_printer_online(),
            "latency_ms": self._latency_ms,
            "jitter_ms": self._jitter_ms,
            "packet_loss_percent": self._packet_loss_pct,
            "schedule_windows": len(self._schedule),
            "force_override": self._force_online,
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # Manual overrides
    # ------------------------------------------------------------------

    def set_online(self) -> None:
        """Force the printer online, overriding the schedule."""
        self._force_online = True
        logger.info("Printer forced ONLINE (manual override)")

    def set_offline(self) -> None:
        """Force the printer offline, overriding the schedule."""
        self._force_online = False
        logger.info("Printer forced OFFLINE (manual override)")

    def clear_override(self) -> None:
        """Remove any manual online/offline override; revert to schedule."""
        self._force_online = None
        logger.info("Manual override cleared; reverting to schedule")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_schedule(self) -> bool:
        """Determine whether the printer should be online right now.

        If no schedule windows are configured the printer is always online.
        Otherwise it is online only when the current local time falls inside
        at least one window.  Windows that cross midnight (e.g. 22:00--06:00)
        are handled correctly.
        """
        if not self._schedule:
            return True

        now = datetime.now().time()
        for start, end in self._schedule:
            if start <= end:
                # Normal window (e.g. 08:00 -- 18:00).
                if start <= now <= end:
                    return True
            else:
                # Overnight window (e.g. 22:00 -- 06:00).
                if now >= start or now <= end:
                    return True
        return False

    async def _schedule_loop(self) -> None:
        """Background coroutine that periodically re-evaluates the schedule."""
        logger.debug("Schedule evaluation loop started")
        try:
            while self._running:
                previous = self._online
                self._online = self._evaluate_schedule()
                if self._online != previous:
                    state = "ONLINE" if self._online else "OFFLINE"
                    logger.info("Printer transitioned to %s (schedule)", state)
                await asyncio.sleep(30)  # re-check every 30 seconds
        except asyncio.CancelledError:
            logger.debug("Schedule evaluation loop cancelled")
            raise

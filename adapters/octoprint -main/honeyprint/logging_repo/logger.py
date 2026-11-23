"""HoneyPrint interaction logger backed by MongoDB with JSON file fallback."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger("honeyprint.logging_repo")

# Canonical collection names and the fields each entry is expected to carry.
COLLECTIONS: dict[str, list[str]] = {
    "web_interactions": ["path", "method", "ip", "user_agent", "data", "timestamp"],
    "telnet_interactions": ["ip", "commands", "responses", "timestamps"],
    "login_attempts": ["username", "password", "ip", "success", "timestamp"],
    "gcode_commands": ["command", "source", "ip", "timestamp"],
    "file_uploads": ["filename", "size", "content_hash", "ip", "timestamp"],
    "print_sessions": ["event_type", "filename", "timestamp"],
    "alerts": ["severity", "reason", "command", "timestamp"],
    "system_events": ["event_type", "data", "timestamp"],
}

_CATEGORY_TO_COLLECTION: dict[str, str] = {
    "web": "web_interactions",
    "telnet": "telnet_interactions",
    "login": "login_attempts",
    "gcode": "gcode_commands",
    "file_upload": "file_uploads",
    "event": "system_events",
    "moonraker_interaction": "web_interactions",
    "print_session": "print_sessions",
    "alert": "alerts",
    "api_interaction": "web_interactions",
    "web_request": "web_interactions",
    "websocket_connect": "web_interactions",
    "websocket_interaction": "web_interactions",
    "terminal_command": "gcode_commands",
}

_FALLBACK_LOG_DIR = Path("logs")


class HoneyPrintLogger:
    """Async MongoDB logger for all honeypot interactions.

    Falls back to appending JSON lines to a local file when MongoDB is
    unavailable.
    """

    def __init__(
        self,
        mongodb_uri: str,
        db_name: str = "honeyprint_logs",
        fallback_dir: str | Path = _FALLBACK_LOG_DIR,
    ) -> None:
        self._uri = mongodb_uri
        self._db_name = db_name
        self._client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]
        self._db: AsyncIOMotorDatabase | None = None  # type: ignore[type-arg]
        self._connected = False
        self._fallback_dir = Path(fallback_dir)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish the MongoDB connection and verify it with a ping."""
        try:
            self._client = AsyncIOMotorClient(
                self._uri, serverSelectionTimeoutMS=2000
            )
            # Force a round-trip to verify the server is reachable.
            await self._client.admin.command("ping")
            self._db = self._client[self._db_name]
            self._connected = True
            logger.info("Connected to MongoDB at %s (db=%s)", self._uri, self._db_name)
        except Exception:
            self._connected = False
            logger.warning(
                "MongoDB unavailable at %s -- falling back to file-based logging",
                self._uri,
                exc_info=True,
            )

    async def close(self) -> None:
        """Close the MongoDB connection gracefully."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
            self._connected = False
            logger.info("MongoDB connection closed")

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    async def log_web(self, request_data: dict[str, Any]) -> None:
        """Log an HTTP / API interaction."""
        await self._insert("web_interactions", request_data)

    async def log_telnet(self, session_data: dict[str, Any]) -> None:
        """Log a Telnet session."""
        await self._insert("telnet_interactions", session_data)

    async def log_login(self, attempt_data: dict[str, Any]) -> None:
        """Log a login attempt."""
        await self._insert("login_attempts", attempt_data)

    async def log_gcode(self, command_data: dict[str, Any]) -> None:
        """Log a G-code command."""
        await self._insert("gcode_commands", command_data)

    async def log_file_upload(self, upload_data: dict[str, Any]) -> None:
        """Log an uploaded file."""
        await self._insert("file_uploads", upload_data)

    async def log_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Log a system-level event (start, stop, error, etc.)."""
        record = {**data, "event_type": event_type}
        await self._insert("system_events", record)

    async def log(self, category: str, data: dict[str, Any]) -> None:
        """Generic logging entry point -- routes to the appropriate collection.

        *category* is one of: ``web``, ``telnet``, ``login``, ``gcode``,
        ``file_upload``, ``event``.  Unknown categories are stored in
        ``system_events`` with the category attached.
        """
        collection = _CATEGORY_TO_COLLECTION.get(category)
        if collection is None:
            logger.warning("Unknown log category %r -- storing in system_events", category)
            data = {**data, "category": category}
            collection = "system_events"
        await self._insert(collection, data)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, int]:
        """Return document counts for every tracked collection."""
        stats: dict[str, int] = {}
        if self._connected and self._db is not None:
            for name in COLLECTIONS:
                stats[name] = await self._db[name].count_documents({})
        else:
            # Best-effort from the fallback files.
            for name in COLLECTIONS:
                path = self._fallback_dir / f"{name}.jsonl"
                if path.exists():
                    stats[name] = sum(1 for _ in path.open())
                else:
                    stats[name] = 0
        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enrich(self, data: dict[str, Any]) -> dict[str, Any]:
        """Add common metadata fields to a log entry."""
        enriched = dict(data)
        enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        enriched.setdefault("source_ip", enriched.get("ip"))
        return enriched

    async def _insert(self, collection: str, data: dict[str, Any]) -> None:
        """Insert a document into *collection*, falling back to file."""
        record = self._enrich(data)
        record["category"] = collection

        if self._connected and self._db is not None:
            try:
                await self._db[collection].insert_one(record)
                logger.debug("Logged to MongoDB %s: %s", collection, record)
                return
            except Exception:
                logger.warning(
                    "MongoDB insert failed for %s -- falling back to file",
                    collection,
                    exc_info=True,
                )

        # Fallback: append to a JSONL file.
        self._write_fallback(collection, record)

    def _write_fallback(self, collection: str, record: dict[str, Any]) -> None:
        """Append *record* as a JSON line to ``<fallback_dir>/<collection>.jsonl``."""
        self._fallback_dir.mkdir(parents=True, exist_ok=True)
        path = self._fallback_dir / f"{collection}.jsonl"
        # Remove non-serialisable MongoDB ObjectId if present.
        record.pop("_id", None)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        logger.debug("Logged to file %s: %s", path, record)


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def create_logger(config: dict[str, Any]) -> HoneyPrintLogger:
    """Instantiate a :class:`HoneyPrintLogger` from a configuration dict.

    Expected keys:
        ``mongodb_uri``  -- MongoDB connection string (required).
        ``db_name``      -- Database name (default ``"honeyprint_logs"``).
        ``fallback_dir`` -- Directory for JSON-line fallback logs (default ``"logs"``).
    """
    return HoneyPrintLogger(
        mongodb_uri=config["mongodb_uri"],
        db_name=config.get("db_name", "honeyprint_logs"),
        fallback_dir=config.get("fallback_dir", _FALLBACK_LOG_DIR),
    )

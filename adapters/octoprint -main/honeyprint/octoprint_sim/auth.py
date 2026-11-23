"""
Authentication module for OctoPrint simulation.

Provides basic auth and API key authentication matching OctoPrint's
security model. Deliberately uses weak defaults for honeypot purposes.
"""

import hashlib
import secrets
import time
import logging
from aiohttp import web

logger = logging.getLogger("honeyprint.auth")


class OctoPrintAuth:
    """Authentication manager for OctoPrint simulation."""

    def __init__(self, config: dict):
        ground_cfg = config.get("ground", {})
        self.username = ground_cfg.get("web_username", "admin")
        self.password = ground_cfg.get("web_password", "admin")
        self.api_key = ground_cfg.get(
            "api_key", "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
        )
        self.auth_enabled = ground_cfg.get("web_auth_enabled", True)
        self._sessions: dict[str, dict] = {}
        self._log_callback = None

    def set_log_callback(self, callback):
        self._log_callback = callback

    def validate_api_key(self, key: str) -> bool:
        """Validate an API key."""
        return key == self.api_key

    def validate_credentials(self, username: str, password: str) -> bool:
        """Validate username/password."""
        return username == self.username and password == self.password

    def create_session(self, username: str) -> str:
        """Create a new session and return session token."""
        token = secrets.token_hex(16)
        self._sessions[token] = {
            "username": username,
            "created": time.time(),
            "last_access": time.time(),
        }
        return token

    def validate_session(self, token: str) -> bool:
        """Validate a session token."""
        if token in self._sessions:
            self._sessions[token]["last_access"] = time.time()
            return True
        return False

    def check_request(self, request: web.Request) -> bool:
        """
        Check if a request is authenticated.

        OctoPrint accepts:
        1. X-Api-Key header
        2. Bearer token in Authorization header
        3. Session cookie
        4. apikey query parameter
        """
        if not self.auth_enabled:
            return True

        # Check API key header
        api_key = request.headers.get("X-Api-Key", "")
        if api_key and self.validate_api_key(api_key):
            return True

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if self.validate_api_key(token):
                return True
            if self.validate_session(token):
                return True

        # Check query parameter
        api_key = request.query.get("apikey", "")
        if api_key and self.validate_api_key(api_key):
            return True

        # Check session cookie
        session_cookie = request.cookies.get("octoprint_session", "")
        if session_cookie and self.validate_session(session_cookie):
            return True

        return False

    async def login_handler(self, request: web.Request) -> web.Response:
        """Handle login requests."""
        try:
            data = await request.json()
        except Exception:
            data = {}

        username = data.get("user", "")
        password = data.get("pass", "")
        client_ip = request.remote or "unknown"

        valid = self.validate_credentials(username, password)
        if self._log_callback:
            self._log_callback("login_success" if valid else "login_attempt", {
                "username": username,
                "ip": client_ip,
                "success": valid,
            })

        if valid:
            token = self.create_session(username)
            response = web.json_response({
                "name": username,
                "active": True,
                "user": True,
                "admin": True,
                "apikey": self.api_key,
                "settings": {},
                "groups": ["admins", "users"],
                "needs": {"group": ["admins"]},
                "session": token,
                "_is_external_client": False,
            })
            response.set_cookie(
                "octoprint_session", token,
                max_age=86400, httponly=True
            )
            return response

        logger.warning("Failed login from %s: user=%s", client_ip, username)
        return web.json_response(
            {"error": "Invalid credentials"}, status=403
        )

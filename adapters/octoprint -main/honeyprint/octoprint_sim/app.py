"""
OctoPrint Web Application - Main aiohttp application.

Serves the OctoPrint-like web interface and REST API.
This is the primary exposed attack surface of HoneyPrint.
"""

import os
import logging
import asyncio
import json
import time
from pathlib import Path
from aiohttp import web

from .api_routes import setup_api_routes
from .auth import OctoPrintAuth
from ..analyst.dashboard import setup_analyst_routes

logger = logging.getLogger("honeyprint.web")

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


async def index_handler(request: web.Request) -> web.Response:
    """Serve the main OctoPrint page."""
    auth = request.app.get("auth")
    if auth and auth.auth_enabled and not auth.check_request(request):
        # Serve login page
        login_path = TEMPLATE_DIR / "login.html"
        return web.FileResponse(login_path)

    index_path = TEMPLATE_DIR / "index.html"
    return web.FileResponse(index_path)


async def terminal_handler(request: web.Request) -> web.Response:
    """Direct terminal G-code endpoint (non-standard, for web UI)."""
    auth = request.app.get("auth")
    if auth and not auth.check_request(request):
        return web.json_response({"error": "Unauthorized"}, status=403)

    data = await request.json()
    command = data.get("command", "")

    # Log the interaction
    log_cb = request.app.get("log_callback")
    if log_cb:
        log_cb("terminal_command", {
            "command": command,
            "ip": request.remote or "unknown",
            "user_agent": request.headers.get("User-Agent", ""),
            "timestamp": time.time(),
        })

    firmware = request.app.get("firmware")
    if firmware:
        telemetry = request.app.get("telemetry")
        if telemetry:
            telemetry.record("terminal_command", {
                "command": command,
                "source": "octoprint_terminal",
                "ip": request.remote or "unknown",
            })
        response = await firmware.process_command(command)
        return web.json_response({"response": response})

    return web.json_response({"response": "ok"})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for real-time printer updates."""
    ws_response = web.WebSocketResponse()
    await ws_response.prepare(request)

    client_ip = request.remote or "unknown"
    logger.info("WebSocket connected from %s", client_ip)

    # Log connection
    log_cb = request.app.get("log_callback")
    if log_cb:
        log_cb("websocket_connect", {
            "ip": client_ip,
            "user_agent": request.headers.get("User-Agent", ""),
            "timestamp": time.time(),
        })

    # Send connected message
    config = request.app.get("config", {})
    api_key = config.get("ground", {}).get("api_key", "")
    await ws_response.send_json({
        "connected": {
            "version": "1.9.3",
            "display_version": "1.9.3",
            "apikey": api_key,
            "branch": "master",
            "plugin_hash": "a1b2c3d4",
        }
    })

    # Track in app
    ws_clients = request.app.setdefault("ws_clients", set())
    ws_clients.add(ws_response)

    try:
        # Start sending periodic updates
        sim = request.app.get("simulator")
        while not ws_response.closed:
            if sim:
                temps = sim.get_temperatures()
                job_status = sim.print_job.get_status()
                state_text = "Operational"
                if job_status["state"] == "printing":
                    state_text = "Printing"
                elif job_status["state"] == "paused":
                    state_text = "Paused"

                msg = {
                    "current": {
                        "state": {
                            "text": state_text,
                            "flags": {
                                "operational": True,
                                "printing": job_status["state"] == "printing",
                                "paused": job_status["state"] == "paused",
                                "error": False,
                                "ready": job_status["state"] == "idle",
                                "sdReady": True,
                            },
                        },
                        "job": {
                            "file": {"name": job_status.get("filename")},
                            "estimatedPrintTime": job_status.get(
                                "estimated_remaining"
                            ),
                        },
                        "progress": {
                            "completion": job_status.get("progress"),
                            "printTime": int(
                                job_status.get("elapsed_time", 0)
                            ),
                            "printTimeLeft": int(
                                job_status.get("estimated_remaining", 0)
                            ),
                            "currentLine": job_status.get("current_line", 0),
                            "totalLines": job_status.get("total_lines", 0),
                            "currentCommand": job_status.get("current_command", ""),
                        },
                        "temps": [{
                            "time": int(time.time()),
                            "tool0": {
                                "actual": temps["hotend"]["current"],
                                "target": temps["hotend"]["target"],
                            },
                            "bed": {
                                "actual": temps["bed"]["current"],
                                "target": temps["bed"]["target"],
                            },
                        }],
                        "logs": [],
                        "messages": [],
                    }
                }
                try:
                    await ws_response.send_json(msg)
                except Exception:
                    break

            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        ws_clients.discard(ws_response)
        logger.info("WebSocket disconnected from %s", client_ip)

    return ws_response


def create_web_app(simulator, firmware, config: dict,
                   log_callback=None, telemetry=None) -> web.Application:
    """
    Create and configure the OctoPrint web application.

    Args:
        simulator: PrinterSimulator instance
        firmware: MarlinEmulator instance
        config: HoneyPrint configuration dict
        log_callback: Function to call for logging interactions
    """
    app = web.Application()

    # Store shared objects
    app["simulator"] = simulator
    app["firmware"] = firmware
    app["config"] = config
    app["log_callback"] = log_callback
    app["telemetry"] = telemetry

    # Setup authentication
    auth = OctoPrintAuth(config)
    if log_callback:
        auth.set_log_callback(log_callback)
    app["auth"] = auth

    # Setup routes
    setup_api_routes(app)

    # Terminal endpoint (for web UI direct G-code)
    app.router.add_post("/api/terminal", terminal_handler)

    # WebSocket
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/sockjs/websocket", websocket_handler)
    setup_analyst_routes(app)

    # Static files
    app.router.add_static("/static", str(STATIC_DIR))

    # Index page (must be last - catch-all)
    app.router.add_get("/", index_handler)

    # CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Api-Key, Authorization"
        )
        # Disable caching for static UI assets so iterative dev/edits show up
        if request.path.startswith("/static/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    app.middlewares.append(cors_middleware)

    # Request logging middleware
    @web.middleware
    async def logging_middleware(request, handler):
        start = time.time()
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = error
        except Exception as error:
            logger.error("Request error: %s %s - %s",
                         request.method, request.path, error)
            response = web.json_response({"error": str(error)}, status=500)
        if log_callback and not request.path.startswith("/static/"):
            log_callback("web_request", {
                "method": request.method,
                "path": request.path,
                "status": response.status,
                "ip": request.remote or "unknown",
                "elapsed_ms": round((time.time() - start) * 1000, 2),
            })
        return response

    app.middlewares.append(logging_middleware)

    logger.info("OctoPrint web application created")
    return app

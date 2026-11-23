"""
OctoPrint REST API Simulation.

Implements the OctoPrint REST API endpoints that adversaries are most
likely to interact with. This is analogous to HoneySat's Ground Software
command interface.

Key API groups:
- /api/version: Version information
- /api/connection: Printer connection state
- /api/printer: Printer state and commands
- /api/job: Print job management
- /api/files: File upload and management
- /api/settings: Printer settings
- /api/system: System commands
- /api/login: Authentication
"""

import time
import json
import logging
from hashlib import sha256
from pathlib import Path
from aiohttp import web

logger = logging.getLogger("honeyprint.api")

ALLOWED_UPLOAD_EXTENSIONS = {".gcode", ".gco", ".g", ".bgcode"}


def _allowed_upload_extensions_text() -> str:
    return ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))


def _is_allowed_upload_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS


def setup_api_routes(app: web.Application) -> None:
    """Register all OctoPrint API routes."""
    # Public endpoints
    app.router.add_get("/api/version", api_version)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/login", login_get)

    # Authenticated endpoints
    app.router.add_get("/api/connection", api_connection)
    app.router.add_post("/api/connection", api_connection_command)
    app.router.add_get("/api/printer", api_printer_state)
    app.router.add_post("/api/printer/command", api_printer_command)
    app.router.add_get("/api/printer/tool", api_printer_tool)
    app.router.add_post("/api/printer/tool", api_printer_tool_cmd)
    app.router.add_get("/api/printer/bed", api_printer_bed)
    app.router.add_post("/api/printer/bed", api_printer_bed_cmd)
    app.router.add_get("/api/printer/sd", api_printer_sd)
    app.router.add_post("/api/printer/sd", api_printer_sd_cmd)
    app.router.add_get("/api/job", api_job)
    app.router.add_post("/api/job", api_job_command)
    app.router.add_get("/api/files", api_files_list)
    app.router.add_get("/api/files/{location}", api_files_list_location)
    app.router.add_post("/api/files/{location}", api_files_upload)
    app.router.add_get("/api/files/{location}/{filename}", api_file_info)
    app.router.add_post("/api/files/{location}/{filename}", api_file_command)
    app.router.add_delete("/api/files/{location}/{filename}", api_file_delete)
    app.router.add_get("/downloads/files/{location}/{filename}", api_file_download)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings", api_settings_save)
    app.router.add_get("/api/system/commands", api_system_commands)
    app.router.add_post("/api/system/commands/{source}/{action}",
                        api_system_command_exec)
    app.router.add_get("/api/printerprofiles", api_printer_profiles)
    app.router.add_get("/api/slicing", api_slicing)
    app.router.add_get("/plugin/tracking/track", api_tracking)


def _check_auth(request: web.Request) -> bool:
    auth = request.app.get("auth")
    if auth:
        return auth.check_request(request)
    return True


def _log_interaction(request: web.Request, action: str, data: dict = None):
    log_cb = request.app.get("log_callback")
    if log_cb:
        log_cb("api_interaction", {
            "action": action,
            "method": request.method,
            "path": str(request.path),
            "ip": request.remote or "unknown",
            "user_agent": request.headers.get("User-Agent", ""),
            "data": data,
            "timestamp": time.time(),
        })


# === Public Endpoints ===

async def api_version(request: web.Request) -> web.Response:
    _log_interaction(request, "version_check")
    return web.json_response({
        "api": "0.1",
        "server": "1.9.3",
        "text": "OctoPrint 1.9.3",
    })


async def login(request: web.Request) -> web.Response:
    auth = request.app.get("auth")
    if auth:
        return await auth.login_handler(request)
    return web.json_response({"error": "Auth not configured"}, status=500)


async def login_get(request: web.Request) -> web.Response:
    if _check_auth(request):
        return web.json_response({
            "name": "admin",
            "active": True,
            "user": True,
            "admin": True,
        })
    return web.json_response({}, status=200)


# === Connection ===

async def api_connection(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "connection_status")
    cfg = request.app.get("config", {})
    printer = cfg.get("printer", {})
    return web.json_response({
        "current": {
            "state": "Operational",
            "port": printer.get("serial_port", "/dev/ttyUSB0"),
            "baudrate": printer.get("baud_rate", 115200),
            "printerProfile": "_default",
        },
        "options": {
            "ports": [printer.get("serial_port", "/dev/ttyUSB0")],
            "baudrates": [115200, 250000],
            "printerProfiles": [
                {"id": "_default", "name": printer.get("name", "Default")}
            ],
            "portPreference": printer.get("serial_port", "/dev/ttyUSB0"),
            "baudratePreference": printer.get("baud_rate", 115200),
            "autoconnect": True,
        },
    })


async def api_connection_command(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    _log_interaction(request, "connection_command", data)
    return web.Response(status=204)


# === Printer State ===

async def api_printer_state(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "printer_state")
    sim = request.app.get("simulator")
    if not sim:
        return web.json_response({"error": "Not connected"}, status=409)

    temps = sim.get_temperatures()
    job = sim.print_job.get_status()

    state_text = "Operational"
    state_flags = {
        "operational": True,
        "paused": False,
        "printing": False,
        "pausing": False,
        "cancelling": False,
        "sdReady": True,
        "error": False,
        "ready": True,
        "closedOrError": False,
    }
    if job["state"] == "printing":
        state_text = "Printing"
        state_flags["printing"] = True
        state_flags["ready"] = False
    elif job["state"] == "paused":
        state_text = "Paused"
        state_flags["paused"] = True

    return web.json_response({
        "temperature": {
            "tool0": {
                "actual": temps["hotend"]["current"],
                "target": temps["hotend"]["target"],
                "offset": 0,
            },
            "bed": {
                "actual": temps["bed"]["current"],
                "target": temps["bed"]["target"],
                "offset": 0,
            },
        },
        "sd": {"ready": True},
        "position": sim.get_position(),
        "state": {
            "text": state_text,
            "flags": state_flags,
        },
    })


async def api_printer_command(request: web.Request) -> web.Response:
    """Send G-code commands to the printer."""
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    commands = data.get("commands", [])
    _log_interaction(request, "gcode_command", {"commands": commands})

    firmware = request.app.get("firmware")
    if firmware:
        for cmd in commands:
            telemetry = request.app.get("telemetry")
            if telemetry:
                telemetry.record("api_command", {
                    "command": cmd,
                    "source": "octoprint_api",
                    "ip": request.remote or "unknown",
                })
            await firmware.process_command(cmd)

    return web.Response(status=204)


async def api_printer_tool(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    sim = request.app.get("simulator")
    temps = sim.get_temperatures() if sim else {}
    return web.json_response({
        "tool0": {
            "actual": temps.get("hotend", {}).get("current", 22.0),
            "target": temps.get("hotend", {}).get("target", 0.0),
            "offset": 0,
        },
    })


async def api_printer_tool_cmd(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    _log_interaction(request, "tool_command", data)
    command = data.get("command", "")
    sim = request.app.get("simulator")
    if sim and command == "target":
        targets = data.get("targets", {})
        if "tool0" in targets:
            sim.set_hotend_temp(targets["tool0"])
    elif sim and command == "extrude":
        amount = data.get("amount", 0)
        sim.move(e=amount)
    return web.Response(status=204)


async def api_printer_bed(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    sim = request.app.get("simulator")
    temps = sim.get_temperatures() if sim else {}
    return web.json_response({
        "bed": {
            "actual": temps.get("bed", {}).get("current", 22.0),
            "target": temps.get("bed", {}).get("target", 0.0),
            "offset": 0,
        },
    })


async def api_printer_bed_cmd(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    _log_interaction(request, "bed_command", data)
    sim = request.app.get("simulator")
    if sim and data.get("command") == "target":
        sim.set_bed_temp(data.get("target", 0))
    return web.Response(status=204)


async def api_printer_sd(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    return web.json_response({"ready": True})


async def api_printer_sd_cmd(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    _log_interaction(request, "sd_command", data)
    return web.Response(status=204)


# === Job ===

async def api_job(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "job_status")
    sim = request.app.get("simulator")
    job = sim.print_job.get_status() if sim else {}

    firmware = request.app.get("firmware")
    selected = firmware.sd.get_selected_file() if firmware else None
    filename = job.get("filename")
    if selected and job.get("state") not in {"printing", "paused"}:
        filename = selected.name

    job_data = {
        "job": {
            "file": {
                "name": filename,
                "display": filename,
                "path": filename,
                "type": "machinecode",
                "typePath": ["machinecode", "gcode"],
                "origin": "local",
                "size": None,
                "date": None,
            },
            "estimatedPrintTime": job.get("estimated_remaining", None),
            "filament": {
                "tool0": {
                    "length": job.get("filament_used_mm", 0),
                    "volume": 0,
                }
            },
        },
        "progress": {
            "completion": (
                job.get("progress", 0)
                if job.get("state") != "idle"
                else None
            ),
            "filepos": None,
            "printTime": (
                int(job.get("elapsed_time", 0))
                if job.get("state") != "idle"
                else None
            ),
            "printTimeLeft": (
                int(job.get("estimated_remaining", 0))
                if job.get("state") != "idle"
                else None
            ),
            "printTimeLeftOrigin": "estimate",
            "currentLine": job.get("current_line", 0),
            "totalLines": job.get("total_lines", 0),
            "currentCommand": job.get("current_command", ""),
            "currentLayer": job.get("current_layer", 0),
            "totalLayers": job.get("total_layers", 0),
        },
        "state": _map_job_state(job.get("state", "idle")),
    }
    return web.json_response(job_data)


async def api_job_command(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    command = data.get("command", "")
    _log_interaction(request, "job_command", data)

    sim = request.app.get("simulator")
    if sim:
        if command == "start":
            firmware = request.app.get("firmware")
            selected = firmware.sd.get_selected_file() if firmware else None
            if selected is None or not sim.start_print_file(selected.name, selected.content):
                return web.json_response({"error": "No selected file or printer busy"}, status=409)
        elif command == "cancel":
            sim.cancel_print()
            sim.set_hotend_temp(0)
            sim.set_bed_temp(0)
            sim.set_fan_speed(0)
        elif command == "pause":
            action = data.get("action", "toggle")
            if action == "pause":
                sim.pause_print()
            elif action == "resume":
                sim.resume_print()
            else:
                if sim.print_job.current_job.state.value == "paused":
                    sim.resume_print()
                else:
                    sim.pause_print()
    return web.Response(status=204)


# === Files ===

async def api_files_list(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "files_list")
    firmware = request.app.get("firmware")
    files = firmware.sd.list_files() if firmware else []
    return web.json_response({
        "files": [_format_file_entry(f, "local") for f in files],
        "free": "4.2GB",
        "total": "7.4GB",
    })


async def api_files_list_location(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    location = request.match_info["location"]
    _log_interaction(request, "files_list_location", {"location": location})
    firmware = request.app.get("firmware")
    files = firmware.sd.list_files() if firmware else []
    return web.json_response({
        "files": [_format_file_entry(f, location) for f in files],
        "free": "4.2GB",
        "total": "7.4GB",
    })


async def api_files_upload(request: web.Request) -> web.Response:
    """Handle file upload - critical attack surface."""
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)

    location = request.match_info["location"]
    reader = await request.multipart()

    filename = ""
    content = ""
    select = False
    print_after = False

    async for part in reader:
        if part.name == "file":
            filename = part.filename or "upload.gcode"
            raw = await part.read()
            content = raw.decode("utf-8", errors="replace")
        elif part.name == "select":
            val = await part.read()
            select = val.decode().lower() == "true"
        elif part.name == "print":
            val = await part.read()
            print_after = val.decode().lower() == "true"

    if not filename:
        _log_interaction(request, "file_upload_rejected", {
            "reason": "missing_file",
            "location": location,
        })
        return web.json_response({"error": "No file was uploaded"}, status=400)

    if not _is_allowed_upload_filename(filename):
        _log_interaction(request, "file_upload_rejected", {
            "filename": filename,
            "size": len(content),
            "location": location,
            "reason": "unsupported_extension",
            "allowed": sorted(ALLOWED_UPLOAD_EXTENSIONS),
        })
        return web.json_response({
            "error": (
                "Unsupported file type. Upload a G-code or BGCODE file "
                f"({_allowed_upload_extensions_text()})."
            ),
            "allowed": sorted(ALLOWED_UPLOAD_EXTENSIONS),
        }, status=400)

    _log_interaction(request, "file_upload", {
        "filename": filename,
        "size": len(content),
        "location": location,
        "select": select,
        "print": print_after,
        "content_preview": content[:500],
    })

    firmware = request.app.get("firmware")
    display_name = filename
    if firmware and filename:
        uploaded = firmware.sd.upload_file(filename, content)
        display_name = uploaded.name
        telemetry = request.app.get("telemetry")
        if telemetry:
            telemetry.record_upload({
                "filename": display_name,
                "original_filename": filename,
                "size": len(content),
                "content_hash": sha256(content.encode()).hexdigest(),
                "ip": request.remote or "unknown",
            })
        if select:
            firmware.sd.select_file(display_name)
        if print_after:
            sim = request.app.get("simulator")
            if sim:
                sim.start_print_file(display_name, content)

    return web.json_response({
        "files": {
            "local": {
                "name": display_name,
                "display": display_name,
                "path": display_name,
                "type": "machinecode",
                "typePath": ["machinecode", "gcode"],
                "origin": location,
            },
        },
        "done": True,
    }, status=201)


async def api_file_info(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    filename = request.match_info["filename"]
    _log_interaction(request, "file_info", {"filename": filename})
    firmware = request.app.get("firmware")
    if firmware:
        content = firmware.sd.get_file_content(filename)
        if content is not None:
            return web.json_response(_format_file_entry(
                {"name": filename, "size": len(content)},
                request.match_info["location"]
            ))
    return web.json_response({"error": "Not found"}, status=404)


async def api_file_command(request: web.Request) -> web.Response:
    """OctoPrint file action: select / select+print / slice."""
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    filename = request.match_info["filename"]
    try:
        data = await request.json()
    except Exception:
        data = {}
    command = data.get("command", "")
    print_after = bool(data.get("print", False))
    _log_interaction(request, "file_command", {
        "filename": filename,
        "command": command,
        "print": print_after,
    })

    firmware = request.app.get("firmware")
    sim = request.app.get("simulator")
    if not firmware or not sim:
        return web.Response(status=204)

    content = firmware.sd.get_file_content(filename)
    if content is None:
        return web.json_response({"error": "Not found"}, status=404)

    if command == "select":
        firmware.sd.select_file(filename)
        if print_after:
            sim.start_print_file(filename, content)
    return web.Response(status=204)


async def api_file_download(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    filename = request.match_info["filename"]
    _log_interaction(request, "file_download", {"filename": filename})
    firmware = request.app.get("firmware")
    if firmware:
        content = firmware.sd.get_file_content(filename)
        if content is not None:
            return web.Response(text=content, content_type="text/plain")
    return web.Response(status=404, text="Not found")


async def api_file_delete(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    filename = request.match_info["filename"]
    _log_interaction(request, "file_delete", {"filename": filename})
    firmware = request.app.get("firmware")
    if firmware:
        firmware.sd.delete_file(filename)
    return web.Response(status=204)


# === Settings ===

async def api_settings(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "settings_read")
    cfg = request.app.get("config", {})
    ground = cfg.get("ground", {})
    printer = cfg.get("printer", {})
    return web.json_response({
        "api": {"key": ground.get("api_key", ""), "allowCrossOrigin": True},
        "appearance": {
            "name": printer.get("name", "OctoPrint"),
            "color": "default",
            "colorTransparent": False,
            "defaultLanguage": "_default",
        },
        "feature": {
            "gcodeViewer": True,
            "temperatureGraph": True,
            "sdSupport": True,
            "keyboardControl": True,
        },
        "printer": {
            "defaultExtrusionLength": 5,
            "defaultExtrusionSpeed": 300,
        },
        "serial": {
            "port": printer.get("serial_port", "/dev/ttyUSB0"),
            "baudrate": printer.get("baud_rate", 115200),
            "autoconnect": True,
        },
        "server": {
            "commands": {
                "systemShutdownCommand": "sudo shutdown -h now",
                "systemRestartCommand": "sudo shutdown -r now",
                "serverRestartCommand": "sudo systemctl restart octoprint",
            },
        },
        "webcam": {
            "webcamEnabled": ground.get("enable_webcam", True),
            "streamUrl": ground.get("webcam_url", "/webcam/?action=stream"),
            "snapshotUrl": "/webcam/?action=snapshot",
            "flipH": False,
            "flipV": False,
            "rotate90": False,
        },
        "temperature": {
            "profiles": [
                {"name": "PLA", "extruder": 210, "bed": 60},
                {"name": "ABS", "extruder": 240, "bed": 100},
                {"name": "PETG", "extruder": 230, "bed": 70},
            ],
        },
    })


async def api_settings_save(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    data = await request.json()
    _log_interaction(request, "settings_write", data)
    return web.json_response({})


# === System ===

async def api_system_commands(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    _log_interaction(request, "system_commands_list")
    return web.json_response({
        "core": [
            {
                "action": "shutdown",
                "name": "Shutdown",
                "source": "core",
                "resource": "/api/system/commands/core/shutdown",
                "confirm": "Shut down the system?",
            },
            {
                "action": "reboot",
                "name": "Reboot",
                "source": "core",
                "resource": "/api/system/commands/core/reboot",
                "confirm": "Reboot the system?",
            },
            {
                "action": "restart",
                "name": "Restart OctoPrint",
                "source": "core",
                "resource": "/api/system/commands/core/restart",
                "confirm": "Restart OctoPrint?",
            },
        ],
    })


async def api_system_command_exec(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    source = request.match_info["source"]
    action = request.match_info["action"]
    _log_interaction(request, "system_command_exec", {
        "source": source,
        "action": action,
    })
    logger.warning(
        "System command attempted: %s/%s from %s",
        source, action, request.remote
    )
    return web.Response(status=204)


# === Printer Profiles ===

async def api_printer_profiles(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "Unauthorized"}, status=403)
    cfg = request.app.get("config", {})
    printer = cfg.get("printer", {})
    vol = printer.get("build_volume", {"x": 220, "y": 220, "z": 250})
    return web.json_response({
        "profiles": {
            "_default": {
                "id": "_default",
                "name": printer.get("name", "Default"),
                "model": printer.get("name", "Default"),
                "color": "default",
                "current": True,
                "default": True,
                "resource": "/api/printerprofiles/_default",
                "volume": {
                    "width": vol.get("x", 220),
                    "depth": vol.get("y", 220),
                    "height": vol.get("z", 250),
                    "formFactor": "rectangular",
                    "origin": "lowerleft",
                },
                "heatedBed": printer.get("heated_bed", True),
                "heatedChamber": printer.get("heated_chamber", False),
                "extruder": {
                    "count": printer.get("num_extruders", 1),
                    "nozzleDiameter": printer.get("nozzle_diameter", 0.4),
                },
                "axes": {
                    "x": {"speed": 6000, "inverted": False},
                    "y": {"speed": 6000, "inverted": False},
                    "z": {"speed": 200, "inverted": False},
                    "e": {"speed": 300, "inverted": False},
                },
            },
        },
    })


async def api_slicing(request: web.Request) -> web.Response:
    return web.json_response({})


async def api_tracking(request: web.Request) -> web.Response:
    return web.Response(status=204)


# === Helpers ===

def _format_file_entry(f: dict, origin: str) -> dict:
    return {
        "name": f.get("name", ""),
        "display": f.get("display", f.get("name", "")),
        "path": f.get("name", ""),
        "type": "machinecode",
        "typePath": ["machinecode", "gcode"],
        "origin": origin,
        "size": f.get("size", 0),
        "date": int(time.time()),
        "gcodeAnalysis": {
            "estimatedPrintTime": 3600,
            "filament": {"tool0": {"length": 5000, "volume": 12.5}},
        },
        "refs": {
            "resource": f"/api/files/{origin}/{f.get('name', '')}",
            "download": f"/downloads/files/{origin}/{f.get('name', '')}",
        },
    }


def _map_job_state(state: str) -> str:
    return {
        "idle": "Operational",
        "printing": "Printing",
        "paused": "Paused",
        "cancelling": "Cancelling",
        "completed": "Operational",
        "error": "Error",
    }.get(state, "Operational")

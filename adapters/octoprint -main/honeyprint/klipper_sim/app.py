"""Moonraker-style HTTP and WebSocket API for the HoneyPrint Klipper variant."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from aiohttp import web

from .firmware import KlipperEmulator
from ..analyst.dashboard import setup_analyst_routes

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_UPLOAD_EXTENSIONS = {".gcode", ".gco", ".g", ".bgcode"}
PRINTER_OBJECTS = [
    "webhooks",
    "toolhead",
    "gcode_move",
    "extruder",
    "heater_bed",
    "fan",
    "virtual_sdcard",
    "print_stats",
    "display_status",
    "mcu",
]


def _result(data: Any) -> web.Response:
    return web.json_response({"result": data})


def _remote(request: web.Request) -> str:
    return request.remote or "unknown"


def _log(request: web.Request, action: str, data: dict | None = None) -> None:
    callback = request.app.get("log_callback")
    if callback:
        callback("moonraker_interaction", {
            "honeypot_variant": "klipper",
            "action": action,
            "method": request.method,
            "path": request.path,
            "ip": _remote(request),
            "user_agent": request.headers.get("User-Agent", ""),
            "data": data or {},
            "timestamp": time.time(),
        })


def _printer_status(app: web.Application) -> dict[str, Any]:
    sim = app["simulator"]
    temps = sim.get_temperatures()
    job = sim.print_job.get_status()
    pos = sim.get_position()
    state = job.get("state", "idle")
    printing = state == "printing"
    paused = state == "paused"
    filename = job.get("filename", "")
    progress = float(job.get("progress", 0))
    position = [pos["x"], pos["y"], pos["z"], pos["e"]]
    return {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "toolhead": {
            "homed_axes": "xyz" if sim.motion.is_homed() else "",
            "position": position[:3],
            "status": "Ready",
            "print_time": float(job.get("elapsed_time", 0)),
            "estimated_print_time": float(job.get("elapsed_time", 0)),
            "max_velocity": 300.0,
            "max_accel": 5000.0,
            "square_corner_velocity": 5.0,
        },
        "gcode_move": {
            "absolute_coordinates": sim.motion.state.position_mode.value == "absolute",
            "absolute_extrude": sim.motion.state.extruder_mode.value == "absolute",
            "gcode_position": position,
            "position": position,
            "speed": sim.motion.state.feedrate * 60,
            "speed_factor": 1.0,
            "extrude_factor": 1.0,
        },
        "extruder": {
            "temperature": round(temps["hotend"]["current"], 2),
            "target": round(temps["hotend"]["target"], 2),
            "power": sim.thermal.get_power("hotend") / 255,
            "can_extrude": temps["hotend"]["current"] >= 170,
        },
        "heater_bed": {
            "temperature": round(temps["bed"]["current"], 2),
            "target": round(temps["bed"]["target"], 2),
            "power": sim.thermal.get_power("bed") / 255,
        },
        "fan": {"speed": sim.thermal.state.fan_speed / 255},
        "virtual_sdcard": {
            "file_path": filename,
            "progress": progress / 100,
            "is_active": printing or paused,
            "file_position": job.get("current_line", 0),
            "file_size": job.get("total_lines", 0),
        },
        "print_stats": {
            "filename": filename,
            "state": "printing" if printing else "paused" if paused else "standby",
            "current_command": job.get("current_command", ""),
            "message": "",
            "total_duration": float(job.get("elapsed_time", 0)),
            "print_duration": float(job.get("elapsed_time", 0)),
            "filament_used": float(job.get("filament_used_mm", 0)),
        },
        "display_status": {"progress": progress / 100, "message": None},
        "mcu": {
            "mcu_version": "v0.12.0-114-g75965fa1",
            "mcu_build_versions": "gcc: (15:12.2.rel1-1) 12.2.1",
            "last_stats": {"mcu_awake": 0.005, "mcu_task_avg": 0.00001},
        },
    }


def _select_objects(
    status: dict[str, Any],
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    if not requested:
        return status
    selected: dict[str, Any] = {}
    for name, fields in requested.items():
        value = status.get(name)
        if value is None:
            continue
        if fields is None:
            selected[name] = value
        else:
            selected[name] = {key: value.get(key) for key in fields if key in value}
    return selected


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def server_info(request: web.Request) -> web.Response:
    _log(request, "server.info")
    return _result({
        "klippy_connected": True,
        "klippy_state": "ready",
        "components": ["database", "file_manager", "klippy_connection", "websockets"],
        "failed_components": [],
        "registered_directories": ["config", "logs", "gcodes"],
        "warnings": [],
        "websocket_count": len(request.app["ws_clients"]),
        "moonraker_version": "v0.9.3-12-gdf1f26f",
        "api_version": [1, 5, 0],
        "api_version_string": "1.5.0",
    })


async def printer_info(request: web.Request) -> web.Response:
    _log(request, "printer.info")
    return _result({
        "state": "ready",
        "state_message": "Printer is ready",
        "hostname": "voron24",
        "software_version": "v0.12.0-114-g75965fa1",
        "cpu_info": "4 core ARMv7 Processor rev 3 (v7l)",
        "klipper_path": "/home/pi/klipper",
        "python_path": "/home/pi/klippy-env/bin/python",
        "log_file": "/home/pi/printer_data/logs/klippy.log",
        "config_file": "/home/pi/printer_data/config/printer.cfg",
    })


async def objects_list(request: web.Request) -> web.Response:
    _log(request, "printer.objects.list")
    return _result({"objects": PRINTER_OBJECTS})


async def objects_query(request: web.Request) -> web.Response:
    requested = {key: None for key in request.query if key in PRINTER_OBJECTS}
    _log(request, "printer.objects.query", {"objects": list(requested)})
    return _result({
        "eventtime": time.monotonic(),
        "status": _select_objects(_printer_status(request.app), requested),
    })


async def gcode_script(request: web.Request) -> web.Response:
    data = await request.json()
    script = str(data.get("script", ""))
    _log(request, "printer.gcode.script", {"script": script})
    if request.app.get("telemetry"):
        request.app["telemetry"].record("terminal_command", {
            "command": script,
            "source": "moonraker_api",
            "ip": _remote(request),
        })
    await request.app["firmware"].process_script(script)
    return _result("ok")


async def print_action(request: web.Request) -> web.Response:
    firmware: KlipperEmulator = request.app["firmware"]
    action = request.match_info["action"]
    data = await request.json() if request.can_read_body else {}
    _log(request, f"printer.print.{action}", data)
    if action == "start":
        filename = data.get("filename", "")
        if not firmware.start_print(filename):
            return web.json_response({"error": {"message": "File not found"}}, status=404)
    elif action == "pause":
        firmware.sim.pause_print()
    elif action == "resume":
        firmware.sim.resume_print()
    elif action == "cancel":
        firmware.cancel_print()
    return _result("ok")


async def emergency_stop(request: web.Request) -> web.Response:
    _log(request, "printer.emergency_stop")
    request.app["firmware"].emergency_stop()
    return _result("ok")


async def files_list(request: web.Request) -> web.Response:
    _log(request, "server.files.list")
    files = []
    for file in request.app["firmware"].sd.list_files():
        files.append({
            "path": file["name"],
            "modified": time.time(),
            "size": file["size"],
            "permissions": "rw",
        })
    return _result(files)


async def files_upload(request: web.Request) -> web.Response:
    reader = await request.multipart()
    filename = ""
    content = ""
    async for part in reader:
        if part.name == "file":
            filename = part.filename or ""
            content = (await part.read()).decode("utf-8", errors="replace")
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        _log(request, "server.files.upload.rejected", {"filename": filename})
        return web.json_response({
            "error": {"message": "Upload a G-code or BGCODE file only"}
        }, status=400)
    file = request.app["firmware"].sd.upload_file(filename, content)
    if request.app.get("telemetry"):
        request.app["telemetry"].record_upload({
            "filename": file.name,
            "original_filename": filename,
            "size": file.size,
            "content_hash": sha256(content.encode()).hexdigest(),
            "ip": _remote(request),
        })
    _log(request, "server.files.upload", {"filename": file.name, "size": file.size})
    return _result({"item": {"path": file.name, "root": "gcodes"}, "action": "create_file"})


async def file_download(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    content = request.app["firmware"].sd.get_file_content(filename)
    _log(request, "server.files.download", {"filename": filename})
    if content is None:
        raise web.HTTPNotFound()
    return web.Response(text=content, content_type="text/plain")


async def file_delete(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    _log(request, "server.files.delete_file", {"filename": filename})
    request.app["firmware"].sd.delete_file(filename)
    return _result({"item": {"path": filename, "root": "gcodes"}, "action": "delete_file"})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app["ws_clients"][ws] = {}
    _log(request, "websocket.connect")
    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            await _handle_rpc(request.app, ws, payload)
    finally:
        request.app["ws_clients"].pop(ws, None)
        _log(request, "websocket.disconnect")
    return ws


async def _handle_rpc(app: web.Application, ws: web.WebSocketResponse, payload: dict) -> None:
    method = payload.get("method", "")
    params = payload.get("params", {})
    rpc_id = payload.get("id")
    if method == "server.info":
        result: Any = {"klippy_connected": True, "klippy_state": "ready"}
    elif method == "printer.info":
        result = {"state": "ready", "state_message": "Printer is ready"}
    elif method == "printer.objects.list":
        result = {"objects": PRINTER_OBJECTS}
    elif method in {"printer.objects.query", "printer.objects.subscribe"}:
        requested = params.get("objects", {})
        if method.endswith("subscribe"):
            app["ws_clients"][ws] = requested
        result = {
            "eventtime": time.monotonic(),
            "status": _select_objects(_printer_status(app), requested),
        }
    elif method == "printer.gcode.script":
        await app["firmware"].process_script(params.get("script", ""))
        result = "ok"
    else:
        await ws.send_json({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })
        return
    await ws.send_json({"jsonrpc": "2.0", "id": rpc_id, "result": result})


async def _broadcast_loop(app: web.Application) -> None:
    while True:
        await asyncio.sleep(1)
        status = _printer_status(app)
        stale = []
        for ws, requested in app["ws_clients"].items():
            try:
                await ws.send_json({
                    "jsonrpc": "2.0",
                    "method": "notify_status_update",
                    "params": [_select_objects(status, requested), time.monotonic()],
                })
            except Exception:
                stale.append(ws)
        for ws in stale:
            app["ws_clients"].pop(ws, None)


def create_moonraker_app(
    simulator: Any,
    firmware: KlipperEmulator,
    config: dict,
    log_callback: Callable | None = None,
    telemetry: Any = None,
) -> web.Application:
    app = web.Application()
    app["simulator"] = simulator
    app["firmware"] = firmware
    app["config"] = config
    app["log_callback"] = log_callback
    app["telemetry"] = telemetry
    app["ws_clients"] = {}

    app.router.add_get("/", index)
    app.router.add_get("/server/info", server_info)
    app.router.add_get("/printer/info", printer_info)
    app.router.add_get("/printer/objects/list", objects_list)
    app.router.add_get("/printer/objects/query", objects_query)
    app.router.add_post("/printer/gcode/script", gcode_script)
    app.router.add_post("/printer/print/{action}", print_action)
    app.router.add_post("/printer/emergency_stop", emergency_stop)
    app.router.add_get("/server/files/list", files_list)
    app.router.add_post("/server/files/upload", files_upload)
    app.router.add_get("/server/files/gcodes/{filename}", file_download)
    app.router.add_delete("/server/files/gcodes/{filename}", file_delete)
    app.router.add_get("/websocket", websocket)
    setup_analyst_routes(app)

    async def start_background(application: web.Application) -> None:
        application["broadcast_task"] = asyncio.create_task(_broadcast_loop(application))

    async def stop_background(application: web.Application) -> None:
        application["broadcast_task"].cancel()
        await asyncio.gather(application["broadcast_task"], return_exceptions=True)

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    return app

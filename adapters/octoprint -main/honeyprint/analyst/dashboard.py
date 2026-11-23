"""Private analyst dashboard routes shared by HoneyPrint variants."""

from pathlib import Path

from aiohttp import web

STATIC_DIR = Path(__file__).parent / "static"


async def analyst_index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def analyst_summary(request: web.Request) -> web.Response:
    expected = request.app.get("config", {}).get("ground", {}).get(
        "analyst_key", "change-me"
    )
    provided = request.headers.get("X-Analyst-Key", "")
    if provided != expected:
        return web.json_response({"error": "Unauthorized"}, status=403)
    telemetry = request.app["telemetry"]
    history = request.app["firmware"].sd.get_upload_history()
    return web.json_response(telemetry.summary(history))


def setup_analyst_routes(app: web.Application) -> None:
    app.router.add_get("/analyst", analyst_index)
    app.router.add_get("/analyst/api/summary", analyst_summary)

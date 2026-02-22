"""ADS-B Server — receives decoded data from collectors and serves the web UI and API.

This FastAPI application:
  - Starts a TCP collector hub to receive raw hex ADS-B messages from collectors
  - Decodes messages using pyModeS and maintains aircraft state
  - Serves a real-time web UI (2D Leaflet map + 3D CesiumJS globe)
  - Exposes REST API endpoints for aircraft, stats, config, and collectors
  - Pushes live updates to browser clients via WebSocket
  - Provides an admin dashboard with live collector/client monitoring
  - Enforces API key authentication for collectors and API/CLI clients
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from app.aircraft_db import AircraftDB
from app.aircraft_store import AircraftStore
from app import auth
from app import config as cfg
from app.collector_hub import CollectorHub
from app.models import AircraftList, CollectorInfo, ConnectedClientInfo, ReceiverStats
from app.type_collector import TypeCollector


class ServerConfig(BaseModel):
    """Server configuration for the API."""
    latitude: float = Field(..., description="Server/receiver latitude")
    longitude: float = Field(..., description="Server/receiver longitude")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
_saved_cfg = cfg.load()

aircraft_db = AircraftDB()
type_collector = TypeCollector()
type_collector.load()
store = AircraftStore(aircraft_db=aircraft_db, type_collector=type_collector)

# Apply saved receiver position
store.receiver_lat = _saved_cfg["latitude"]
store.receiver_lon = _saved_cfg["longitude"]

# Collector hub (TCP server for raw hex feeds)
collector_hub: CollectorHub | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop server components."""
    global collector_hub

    await aircraft_db.start()
    await store.start()

    # Start the TCP collector hub
    collector_port = _saved_cfg.get("collector_port", 4002)
    collector_hub = CollectorHub(store=store, port=collector_port)
    await collector_hub.start()

    logger.info(
        "ADS-B server started — HTTP on this process, collectors on TCP port %d",
        collector_port,
    )
    yield

    # --- Shutdown ---
    if collector_hub:
        await collector_hub.stop()
    await store.stop()
    await aircraft_db.stop()
    logger.info("ADS-B server stopped")


app = FastAPI(
    title="ADS-B Server API",
    description="Real-time aircraft tracking server — receives data from remote collectors",
    version="3.0.0",
    lifespan=lifespan,
)

# Static files are relative to the server/ directory
_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Prevent browsers from serving stale JS/CSS from cache."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ===========================================================================
# HTML pages (no auth required)
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the 2D map UI."""
    index_path = os.path.join(_static_dir, "index.html")
    with open(index_path, "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/3d", response_class=HTMLResponse)
async def index_3d():
    """Serve the CesiumJS 3D tracking UI."""
    cesium_path = os.path.join(_static_dir, "cesium.html")
    with open(cesium_path, "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin dashboard."""
    admin_path = os.path.join(_static_dir, "admin.html")
    with open(admin_path, "r") as f:
        return HTMLResponse(content=f.read())


# ===========================================================================
# Admin SSE stream (no auth required — admin page is open)
# ===========================================================================

async def _admin_event_generator(request: Request):
    """Yield Server-Sent Events with collector/client/stats data every 2s."""
    while True:
        if await request.is_disconnected():
            break

        collectors = []
        if collector_hub is not None:
            collectors = [
                c.model_dump(mode="json") for c in collector_hub.get_collectors()
            ]

        clients = [c.model_dump(mode="json") for c in store.get_connected_clients()]
        stats = store.get_stats()
        stats["collector_count"] = len(collectors)
        stats["client_count"] = len(clients)

        payload = json.dumps({
            "collectors": collectors,
            "clients": clients,
            "stats": stats,
        })
        yield f"data: {payload}\n\n"

        await asyncio.sleep(2)


@app.get("/admin/stream")
async def admin_stream(request: Request):
    """SSE endpoint for live admin dashboard updates."""
    return StreamingResponse(
        _admin_event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ===========================================================================
# Aircraft REST API (requires API key for non-browser clients)
# ===========================================================================

@app.get("/api/aircraft", response_model=AircraftList, dependencies=[Depends(auth.require_api_key)])
async def list_aircraft():
    """List all currently tracked aircraft."""
    aircraft = await store.get_all()
    return AircraftList(count=len(aircraft), aircraft=aircraft)


@app.get("/api/aircraft/{icao}", dependencies=[Depends(auth.require_api_key)])
async def get_aircraft(icao: str):
    """Get details for a specific aircraft by ICAO hex code."""
    ac = await store.get_by_icao(icao)
    if ac is None:
        return {"error": f"Aircraft {icao.upper()} not found"}
    return ac


@app.get("/api/stats", response_model=ReceiverStats, dependencies=[Depends(auth.require_api_key)])
async def get_stats():
    """Get receiver statistics."""
    return ReceiverStats(**store.get_stats())


# ===========================================================================
# Server config
# ===========================================================================

@app.get("/api/config", dependencies=[Depends(auth.require_api_key)])
async def get_config():
    """Get current server configuration."""
    return {
        "latitude": store.receiver_lat,
        "longitude": store.receiver_lon,
        "collector_port": _saved_cfg.get("collector_port", 4002),
        "http_port": _saved_cfg.get("http_port", 8080),
    }


@app.put("/api/config", dependencies=[Depends(auth.require_api_key)])
async def update_config(config: ServerConfig):
    """Update server configuration (receiver location). Saves to disk."""
    store.receiver_lat = config.latitude
    store.receiver_lon = config.longitude
    cfg.save({
        **_saved_cfg,
        "latitude": config.latitude,
        "longitude": config.longitude,
    })
    return {
        "latitude": store.receiver_lat,
        "longitude": store.receiver_lon,
    }


# ===========================================================================
# Aircraft database
# ===========================================================================

@app.get("/api/watchlist")
async def get_watchlist():
    """Get the aircraft registration watchlist (no auth required for browser clients)."""
    return {"watchlist": _saved_cfg.get("watchlist", [])}


@app.get("/api/db/status", dependencies=[Depends(auth.require_api_key)])
async def db_status():
    """Get aircraft database status including age and record count."""
    return aircraft_db.get_status()


@app.post("/api/db/update", dependencies=[Depends(auth.require_api_key)])
async def db_update():
    """Force re-download and hot-reload the aircraft database."""
    result = await aircraft_db.refresh()
    return result


# ===========================================================================
# Aircraft types
# ===========================================================================

@app.get("/api/types", dependencies=[Depends(auth.require_api_key)])
async def get_types():
    """Get all unique aircraft types and models detected by the receiver."""
    types = type_collector.get_types()
    return {
        "count": len(types),
        "types": types,
    }


@app.get("/api/types/summary", dependencies=[Depends(auth.require_api_key)])
async def get_types_summary():
    """Get summary statistics about collected aircraft types."""
    return type_collector.get_summary()


# ===========================================================================
# Collectors
# ===========================================================================

@app.get("/api/collectors", response_model=list[CollectorInfo], dependencies=[Depends(auth.require_api_key)])
async def list_collectors():
    """List all currently connected remote collectors."""
    if collector_hub is None:
        return []
    return collector_hub.get_collectors()


@app.get("/api/collectors/{collector_id}", dependencies=[Depends(auth.require_api_key)])
async def get_collector(collector_id: str):
    """Get details for a specific connected collector."""
    if collector_hub is None:
        return {"error": "Collector hub not initialized"}
    info = collector_hub.get_collector(collector_id)
    if info is None:
        return {"error": f"Collector {collector_id} not found"}
    return info


# ===========================================================================
# Connected clients
# ===========================================================================

@app.get("/api/clients", response_model=list[ConnectedClientInfo], dependencies=[Depends(auth.require_api_key)])
async def list_clients():
    """List all currently connected WebSocket clients."""
    return store.get_connected_clients()


# ===========================================================================
# WebSocket — browser clients
# ===========================================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time aircraft updates."""
    await ws.accept()

    # Determine remote address for client tracking
    remote_addr = ""
    if ws.client:
        remote_addr = f"{ws.client.host}:{ws.client.port}"

    await store.register_websocket(ws, client_type="browser", remote_addr=remote_addr)

    # Send current aircraft state as initial snapshot
    aircraft = await store.get_all()
    for ac in aircraft:
        await ws.send_json({"type": "update", "aircraft": ac.model_dump(mode="json")})

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await store.unregister_websocket(ws)

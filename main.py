import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from collectors.system import get_cpu, get_memory, get_network, get_disk
from collectors.gpu import get_gpu
from collectors.sensor import get_sensors
from collectors.services import get_services
from collectors.command import CommandRunner
from collectors import fanctl
from fan_auth import FanControlPassword

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


config = load_config()
_shutdown = asyncio.Event()

server_cfg = config.get("server", {})
gpu_enabled = config.get("gpu", {}).get("enabled", True)
service_list = config.get("services", [])
command_configs = config.get("commands", [])
refresh_interval = server_cfg.get("refresh_interval", 1)
fanctl_enabled = config.get("fanctl", {}).get("enabled", False)
fan_password = FanControlPassword.load()
_fan_auth_lock = asyncio.Lock()

class IntervalUpdate(BaseModel):
    interval: int

cmd_runner = CommandRunner(command_configs)

_latest_snapshot = {}
_collection_lock = asyncio.Lock()


def collect_metrics():
    """Run blocking hardware and OS queries outside the event loop."""
    return {
        "cpu": get_cpu(),
        "memory": get_memory(),
        "network": get_network(),
        "disk": get_disk(),
        "sensors": get_sensors(config),
        "gpu": get_gpu() if gpu_enabled else None,
        "services": get_services(service_list),
        "fanctl": fanctl.get_state() if fanctl_enabled else None,
    }


async def collect_all():
    # Serialize startup/API collection with the background collector to avoid
    # overlapping hardware queries while the first snapshot is being built.
    async with _collection_lock:
        metrics, commands = await asyncio.gather(
            asyncio.to_thread(collect_metrics), cmd_runner.get_all()
        )
        return {
            **metrics,
            "commands": commands,
            "timestamp": datetime.now().isoformat(),
        }


async def background_collector():
    global _latest_snapshot
    while not _shutdown.is_set():
        try:
            _latest_snapshot = await collect_all()
        except Exception as e:
            _latest_snapshot = {"error": str(e), "timestamp": datetime.now().isoformat()}
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=refresh_interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app):
    if fanctl_enabled:
        fanctl.configure(config.get("fanctl", {}))
        fanctl.start()
    task = asyncio.create_task(background_collector())
    yield
    _shutdown.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if fanctl_enabled:
        fanctl.stop()


app = FastAPI(title="Server Monitor", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
async def api_status():
    if not _latest_snapshot:
        snapshot = await collect_all()
        return JSONResponse(snapshot)
    return JSONResponse(_latest_snapshot)


@app.get("/api/stream")
async def api_stream():
    async def event_generator():
        while not _shutdown.is_set():
            if _latest_snapshot:
                data = json.dumps(_latest_snapshot, ensure_ascii=False)
                yield f"data: {data}\n\n"
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=refresh_interval)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/interval")
async def set_interval(body: IntervalUpdate):
    global refresh_interval
    refresh_interval = max(0.1, min(2.0, body.interval / 1000.0))
    return {"interval_ms": int(refresh_interval * 1000)}


class FanAuthRequest(BaseModel):
    password: SecretStr | None = Field(default=None, max_length=1024)


class FanModeUpdate(FanAuthRequest):
    mode: str  # curve | manual | full | bios


class FanPwmUpdate(FanAuthRequest):
    zones: dict[int, int]  # {zone_number: pwm_value}


async def require_fan_password(password):
    if fan_password is None:
        raise HTTPException(status_code=503, detail="Fan-control password is not configured")
    if password is None or not password.get_secret_value():
        raise HTTPException(status_code=401, detail="Fan-control password required")
    # Password hashing must not stall SSE/API responses or run many costly
    # verifications concurrently in the shared worker pool.
    async with _fan_auth_lock:
        valid = await asyncio.to_thread(fan_password.verify, password.get_secret_value())
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect fan-control password")


@app.get("/api/fanctl")
async def api_fanctl_status():
    if not fanctl_enabled:
        return JSONResponse({"error": "fanctl disabled"}, status_code=404)
    return JSONResponse(fanctl.get_state())


@app.post("/api/fanctl/mode")
async def api_fanctl_mode(body: FanModeUpdate):
    if not fanctl_enabled:
        return JSONResponse({"error": "fanctl disabled"}, status_code=404)
    await require_fan_password(body.password)
    try:
        fanctl.set_mode(body.mode)
        return JSONResponse({"ok": True, "mode": body.mode})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/fanctl/pwm")
async def api_fanctl_pwm(body: FanPwmUpdate):
    if not fanctl_enabled:
        return JSONResponse({"error": "fanctl disabled"}, status_code=404)
    await require_fan_password(body.password)
    try:
        fanctl.set_manual_pwm(body.zones)
        return JSONResponse({"ok": True, "zones": body.zones})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


if __name__ == "__main__":
    import uvicorn

    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)
    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=0)

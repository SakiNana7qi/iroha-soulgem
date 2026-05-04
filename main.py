import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from collectors.system import get_cpu, get_memory, get_network, get_disk
from collectors.gpu import get_gpu
from collectors.sensor import get_sensors
from collectors.services import get_services
from collectors.command import CommandRunner

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

class IntervalUpdate(BaseModel):
    interval: int

cmd_runner = CommandRunner(command_configs)

_latest_snapshot = {}


async def collect_all():
    cpu = get_cpu()
    memory = get_memory()
    network = get_network()
    disk = get_disk()
    sensors = get_sensors(config)
    services = get_services(service_list)
    commands = await cmd_runner.get_all()
    gpu = get_gpu() if gpu_enabled else None

    return {
        "timestamp": datetime.now().isoformat(),
        "cpu": cpu,
        "memory": memory,
        "network": network,
        "disk": disk,
        "sensors": sensors,
        "gpu": gpu,
        "services": services,
        "commands": commands,
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
    task = asyncio.create_task(background_collector())
    yield
    _shutdown.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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


if __name__ == "__main__":
    import uvicorn

    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)
    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=0)

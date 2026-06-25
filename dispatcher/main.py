"""
dispatcher/main.py — OpenMind Dispatcher Service

The central coordinator that bridges OpenMind MCP tools to the Hyperspace Pod
(LLM inference) and ComfyUI-Distributed cluster (image/video generation).

Features:
- Job queue with priority and load balancing
- Worker health monitoring
- ComfyUI API proxy with smart routing
- Hyperspace Pod API proxy with fallback
- Result caching and retrieval
- REST API for all pod services
- Optional Redis persistence (graceful fallback to in-memory)
- API key authentication
- Job retry on worker failure
- WebSocket-based ComfyUI monitoring (fallback to HTTP polling)
- ComfyUI master health monitoring with alerting
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── Logging ────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("openmind.dispatcher")

# ── Configuration ──────────────────────────────────────────────────

CONFIG_PATH = os.environ.get("DISPATCHER_CONFIG", str(Path(__file__).parent / "config.yaml"))

with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

HYPERSPACE_API_URL = os.environ.get(
    "POD_API_URL", config.get("hyperspace", {}).get("api_url", "http://127.0.0.1:8080")
)
HYPERSPACE_API_KEY = os.environ.get(
    "POD_API_KEY", config.get("hyperspace", {}).get("api_key", "")
)
COMFYUI_MASTER_URL = os.environ.get(
    "COMFYUI_MASTER_URL", config.get("comfyui", {}).get("master_url", "http://127.0.0.1:8188")
)

# Auth
DISPATCHER_API_KEY = os.environ.get(
    "DISPATCHER_API_KEY", config.get("auth", {}).get("api_key", "")
)

# Redis
REDIS_URL = os.environ.get(
    "REDIS_URL", config.get("redis", {}).get("url", "redis://localhost:6379")
)
REDIS_KEY_PREFIX = config.get("redis", {}).get("key_prefix", "openmind:")

# Retry
MAX_RETRIES = config.get("retry", {}).get("max_retries", 2)
RETRY_DELAY_SECONDS = config.get("retry", {}).get("retry_delay_seconds", 5)

# Output directory — cross-platform
_output_dir_config = os.environ.get(
    "DISPATCHER_OUTPUT_DIR", config.get("output_dir", "auto")
)
if _output_dir_config == "auto":
    OUTPUT_DIR = Path(tempfile.gettempdir()) / "pod-output"
else:
    OUTPUT_DIR = Path(_output_dir_config)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT_VIDEO_JOBS = config.get("limits", {}).get("max_concurrent_video_jobs", 4)

# ── Workflow filename mapping ──────────────────────────────────────
# Maps model names used in the API to actual workflow file basenames.
# This avoids the broken `.replace('.', '')` heuristic.

WORKFLOW_FILES = {
    "wan2.2": "wan22_t2v.json",
    "wan2.2-i2v": "wan22_i2v.json",
    "ltx-video": "ltx_video.json",
    "hunyuan": "hunyuan_video.json",
    "cogvideox": "cogvideox_t2v.json",
    "animatediff": "animatediff_t2v.json",
}

# ── Redis Client (optional) ───────────────────────────────────────

redis_client = None  # Will be set on startup if Redis is available


async def _init_redis():
    """Try to connect to Redis. Returns client or None."""
    global redis_client
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await client.ping()
        redis_client = client
        logger.info(f"✅ Redis connected at {REDIS_URL}")
        return client
    except ImportError:
        logger.warning("⚠️  redis package not installed — running without persistence")
        logger.warning("   Install with: pip install redis[hiredis]")
        return None
    except Exception as e:
        logger.warning(f"⚠️  Redis unavailable ({e}) — running without persistence")
        logger.warning("   State will NOT survive restarts. Start Redis for persistence.")
        return None


async def _redis_save_job(job_id: str, job: dict):
    """Persist job to Redis if available."""
    if redis_client:
        try:
            await redis_client.hset(f"{REDIS_KEY_PREFIX}jobs:{job_id}", mapping={
                "data": json.dumps(job)
            })
        except Exception as e:
            logger.debug(f"Redis save job failed: {e}")


async def _redis_save_worker(worker_id: str, worker: dict):
    """Persist worker to Redis if available."""
    if redis_client:
        try:
            await redis_client.hset(f"{REDIS_KEY_PREFIX}workers:{worker_id}", mapping={
                "data": json.dumps(worker)
            })
        except Exception as e:
            logger.debug(f"Redis save worker failed: {e}")


async def _redis_delete_worker(worker_id: str):
    """Remove worker from Redis."""
    if redis_client:
        try:
            await redis_client.delete(f"{REDIS_KEY_PREFIX}workers:{worker_id}")
        except Exception:
            pass


async def _redis_hydrate():
    """Load jobs and workers from Redis into memory on startup."""
    if not redis_client:
        return

    try:
        # Hydrate jobs
        job_keys = []
        async for key in redis_client.scan_iter(f"{REDIS_KEY_PREFIX}jobs:*"):
            job_keys.append(key)

        for key in job_keys:
            data = await redis_client.hget(key, "data")
            if data:
                job = json.loads(data)
                job_id = job.get("id")
                if job_id:
                    jobs[job_id] = job

        # Hydrate workers
        worker_keys = []
        async for key in redis_client.scan_iter(f"{REDIS_KEY_PREFIX}workers:*"):
            worker_keys.append(key)

        for key in worker_keys:
            data = await redis_client.hget(key, "data")
            if data:
                worker = json.loads(data)
                wid = worker.get("id")
                if wid:
                    workers[wid] = worker

        logger.info(f"✅ Hydrated {len(jobs)} jobs and {len(workers)} workers from Redis")
    except Exception as e:
        logger.warning(f"⚠️  Redis hydration failed: {e}")


# ── Data Models ────────────────────────────────────────────────────

class JobType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    UPSCALE = "upscale"

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class VideoJobRequest(BaseModel):
    job_type: str = "video"
    prompt: str
    model: str = "auto"
    duration: float = 5.0
    resolution: str = "720p"
    fps: Optional[int] = None
    negative_prompt: str = ""
    seed: Optional[int] = None
    image_url: Optional[str] = None
    num_variations: int = 1

class UpscaleJobRequest(BaseModel):
    job_type: str = "upscale"
    video_url: str
    target_resolution: str = "1080p"
    style: str = "fidelity"

class ImageJobRequest(BaseModel):
    job_type: str = "image"
    prompt: str
    model: str = "auto"
    width: int = 1024
    height: int = 1024
    negative_prompt: str = ""
    seed: Optional[int] = None
    num_variations: int = 1

class WorkerInfo(BaseModel):
    id: str
    name: str
    gpu: str
    vram_gb: float
    free_vram_gb: float
    status: str = "idle"
    loaded_model: str = ""
    current_job: Optional[str] = None
    last_heartbeat: float = 0.0
    comfyui_url: str = ""

# ── In-Memory State ────────────────────────────────────────────────

app = FastAPI(title="OpenMind Dispatcher", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict[str, dict] = {}
workers: dict[str, dict] = {}
job_queue: list[str] = []
active_jobs: set[str] = set()

# ComfyUI master health state
comfyui_master_healthy: bool = True
comfyui_master_last_check: float = 0.0
comfyui_master_alerts: list[dict] = []


# ── Auth Middleware ─────────────────────────────────────────────────

# Paths that bypass authentication
AUTH_EXEMPT_PATHS = {"/api/v1/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """API key authentication middleware. Disabled when DISPATCHER_API_KEY is empty."""
    if not DISPATCHER_API_KEY:
        # Auth disabled — pass through
        return await call_next(request)

    path = request.url.path

    # Allow health check and docs without auth
    if path in AUTH_EXEMPT_PATHS:
        return await call_next(request)

    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == DISPATCHER_API_KEY:
            return await call_next(request)

    # Also accept X-API-Key header
    api_key_header = request.headers.get("X-API-Key", "")
    if api_key_header == DISPATCHER_API_KEY:
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Invalid or missing API key. Provide via 'Authorization: Bearer <key>' or 'X-API-Key: <key>' header."},
    )


# ── Helper Functions ───────────────────────────────────────────────

async def _comfyui_request(endpoint: str, method: str = "GET", json_data: dict = None, timeout: int = 300):
    """Make a request to the ComfyUI Master API."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=timeout)) as client:
        url = f"{COMFYUI_MASTER_URL}{endpoint}"
        if method == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=json_data)
        resp.raise_for_status()
        return resp.json()


async def _hyperspace_request(endpoint: str, method: str = "GET", json_data: dict = None, timeout: int = 120):
    """Make a request to the Hyperspace Pod API."""
    headers = {"Content-Type": "application/json"}
    if HYPERSPACE_API_KEY:
        headers["Authorization"] = f"Bearer {HYPERSPACE_API_KEY}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=timeout)) as client:
        url = f"{HYPERSPACE_API_URL}{endpoint}"
        if method == "GET":
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.post(url, json=json_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _select_best_worker(vram_required_gb: float, model_preference: str = "", exclude_ids: set = None) -> Optional[str]:
    """Select the best idle worker for a job based on VRAM and model match.

    Args:
        vram_required_gb: Minimum VRAM needed.
        model_preference: Preferred model name (for affinity scoring).
        exclude_ids: Worker IDs to skip (e.g., previously failed workers).
    """
    best = None
    best_score = -1
    exclude_ids = exclude_ids or set()

    for wid, w in workers.items():
        if wid in exclude_ids:
            continue
        if w["status"] != "idle":
            continue
        if w["free_vram_gb"] < vram_required_gb:
            continue

        # Score: exact model match > partial match > just has VRAM
        score = 0
        if model_preference and w.get("loaded_model") == model_preference:
            score = 100
        elif model_preference and w.get("loaded_model") and model_preference in w["loaded_model"]:
            score = 50
        score += w["free_vram_gb"] * 0.1  # prefer more free VRAM

        if score > best_score:
            best_score = score
            best = wid

    return best


def _estimate_video_time(duration: float, resolution: str, model: str) -> float:
    """Rough time estimate in seconds for video generation."""
    base = duration * 10  # ~10s per second of video for Wan 2.2 at 720p
    if resolution == "1080p":
        base *= 2.5
    elif resolution == "480p":
        base *= 0.5
    if model == "ltx-video":
        base *= 0.4  # LTX is faster
    elif model == "hunyuan":
        base *= 3.0  # Hunyuan is slower
    return base


# ── API: Job Management ────────────────────────────────────────────

@app.post("/api/v1/jobs/video")
async def submit_video_job(req: VideoJobRequest):
    """Submit a video generation job."""
    job_id = f"vid_{uuid.uuid4().hex[:12]}"

    # Determine VRAM requirement
    vram_map = {
        "wan2.2": 24, "wan2.2-i2v": 24, "ltx-video": 12,
        "hunyuan": 48, "cogvideox": 16, "animatediff": 8, "auto": 16,
    }
    vram_required = vram_map.get(req.model, 16)

    # Select worker
    worker_id = _select_best_worker(vram_required, req.model)
    workers_used = 1 if worker_id else 0

    if not worker_id and req.model != "auto":
        # Try any idle worker with enough VRAM
        for wid, w in workers.items():
            if w["status"] == "idle" and w["free_vram_gb"] >= vram_required:
                worker_id = wid
                workers_used = 1
                break

    est_time = _estimate_video_time(req.duration, req.resolution, req.model)

    jobs[job_id] = {
        "id": job_id,
        "type": "video",
        "status": JobStatus.QUEUED.value,
        "prompt": req.prompt,
        "model": req.model,
        "duration": req.duration,
        "resolution": req.resolution,
        "fps": req.fps,
        "negative_prompt": req.negative_prompt,
        "seed": req.seed,
        "image_url": req.image_url,
        "num_variations": req.num_variations,
        "worker_id": worker_id,
        "workers_used": workers_used,
        "estimated_time_seconds": est_time,
        "progress": 0,
        "result_url": None,
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "retry_count": 0,
        "max_retries": MAX_RETRIES,
        "failed_workers": [],
    }

    await _redis_save_job(job_id, jobs[job_id])

    if worker_id:
        job_queue.append(job_id)
        asyncio.create_task(_process_job(job_id))

    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
        "model": req.model,
        "workers_used": workers_used,
        "estimated_time_seconds": est_time,
        "result_url": None,
    })


@app.post("/api/v1/jobs/upscale")
async def submit_upscale_job(req: UpscaleJobRequest):
    """Submit a video upscale job."""
    job_id = f"ups_{uuid.uuid4().hex[:12]}"

    # Upscaling needs a worker with enough VRAM based on target resolution
    vram_map = {"1080p": 16, "2K": 24, "4K": 48}
    vram_required = vram_map.get(req.target_resolution, 16)
    worker_id = _select_best_worker(vram_required)

    jobs[job_id] = {
        "id": job_id,
        "type": "upscale",
        "status": JobStatus.QUEUED.value,
        "video_url": req.video_url,
        "target_resolution": req.target_resolution,
        "style": req.style,
        "worker_id": worker_id,
        "workers_used": 1 if worker_id else 0,
        "estimated_time_seconds": 120,
        "progress": 0,
        "result_url": None,
        "error": None,
        "created_at": time.time(),
        "retry_count": 0,
        "max_retries": MAX_RETRIES,
        "failed_workers": [],
    }

    await _redis_save_job(job_id, jobs[job_id])

    if worker_id:
        job_queue.append(job_id)
        asyncio.create_task(_process_job(job_id))

    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
    })


@app.post("/api/v1/jobs/image")
async def submit_image_job(req: ImageJobRequest):
    """Submit an image generation job."""
    job_id = f"img_{uuid.uuid4().hex[:12]}"
    worker_id = _select_best_worker(8, "sdxl")  # Most image models need 8GB+

    jobs[job_id] = {
        "id": job_id,
        "type": "image",
        "status": JobStatus.QUEUED.value,
        "prompt": req.prompt,
        "model": req.model,
        "width": req.width,
        "height": req.height,
        "negative_prompt": req.negative_prompt,
        "seed": req.seed,
        "num_variations": req.num_variations,
        "worker_id": worker_id,
        "estimated_time_seconds": 15,
        "progress": 0,
        "result_url": None,
        "error": None,
        "created_at": time.time(),
        "retry_count": 0,
        "max_retries": MAX_RETRIES,
        "failed_workers": [],
    }

    await _redis_save_job(job_id, jobs[job_id])

    if worker_id:
        job_queue.append(job_id)
        asyncio.create_task(_process_job(job_id))

    return JSONResponse({
        "job_id": job_id,
        "status": "queued",
    })


@app.get("/api/v1/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Get the status of any job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return JSONResponse({
        "job_id": job["id"],
        "status": job["status"],
        "type": job.get("type", "unknown"),
        "progress": job.get("progress", 0),
        "result_url": job.get("result_url"),
        "error": job.get("error"),
        "workers_active": job.get("workers_used", 0),
        "duration_s": job.get("duration"),
        "file_size_mb": job.get("file_size_mb"),
        "estimated_time_seconds": job.get("estimated_time_seconds"),
        "retry_count": job.get("retry_count", 0),
    })


@app.get("/api/v1/jobs")
async def list_jobs(status: Optional[str] = None, limit: int = 20):
    """List recent jobs, optionally filtered by status."""
    result = []
    for jid, job in list(jobs.items())[-limit:]:
        if status and job["status"] != status:
            continue
        result.append({
            "job_id": jid,
            "type": job.get("type"),
            "status": job["status"],
            "prompt": job.get("prompt", "")[:100],
            "created_at": job.get("created_at"),
        })
    return JSONResponse({"jobs": result, "total": len(result)})


# ── API: Worker Management ─────────────────────────────────────────

@app.get("/api/v1/workers")
async def list_workers():
    """List all registered video workers."""
    worker_list = []
    for wid, w in workers.items():
        worker_list.append({
            "id": wid,
            "name": w["name"],
            "gpu": w["gpu"],
            "vram_gb": w["vram_gb"],
            "free_vram_gb": w["free_vram_gb"],
            "status": w["status"],
            "loaded_model": w.get("loaded_model", ""),
            "current_job": w.get("current_job"),
            "last_heartbeat": w.get("last_heartbeat", 0),
        })
    return JSONResponse({"workers": worker_list, "total": len(worker_list)})


@app.post("/api/v1/workers/register")
async def register_worker(worker: WorkerInfo):
    """Register a ComfyUI worker with the dispatcher."""
    workers[worker.id] = worker.model_dump()
    workers[worker.id]["last_heartbeat"] = time.time()
    workers[worker.id]["status"] = "idle"
    await _redis_save_worker(worker.id, workers[worker.id])
    return JSONResponse({"status": "registered", "worker_id": worker.id})


@app.post("/api/v1/workers/{worker_id}/heartbeat")
async def worker_heartbeat(worker_id: str):
    """Worker heartbeat to signal it's still alive."""
    if worker_id in workers:
        workers[worker_id]["last_heartbeat"] = time.time()
        await _redis_save_worker(worker_id, workers[worker_id])
        return JSONResponse({"status": "ok"})
    raise HTTPException(status_code=404, detail="Worker not found")


@app.post("/api/v1/workers/{worker_id}/status")
async def update_worker_status(worker_id: str, status: str = "idle",
                                free_vram_gb: float = None, loaded_model: str = None):
    """Update worker status."""
    if worker_id not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    workers[worker_id]["status"] = status
    workers[worker_id]["last_heartbeat"] = time.time()
    if free_vram_gb is not None:
        workers[worker_id]["free_vram_gb"] = free_vram_gb
    if loaded_model is not None:
        workers[worker_id]["loaded_model"] = loaded_model
    await _redis_save_worker(worker_id, workers[worker_id])
    return JSONResponse({"status": "updated"})


# ── API: Pod Proxy ─────────────────────────────────────────────────

@app.get("/api/v1/pod/status")
async def pod_status():
    """Proxy to Hyperspace Pod status."""
    try:
        data = await _hyperspace_request("/v1/models")
        return JSONResponse({"pod_online": True, "models": data})
    except Exception as e:
        return JSONResponse({"pod_online": False, "error": str(e)})


@app.post("/api/v1/pod/chat")
async def pod_chat(prompt: str, model: str = "auto", system: str = "You are a helpful assistant.",
                   temperature: float = 0.7, max_tokens: int = 2048):
    """Proxy to Hyperspace Pod chat API."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        data = await _hyperspace_request("/v1/chat/completions", method="POST", json_data=payload)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Pod chat failed: {str(e)}")


# ── API: ComfyUI Master Health ─────────────────────────────────────

@app.get("/api/v1/master/health")
async def master_health():
    """Check ComfyUI master health and return recent alerts."""
    return JSONResponse({
        "master_url": COMFYUI_MASTER_URL,
        "healthy": comfyui_master_healthy,
        "last_check": comfyui_master_last_check,
        "recent_alerts": comfyui_master_alerts[-10:],
    })


# ── API: Health ────────────────────────────────────────────────────

@app.get("/api/v1/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workers_registered": len(workers),
        "workers_idle": sum(1 for w in workers.values() if w["status"] == "idle"),
        "jobs_queued": len(job_queue),
        "jobs_active": len(active_jobs),
        "jobs_total": len(jobs),
        "redis_connected": redis_client is not None,
        "auth_enabled": bool(DISPATCHER_API_KEY),
        "comfyui_master_healthy": comfyui_master_healthy,
    })


# ── WebSocket-based ComfyUI Monitoring ─────────────────────────────

async def _monitor_comfyui_ws(prompt_id: str, job: dict, comfyui_url: str, timeout: int = 600):
    """Monitor a ComfyUI job via WebSocket for real-time progress updates.

    Connects to ComfyUI's WebSocket endpoint and listens for:
    - 'progress': step-level progress (value/max)
    - 'executing' with node=None: workflow complete
    - 'execution_success': final confirmation
    - 'execution_error': failure

    Falls back to HTTP polling if WebSocket connection fails.
    """
    try:
        import websockets
    except ImportError:
        logger.debug("websockets not installed — falling back to HTTP polling")
        return await _poll_comfyui_http(prompt_id, job, comfyui_url, timeout)

    ws_url = comfyui_url.replace("http://", "ws://").replace("https://", "wss://")
    client_id = uuid.uuid4().hex

    try:
        async with websockets.connect(
            f"{ws_url}/ws?clientId={client_id}",
            open_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info(f"WebSocket connected for job {job['id']} (prompt {prompt_id})")

            deadline = time.time() + timeout

            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # No message in 30s — check if we should keep waiting
                    continue

                # Skip binary frames (preview images)
                if isinstance(raw, bytes):
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                data = msg.get("data", {})

                # Filter to our prompt_id where applicable
                if "prompt_id" in data and data["prompt_id"] != prompt_id:
                    continue

                if msg_type == "progress":
                    value = data.get("value", 0)
                    max_val = data.get("max", 1)
                    if max_val > 0:
                        pct = int((value / max_val) * 90)  # cap at 90% until download
                        job["progress"] = pct
                        await _redis_save_job(job["id"], job)

                elif msg_type == "executing":
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        # Workflow execution complete — download result
                        job["progress"] = 95
                        return

                elif msg_type == "execution_success":
                    if data.get("prompt_id") == prompt_id:
                        job["progress"] = 95
                        return

                elif msg_type == "execution_error":
                    if data.get("prompt_id") == prompt_id:
                        error_msg = data.get("exception_message", "ComfyUI execution error")
                        raise Exception(error_msg)

            raise Exception(f"WebSocket monitoring timed out after {timeout}s")

    except ImportError:
        return await _poll_comfyui_http(prompt_id, job, comfyui_url, timeout)
    except Exception as e:
        if "WebSocket" in str(type(e).__name__) or "connect" in str(e).lower():
            logger.warning(f"WebSocket failed ({e}), falling back to HTTP polling")
            return await _poll_comfyui_http(prompt_id, job, comfyui_url, timeout)
        raise


async def _poll_comfyui_http(prompt_id: str, job: dict, comfyui_url: str, timeout: int = 600):
    """Fallback: poll ComfyUI HTTP API for job completion."""
    iterations = timeout // 2  # poll every 2 seconds
    for _ in range(iterations):
        await asyncio.sleep(2)
        try:
            history = await _comfyui_request(f"/history/{prompt_id}", timeout=10)
            if prompt_id in history:
                job["progress"] = 95
                return
            job["progress"] = min(job.get("progress", 0) + 2, 90)
        except Exception:
            job["progress"] = min(job.get("progress", 0) + 1, 90)

    raise Exception("Job monitoring timed out (HTTP polling)")


# ── Background Processing ──────────────────────────────────────────

async def _process_job(job_id: str):
    """Process a job from the queue using the assigned ComfyUI worker."""
    job = jobs.get(job_id)
    if not job:
        return

    job["status"] = JobStatus.RUNNING.value
    job["started_at"] = time.time()
    active_jobs.add(job_id)
    await _redis_save_job(job_id, job)

    try:
        worker_id = job.get("worker_id")
        if not worker_id or worker_id not in workers:
            raise Exception("No available worker")

        worker = workers[worker_id]
        worker["status"] = "busy"
        worker["current_job"] = job_id
        await _redis_save_worker(worker_id, worker)

        comfyui_url = worker.get("comfyui_url", COMFYUI_MASTER_URL)

        if job["type"] == "video":
            await _process_video_job(job, comfyui_url)
        elif job["type"] == "image":
            await _process_image_job(job, comfyui_url)
        elif job["type"] == "upscale":
            await _process_upscale_job(job, comfyui_url)

        job["status"] = JobStatus.COMPLETED.value
        job["progress"] = 100
        job["completed_at"] = time.time()
        logger.info(f"✅ Job {job_id} completed successfully")

    except Exception as e:
        logger.warning(f"❌ Job {job_id} failed: {e}")
        await _handle_job_failure(job_id, job, str(e))

    finally:
        active_jobs.discard(job_id)
        worker_id = job.get("worker_id")
        if worker_id and worker_id in workers:
            workers[worker_id]["status"] = "idle"
            workers[worker_id]["current_job"] = None
            await _redis_save_worker(worker_id, workers[worker_id])

        await _redis_save_job(job_id, job)

        # Process next job in queue
        if job_queue:
            next_job_id = job_queue.pop(0)
            asyncio.create_task(_process_job(next_job_id))


async def _handle_job_failure(job_id: str, job: dict, error: str):
    """Handle job failure with retry logic.

    If retry_count < max_retries, re-assign the job to a different worker.
    Otherwise, mark as permanently failed.
    """
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", MAX_RETRIES)
    failed_workers = job.get("failed_workers", [])

    # Track which worker failed
    current_worker = job.get("worker_id")
    if current_worker and current_worker not in failed_workers:
        failed_workers.append(current_worker)
        job["failed_workers"] = failed_workers

    if retry_count < max_retries:
        # Try to find a different worker
        vram_map = {
            "video": {"wan2.2": 24, "ltx-video": 12, "hunyuan": 48, "auto": 16},
            "image": {"auto": 8, "sdxl": 8},
            "upscale": {"1080p": 16, "2K": 24, "4K": 48},
        }
        job_type = job.get("type", "video")
        model = job.get("model", "auto")

        if job_type == "upscale":
            vram_needed = vram_map["upscale"].get(job.get("target_resolution", "1080p"), 16)
        elif job_type == "image":
            vram_needed = 8
        else:
            vram_needed = vram_map.get("video", {}).get(model, 16)

        new_worker = _select_best_worker(vram_needed, model, exclude_ids=set(failed_workers))

        if new_worker:
            job["retry_count"] = retry_count + 1
            job["worker_id"] = new_worker
            job["status"] = JobStatus.QUEUED.value
            job["error"] = None
            job["progress"] = 0
            logger.info(
                f"🔄 Retrying job {job_id} on worker {new_worker} "
                f"(attempt {retry_count + 1}/{max_retries})"
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            asyncio.create_task(_process_job(job_id))
            return

    # No retries left or no alternative worker available
    job["status"] = JobStatus.FAILED.value
    job["error"] = error
    if retry_count > 0:
        job["error"] = f"{error} (failed after {retry_count + 1} attempts)"


async def _process_video_job(job: dict, comfyui_url: str):
    """Send video generation to ComfyUI and download the result."""
    # Build ComfyUI workflow based on model
    model = job["model"]
    if model == "auto":
        model = "ltx-video"  # safest default for most GPUs

    # Load the appropriate workflow template using explicit mapping
    workflow_filename = WORKFLOW_FILES.get(model)
    if workflow_filename:
        workflow_path = Path(__file__).parent.parent / "comfyui_workflows" / workflow_filename
    else:
        workflow_path = Path(__file__).parent.parent / "comfyui_workflows" / "generic_t2v.json"

    if not workflow_path.exists():
        # Ultimate fallback to generic
        workflow_path = Path(__file__).parent.parent / "comfyui_workflows" / "generic_t2v.json"

    try:
        with open(workflow_path) as f:
            workflow = json.load(f)
    except FileNotFoundError:
        # Build minimal workflow dynamically
        workflow = _build_dynamic_video_workflow(job)

    # Inject prompt and parameters
    _inject_workflow_params(workflow, job)

    # Submit to ComfyUI
    prompt_resp = await _comfyui_request(
        "/prompt",
        method="POST",
        json_data={"prompt": workflow},
        timeout=30,
    )
    prompt_id = prompt_resp.get("prompt_id")

    # Monitor via WebSocket (with HTTP polling fallback)
    video_timeout = config.get("limits", {}).get("video_timeout_seconds", 600)
    await _monitor_comfyui_ws(prompt_id, job, comfyui_url, timeout=video_timeout)

    # Download the result
    await _download_comfyui_result(prompt_id, job, comfyui_url, media_type="video")


async def _process_image_job(job: dict, comfyui_url: str):
    """Send image generation to ComfyUI."""
    workflow = _build_dynamic_image_workflow(job)
    _inject_workflow_params(workflow, job)

    prompt_resp = await _comfyui_request(
        "/prompt",
        method="POST",
        json_data={"prompt": workflow},
    )
    prompt_id = prompt_resp.get("prompt_id")

    image_timeout = config.get("limits", {}).get("image_timeout_seconds", 120)
    await _monitor_comfyui_ws(prompt_id, job, comfyui_url, timeout=image_timeout)
    await _download_comfyui_result(prompt_id, job, comfyui_url, media_type="image")


async def _process_upscale_job(job: dict, comfyui_url: str):
    """Process a video upscale job through ComfyUI-Distributed."""
    workflow = {
        "1": {"inputs": {"video_url": job["video_url"]}, "class_type": "LoadVideo"},
        "2": {
            "inputs": {
                "image": ["1", 0],
                "target_resolution": job["target_resolution"],
                "style": job["style"],
            },
            "class_type": "UltimateSDUpscaleDistributed",
        },
        "3": {
            "inputs": {"filename_prefix": f"upscaled_{uuid.uuid4().hex[:8]}", "images": ["2", 0]},
            "class_type": "SaveVideo",
        },
    }

    prompt_resp = await _comfyui_request(
        "/prompt",
        method="POST",
        json_data={"prompt": workflow},
    )
    prompt_id = prompt_resp.get("prompt_id")

    video_timeout = config.get("limits", {}).get("video_timeout_seconds", 600)
    await _monitor_comfyui_ws(prompt_id, job, comfyui_url, timeout=video_timeout)
    await _download_comfyui_result(prompt_id, job, comfyui_url, media_type="video")


async def _download_comfyui_result(prompt_id: str, job: dict, comfyui_url: str, media_type: str = "video"):
    """Download the output file from ComfyUI after job completion."""
    history = await _comfyui_request(f"/history/{prompt_id}", timeout=30)

    if prompt_id not in history:
        raise Exception("Job completed but no history found")

    outputs = history[prompt_id].get("outputs", {})
    for node_id, node_output in outputs.items():
        if media_type == "video":
            media_list = node_output.get("gifs") or node_output.get("videos") or []
        else:
            media_list = node_output.get("images") or []

        if media_list:
            filename = media_list[0]["filename"]
            subfolder = media_list[0].get("subfolder", "")
            view_url = f"{comfyui_url}/view"
            params = {"filename": filename, "subfolder": subfolder, "type": "output"}

            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300)) as client:
                dl_resp = await client.get(view_url, params=params)
                output_path = OUTPUT_DIR / filename
                output_path.write_bytes(dl_resp.content)
                job["result_url"] = f"/api/v1/output/{filename}"
                job["file_size_mb"] = round(output_path.stat().st_size / (1024 * 1024), 2)
                if media_type == "video":
                    job["duration_s"] = job.get("duration", 5)
            return

    raise Exception(f"No {media_type} output found in ComfyUI history")


@app.get("/api/v1/output/{filename}")
async def serve_output(filename: str):
    """Serve a generated file."""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    filepath = OUTPUT_DIR / safe_name
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath)


# ── Workflow Builders ──────────────────────────────────────────────

def _build_dynamic_video_workflow(job: dict) -> dict:
    """Build a basic ComfyUI video workflow dynamically."""
    prompt = job.get("prompt", "")
    neg = job.get("negative_prompt", "")
    duration = job.get("duration", 5)
    resolution = job.get("resolution", "720p")
    fps = job.get("fps", 24)
    seed = job.get("seed", int(time.time() * 1000) % 2**31)

    w, h = {"480p": (640, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(resolution, (1280, 720))
    frames = int(duration * fps)

    return {
        "1": {"inputs": {"text": prompt}, "class_type": "CLIPTextEncode"},
        "2": {"inputs": {"text": neg or "blurry, low quality, distorted"}, "class_type": "CLIPTextEncode"},
        "3": {
            "inputs": {
                "width": w, "height": h, "num_frames": frames, "seed": seed,
                "positive": ["1", 0], "negative": ["2", 0],
            },
            "class_type": "WanVideoSampler",
        },
        "4": {
            "inputs": {
                "filename_prefix": f"video_{uuid.uuid4().hex[:8]}",
                "samples": ["3", 0],
            },
            "class_type": "SaveVideo",
        },
    }


def _build_dynamic_image_workflow(job: dict) -> dict:
    """Build a basic ComfyUI image workflow."""
    prompt = job.get("prompt", "")
    neg = job.get("negative_prompt", "")
    w, h = job.get("width", 1024), job.get("height", 1024)
    seed = job.get("seed", int(time.time() * 1000) % 2**31)

    return {
        "1": {"inputs": {"text": prompt}, "class_type": "CLIPTextEncode"},
        "2": {"inputs": {"text": neg or "blurry, low quality"}, "class_type": "CLIPTextEncode"},
        "3": {
            "inputs": {"width": w, "height": h, "seed": seed, "positive": ["1", 0], "negative": ["2", 0]},
            "class_type": "KSampler",
        },
        "4": {
            "inputs": {"filename_prefix": f"img_{uuid.uuid4().hex[:8]}", "images": ["3", 0]},
            "class_type": "SaveImage",
        },
    }


def _inject_workflow_params(workflow: dict, job: dict):
    """Inject job parameters into a ComfyUI workflow."""
    for node_id, node in workflow.items():
        if "inputs" not in node:
            continue
        cls = node.get("class_type", "")

        # Inject prompt into text encode nodes
        if cls == "CLIPTextEncode" and "prompt" in job:
            txt = node["inputs"].get("text", "")
            if (txt and "positive" in node_id.lower()) or (txt and node_id == "1"):
                node["inputs"]["text"] = job["prompt"]
            elif (txt and "negative" in node_id.lower()) or (txt and node_id == "2"):
                node["inputs"]["text"] = job.get("negative_prompt", "")

        # Inject seed
        if "seed" in node["inputs"] and "seed" in job:
            node["inputs"]["seed"] = job["seed"] or int(time.time() * 1000) % 2**31


# ── Background Health Loops ────────────────────────────────────────

async def _health_check_loop():
    """Periodically check worker health and mark offline workers."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for wid, w in list(workers.items()):
            if now - w.get("last_heartbeat", 0) > 120:
                if w["status"] != "offline":
                    logger.warning(f"⚠️  Worker {w.get('name', wid)} went offline")
                w["status"] = "offline"

                # Re-queue any jobs from this worker (retry logic)
                if w.get("current_job") and w["current_job"] in jobs:
                    job = jobs[w["current_job"]]
                    if job["status"] == JobStatus.RUNNING.value:
                        logger.warning(f"🔄 Worker offline — triggering retry for job {w['current_job']}")
                        await _handle_job_failure(
                            w["current_job"], job, "Worker went offline during processing"
                        )
                        w["current_job"] = None

                await _redis_save_worker(wid, w)


async def _comfyui_master_health_loop():
    """Periodically check ComfyUI master health and emit alerts."""
    global comfyui_master_healthy, comfyui_master_last_check

    was_healthy = True

    while True:
        await asyncio.sleep(30)
        comfyui_master_last_check = time.time()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0)) as client:
                resp = await client.get(f"{COMFYUI_MASTER_URL}/system_stats")
                resp.raise_for_status()

            if not was_healthy:
                # Master recovered
                alert = {
                    "type": "master_recovered",
                    "message": "✅ ComfyUI master is back online",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                comfyui_master_alerts.append(alert)
                logger.info(alert["message"])

            comfyui_master_healthy = True
            was_healthy = True

        except Exception as e:
            comfyui_master_healthy = False

            if was_healthy:
                # Master just went down — first alert
                alert = {
                    "type": "master_down",
                    "message": f"🚨 ComfyUI master is DOWN — video/image generation unavailable. Error: {e}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "master_url": COMFYUI_MASTER_URL,
                }
                comfyui_master_alerts.append(alert)
                logger.error(alert["message"])

            was_healthy = False

        # Keep alert history manageable
        if len(comfyui_master_alerts) > 100:
            comfyui_master_alerts[:] = comfyui_master_alerts[-50:]


@app.on_event("startup")
async def startup():
    # Initialize Redis (optional)
    await _init_redis()
    await _redis_hydrate()

    # Start background health loops
    asyncio.create_task(_health_check_loop())
    asyncio.create_task(_comfyui_master_health_loop())

    logger.info("🚀 OpenMind Dispatcher v2.0 started")
    logger.info(f"   Hyperspace API: {HYPERSPACE_API_URL}")
    logger.info(f"   ComfyUI Master: {COMFYUI_MASTER_URL}")
    logger.info(f"   Output dir:     {OUTPUT_DIR}")
    logger.info(f"   Auth:           {'enabled' if DISPATCHER_API_KEY else 'disabled'}")
    logger.info(f"   Redis:          {'connected' if redis_client else 'not available (in-memory mode)'}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")

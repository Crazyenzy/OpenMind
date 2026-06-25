"""
Shared test fixtures for OpenMind tests.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the dispatcher to the path
sys.path.insert(0, str(Path(__file__).parent.parent / "dispatcher"))


@pytest.fixture(autouse=True)
def _disable_redis(monkeypatch):
    """Disable Redis for all tests — use in-memory state."""
    monkeypatch.setattr("main.redis_client", None)


@pytest.fixture
def sample_workers():
    """Pre-built worker data for testing."""
    return {
        "w1": {
            "id": "w1",
            "name": "Bhupesh-RTX4090",
            "gpu": "RTX 4090",
            "vram_gb": 24,
            "free_vram_gb": 24,
            "status": "idle",
            "loaded_model": "wan2.2",
            "current_job": None,
            "last_heartbeat": time.time(),
            "comfyui_url": "http://100.64.0.1:8188",
        },
        "w2": {
            "id": "w2",
            "name": "Nidhi-MacM4",
            "gpu": "Apple M4",
            "vram_gb": 16,
            "free_vram_gb": 16,
            "status": "idle",
            "loaded_model": "ltx-video",
            "current_job": None,
            "last_heartbeat": time.time(),
            "comfyui_url": "http://100.64.0.2:8188",
        },
        "w3": {
            "id": "w3",
            "name": "Snehal-RTX3090",
            "gpu": "RTX 3090",
            "vram_gb": 24,
            "free_vram_gb": 24,
            "status": "idle",
            "loaded_model": "",
            "current_job": None,
            "last_heartbeat": time.time(),
            "comfyui_url": "http://100.64.0.3:8188",
        },
    }


@pytest.fixture
def busy_worker():
    """A worker that's currently busy."""
    return {
        "id": "w_busy",
        "name": "Busy-Worker",
        "gpu": "RTX 4090",
        "vram_gb": 24,
        "free_vram_gb": 0,
        "status": "busy",
        "loaded_model": "wan2.2",
        "current_job": "vid_test123",
        "last_heartbeat": time.time(),
        "comfyui_url": "http://100.64.0.4:8188",
    }


@pytest.fixture
def offline_worker():
    """A worker that hasn't sent a heartbeat in a long time."""
    return {
        "id": "w_offline",
        "name": "Offline-Worker",
        "gpu": "RTX 3060",
        "vram_gb": 12,
        "free_vram_gb": 12,
        "status": "idle",
        "loaded_model": "",
        "current_job": None,
        "last_heartbeat": time.time() - 300,  # 5 minutes ago
        "comfyui_url": "http://100.64.0.5:8188",
    }


@pytest.fixture
def sample_video_job():
    """A sample video job dict."""
    return {
        "id": "vid_test123abc",
        "type": "video",
        "status": "queued",
        "prompt": "A sunset over mountains",
        "model": "wan2.2",
        "duration": 5,
        "resolution": "720p",
        "fps": 24,
        "negative_prompt": "",
        "seed": 42,
        "image_url": None,
        "num_variations": 1,
        "worker_id": "w1",
        "workers_used": 1,
        "estimated_time_seconds": 50,
        "progress": 0,
        "result_url": None,
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "retry_count": 0,
        "max_retries": 2,
        "failed_workers": [],
    }

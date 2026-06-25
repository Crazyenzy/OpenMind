"""
test_dispatcher.py — Tests for the OpenMind Dispatcher

Tests cover:
- Worker selection logic (VRAM filtering, model affinity, exclusions)
- Job lifecycle (submit → queued → completed/failed)
- Workflow injection (operator precedence fix verification)
- Workflow filename mapping
- Auth middleware (valid key, invalid key, missing key, health bypass)
- Job retry logic
- Video time estimation
- Health endpoint
- ComfyUI master health tracking
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure dispatcher is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "dispatcher"))

# We need to patch config loading before importing main
with patch("builtins.open", create=True):
    pass

import main
from main import (
    _select_best_worker,
    _estimate_video_time,
    _inject_workflow_params,
    _build_dynamic_video_workflow,
    _build_dynamic_image_workflow,
    WORKFLOW_FILES,
    app,
)


# ── Worker Selection ────────────────────────────────────────────────

class TestWorkerSelection:
    """Tests for _select_best_worker()."""

    def setup_method(self):
        main.workers.clear()

    def teardown_method(self):
        main.workers.clear()

    def test_selects_idle_worker_with_enough_vram(self, sample_workers):
        main.workers.update(sample_workers)
        result = _select_best_worker(24)
        assert result in ("w1", "w3")  # Both have 24GB idle

    def test_skips_workers_with_insufficient_vram(self, sample_workers):
        main.workers.update(sample_workers)
        result = _select_best_worker(48)
        assert result is None  # No worker has 48GB

    def test_skips_busy_workers(self, sample_workers, busy_worker):
        main.workers.update(sample_workers)
        main.workers["w_busy"] = busy_worker
        # Make all idle workers low VRAM except busy one
        main.workers["w1"]["free_vram_gb"] = 4
        main.workers["w2"]["free_vram_gb"] = 4
        main.workers["w3"]["free_vram_gb"] = 4
        result = _select_best_worker(24)
        assert result is None  # Busy worker skipped, others too small

    def test_prefers_model_affinity(self, sample_workers):
        main.workers.update(sample_workers)
        # w1 has "wan2.2" loaded, w3 has nothing
        result = _select_best_worker(24, model_preference="wan2.2")
        assert result == "w1"  # Exact model match scored highest

    def test_partial_model_match(self, sample_workers):
        main.workers.update(sample_workers)
        main.workers["w1"]["loaded_model"] = "wan2.2-full"
        result = _select_best_worker(24, model_preference="wan2.2")
        assert result == "w1"  # Partial match still scores

    def test_excludes_specified_workers(self, sample_workers):
        main.workers.update(sample_workers)
        result = _select_best_worker(24, exclude_ids={"w1", "w3"})
        assert result is None  # w2 only has 16GB, not enough

    def test_returns_none_when_no_workers(self):
        result = _select_best_worker(8)
        assert result is None

    def test_prefers_more_free_vram(self, sample_workers):
        main.workers.update(sample_workers)
        # Remove model affinity advantage
        main.workers["w1"]["loaded_model"] = ""
        main.workers["w3"]["loaded_model"] = ""
        main.workers["w1"]["free_vram_gb"] = 24
        main.workers["w3"]["free_vram_gb"] = 20
        result = _select_best_worker(16)
        assert result == "w1"  # More free VRAM wins as tiebreaker


# ── Video Time Estimation ───────────────────────────────────────────

class TestVideoTimeEstimation:
    """Tests for _estimate_video_time()."""

    def test_base_estimate(self):
        result = _estimate_video_time(5.0, "720p", "wan2.2")
        assert result == 50.0  # 5s * 10

    def test_1080p_multiplier(self):
        result = _estimate_video_time(5.0, "1080p", "wan2.2")
        assert result == 125.0  # 5s * 10 * 2.5

    def test_480p_multiplier(self):
        result = _estimate_video_time(5.0, "480p", "wan2.2")
        assert result == 25.0  # 5s * 10 * 0.5

    def test_ltx_faster(self):
        result = _estimate_video_time(5.0, "720p", "ltx-video")
        assert result == 20.0  # 5s * 10 * 0.4

    def test_hunyuan_slower(self):
        result = _estimate_video_time(5.0, "720p", "hunyuan")
        assert result == 150.0  # 5s * 10 * 3.0


# ── Workflow Injection ──────────────────────────────────────────────

class TestWorkflowInjection:
    """Tests for _inject_workflow_params() — verifies operator precedence fix."""

    def test_injects_prompt_into_node_1(self):
        workflow = {
            "1": {"inputs": {"text": "PROMPT_PLACEHOLDER"}, "class_type": "CLIPTextEncode"},
            "2": {"inputs": {"text": "NEGATIVE_PLACEHOLDER"}, "class_type": "CLIPTextEncode"},
        }
        job = {"prompt": "A beautiful sunset", "negative_prompt": "blurry"}
        _inject_workflow_params(workflow, job)
        assert workflow["1"]["inputs"]["text"] == "A beautiful sunset"
        assert workflow["2"]["inputs"]["text"] == "blurry"

    def test_does_not_inject_when_text_is_empty(self):
        """Verifies the operator precedence fix: empty txt should NOT trigger injection."""
        workflow = {
            "1": {"inputs": {"text": ""}, "class_type": "CLIPTextEncode"},
        }
        job = {"prompt": "test prompt"}
        _inject_workflow_params(workflow, job)
        # With the bug, node "1" would be overwritten even when txt is empty.
        # With the fix, empty txt means no injection.
        assert workflow["1"]["inputs"]["text"] == ""

    def test_injects_seed(self):
        workflow = {
            "3": {"inputs": {"seed": 0, "width": 1024}, "class_type": "KSampler"},
        }
        job = {"seed": 42}
        _inject_workflow_params(workflow, job)
        assert workflow["3"]["inputs"]["seed"] == 42

    def test_seed_fallback_when_none(self):
        workflow = {
            "3": {"inputs": {"seed": 0}, "class_type": "KSampler"},
        }
        job = {"seed": None}
        _inject_workflow_params(workflow, job)
        assert workflow["3"]["inputs"]["seed"] != 0  # Should be replaced with timestamp-based seed

    def test_named_positive_negative_nodes(self):
        """Test injection with descriptively named nodes."""
        workflow = {
            "positive_1": {"inputs": {"text": "old prompt"}, "class_type": "CLIPTextEncode"},
            "negative_1": {"inputs": {"text": "old negative"}, "class_type": "CLIPTextEncode"},
        }
        job = {"prompt": "new prompt", "negative_prompt": "new negative"}
        _inject_workflow_params(workflow, job)
        assert workflow["positive_1"]["inputs"]["text"] == "new prompt"
        assert workflow["negative_1"]["inputs"]["text"] == "new negative"


# ── Workflow Filename Mapping ───────────────────────────────────────

class TestWorkflowFileMapping:
    """Tests for the WORKFLOW_FILES mapping."""

    def test_all_supported_models_have_mapping(self):
        expected_models = ["wan2.2", "ltx-video", "hunyuan"]
        for model in expected_models:
            assert model in WORKFLOW_FILES, f"Missing workflow mapping for '{model}'"

    def test_mapping_produces_valid_filenames(self):
        for model, filename in WORKFLOW_FILES.items():
            assert filename.endswith(".json"), f"Workflow file for '{model}' doesn't end with .json"
            assert "/" not in filename, f"Workflow file for '{model}' contains path separator"

    def test_ltx_video_mapping(self):
        """The original bug: ltx-video.replace('.', '') produced 'ltx-video', not 'ltx_video'."""
        assert WORKFLOW_FILES["ltx-video"] == "ltx_video.json"

    def test_wan22_mapping(self):
        assert WORKFLOW_FILES["wan2.2"] == "wan22_t2v.json"

    def test_hunyuan_mapping(self):
        assert WORKFLOW_FILES["hunyuan"] == "hunyuan_video.json"


# ── Dynamic Workflow Builders ───────────────────────────────────────

class TestWorkflowBuilders:

    def test_video_workflow_has_required_nodes(self):
        job = {"prompt": "test", "duration": 5, "resolution": "720p", "fps": 24}
        workflow = _build_dynamic_video_workflow(job)
        assert "1" in workflow  # positive prompt
        assert "2" in workflow  # negative prompt
        assert "3" in workflow  # sampler
        assert "4" in workflow  # save

    def test_video_workflow_correct_resolution(self):
        job = {"prompt": "test", "resolution": "1080p", "fps": 24}
        workflow = _build_dynamic_video_workflow(job)
        assert workflow["3"]["inputs"]["width"] == 1920
        assert workflow["3"]["inputs"]["height"] == 1080

    def test_image_workflow_has_required_nodes(self):
        job = {"prompt": "test", "width": 1024, "height": 1024}
        workflow = _build_dynamic_image_workflow(job)
        assert "1" in workflow
        assert "2" in workflow
        assert "3" in workflow
        assert "4" in workflow

    def test_image_workflow_respects_dimensions(self):
        job = {"prompt": "test", "width": 512, "height": 768}
        workflow = _build_dynamic_image_workflow(job)
        assert workflow["3"]["inputs"]["width"] == 512
        assert workflow["3"]["inputs"]["height"] == 768


# ── Auth Middleware ─────────────────────────────────────────────────

class TestAuthMiddleware:

    @pytest.fixture
    def client(self):
        """Create a test client."""
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_health_bypasses_auth(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "secret-key")
        async with client as c:
            resp = await c.get("/api/v1/health")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_auth_disabled_when_key_empty(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/workers")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "my-secret")
        async with client as c:
            resp = await c.get(
                "/api/v1/workers",
                headers={"Authorization": "Bearer my-secret"},
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_api_key_header(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "my-secret")
        async with client as c:
            resp = await c.get(
                "/api/v1/workers",
                headers={"X-API-Key": "my-secret"},
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "my-secret")
        async with client as c:
            resp = await c.get(
                "/api/v1/workers",
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_auth_rejected(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "my-secret")
        async with client as c:
            resp = await c.get("/api/v1/workers")
            assert resp.status_code == 401


# ── Job Retry Logic ─────────────────────────────────────────────────

class TestJobRetry:

    def setup_method(self):
        main.workers.clear()
        main.jobs.clear()

    def teardown_method(self):
        main.workers.clear()
        main.jobs.clear()

    @pytest.mark.asyncio
    async def test_retry_assigns_different_worker(self, sample_workers, sample_video_job):
        main.workers.update(sample_workers)
        main.jobs[sample_video_job["id"]] = sample_video_job

        with patch("main.asyncio.create_task"):
            with patch("main.asyncio.sleep", new_callable=AsyncMock):
                await main._handle_job_failure(
                    sample_video_job["id"],
                    sample_video_job,
                    "Worker crashed",
                )

        # Should have retried
        assert sample_video_job["retry_count"] == 1
        assert sample_video_job["status"] == "queued"
        assert sample_video_job["worker_id"] != "w1"  # Should pick a different worker
        assert "w1" in sample_video_job["failed_workers"]

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, sample_video_job):
        main.jobs[sample_video_job["id"]] = sample_video_job
        sample_video_job["retry_count"] = 2
        sample_video_job["max_retries"] = 2

        await main._handle_job_failure(
            sample_video_job["id"],
            sample_video_job,
            "Worker crashed again",
        )

        assert sample_video_job["status"] == "failed"
        assert "3 attempts" in sample_video_job["error"]

    @pytest.mark.asyncio
    async def test_retry_with_no_alternative_workers(self, sample_video_job):
        # No workers at all
        main.jobs[sample_video_job["id"]] = sample_video_job

        await main._handle_job_failure(
            sample_video_job["id"],
            sample_video_job,
            "Worker crashed",
        )

        assert sample_video_job["status"] == "failed"


# ── API Endpoints ───────────────────────────────────────────────────

class TestAPIEndpoints:

    @pytest.fixture
    def client(self):
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    def setup_method(self):
        main.workers.clear()
        main.jobs.clear()
        main.job_queue.clear()
        main.active_jobs.clear()

    def teardown_method(self):
        main.workers.clear()
        main.jobs.clear()
        main.job_queue.clear()
        main.active_jobs.clear()

    @pytest.mark.asyncio
    async def test_health_returns_status(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "redis_connected" in data
            assert "auth_enabled" in data
            assert "comfyui_master_healthy" in data

    @pytest.mark.asyncio
    async def test_list_workers_empty(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/workers")
            assert resp.status_code == 200
            data = resp.json()
            assert data["workers"] == []
            assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_register_worker(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.post("/api/v1/workers/register", json={
                "id": "test-w1",
                "name": "Test Worker",
                "gpu": "RTX 4090",
                "vram_gb": 24,
                "free_vram_gb": 24,
            })
            assert resp.status_code == 200
            assert main.workers["test-w1"]["name"] == "Test Worker"

    @pytest.mark.asyncio
    async def test_get_nonexistent_job(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/jobs/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_master_health_endpoint(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/master/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "healthy" in data
            assert "master_url" in data

    @pytest.mark.asyncio
    async def test_output_path_traversal_blocked(self, client, monkeypatch):
        monkeypatch.setattr("main.DISPATCHER_API_KEY", "")
        async with client as c:
            resp = await c.get("/api/v1/output/../../../etc/passwd")
            assert resp.status_code == 404  # Path.name strips traversal

"""
test_mcp_servers.py — Tests for OpenMind MCP servers

Tests cover:
- Tool listing returns expected schemas
- Input validation (missing prompt, unknown model)
- Auth header propagation
- Dispatcher URL configuration from env
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── video_gen_server tests ──────────────────────────────────────────

class TestVideoGenServer:
    """Tests for video_gen_server.py."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers"))

    def test_dispatcher_url_from_env(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_URL", "http://custom-host:9999")
        # Re-import to pick up env var
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        assert video_gen_server.DISPATCHER_URL == "http://custom-host:9999"

    def test_dispatcher_url_default(self, monkeypatch):
        monkeypatch.delenv("DISPATCHER_URL", raising=False)
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        assert video_gen_server.DISPATCHER_URL == "http://127.0.0.1:9000"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_API_KEY", "test-key-123")
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        assert video_gen_server.DISPATCHER_API_KEY == "test-key-123"

    def test_auth_headers_with_key(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_API_KEY", "my-key")
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        headers = video_gen_server._dispatcher_headers()
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"

    def test_auth_headers_without_key(self, monkeypatch):
        monkeypatch.delenv("DISPATCHER_API_KEY", raising=False)
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        headers = video_gen_server._dispatcher_headers()
        assert "Authorization" not in headers

    def test_supported_models_defined(self):
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        models = video_gen_server.SUPPORTED_MODELS
        assert "wan2.2" in models
        assert "ltx-video" in models
        assert "hunyuan" in models
        assert "animatediff" in models

    def test_model_metadata_complete(self):
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        for name, info in video_gen_server.SUPPORTED_MODELS.items():
            assert "name" in info, f"Model {name} missing 'name'"
            assert "type" in info, f"Model {name} missing 'type'"
            assert "min_vram_gb" in info, f"Model {name} missing 'min_vram_gb'"
            assert info["type"] in ("t2v", "i2v"), f"Model {name} has invalid type"

    @pytest.mark.asyncio
    async def test_generate_video_missing_prompt(self):
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        result = await video_gen_server._handle_generate_video({})
        assert len(result) == 1
        assert "required" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_generate_video_unknown_model(self):
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        result = await video_gen_server._handle_generate_video({
            "prompt": "test",
            "model": "nonexistent-model",
        })
        assert len(result) == 1
        assert "unknown model" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_generate_video_i2v_with_wrong_model(self):
        if "video_gen_server" in sys.modules:
            del sys.modules["video_gen_server"]
        import video_gen_server
        result = await video_gen_server._handle_generate_video({
            "prompt": "test",
            "model": "ltx-video",
            "image_url": "http://example.com/image.png",
        })
        assert len(result) == 1
        assert "image-to-video" in result[0].text.lower()


# ── pod_manager_server tests ───────────────────────────────────────

class TestPodManagerServer:
    """Tests for pod_manager_server.py."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers"))

    def test_dispatcher_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_API_KEY", "pod-key-456")
        if "pod_manager_server" in sys.modules:
            del sys.modules["pod_manager_server"]
        import pod_manager_server
        assert pod_manager_server.DISPATCHER_API_KEY == "pod-key-456"

    def test_auth_headers_helper(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_API_KEY", "pod-key")
        if "pod_manager_server" in sys.modules:
            del sys.modules["pod_manager_server"]
        import pod_manager_server
        headers = pod_manager_server._auth_headers()
        assert headers["Authorization"] == "Bearer pod-key"

    def test_hyperspace_bin_configurable(self, monkeypatch):
        monkeypatch.setenv("HYPERSPACE_BIN", "/custom/path/hyperspace")
        if "pod_manager_server" in sys.modules:
            del sys.modules["pod_manager_server"]
        import pod_manager_server
        assert pod_manager_server.HYPERSPACE_BIN == "/custom/path/hyperspace"

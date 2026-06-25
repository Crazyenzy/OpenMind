"""
video_gen_server.py — MCP server for distributed video generation

Plugs into the OpenMind MCP framework (installed into Odysseus). Routes video generation requests
through the Dispatcher, which distributes work across ComfyUI-Distributed
workers in the pod.

Supports:
- Text-to-video (Wan 2.2, LTX-Video, Hunyuan Video)
- Image-to-video
- Video upscaling (distributed tile-based)
- Job status polling
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Allow importing from parent (Odysseus source)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("video_gen")

# --- Configuration (override via env vars) ---
DISPATCHER_URL = os.environ.get("DISPATCHER_URL", "http://127.0.0.1:9000")
COMFYUI_MASTER_URL = os.environ.get("COMFYUI_MASTER_URL", "http://127.0.0.1:8188")
DISPATCHER_API_KEY = os.environ.get("DISPATCHER_API_KEY", "")

SUPPORTED_MODELS = {
    "wan2.2": {
        "name": "Wan 2.2",
        "type": "t2v",  # text-to-video
        "min_vram_gb": 24,
        "max_duration_s": 30,
        "resolutions": ["480p", "720p", "1080p"],
        "fps_options": [16, 24, 30],
    },
    "wan2.2-i2v": {
        "name": "Wan 2.2 (Image-to-Video)",
        "type": "i2v",
        "min_vram_gb": 24,
        "max_duration_s": 15,
        "resolutions": ["480p", "720p"],
        "fps_options": [16, 24],
    },
    "ltx-video": {
        "name": "LTX-Video",
        "type": "t2v",
        "min_vram_gb": 12,
        "max_duration_s": 10,
        "resolutions": ["480p", "720p"],
        "fps_options": [24],
    },
    "hunyuan": {
        "name": "Hunyuan Video",
        "type": "t2v",
        "min_vram_gb": 48,
        "max_duration_s": 60,
        "resolutions": ["720p", "1080p"],
        "fps_options": [24, 30, 60],
    },
    "cogvideox": {
        "name": "CogVideoX-5B",
        "type": "t2v",
        "min_vram_gb": 16,
        "max_duration_s": 6,
        "resolutions": ["480p", "720p"],
        "fps_options": [8, 16],
    },
    "animatediff": {
        "name": "AnimateDiff (SDXL-based)",
        "type": "t2v",
        "min_vram_gb": 8,
        "max_duration_s": 8,
        "resolutions": ["480p"],
        "fps_options": [8, 16],
    },
}


def _dispatcher_headers() -> dict:
    """Build request headers with auth if configured."""
    headers = {"Content-Type": "application/json"}
    if DISPATCHER_API_KEY:
        headers["Authorization"] = f"Bearer {DISPATCHER_API_KEY}"
    return headers


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_video",
            description="Generate a video clip using distributed GPU workers in the pod. Supports text-to-video and image-to-video.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the video to generate",
                    },
                    "model": {
                        "type": "string",
                        "description": f"Video model to use. Options: {', '.join(SUPPORTED_MODELS.keys())}. Auto-selects based on available GPUs if omitted.",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Video duration in seconds (default: 5, max depends on model)",
                    },
                    "resolution": {
                        "type": "string",
                        "description": "Output resolution: 480p, 720p, or 1080p (default: 720p)",
                    },
                    "fps": {
                        "type": "integer",
                        "description": "Frames per second (default depends on model)",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "What to avoid in the generated video",
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Random seed for reproducibility (random if omitted)",
                    },
                    "image_url": {
                        "type": "string",
                        "description": "URL or path to an input image for image-to-video models",
                    },
                    "num_variations": {
                        "type": "integer",
                        "description": "Number of parallel variations to generate across workers (default: 1)",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="check_video_status",
            description="Check the status of a video generation job. Returns progress and result URL when complete.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID returned from generate_video",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="list_video_workers",
            description="List all available GPU workers in the ComfyUI cluster, their VRAM, loaded models, and current status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="upscale_video",
            description="Upscale an existing video using distributed tile-based processing across workers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "video_url": {
                        "type": "string",
                        "description": "URL or path to the video to upscale",
                    },
                    "target_resolution": {
                        "type": "string",
                        "description": "Target resolution: 1080p, 2K, or 4K (default: 1080p)",
                    },
                    "style": {
                        "type": "string",
                        "description": "Upscaling style: fidelity (sharp detail) or artistic (creative enhancement)",
                    },
                },
                "required": ["video_url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        import httpx
    except ImportError:
        return [TextContent(type="text", text="Error: httpx is required. Install with: pip install httpx")]

    if name == "generate_video":
        return await _handle_generate_video(arguments)
    elif name == "check_video_status":
        return await _handle_check_status(arguments)
    elif name == "list_video_workers":
        return await _handle_list_workers()
    elif name == "upscale_video":
        return await _handle_upscale_video(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_generate_video(args: dict) -> list[TextContent]:
    prompt = args.get("prompt", "")
    if not prompt:
        return [TextContent(type="text", text="Error: 'prompt' is required.")]

    model = args.get("model", "")
    duration = args.get("duration", 5)
    resolution = args.get("resolution", "720p")
    fps = args.get("fps")
    negative_prompt = args.get("negative_prompt", "")
    seed = args.get("seed")
    image_url = args.get("image_url")
    num_variations = args.get("num_variations", 1)

    # Validate model
    if model and model not in SUPPORTED_MODELS:
        return [TextContent(
            type="text",
            text=f"Error: Unknown model '{model}'. Supported: {', '.join(SUPPORTED_MODELS.keys())}"
        )]

    if image_url and model and SUPPORTED_MODELS.get(model, {}).get("type") != "i2v":
        return [TextContent(
            type="text",
            text=f"Error: Model '{model}' does not support image-to-video. Use: wan2.2-i2v"
        )]

    payload = {
        "job_type": "video",
        "prompt": prompt,
        "model": model or "auto",
        "duration": duration,
        "resolution": resolution,
        "fps": fps,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "image_url": image_url,
        "num_variations": num_variations,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=30.0)) as client:
            resp = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/video",
                json=payload,
                headers=_dispatcher_headers(),
            )
            if resp.status_code == 401:
                return [TextContent(type="text", text="Error: Authentication failed. Check DISPATCHER_API_KEY.")]
            if resp.status_code != 200:
                err = resp.text[:500]
                try:
                    err = resp.json().get("detail", err)
                except Exception:
                    pass
                return [TextContent(type="text", text=f"Error: Video generation request failed: {err}")]

            data = resp.json()
            job_id = data.get("job_id")
            workers_used = data.get("workers_used", 0)
            est_time = data.get("estimated_time_seconds", "unknown")

            return [TextContent(type="text", text=(
                f"✅ Video generation started!\n"
                f"Job ID: {job_id}\n"
                f"Prompt: \"{prompt}\"\n"
                f"Model: {data.get('model', 'auto')}\n"
                f"Duration: {duration}s\n"
                f"Resolution: {resolution}\n"
                f"Workers assigned: {workers_used}\n"
                f"Estimated time: {est_time}s\n\n"
                f"Check progress with: check_video_status(job_id=\"{job_id}\")\n"
                f"Results will be available at: {data.get('result_url', 'pending')}"
            ))]
    except httpx.ConnectError:
        return [TextContent(type="text", text=(
            "Error: Cannot reach the video dispatcher. Make sure the pod dispatcher "
            f"service is running at {DISPATCHER_URL}."
        ))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: Video generation failed: {str(e)}")]


async def _handle_check_status(args: dict) -> list[TextContent]:
    job_id = args.get("job_id", "")
    if not job_id:
        return [TextContent(type="text", text="Error: 'job_id' is required.")]

    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0)) as client:
            resp = await client.get(
                f"{DISPATCHER_URL}/api/v1/jobs/{job_id}",
                headers=_dispatcher_headers(),
            )
            if resp.status_code == 401:
                return [TextContent(type="text", text="Error: Authentication failed. Check DISPATCHER_API_KEY.")]
            if resp.status_code == 404:
                return [TextContent(type="text", text=f"Error: Job '{job_id}' not found.")]
            if resp.status_code != 200:
                return [TextContent(type="text", text=f"Error: Status check failed ({resp.status_code})")]

            data = resp.json()
            status = data.get("status", "unknown")
            progress = data.get("progress", 0)
            result_url = data.get("result_url")
            error = data.get("error")
            retry_count = data.get("retry_count", 0)

            if status == "completed":
                return [TextContent(type="text", text=(
                    f"✅ Video generation complete!\n"
                    f"Job ID: {job_id}\n"
                    f"Status: {status}\n"
                    f"Download: {result_url}\n"
                    f"File size: {data.get('file_size_mb', 'unknown')} MB\n"
                    f"Duration: {data.get('duration_s', '?')}s"
                ))]
            elif status == "failed":
                return [TextContent(type="text", text=f"❌ Video generation failed: {error}")]
            elif status == "queued":
                retry_info = f" (retry {retry_count})" if retry_count > 0 else ""
                return [TextContent(type="text", text=f"⏳ Job queued{retry_info} — waiting for a free GPU worker...")]
            else:
                return [TextContent(type="text", text=(
                    f"🔄 In progress — {progress:.0f}% complete\n"
                    f"Job ID: {job_id}\n"
                    f"Status: {status}\n"
                    f"Workers active: {data.get('workers_active', '?')}"
                ))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error checking job status: {str(e)}")]


async def _handle_list_workers() -> list[TextContent]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0)) as client:
            resp = await client.get(
                f"{DISPATCHER_URL}/api/v1/workers",
                headers=_dispatcher_headers(),
            )
            if resp.status_code == 401:
                return [TextContent(type="text", text="Error: Authentication failed. Check DISPATCHER_API_KEY.")]
            if resp.status_code != 200:
                return [TextContent(type="text", text="Error: Could not fetch worker list.")]

            data = resp.json()
            workers = data.get("workers", [])
            if not workers:
                return [TextContent(type="text", text="No video workers connected to the pod.")]

            lines = ["🖥️ **Available Video GPU Workers:**", ""]
            for w in workers:
                status_icon = "🟢" if w.get("status") == "idle" else "🔴" if w.get("status") == "offline" else "🟡"
                lines.append(
                    f"{status_icon} **{w.get('name', 'Unknown')}** — "
                    f"{w.get('gpu', 'CPU')}, {w.get('vram_gb', '?')}GB VRAM | "
                    f"Model: {w.get('loaded_model', 'none')} | "
                    f"Status: {w.get('status', 'unknown')}"
                )
            return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error listing workers: {str(e)}")]


async def _handle_upscale_video(args: dict) -> list[TextContent]:
    video_url = args.get("video_url", "")
    if not video_url:
        return [TextContent(type="text", text="Error: 'video_url' is required.")]

    target = args.get("target_resolution", "1080p")
    style = args.get("style", "fidelity")

    payload = {
        "job_type": "upscale",
        "video_url": video_url,
        "target_resolution": target,
        "style": style,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=30.0)) as client:
            resp = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/upscale",
                json=payload,
                headers=_dispatcher_headers(),
            )
            if resp.status_code == 401:
                return [TextContent(type="text", text="Error: Authentication failed. Check DISPATCHER_API_KEY.")]
            if resp.status_code != 200:
                err = resp.text[:500]
                return [TextContent(type="text", text=f"Error: Upscale request failed: {err}")]

            data = resp.json()
            return [TextContent(type="text", text=(
                f"✅ Video upscale started!\n"
                f"Job ID: {data['job_id']}\n"
                f"Target: {target}\n"
                f"Style: {style}\n"
                f"Check progress with: check_video_status(job_id=\"{data['job_id']}\")"
            ))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error submitting upscale job: {str(e)}")]


if __name__ == "__main__":
    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())

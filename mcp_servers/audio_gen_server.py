"""
audio_gen_server.py — MCP server for audio generation

Provides text-to-speech, voice cloning, and music generation capabilities
using Bark, Tortoise TTS, and MusicGen.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("audio_gen")

DISPATCHER_URL = os.environ.get("DISPATCHER_URL", "http://127.0.0.1:9000")
DISPATCHER_API_KEY = os.environ.get("DISPATCHER_API_KEY", "")


def _auth_headers() -> dict:
    """Build request headers with auth if configured."""
    headers = {"Content-Type": "application/json"}
    if DISPATCHER_API_KEY:
        headers["Authorization"] = f"Bearer {DISPATCHER_API_KEY}"
    return headers


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_speech",
            description="Generate speech from text using TTS models (Bark or Tortoise). Supports multiple voices and languages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to convert to speech",
                    },
                    "model": {
                        "type": "string",
                        "description": "TTS model to use: 'bark' (fast, good quality) or 'tortoise' (slower, best quality)",
                        "enum": ["bark", "tortoise"],
                        "default": "bark",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice preset (e.g., 'v2/en_speaker_6' for Bark, 'random' for Tortoise)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code (e.g., 'en', 'es', 'fr', 'de', 'ja')",
                        "default": "en",
                    },
                    "speed": {
                        "type": "number",
                        "description": "Speech speed multiplier (0.5-2.0, default 1.0)",
                        "default": 1.0,
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="clone_voice",
            description="Clone a voice from an audio sample and generate new speech in that voice.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to speak in the cloned voice",
                    },
                    "reference_audio": {
                        "type": "string",
                        "description": "Path or URL to reference audio sample (10-30 seconds recommended)",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use for voice cloning",
                        "default": "bark",
                    },
                },
                "required": ["text", "reference_audio"],
            },
        ),
        Tool(
            name="generate_music",
            description="Generate music from a text description using MusicGen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the music to generate (e.g., 'upbeat electronic dance music')",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Duration in seconds (1-30, default 8)",
                        "default": 8,
                    },
                    "model_size": {
                        "type": "string",
                        "description": "Model size: 'small' (300M), 'medium' (1.5B), or 'large' (3.3B)",
                        "enum": ["small", "medium", "large"],
                        "default": "medium",
                    },
                    "guidance_scale": {
                        "type": "number",
                        "description": "Guidance scale for generation (1-10, default 3)",
                        "default": 3,
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Random seed for reproducibility",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="upscale_audio",
            description="Enhance and upscale audio quality using AI models.",
            inputSchema={
                "type": "object",
                "properties": {
                    "audio_url": {
                        "type": "string",
                        "description": "Path or URL to the audio file to enhance",
                    },
                    "enhancement_type": {
                        "type": "string",
                        "description": "Type of enhancement",
                        "enum": ["denoise", "upsample", "enhance", "all"],
                        "default": "all",
                    },
                    "target_sample_rate": {
                        "type": "integer",
                        "description": "Target sample rate in Hz (44100, 48000, 96000)",
                        "default": 48000,
                    },
                },
                "required": ["audio_url"],
            },
        ),
        Tool(
            name="list_audio_models",
            description="List available audio generation models and their capabilities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category: 'tts', 'music', 'enhancement', or 'all'",
                        "enum": ["tts", "music", "enhancement", "all"],
                        "default": "all",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        import httpx
    except ImportError:
        return [TextContent(type="text", text="Error: httpx is required. Install with: pip install httpx")]

    handlers = {
        "generate_speech": _handle_generate_speech,
        "clone_voice": _handle_clone_voice,
        "generate_music": _handle_generate_music,
        "upscale_audio": _handle_upscale_audio,
        "list_audio_models": _handle_list_models,
    }

    handler = handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return await handler(arguments)


async def _handle_generate_speech(args: dict) -> list[TextContent]:
    """Handle text-to-speech generation."""
    import httpx

    text = args.get("text", "")
    model = args.get("model", "bark")
    voice = args.get("voice", "")
    language = args.get("language", "en")
    speed = args.get("speed", 1.0)

    if not text:
        return [TextContent(type="text", text="Error: text is required")]

    # Prepare job request
    job_request = {
        "job_type": "audio",
        "task": "tts",
        "text": text,
        "model": model,
        "voice": voice,
        "language": language,
        "speed": speed,
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/audio",
                json=job_request,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            result = response.json()

            job_id = result.get("job_id")
            if not job_id:
                return [TextContent(type="text", text=f"Error: {result}")]

            # Poll for completion
            return await _poll_job(job_id, f"Speech generated: {text[:50]}...")

    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"HTTP error: {e.response.status_code} - {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error generating speech: {str(e)}")]


async def _handle_clone_voice(args: dict) -> list[TextContent]:
    """Handle voice cloning."""
    import httpx

    text = args.get("text", "")
    reference_audio = args.get("reference_audio", "")
    model = args.get("model", "bark")

    if not text or not reference_audio:
        return [TextContent(type="text", text="Error: text and reference_audio are required")]

    job_request = {
        "job_type": "audio",
        "task": "voice_clone",
        "text": text,
        "reference_audio": reference_audio,
        "model": model,
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/audio",
                json=job_request,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            result = response.json()

            job_id = result.get("job_id")
            return await _poll_job(job_id, "Voice cloning complete")

    except Exception as e:
        return [TextContent(type="text", text=f"Error cloning voice: {str(e)}")]


async def _handle_generate_music(args: dict) -> list[TextContent]:
    """Handle music generation."""
    import httpx

    prompt = args.get("prompt", "")
    duration = args.get("duration", 8)
    model_size = args.get("model_size", "medium")
    guidance_scale = args.get("guidance_scale", 3)
    seed = args.get("seed")

    if not prompt:
        return [TextContent(type="text", text="Error: prompt is required")]

    job_request = {
        "job_type": "audio",
        "task": "music",
        "prompt": prompt,
        "duration": duration,
        "model_size": model_size,
        "guidance_scale": guidance_scale,
        "seed": seed,
    }

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/audio",
                json=job_request,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            result = response.json()

            job_id = result.get("job_id")
            return await _poll_job(job_id, f"Music generated: {prompt[:50]}...")

    except Exception as e:
        return [TextContent(type="text", text=f"Error generating music: {str(e)}")]


async def _handle_upscale_audio(args: dict) -> list[TextContent]:
    """Handle audio upscaling/enhancement."""
    import httpx

    audio_url = args.get("audio_url", "")
    enhancement_type = args.get("enhancement_type", "all")
    target_sample_rate = args.get("target_sample_rate", 48000)

    if not audio_url:
        return [TextContent(type="text", text="Error: audio_url is required")]

    job_request = {
        "job_type": "audio",
        "task": "upscale",
        "audio_url": audio_url,
        "enhancement_type": enhancement_type,
        "target_sample_rate": target_sample_rate,
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                f"{DISPATCHER_URL}/api/v1/jobs/audio",
                json=job_request,
                headers=_auth_headers(),
            )
            response.raise_for_status()
            result = response.json()

            job_id = result.get("job_id")
            return await _poll_job(job_id, "Audio enhancement complete")

    except Exception as e:
        return [TextContent(type="text", text=f"Error enhancing audio: {str(e)}")]


async def _handle_list_models(args: dict) -> list[TextContent]:
    """List available audio models."""
    category = args.get("category", "all")

    models = {
        "tts": [
            {
                "name": "bark",
                "description": "Fast, high-quality TTS with voice presets",
                "vram_required": "4GB",
                "speed": "Fast",
                "quality": "Good",
                "languages": ["en", "es", "fr", "de", "ja", "ko", "zh"],
            },
            {
                "name": "tortoise",
                "description": "Slower but highest quality TTS",
                "vram_required": "6GB",
                "speed": "Slow",
                "quality": "Excellent",
                "languages": ["en"],
            },
        ],
        "music": [
            {
                "name": "musicgen-small",
                "description": "300M parameter music generation model",
                "vram_required": "4GB",
                "speed": "Fast",
                "quality": "Good",
            },
            {
                "name": "musicgen-medium",
                "description": "1.5B parameter music generation model",
                "vram_required": "8GB",
                "speed": "Medium",
                "quality": "Very Good",
            },
            {
                "name": "musicgen-large",
                "description": "3.3B parameter music generation model",
                "vram_required": "12GB",
                "speed": "Slow",
                "quality": "Excellent",
            },
        ],
        "enhancement": [
            {
                "name": "audio-super-resolution",
                "description": "Upscale audio to higher sample rates",
                "vram_required": "2GB",
                "speed": "Fast",
            },
            {
                "name": "denoiser",
                "description": "Remove background noise from audio",
                "vram_required": "2GB",
                "speed": "Fast",
            },
        ],
    }

    if category == "all":
        result = models
    else:
        result = {category: models.get(category, [])}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _poll_job(job_id: str, success_message: str) -> list[TextContent]:
    """Poll for job completion."""
    import httpx

    max_attempts = 120  # 10 minutes max
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{DISPATCHER_URL}/api/v1/jobs/{job_id}",
                    headers=_auth_headers(),
                )
                response.raise_for_status()
                job = response.json()

                status = job.get("status")
                progress = job.get("progress", 0)

                if status == "completed":
                    result_url = job.get("result_url", "")
                    file_size = job.get("file_size_mb", 0)
                    return [TextContent(
                        type="text",
                        text=f"✅ {success_message}\n"
                             f"Result: {result_url}\n"
                             f"Size: {file_size}MB"
                    )]
                elif status == "failed":
                    error = job.get("error", "Unknown error")
                    return [TextContent(type="text", text=f"❌ Job failed: {error}")]

                # Still running, wait and retry
                await asyncio.sleep(5)

        except Exception as e:
            if attempt == max_attempts - 1:
                return [TextContent(type="text", text=f"❌ Job polling failed: {str(e)}")]
            await asyncio.sleep(5)

    return [TextContent(type="text", text=f"⏰ Job timed out after {max_attempts * 5} seconds")]


if __name__ == "__main__":
    asyncio.run(stdio_server(server))

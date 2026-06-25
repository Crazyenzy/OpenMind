"""
pod_manager_server.py — MCP server for Hyperspace Pod management

Plugs into the OpenMind MCP framework (installed into Odysseus). Provides pod lifecycle management,
model discovery, distributed inference control, and pod health monitoring.
Communicates with the local Hyperspace CLI/daemon and the Dispatcher.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("pod_manager")

DISPATCHER_URL = os.environ.get("DISPATCHER_URL", "http://127.0.0.1:9000")
HYPERSPACE_BIN = os.environ.get("HYPERSPACE_BIN", "hyperspace")
POD_API_URL = os.environ.get("POD_API_URL", "http://127.0.0.1:8080")
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
            name="pod_status",
            description="Get the current status of the Hyperspace pod: online members, GPU resources, sharded models, and treasury.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_resources": {
                        "type": "boolean",
                        "description": "Include per-node GPU/VRAM details (default: true)",
                    },
                },
            },
        ),
        Tool(
            name="pod_list_models",
            description="List all AI models available across the pod — both locally sharded models and cloud provider models.",
            inputSchema={
                "type": "object",
                "properties": {
                    "shardable_only": {
                        "type": "boolean",
                        "description": "Show only models that can be sharded across multiple nodes (default: false)",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Filter by provider: local, openrouter, groq, anthropic, openai, etc.",
                    },
                },
            },
        ),
        Tool(
            name="pod_chat",
            description="Send a chat completion request to the pod's distributed LLM (OpenAI-compatible API). The pod automatically routes to the best available model.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The user message to send to the model",
                    },
                    "system": {
                        "type": "string",
                        "description": "System prompt / instructions for the model",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use (default: auto-select best available)",
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Sampling temperature (0-2, default: 0.7)",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum tokens to generate (default: 2048)",
                    },
                    "stream": {
                        "type": "boolean",
                        "description": "Stream tokens as they're generated (default: false)",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="pod_shard_model",
            description="Shard (distribute) a large model across the pod's GPUs using pipeline parallelism. Required for models that don't fit on a single GPU.",
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Model to shard: ollama name (qwen3.5:32b), HuggingFace (hf:Qwen/Qwen2.5-32B-GGUF), or direct URL",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Show the shard plan without executing (default: false)",
                    },
                    "max_nodes": {
                        "type": "integer",
                        "description": "Maximum nodes to use for sharding (default: auto)",
                    },
                },
                "required": ["model"],
            },
        ),
        Tool(
            name="pod_members",
            description="List all members of the pod with their roles, online status, GPU info, and loaded models.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="pod_providers",
            description="List configured cloud AI providers (OpenRouter, Groq, Anthropic, etc.) with their status, caps, and available models.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="pod_invite",
            description="Generate an invite code so a new member can join the pod. Only works if you're a pod admin/owner.",
            inputSchema={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Role: member or viewer (default: member)",
                    },
                    "ttl_hours": {
                        "type": "integer",
                        "description": "How long the invite is valid in hours (default: 72)",
                    },
                    "multi_use": {
                        "type": "boolean",
                        "description": "Allow unlimited uses (default: false, single use)",
                    },
                },
            },
        ),
        Tool(
            name="pod_budget",
            description="Check your budget status in the pod — spending limits, usage, and remaining balance.",
            inputSchema={
                "type": "object",
                "properties": {},
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
        "pod_status": _handle_pod_status,
        "pod_list_models": _handle_list_models,
        "pod_chat": _handle_pod_chat,
        "pod_shard_model": _handle_shard_model,
        "pod_members": _handle_members,
        "pod_providers": _handle_providers,
        "pod_invite": _handle_invite,
        "pod_budget": _handle_budget,
    }

    handler = handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return await handler(arguments)


async def _run_hyperspace(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a hyperspace CLI command and return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            HYPERSPACE_BIN, *args, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(), stderr.decode()
    except FileNotFoundError:
        return -1, "", f"hyperspace CLI not found at '{HYPERSPACE_BIN}'. Is it installed?"
    except asyncio.TimeoutError:
        return -1, "", f"Command timed out after {timeout}s"


async def _handle_pod_status(args: dict) -> list[TextContent]:
    include_resources = args.get("include_resources", True)
    code, stdout, stderr = await _run_hyperspace(["pod", "status"])

    if code != 0:
        return [TextContent(type="text", text=f"Error: {stderr or 'pod status failed'}")]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Pod status:\n{stdout}")]

    lines = ["📊 **Pod Status**", ""]
    lines.append(f"Name: {data.get('name', 'unknown')}")
    lines.append(f"Members online: {data.get('online_members', 0)}/{data.get('total_members', 0)}")
    lines.append(f"Active shard ring: {data.get('active_ring', 'none')}")
    lines.append(f"Treasury: {data.get('treasury', {}).get('balance', 'N/A')}")

    if include_resources and "resources" in data:
        lines.append("")
        lines.append("**GPU Resources:**")
        for node in data["resources"]:
            lines.append(
                f"  • {node.get('name', '?')}: {node.get('gpu', 'CPU')}, "
                f"{node.get('free_vram_gb', '?')}/{node.get('total_vram_gb', '?')}GB free, "
                f"models: {', '.join(node.get('loaded_models', [])) or 'none'}"
            )

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_list_models(args: dict) -> list[TextContent]:
    cmd = ["pod", "models"]
    if args.get("shardable_only"):
        cmd.append("--shardable")

    code, stdout, stderr = await _run_hyperspace(cmd)

    if code != 0:
        return [TextContent(type="text", text=f"Error: {stderr or 'list models failed'}")]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Available models:\n{stdout}")]

    provider_filter = args.get("provider")
    lines = ["🤖 **Available Models**", ""]

    for model in data.get("models", []):
        provider = model.get("provider", "local")
        if provider_filter and provider != provider_filter:
            continue
        shardable = "🔗 shardable" if model.get("shardable") else ""
        lines.append(
            f"  • **{model.get('name', '?')}** — {provider} "
            f"({model.get('vram_required_gb', '?')}GB) {shardable}"
        )

    if not data.get("models"):
        lines.append("  No models currently available. Use `pod_shard_model` to load one.")

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_pod_chat(args: dict) -> list[TextContent]:
    prompt = args.get("prompt", "")
    if not prompt:
        return [TextContent(type="text", text="Error: 'prompt' is required.")]

    system_msg = args.get("system", "You are a helpful assistant.")
    model = args.get("model", "auto")
    temperature = args.get("temperature", 0.7)
    max_tokens = args.get("max_tokens", 2048)

    # Use the Hyperspace Pod OpenAI-compatible API
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        import httpx
        pod_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('POD_API_KEY', '')}",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0)) as client:
            resp = await client.post(
                f"{POD_API_URL}/v1/chat/completions",
                json=payload,
                headers=pod_headers,
            )
            if resp.status_code != 200:
                err = resp.text[:500]
                try:
                    err = resp.json().get("error", {}).get("message", err)
                except Exception:
                    pass
                return [TextContent(type="text", text=f"Error: Chat request failed ({resp.status_code}): {err}")]

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            model_used = data.get("model", "unknown")

            lines = [f"**Response** (model: {model_used}):", "", content]

            if usage:
                lines.append("")
                lines.append(
                    f"Tokens: {usage.get('prompt_tokens', '?')} prompt + "
                    f"{usage.get('completion_tokens', '?')} completion = "
                    f"{usage.get('total_tokens', '?')} total"
                )

            return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error calling pod chat API: {str(e)}")]


async def _handle_shard_model(args: dict) -> list[TextContent]:
    model = args.get("model", "")
    if not model:
        return [TextContent(type="text", text="Error: 'model' is required.")]

    cmd = ["pod", "shard", model]
    if args.get("dry_run"):
        cmd.append("--dry-run")
    if args.get("max_nodes"):
        cmd.extend(["--nodes", str(args["max_nodes"])])

    code, stdout, stderr = await _run_hyperspace(cmd, timeout=120)

    if code != 0:
        return [TextContent(type="text", text=f"Error sharding model: {stderr or stdout}")]

    return [TextContent(type="text", text=f"✅ Model shard initiated:\n{stdout}")]


async def _handle_members(args: dict) -> list[TextContent]:
    code, stdout, stderr = await _run_hyperspace(["pod", "members"])

    if code != 0:
        return [TextContent(type="text", text=f"Error: {stderr or 'list members failed'}")]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Pod members:\n{stdout}")]

    lines = ["👥 **Pod Members**", ""]
    for m in data.get("members", []):
        status_icon = "🟢" if m.get("online") else "🔴"
        lines.append(
            f"{status_icon} **{m.get('name', '?')}** — "
            f"{m.get('role', 'member')} | "
            f"GPU: {m.get('gpu', 'CPU')} | "
            f"VRAM: {m.get('vram_gb', '?')}GB | "
            f"Models: {', '.join(m.get('loaded_models', [])) or 'none'}"
        )

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_providers(args: dict) -> list[TextContent]:
    code, stdout, stderr = await _run_hyperspace(["pod", "providers", "list"])

    if code != 0:
        return [TextContent(type="text", text=f"Error: {stderr or 'list providers failed'}")]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Cloud providers:\n{stdout}")]

    lines = ["☁️ **Cloud Providers**", ""]
    for p in data.get("providers", []):
        status = "✅" if p.get("enabled") else "❌"
        lines.append(
            f"{status} **{p.get('provider', '?')}** — "
            f"cap: ${p.get('monthly_cap', '?')}/mo | "
            f"models: {', '.join(p.get('models', [])) or 'all'}"
        )

    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_invite(args: dict) -> list[TextContent]:
    cmd = ["pod", "invite"]
    role = args.get("role", "member")
    cmd.extend(["--role", role])

    ttl = args.get("ttl_hours", 72)
    cmd.extend(["--ttl", f"{ttl}h"])

    if args.get("multi_use"):
        cmd.append("--multi-use")

    code, stdout, stderr = await _run_hyperspace(cmd)

    if code != 0:
        return [TextContent(type="text", text=f"Error creating invite: {stderr or stdout}")]

    try:
        data = json.loads(stdout)
        invite_code = data.get("inviteCode", "unknown")
        join_cmd = data.get("joinCommand", f"hyperspace pod join {invite_code}")
        magic_link = data.get("magicLink", "")

        lines = [
            "📨 **Invite Created**",
            "",
            f"Code: `{invite_code}`",
            f"Join command: `{join_cmd}`",
            f"Role: {role}",
            f"Expires: {ttl}h",
        ]
        if magic_link:
            lines.append(f"Link: {magic_link}")

        return [TextContent(type="text", text="\n".join(lines))]
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Invite created:\n{stdout}")]


async def _handle_budget(args: dict) -> list[TextContent]:
    code, stdout, stderr = await _run_hyperspace(["pod", "budgets", "me"])

    if code != 0:
        return [TextContent(type="text", text=f"Error: {stderr or 'budget check failed'}")]

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return [TextContent(type="text", text=f"Budget:\n{stdout}")]

    lines = ["💰 **My Budget**", ""]
    lines.append(f"Mode: {data.get('mode', 'unknown')}")
    lines.append(f"Limit: ${data.get('limit_dollars', '?')}")
    lines.append(f"Spent: ${data.get('spent_dollars', '?')}")
    lines.append(f"Remaining: ${data.get('remaining_dollars', '?')}")
    lines.append(f"Requests today: {data.get('requests_today', '?')}")

    return [TextContent(type="text", text="\n".join(lines))]


if __name__ == "__main__":
    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())

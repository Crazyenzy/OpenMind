# OpenMind — Distributed AI Compute Mesh

<p align="center">
  <img src="OpenMind.png" alt="OpenMind Logo" width="200" />
</p>

**Pool your laptops, desktops, and external GPUs into a private AI cluster.** Chat with large language models sharded across every machine. Generate images and videos using every GPU in the network. All self-hosted, encrypted, peer-to-peer.

Built by integrating three open-source projects into one unified system, branded as **OpenMind**:

| Project | Role |
|---------|------|
| **[Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)** (66.8k ⭐) | Per-person AI workspace — chat, agents, docs, memory, email, calendar |
| **[Hyperspace AGI](https://github.com/hyperspaceai/agi)** (1.9k ⭐) | P2P distributed LLM inference with model sharding |
| **[ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed)** (564 ⭐) | Multi-GPU image & video generation across machines |

---

## Architecture

```
OpenMind (your workspace — Odysseus under the hood)
   │
   ├─→ pod_manager MCP ──→ Hyperspace Pod (:8080) ──→ LLM inference (sharded across GPUs)
   │
   ├─→ video_gen MCP ──→ Dispatcher (:9000) ──→ ComfyUI Master ──→ Workers (parallel video gen)
   │
   └─→ Tailscale mesh (encrypted, 100.x.x.x)
```

- **Chat**: Hyperspace shards models across pod GPUs for instant responses
- **Video**: ComfyUI-Distributed splits frames/batches across workers
- **Image**: Same pipeline, same workers, different models
- **Workspace**: Full Odysseus feature set — agents, deep research, email, calendar, memory, document editor, cookbook

---

## Quick Start

### Prerequisites

- 2+ machines with GPUs (at least one with 8GB+ VRAM for video)
- Tailscale account (free for up to 3 users)
- Docker (optional, for containerized deployment)

### 1. Network Layer (all machines)

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Note each machine's 100.x.x.x IP
tailscale ip -4
```

### 2. Hyperspace Pod (all machines)

```bash
# Install Hyperspace CLI
curl -fsSL https://agents.hyper.space/api/install | bash

# On coordinator:
hyperspace pod create "openmind-cluster"
hyperspace pod invite --role member --ttl 168h
# → Share the invite code with pod members

# On workers:
hyperspace pod join <invite-code>

# Shard a model across all GPUs:
hyperspace pod shard qwen3.5:32b

# Test inference:
hyperspace pod infer -p "Hello from OpenMind!"
```

### 3. ComfyUI Cluster

```bash
# Clone ComfyUI on each GPU machine
git clone https://github.com/comfyanonymous/ComfyUI.git ~/comfyui

# Install ComfyUI-Distributed extension
git clone https://github.com/robertvoy/ComfyUI-Distributed.git \
  ~/comfyui/custom_nodes/ComfyUI-Distributed

# Start master (coordinator):
cd ~/comfyui && python main.py --enable-cors-header --listen

# Start worker (other machines):
cd ~/comfyui && python main.py --enable-cors-header --listen
# Configure worker in ComfyUI UI → Distributed panel → enter master's Tailscale IP
```

### 4. Dispatcher (coordinator only)

```bash
pip install fastapi uvicorn httpx pyyaml pydantic

# Edit dispatcher/config.yaml with your IPs, then:
python dispatcher/main.py
```

### 5. OpenMind MCP Integration

Copy the new MCP servers into Odysseus:

```bash
cp mcp_servers/video_gen_server.py ~/odysseus/mcp_servers/
cp mcp_servers/pod_manager_server.py ~/odysseus/mcp_servers/
```

Add to Odysseus config:

```yaml
mcp_servers:
  video_gen:
    command: python
    args: [mcp_servers/video_gen_server.py]
  pod_manager:
    command: python
    args: [mcp_servers/pod_manager_server.py]
```

---

## One-Command Full Setup

```bash
chmod +x setup.sh
./setup.sh                    # auto-detect role
./setup.sh --role coordinator # force coordinator
./setup.sh --role worker      # force worker
```

---

## What You Can Do in OpenMind

### Chat
```
"Summarize this 50-page PDF" → Routed to Qwen 3.5 32B sharded across 3 GPUs
```

### Video Generation
```
"Create a 10s cinematic of a dragon flying over mountains at sunset"
→ Routed to ComfyUI worker with Wan 2.2 (24GB GPU)
→ Parallel variations generated across multiple workers
```

### Image Generation
```
"Generate a realistic photo of a cyberpunk city street at night"
→ Routed to ComfyUI worker with SDXL/Flux
→ 4 variations in the time of 1
```

### Pod Management
```
"Show me who's online and what GPUs are free"
→ pod_manager MCP returns live pod status
```

### All Standard Odysseus Features
Chat · Agents (bash/files/web) · Deep Research · Document Editor · Memory (ChromaDB) · Email (IMAP/SMTP) · Calendar (CalDAV) · Model Comparison · Cookbook (hardware scan + model recommendations) · 10,000+ MCP tools · PWA mobile access

---

## Model Reference

### LLM (shardable across pod)

| Pod VRAM | Model |
|----------|-------|
| 16 GB (2×8) | Gemma 3 12B, Qwen 2.5 14B |
| 32 GB (2×16) | Qwen 3.5 32B |
| 64 GB (4×16) | Qwen 2.5 72B (Q4), Llama 3.1 70B (Q4) |
| 96 GB+ | DeepSeek V3 (Q4) |

### Video (per single GPU — not shardable)

| GPU VRAM | Model |
|----------|-------|
| 8-12 GB | LTX-Video, AnimateDiff |
| 16-24 GB | Wan 2.2, CogVideoX-5B |
| 24-48 GB | Wan 2.2 (1080p), Hunyuan Video (Q8) |
| 48-80 GB | Hunyuan Video (full), CogVideoX-5B-I2V |

---

## File Layout

```
openmind/
├── OpenMind.png                  ← Logo
├── README.md                     ← This file
├── ARCHITECTURE.md               ← Full architecture documentation
├── docker-compose.yml            ← Full stack Docker deployment
├── setup.sh                      ← One-command automated setup
├── dispatcher/                   ← Glue service (FastAPI)
│   ├── main.py                   ← Central job router
│   ├── config.yaml               ← Configuration
│   ├── requirements.txt
│   └── Dockerfile
├── mcp_servers/                  ← MCP tools (drop into Odysseus)
│   ├── video_gen_server.py       ← Video generation
│   └── pod_manager_server.py     ← Pod management + chat
└── comfyui_workflows/            ← Pre-built ComfyUI workflows
    ├── wan22_t2v.json            ← Wan 2.2 text-to-video
    ├── ltx_video.json            ← LTX-Video
    ├── hunyuan_video.json        ← Hunyuan Video
    └── sdxl_image.json           ← SDXL image generation
```

---

## FAQ

**Q: Does OpenMind replace Odysseus?**
No — OpenMind augments it. You still run Odysseus as your workspace. OpenMind adds two new MCP servers (video generation + pod management) and the Dispatcher/ComfyUI infrastructure to pool pod GPUs. All existing Odysseus features (chat, agents, memory, email, calendar, cookbook, deep research) work as before.

**Q: Can I use this with just CPU machines?**
LLM inference via Hyperspace will work but slowly. Video generation requires at least one GPU. For CPU-only, configure a cloud provider as fallback (`hyperspace pod providers add`).

**Q: What if the coordinator goes offline?**
Hyperspace Pod (LLM) is fully P2P — no single point of failure. ComfyUI-Distributed has a master-worker model — if the master dies, video gen stops. Run the master on the most reliable machine.

**Q: Is this actually private?**
Yes. Tailscale encrypts all traffic with WireGuard. Hyperspace uses Noise-encrypted libp2p. ComfyUI workers bind to Tailscale IPs only. Nothing is exposed to the public internet.

**Q: What's the minimum VRAM for video?**
LTX-Video runs on 12GB. For a group with mixed hardware, start with LTX-Video as the baseline.

---

## License

MIT — all glue code in this repo. Individual components retain their original licenses (Odysseus: Apache 2.0, Hyperspace: MIT, ComfyUI-Distributed: MIT).

# OpenMind — Full Integration Architecture

## Overview

A **private distributed AI mesh** where people connect their laptops, desktops, and external GPUs into a pod, sharing compute for chat, image generation, and video generation. Built by integrating three open-source projects with custom glue code.

```
┌──────────────────────────────────────────────────────────────────┐
│                     OPENMIND WORKSPACE (Odysseus + pod layer)              │
│  chat · docs · memory · email · calendar · image gen · video gen │
│  MCP Tool Layer ──────────────────────────────────────────────── │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────────┐│
│  │Chat MCP  │ │Image Gen │ │Video Gen │ │  Pod Manager MCP     ││
│  │(built-in)│ │ MCP      │ │ MCP (NEW)│ │  (NEW)               ││
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────────┬───────────┘│
└───────┼────────────┼────────────┼──────────────────┼────────────┘
        │            │            │                  │
        ▼            ▼            ▼                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                    DISPATCHER (NEW — Python/Redis)               │
│  - Routes chat → Hyperspace Pod (distributed LLM inference)     │
│  - Routes image → ComfyUI Master (Flux/SDXL)                    │
│  - Routes video → ComfyUI Master (Wan 2.2/LTX/Hunyuan)          │
│  - Queue management, load balancing, result aggregation          │
└────────┬──────────────────────────┬──────────────────────────────┘
         │                          │
         ▼                          ▼
┌─────────────────────┐   ┌────────────────────────────────────────┐
│  HYPERSPACE POD      │   │  COMFYUI-DISTRIBUTED CLUSTER           │
│  (LLM inference)     │   │  (Image/Video generation)              │
│                      │   │                                        │
│  libp2p mesh:        │   │  Master (coordinator):                 │
│  - Shard models      │   │  - Receives API requests               │
│    across GPU nodes  │   │  - Splits work across workers          │
│  - Pipeline parallel │   │  - Aggregates results                  │
│  - OpenAI-compat API │   │                                        │
│    on :8080          │   │  Workers:                              │
│                      │   │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐  │
│  ┌──────┐ ┌──────┐   │   │  │You   │ │Nidhi │ │Sneha │ │Cloud │  │
│  │You   │ │Nidhi │   │   │  │RTX   │ │Mac   │ │Idle  │ │Rented│  │
│  │16GB  │ │8GB   │   │   │  │24GB  │ │M4    │ │12GB  │ │80GB  │  │
│  └──────┘ └──────┘   │   │  └──────┘ └──────┘ └──────┘ └──────┘  │
└─────────────────────┘   └────────────────────────────────────────┘
         │                          │
         ▼                          ▼
┌──────────────────────────────────────────────────────────────────┐
│            NETWORK LAYER (Tailscale or Hyperspace libp2p)         │
│  Encrypted private mesh — all machines reachable on 100.x.x.x   │
└──────────────────────────────────────────────────────────────────┘
```

## Component Architecture

### Layer 1: Network (Tailscale — recommended for ≤20 people)

```
Every pod member installs Tailscale.
All machines share a private 100.x.x.x mesh.
Hyperspace Pod uses this for its libp2p transport.
ComfyUI workers connect to master via this mesh.
```

**Alternative**: Hyperspace's native libp2p mesh (better for >20 people, but more complex).

### Layer 2: Hyperspace Pod (LLM Inference)

```
hyperspace pod create "our-ai-cluster"
hyperspace pod invite --role member --ttl 168h

# Each member joins:
hyperspace pod join hp_inv_abc123

# Shard a large model across all GPUs:
hyperspace pod shard qwen3.5:32b

# OpenAI-compatible API available at localhost:8080
```

**Smart routing priority**:
1. Pod-distributed (sharded model, free)
2. Pod-peer (federated pod)
3. Cloud-BYOK (member's own API keys)
4. Cloud-funded (pod treasury)

### Layer 3: ComfyUI-Distributed Cluster (Image/Video Generation)

```
Master machine:
  - ComfyUI running with --enable-cors-header --listen
  - ComfyUI-Distributed extension installed
  - Acts as coordinator + API gateway

Worker machines:
  - ComfyUI running (GPU mode)
  - ComfyUI-Distributed extension installed
  - Register with master via Tailscale IP
  - Load appropriate models based on VRAM:
    12GB → LTX-Video, SDXL, Flux Schnell
    24GB → Wan 2.2, Hunyuan Video (quantized)
    48GB+ → Hunyuan Video, CogVideoX
```

### Layer 4: Odysseus (User Workspace)

Each person runs Odysseus locally with:
- Built-in chat, docs, memory, email, calendar
- NEW: `video_gen_server.py` MCP — generates videos via ComfyUI cluster
- NEW: `pod_manager_server.py` MCP — manages Hyperspace pod, lists models, checks status
- Existing: `image_gen_server.py` MCP — enhanced to route through ComfyUI cluster

### Layer 5: Dispatcher (Glue Code)

A lightweight Python service (FastAPI + Redis) that:
1. Accepts requests from OpenMind MCP tools
2. Routes chat requests → Hyperspace Pod API (:8080)
3. Routes image/video requests → ComfyUI Master API
4. Manages queues, tracks job status, returns results
5. Implements load balancing across workers

## Model↔VRAM Reference

### LLM (via Hyperspace Pod Sharding)

| Combined Pod VRAM | Recommended Model |
|-------------------|-------------------|
| 16 GB (2×8 GB)   | Gemma 3 12B, Qwen 2.5 14B |
| 32 GB (2×16 GB)  | Qwen 3.5 32B, DeepSeek Coder V2 Lite |
| 48 GB (3×16 GB)  | Gemma 3 27B (full precision) |
| 64 GB (4×16 GB)  | Qwen 2.5 72B (Q4), Llama 3.1 70B (Q4) |
| 96 GB+           | Qwen 2.5 72B (Q8), DeepSeek V3 (Q4) |

### Video Models (per single GPU — not shardable)

| GPU VRAM | Recommended Model |
|----------|-------------------|
| 8-12 GB  | LTX-Video, AnimateDiff, SDXL+AnimateDiff |
| 16-24 GB | Wan 2.2 (T2V), CogVideoX-5B |
| 24-48 GB | Wan 2.2 (I2V), Hunyuan Video (quantized) |
| 48-80 GB | Hunyuan Video (full), CogVideoX-5B-I2V |

## Job Flow: "Generate a 10-second video clip of a sunset over mountains"

```
1. User types in Odysseus chat: "Generate a 10-second video clip of a sunset
   over mountains"

2. Odysseus agent detects video request → calls video_gen MCP tool:
   generate_video(prompt="sunset over mountains", duration=10, ...)

3. video_gen MCP → Dispatcher API:
   POST /api/v1/jobs/video
   {"prompt": "sunset over mountains", "duration": 10, "model": "wan2.2"}

4. Dispatcher:
   a. Checks ComfyUI-Distributed workers for available GPU
   b. Finds: Nidhi's Mac (8GB) busy, Snehal's desktop (24GB) idle
   c. Routes job to Snehal's worker via ComfyUI Master
   d. Returns job_id: "job_abc123"

5. ComfyUI Master:
   a. Receives workflow (Wan 2.2 T2V)
   b. If multiple GPUs free → splits batch (parallel variations)
   c. If upscaling needed → distributes tiles
   d. Processes, generates video

6. Dispatcher polls for completion, retrieves video file

7. Odysseus displays the video in chat (or link to file)
```

## Network Topology

```
Internet
   │
   ├── Tailscale DERP relay (fallback)
   │
   ├── 100.64.0.1 — Bhupesh (RTX 4090, 24GB)
   │    ├── Odysseus :3000
   │    ├── Hyperspace Node :8080
   │    ├── ComfyUI Worker :8188
   │    └── Dispatcher :9000
   │
   ├── 100.64.0.2 — Nidhi (Mac M4, 16GB unified)
   │    ├── Odysseus :3000
   │    ├── Hyperspace Node :8080
   │    └── ComfyUI Worker :8188
   │
   ├── 100.64.0.3 — Snehal (RTX 3090, 24GB)
   │    ├── Odysseus :3000
   │    ├── Hyperspace Node :8080
   │    ├── ComfyUI Master :8188  ← coordinator
   │    └── Dispatcher :9000
   │
   └── 100.64.0.4 — Cloud GPU (A100, 80GB, rented)
        ├── Hyperspace Node :8080
        └── ComfyUI Worker :8188
```

**Note**: Only ONE machine runs ComfyUI Master + Dispatcher. All others run workers. Hyperspace Pod is fully P2P — no single coordinator needed for LLM inference.

## Phased Implementation Plan

### Phase 1: Network Layer (1 evening)
- Install Tailscale on all machines
- Verify p2p connectivity (ping, iperf)
- Document IPs

### Phase 2: Hyperspace Pod (1 evening)
- Install Hyperspace CLI on all machines
- Create pod, invite members
- Test distributed inference
- Verify OpenAI-compatible API

### Phase 3: ComfyUI Cluster (1 weekend)
- Install ComfyUI on all GPU machines
- Install ComfyUI-Distributed extension
- Set up master + workers
- Test distributed image/video generation
- Download models: LTX-Video, Wan 2.2, SDXL

### Phase 4: Odysseus Setup (1 evening)
- Each member installs Odysseus
- Configure to use Hyperspace Pod API for chat
- Install new MCP servers (video_gen, pod_manager)

### Phase 5: Dispatcher + Glue (1 weekend)
- Deploy dispatcher service
- Wire up OpenMind MCP tools → Dispatcher → ComfyUI/Hyperspace
- Test end-to-end: chat prompt → video → return to Odysseus

## File Inventory (this project)

```
openmind/
├── ARCHITECTURE.md              ← This document
├── docker-compose.yml           ← Full stack deployment
├── setup.sh                     ← Automated setup script
├── dispatcher/                  ← Glue code service
│   ├── main.py                  ← FastAPI dispatcher server
│   ├── requirements.txt
│   ├── config.yaml
│   └── Dockerfile
├── mcp_servers/                 ← New Odysseus MCP servers
│   ├── video_gen_server.py      ← Video generation via ComfyUI
│   └── pod_manager_server.py    ← Hyperspace pod management
├── comfyui_workflows/           ← Pre-built ComfyUI workflows
│   ├── wan22_t2v.json           ← Wan 2.2 text-to-video
│   ├── ltx_video.json           ← LTX-Video
│   ├── hunyuan_video.json       ← Hunyuan Video
│   └── sdxl_image.json          ← SDXL image generation
└── README.md                    ← User-facing setup guide
```

## Security Considerations

1. **Tailscale** encrypts all inter-node traffic (WireGuard)
2. **Hyperspace Pod** uses Noise-encrypted libp2p connections
3. **ComfyUI** workers should bind to Tailscale IP only (not 0.0.0.0)
4. **API keys** encrypted at rest (Hyperspace Pod Capsule AES-256-GCM)
5. **File transfer** of generated videos happens over encrypted Tailscale mesh

## Caveats & Limitations

| Concern | Reality |
|---------|---------|
| VRAM ceiling | Models can't be bigger than single largest GPU for video (sharding works for LLM only) |
| Bandwidth | Video files 50-500MB; home upload speed matters for distribution |
| Master SPOF | ComfyUI-Distributed has single master; if it dies, video gen stops (LLM unaffected) |
| Hyperspace beta | CLI is rapidly changing; pin versions |
| Power consumption | Running GPUs 24/7 is expensive; use ComfyUI worker idle timeout |

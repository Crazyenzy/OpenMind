#!/usr/bin/env bash
# =============================================================================
# setup.sh — Automated setup for OpenMind
# =============================================================================
#
# Run this on every machine in the pod. It auto-detects the machine's role
# (coordinator or worker), installs dependencies, and configures services.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh                          # auto-detect role
#   ./setup.sh --role coordinator       # force coordinator
#   ./setup.sh --role worker            # force worker
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
info() { echo -e "${BLUE}[i]${NC} $*"; }

ROLE="${ROLE:-auto}"
WORKER_NAME="${WORKER_NAME:-$(hostname)}"

# ── Parse args ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --role) ROLE="$2"; shift 2 ;;
        --name) WORKER_NAME="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--role coordinator|worker] [--name worker-name]"
            exit 0 ;;
        *) err "Unknown arg: $1" ;;
    esac
done

# ── System checks ──────────────────────────────────────────────────
info "Checking system..."
OS="$(uname -s)"
case "$OS" in
    Linux)  PKG_MGR="apt-get" ;;
    Darwin) PKG_MGR="brew" ;;
    *)      err "Unsupported OS: $OS (Linux or macOS required)" ;;
esac

# Check for GPU (NVIDIA)
HAS_NVIDIA=false
if command -v nvidia-smi &>/dev/null; then
    HAS_NVIDIA=true
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    GPU_VRAM="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)"
    GPU_VRAM_GB=$(( (GPU_VRAM + 1023) / 1024 ))
    log "NVIDIA GPU detected: $GPU_NAME ($GPU_VRAM_GB GB VRAM)"
elif [[ "$OS" == "Darwin" ]] && system_profiler SPDisplaysDataType 2>/dev/null | grep -q "Chipset Model"; then
    # macOS with Apple Silicon or AMD GPU
    GPU_NAME="$(system_profiler SPDisplaysDataType 2>/dev/null | grep "Chipset Model" | head -1 | sed 's/.*: //')"
    HAS_APPLE_SILICON=true
    log "Apple GPU detected: $GPU_NAME"
else
    warn "No discrete GPU detected. This machine can only run CPU inference."
fi

# ── Auto-detect role ───────────────────────────────────────────────
if [[ "$ROLE" == "auto" ]]; then
    if [[ -f .pod-coordinator ]]; then
        ROLE="coordinator"
    else
        ROLE="worker"
    fi
    info "Auto-detected role: $ROLE"
fi

# ── 1. Install Tailscale ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 1/6: Tailscale (private mesh network)"
echo "═══════════════════════════════════════════════════════════════"

if command -v tailscale &>/dev/null; then
    log "Tailscale already installed: $(tailscale version | head -1)"
else
    info "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    log "Tailscale installed. Run: sudo tailscale up"
fi

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo "not-connected")"
if [[ "$TAILSCALE_IP" == "not-connected" ]]; then
    warn "Tailscale not connected. Run: sudo tailscale up"
    warn "Share your Tailscale IP with pod members once connected."
else
    log "Tailscale IP: $TAILSCALE_IP"
fi

# ── 2. Install Hyperspace CLI ─────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2/6: Hyperspace CLI (distributed LLM inference)"
echo "═══════════════════════════════════════════════════════════════"

if command -v hyperspace &>/dev/null; then
    log "Hyperspace CLI already installed: $(hyperspace --version 2>/dev/null || echo 'unknown')"
else
    info "Installing Hyperspace CLI..."
    curl -fsSL https://agents.hyper.space/api/install | bash
    log "Hyperspace installed"
fi

# Start Hyperspace daemon
info "Starting Hyperspace daemon..."
hyperspace start 2>/dev/null || warn "Hyperspace start failed (may already be running)"
sleep 3
hyperspace status 2>/dev/null || warn "Hyperspace daemon may need attention"

# ── 3. Install ComfyUI ────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 3/6: ComfyUI (image/video generation)"
echo "═══════════════════════════════════════════════════════════════"

COMFYUI_DIR="${COMFYUI_DIR:-$HOME/comfyui}"

if [[ -d "$COMFYUI_DIR/.git" ]]; then
    log "ComfyUI already installed at $COMFYUI_DIR"
    (cd "$COMFYUI_DIR" && git pull --ff-only 2>/dev/null || true)
else
    info "Cloning ComfyUI to $COMFYUI_DIR..."
    git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFYUI_DIR"
    log "ComfyUI cloned"
fi

# Install ComfyUI-Distributed extension
DIST_EXT_DIR="$COMFYUI_DIR/custom_nodes/ComfyUI-Distributed"
if [[ -d "$DIST_EXT_DIR/.git" ]]; then
    log "ComfyUI-Distributed already installed"
    (cd "$DIST_EXT_DIR" && git pull --ff-only 2>/dev/null || true)
else
    info "Cloning ComfyUI-Distributed extension..."
    git clone https://github.com/robertvoy/ComfyUI-Distributed.git "$DIST_EXT_DIR"
    log "ComfyUI-Distributed extension installed"
fi

# Install ComfyUI dependencies
if [[ -f "$COMFYUI_DIR/requirements.txt" ]]; then
    info "Installing ComfyUI Python dependencies..."
    pip install -r "$COMFYUI_DIR/requirements.txt" 2>/dev/null || warn "pip install had warnings"
fi

# ── 4. Configure ComfyUI for this machine ─────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 4/6: Configure ComfyUI role"
echo "═══════════════════════════════════════════════════════════════"

# Create config
mkdir -p "$COMFYUI_DIR/user/default"
cat > "$COMFYUI_DIR/user/default/comfy.settings.json" <<EOF
{
  "Comfy.Distributed.Role": "${ROLE}",
  "Comfy.Distributed.MasterURL": "http://\${MASTER_IP:-SET_MASTER_IP}:8188",
  "Comfy.Distributed.WorkerName": "${WORKER_NAME}",
  "Comfy.Distributed.WorkerGPU": "${GPU_NAME:-CPU}"
}
EOF

if [[ "$ROLE" == "coordinator" ]]; then
    log "Configured as ComfyUI MASTER (coordinator)"
    # Create coordinator marker
    touch .pod-coordinator
else
    log "Configured as ComfyUI WORKER"
    warn "Set MASTER_IP to the coordinator's Tailscale IP in your environment"
fi

# ── 5. Install Dispatcher (coordinator only) ─────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 5/6: Dispatcher service"
echo "═══════════════════════════════════════════════════════════════"

if [[ "$ROLE" == "coordinator" ]]; then
    info "Installing dispatcher dependencies..."
    pip install fastapi uvicorn httpx pyyaml pydantic 2>/dev/null || warn "pip install had warnings"
    log "Dispatcher ready to run: python dispatcher/main.py"
else
    log "Skipping dispatcher (worker-only machine)"
fi

# ── 6. Install Odysseus MCP servers ───────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 6/6: Odysseus MCP integration"
echo "═══════════════════════════════════════════════════════════════"

MCP_DIR="${ODYSSEUS_DIR:-$HOME/odysseus}/mcp_servers"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -d "$MCP_DIR" ]]; then
    info "Copying MCP servers to $MCP_DIR..."
    cp "$SCRIPT_DIR/mcp_servers/video_gen_server.py" "$MCP_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/mcp_servers/pod_manager_server.py" "$MCP_DIR/" 2>/dev/null || true
    log "MCP servers installed. Add to your Odysseus config:"
    echo ""
    echo "  mcp_servers:"
    echo "    video_gen:"
    echo "      command: python"
    echo "      args: [mcp_servers/video_gen_server.py]"
    echo "    pod_manager:"
    echo "      command: python"
    echo "      args: [mcp_servers/pod_manager_server.py]"
else
    warn "Odysseus directory not found at $MCP_DIR"
    info "After installing Odysseus, copy mcp_servers/ into its mcp_servers/ directory"
fi

# ── Summary ────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  SETUP COMPLETE"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Machine role:  $ROLE"
echo "Tailscale IP:  ${TAILSCALE_IP:-not connected}"
echo "GPU:           ${GPU_NAME:-none} (${GPU_VRAM_GB:-0} GB)"
echo ""
echo "─── Next steps ───────────────────────────────────────────────"
echo ""
echo "For the COORDINATOR:"
echo "  1. Create the Hyperspace pod:"
echo "       hyperspace pod create \"our-cluster\""
echo "       hyperspace pod invite --role member"
echo "  2. Start ComfyUI master:"
echo "       cd \$HOME/comfyui && python main.py --enable-cors-header --listen"
echo "  3. Start the dispatcher:"
echo "       python dispatcher/main.py"
echo ""
echo "For WORKERS:"
echo "  1. Join the Hyperspace pod:"
echo "       hyperspace pod join <invite-code>"
echo "  2. Start ComfyUI worker:"
echo "       cd \$HOME/comfyui && python main.py --enable-cors-header --listen"
echo ""
echo "All members:"
echo "  1. Start Odysseus"
echo "  2. The MCP tools (pod_manager, video_gen) will be available in chat"
echo ""
echo "Test the pod:"
echo "  hyperspace pod status"
echo "  hyperspace pod models"
echo "  hyperspace pod infer -p \"Hello from the pod!\""

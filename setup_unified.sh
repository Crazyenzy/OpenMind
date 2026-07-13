#!/bin/bash
# =============================================================================
# OpenMind Unified Setup Script
# One-command setup for the complete OpenMind distributed AI cluster
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Version
OPENMIND_VERSION="3.0.0"

# ── Helper Functions ───────────────────────────────────────────────

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_header() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# ── System Detection ───────────────────────────────────────────────

detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS=$ID
            OS_VERSION=$VERSION_ID
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
        OS_VERSION=$(sw_vers -productVersion)
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        OS="windows"
    else
        OS="unknown"
    fi
    
    log_info "Detected OS: $OS $OS_VERSION"
}

detect_gpu() {
    log_header "Detecting GPU Hardware"
    
    GPU_COUNT=0
    GPU_TYPE="none"
    GPU_NAMES=()
    GPU_VRAM=()
    
    # Check for NVIDIA GPU
    if command -v nvidia-smi &> /dev/null; then
        GPU_TYPE="nvidia"
        while IFS= read -r line; do
            if [[ $line == *"index"* ]]; then
                continue
            fi
            IFS=',' read -ra PARTS <<< "$line"
            if [ ${#PARTS[@]} -ge 3 ]; then
                GPU_NAMES+=("${PARTS[1]// /}")
                GPU_VRAM+=("${PARTS[2]// /}")
                GPU_COUNT=$((GPU_COUNT + 1))
            fi
        done < <(nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null)
        
        if [ $GPU_COUNT -gt 0 ]; then
            log_success "Found $GPU_COUNT NVIDIA GPU(s)"
            for i in "${!GPU_NAMES[@]}"; do
                echo "  GPU $i: ${GPU_NAMES[$i]} (${GPU_VRAM[$i]}MB)"
            done
        fi
    fi
    
    # Check for AMD GPU
    if [ $GPU_COUNT -eq 0 ] && command -v rocm-smi &> /dev/null; then
        GPU_TYPE="amd"
        GPU_COUNT=$(rocm-smi --showid 2>/dev/null | grep -c "GPU" || echo "0")
        if [ $GPU_COUNT -gt 0 ]; then
            log_success "Found $GPU_COUNT AMD GPU(s)"
        fi
    fi
    
    # Check for Apple Silicon
    if [ $GPU_COUNT -eq 0 ] && [[ "$OS" == "macos" ]]; then
        if sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -q "Apple"; then
            GPU_TYPE="apple"
            GPU_COUNT=1
            TOTAL_MEMORY=$(sysctl -n hw.memsize 2>/dev/null)
            GPU_VRAM=$((TOTAL_MEMORY / 1024 / 1024))
            log_success "Found Apple Silicon with unified memory"
        fi
    fi
    
    if [ $GPU_COUNT -eq 0 ]; then
        log_warning "No GPU detected. Some features will be limited."
        GPU_TYPE="cpu"
    fi
}

detect_network() {
    log_header "Detecting Network Configuration"
    
    # Get local IP
    if [[ "$OS" == "macos" ]]; then
        LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")
    else
        LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
    fi
    
    # Check for Tailscale
    HAS_TAILSCALE=false
    TAILSCALE_IP=""
    if command -v tailscale &> /dev/null; then
        TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [ -n "$TAILSCALE_IP" ]; then
            HAS_TAILSCALE=true
            log_success "Tailscale detected: $TAILSCALE_IP"
        fi
    fi
    
    log_info "Local IP: $LOCAL_IP"
}

# ── Dependency Installation ────────────────────────────────────────

install_dependencies() {
    log_header "Installing Dependencies"
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is required. Please install Python 3.10+"
        exit 1
    fi
    
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    log_info "Python version: $PYTHON_VERSION"
    
    # Check pip
    if ! command -v pip3 &> /dev/null; then
        log_error "pip3 is required. Please install pip"
        exit 1
    fi
    
    # Install Python dependencies
    log_info "Installing Python dependencies..."
    pip3 install --quiet --upgrade pip
    pip3 install --quiet -r requirements.txt
    
    log_success "Python dependencies installed"
}

install_tailscale() {
    if [ "$HAS_TAILSCALE" = true ]; then
        log_info "Tailscale already installed"
        return
    fi
    
    log_header "Installing Tailscale"
    
    read -p "Install Tailscale for secure networking? (recommended) [Y/n]: " INSTALL_TS
    INSTALL_TS=${INSTALL_TS:-Y}
    
    if [[ $INSTALL_TS =~ ^[Yy]$ ]]; then
        if [[ "$OS" == "linux" ]]; then
            curl -fsSL https://tailscale.com/install.sh | sh
            sudo tailscale up
        elif [[ "$OS" == "macos" ]]; then
            brew install tailscale
            tailscale up
        else
            log_warning "Please install Tailscale manually from https://tailscale.com"
        fi
        
        TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [ -n "$TAILSCALE_IP" ]; then
            HAS_TAILSCALE=true
            log_success "Tailscale installed: $TAILSCALE_IP"
        fi
    fi
}

install_hyperspace() {
    log_header "Installing Hyperspace AGI"
    
    if command -v hyperspace &> /dev/null; then
        log_info "Hyperspace CLI already installed"
        HYPERSPACE_VERSION=$(hyperspace --version 2>/dev/null || echo "unknown")
        log_info "Version: $HYPERSPACE_VERSION"
        return
    fi
    
    read -p "Install Hyperspace AGI for distributed LLM inference? [Y/n]: " INSTALL_HS
    INSTALL_HS=${INSTALL_HS:-Y}
    
    if [[ $INSTALL_HS =~ ^[Yy]$ ]]; then
        curl -fsSL https://agents.hyper.space/api/install | bash
        log_success "Hyperspace CLI installed"
    fi
}

install_comfyui() {
    log_header "Setting Up ComfyUI"
    
    COMFYUI_DIR="$HOME/comfyui"
    
    if [ -d "$COMFYUI_DIR" ]; then
        log_info "ComfyUI directory exists at $COMFYUI_DIR"
        read -p "Update existing installation? [y/N]: " UPDATE_COMFYUI
        if [[ $UPDATE_COMFYUI =~ ^[Yy]$ ]]; then
            cd "$COMFYUI_DIR"
            git pull
        fi
    else
        read -p "Install ComfyUI for image/video generation? [Y/n]: " INSTALL_COMFYUI
        INSTALL_COMFYUI=${INSTALL_COMFYUI:-Y}
        
        if [[ $INSTALL_COMFYUI =~ ^[Yy]$ ]]; then
            git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFYUI_DIR"
            cd "$COMFYUI_DIR"
            
            # Install dependencies
            pip3 install --quiet -r requirements.txt
            
            # Install ComfyUI-Distributed extension
            git clone https://github.com/robertvoy/ComfyUI-Distributed.git \
                "$COMFYUI_DIR/custom_nodes/ComfyUI-Distributed"
            
            log_success "ComfyUI installed at $COMFYUI_DIR"
        fi
    fi
}

# ── Auto-Configuration ────────────────────────────────────────────

configure_auto_discovery() {
    log_header "Configuring Auto-Discovery"
    
    WORKER_ID="worker_$(hostname)_$(date +%s)"
    WORKER_NAME=$(hostname)
    
    cat > "$SCRIPT_DIR/discovery_config.yaml" << EOF
# OpenMind Auto-Discovery Configuration
# Generated: $(date)

worker:
  id: "$WORKER_ID"
  name: "$WORKER_NAME"
  port: 8188

mdns:
  enabled: true
  service_name: "OpenMind Worker"

hyperspace:
  enabled: true
  discovery_interval: 60

gpu:
  auto_detect: true
  cache_duration: 300
EOF
    
    log_success "Auto-discovery configured"
}

configure_dispatcher() {
    log_header "Configuring Dispatcher"
    
    # Determine role
    if [ "$1" == "coordinator" ] || [ "$ROLE" == "coordinator" ]; then
        ROLE="coordinator"
        MASTER_IP="${TAILSCALE_IP:-$LOCAL_IP}"
    elif [ "$1" == "worker" ] || [ "$ROLE" == "worker" ]; then
        ROLE="worker"
        read -p "Enter coordinator IP address: " MASTER_IP
    else
        echo ""
        echo "Select your role:"
        echo "  1) Coordinator (runs dispatcher + ComfyUI master)"
        echo "  2) Worker (connects to coordinator)"
        echo ""
        read -p "Enter choice [1-2]: " ROLE_CHOICE
        
        if [ "$ROLE_CHOICE" == "1" ]; then
            ROLE="coordinator"
            MASTER_IP="${TAILSCALE_IP:-$LOCAL_IP}"
        else
            ROLE="worker"
            read -p "Enter coordinator IP address: " MASTER_IP
        fi
    fi
    
    # Generate API key
    API_KEY=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))")
    
    # Write configuration
    cat > "$SCRIPT_DIR/dispatcher/config.yaml" << EOF
# OpenMind Dispatcher Configuration
# Generated: $(date)
# Role: $ROLE

hyperspace:
  api_url: "http://127.0.0.1:8080"
  api_key: ""

comfyui:
  master_url: "http://${MASTER_IP}:8188"

auth:
  api_key: "$API_KEY"

cors:
  origins: "*"

redis:
  url: "redis://localhost:6379"
  key_prefix: "openmind:"

retry:
  max_retries: 2
  retry_delay_seconds: 5

limits:
  max_concurrent_video_jobs: 4
  max_concurrent_image_jobs: 8
  video_timeout_seconds: 600
  image_timeout_seconds: 120

output_dir: "auto"

# Intelligent routing
routing:
  algorithm: "multi_factor"
  factors:
    vram_weight: 0.30
    latency_weight: 0.15
    load_weight: 0.25
    affinity_weight: 0.20
    tier_weight: 0.10

# Auto-discovery
discovery:
  enabled: true
  mdns_enabled: true
  hyperspace_enabled: true
  cleanup_interval: 60
  stale_threshold: 300

# Fault tolerance
fault_tolerance:
  enabled: true
  master_failover: true
  checkpointing: true
  circuit_breaker:
    enabled: true
    failure_threshold: 5
    recovery_timeout: 60

# Audio generation
audio:
  enabled: true
  models: ["bark", "tortoise", "musicgen"]

# Batch processing
batch:
  enabled: true
  max_batch_size: 10
  priority_levels: ["urgent", "normal", "low"]
  rate_limit_per_user: 10

# Dashboard
dashboard:
  enabled: true
  port: 9001
EOF
    
    log_success "Dispatcher configured (Role: $ROLE)"
    log_info "API Key: $API_KEY"
    log_warning "Save this API key - you'll need it for authentication!"
}

# ── Service Startup ────────────────────────────────────────────────

start_services() {
    log_header "Starting OpenMind Services"
    
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    if [ "$ROLE" == "coordinator" ]; then
        log_info "Starting as Coordinator..."
        
        # Start ComfyUI Master
        if [ -d "$HOME/comfyui" ]; then
            log_info "Starting ComfyUI Master..."
            cd "$HOME/comfyui"
            python3 main.py --enable-cors-header --listen --port 8188 &
            COMFYUI_PID=$!
            sleep 5
        fi
        
        # Start Dispatcher
        log_info "Starting Dispatcher..."
        cd "$SCRIPT_DIR"
        python3 dispatcher/main.py &
        DISPATCHER_PID=$!
        
        # Start Dashboard
        log_info "Starting Dashboard..."
        python3 -m dashboard.dashboard_app &
        DASHBOARD_PID=$!
        
        log_success "Coordinator services started"
        log_info "Dispatcher: http://localhost:9000"
        log_info "Dashboard: http://localhost:9001"
        log_info "ComfyUI Master: http://localhost:8188"
        
    else
        log_info "Starting as Worker..."
        
        # Start ComfyUI Worker
        if [ -d "$HOME/comfyui" ]; then
            cd "$HOME/comfyui"
            python3 main.py --enable-cors-header --listen --port 8188 &
            COMFYUI_PID=$!
        fi
        
        log_success "Worker services started"
        log_info "ComfyUI Worker: http://localhost:8188"
    fi
    
    # Start Hyperspace Node
    if command -v hyperspace &> /dev/null; then
        log_info "Starting Hyperspace node..."
        hyperspace start &
        HYPERSPACE_PID=$!
    fi
    
    # Save PIDs
    cat > "$SCRIPT_DIR/.pids" << EOF
COMFYUI_PID=${COMFYUI_PID:-}
DISPATCHER_PID=${DISPATCHER_PID:-}
DASHBOARD_PID=${DASHBOARD_PID:-}
HYPERSPACE_PID=${HYPERSPACE_PID:-}
EOF
    
    log_success "All services started!"
}

# ── Status Check ───────────────────────────────────────────────────

check_status() {
    log_header "Checking Service Status"
    
    # Check Dispatcher
    if curl -s http://localhost:9000/api/v1/health > /dev/null 2>&1; then
        log_success "Dispatcher: Running"
    else
        log_warning "Dispatcher: Not running"
    fi
    
    # Check ComfyUI
    if curl -s http://localhost:8188/system_stats > /dev/null 2>&1; then
        log_success "ComfyUI: Running"
    else
        log_warning "ComfyUI: Not running"
    fi
    
    # Check Dashboard
    if curl -s http://localhost:9001/api/dashboard/health > /dev/null 2>&1; then
        log_success "Dashboard: Running"
    else
        log_warning "Dashboard: Not running"
    fi
    
    # Check Hyperspace
    if command -v hyperspace &> /dev/null; then
        if hyperspace status > /dev/null 2>&1; then
            log_success "Hyperspace: Running"
        else
            log_warning "Hyperspace: Not running"
        fi
    fi
    
    echo ""
    log_info "Service URLs:"
    echo "  • Dispatcher API: http://localhost:9000"
    echo "  • Dashboard: http://localhost:9001"
    echo "  • ComfyUI: http://localhost:8188"
    echo "  • API Docs: http://localhost:9000/docs"
}

# ── Main Script ────────────────────────────────────────────────────

main() {
    log_header "OpenMind v${OPENMIND_VERSION} Setup"
    
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    # Parse arguments
    ROLE=""
    SKIP_INSTALL=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            --role)
                ROLE="$2"
                shift 2
                ;;
            --skip-install)
                SKIP_INSTALL=true
                shift
                ;;
            --status)
                check_status
                exit 0
                ;;
            --help)
                echo "Usage: $0 [options]"
                echo ""
                echo "Options:"
                echo "  --role <coordinator|worker>  Set role without prompting"
                echo "  --skip-install              Skip dependency installation"
                echo "  --status                    Check service status"
                echo "  --help                      Show this help"
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done
    
    # System detection
    detect_os
    detect_gpu
    detect_network
    
    # Installation
    if [ "$SKIP_INSTALL" = false ]; then
        install_dependencies
        install_tailscale
        install_hyperspace
        install_comfyui
    fi
    
    # Configuration
    configure_auto_discovery
    configure_dispatcher "$ROLE"
    
    # Start services
    start_services
    
    # Final status
    sleep 5
    check_status
    
    log_header "Setup Complete!"
    echo ""
    echo "Your OpenMind cluster is ready!"
    echo ""
    echo "Next steps:"
    echo "  1. Share the API key with other users"
    echo "  2. Have workers run: ./setup_unified.sh --role worker"
    echo "  3. Access the dashboard at http://localhost:9001"
    echo "  4. Access Odysseus at http://localhost:3000 (if installed)"
    echo ""
    echo "For help: ./setup_unified.sh --help"
}

# Run main function
main "$@"

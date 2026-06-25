# =============================================================================
# setup.ps1 — Automated setup for OpenMind (Windows)
# =============================================================================
#
# Run this on every Windows machine in the pod. It auto-detects the machine's
# role (coordinator or worker), installs dependencies, and configures services.
#
# Usage:
#   .\setup.ps1                          # auto-detect role
#   .\setup.ps1 -Role coordinator        # force coordinator
#   .\setup.ps1 -Role worker             # force worker
#   .\setup.ps1 -Role worker -Name gpu1  # force worker with custom name
#   .\setup.ps1 -Help                    # show help
# =============================================================================

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet("coordinator", "worker", "auto")]
    [string]$Role = "auto",

    [Parameter()]
    [string]$Name = $env:COMPUTERNAME,

    [Parameter()]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

# ── Logging helpers ────────────────────────────────────────────────
function Write-Log {
    param([string]$Message)
    Write-Host "[+] $Message" -ForegroundColor Green
}
function Write-Warn {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}
function Write-Err {
    param([string]$Message)
    Write-Host "[x] $Message" -ForegroundColor Red
    exit 1
}
function Write-Info {
    param([string]$Message)
    Write-Host "[i] $Message" -ForegroundColor Cyan
}

function Write-StepBanner {
    param([string]$Text)
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor White
    Write-Host "  $Text" -ForegroundColor White
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor White
}

# ── Help ───────────────────────────────────────────────────────────
if ($Help) {
    Write-Host "Usage: .\setup.ps1 [-Role coordinator|worker] [-Name worker-name] [-Help]"
    Write-Host ""
    Write-Host "Parameters:"
    Write-Host "  -Role    Machine role: coordinator, worker, or auto (default: auto)"
    Write-Host "  -Name    Worker display name (default: hostname)"
    Write-Host "  -Help    Show this help message"
    exit 0
}

# ── System checks ─────────────────────────────────────────────────
Write-Info "Checking system..."

if ($env:OS -ne "Windows_NT") {
    Write-Err "This script requires Windows. Use setup.sh for Linux/macOS."
}

# Check for GPU (NVIDIA)
$HAS_NVIDIA = $false
$GPU_NAME   = "none"
$GPU_VRAM_GB = 0

try {
    $null = Get-Command nvidia-smi -ErrorAction Stop
    $GPU_NAME = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1).Trim()
    $GPU_VRAM_RAW = (& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1).Trim()
    if ($GPU_VRAM_RAW -match '^\d+$') {
        $GPU_VRAM_GB = [math]::Ceiling([int]$GPU_VRAM_RAW / 1024)
    }
    $HAS_NVIDIA = $true
    Write-Log "NVIDIA GPU detected: $GPU_NAME ($GPU_VRAM_GB GB VRAM)"
}
catch {
    Write-Warn "No NVIDIA GPU detected. This machine can only run CPU inference."
}

# ── Auto-detect role ──────────────────────────────────────────────
if ($Role -eq "auto") {
    if (Test-Path -Path ".pod-coordinator") {
        $Role = "coordinator"
    }
    else {
        $Role = "worker"
    }
    Write-Info "Auto-detected role: $Role"
}

# ── 1. Install Tailscale ─────────────────────────────────────────
Write-StepBanner "STEP 1/6: Tailscale (private mesh network)"

$TailscaleInstalled = $false
try {
    $null = Get-Command tailscale -ErrorAction Stop
    $TailscaleInstalled = $true
}
catch {
    $TailscaleInstalled = $false
}

if ($TailscaleInstalled) {
    try {
        $tsVersion = (& tailscale version 2>$null | Select-Object -First 1)
        Write-Log "Tailscale already installed: $tsVersion"
    }
    catch {
        Write-Log "Tailscale already installed"
    }
}
else {
    Write-Info "Installing Tailscale via winget..."
    try {
        winget install Tailscale.Tailscale --accept-source-agreements --accept-package-agreements
        Write-Log "Tailscale installed. You may need to restart your terminal, then run: tailscale up"
    }
    catch {
        Write-Warn "Tailscale installation failed. Install manually from https://tailscale.com/download/windows"
    }
}

$TailscaleIP = "not-connected"
try {
    $tsIP = (& tailscale ip -4 2>$null).Trim()
    if ($tsIP) { $TailscaleIP = $tsIP }
}
catch { }

if ($TailscaleIP -eq "not-connected") {
    Write-Warn "Tailscale not connected. Run: tailscale up"
    Write-Warn "Share your Tailscale IP with pod members once connected."
}
else {
    Write-Log "Tailscale IP: $TailscaleIP"
}

# ── 2. Install Hyperspace CLI ────────────────────────────────────
Write-StepBanner "STEP 2/6: Hyperspace CLI (distributed LLM inference)"

$HyperspaceInstalled = $false
try {
    $null = Get-Command hyperspace -ErrorAction Stop
    $HyperspaceInstalled = $true
}
catch {
    $HyperspaceInstalled = $false
}

if ($HyperspaceInstalled) {
    try {
        $hsVersion = (& hyperspace --version 2>$null)
        Write-Log "Hyperspace CLI already installed: $hsVersion"
    }
    catch {
        Write-Log "Hyperspace CLI already installed"
    }
}
else {
    Write-Info "Installing Hyperspace CLI..."
    try {
        # Download and run the Hyperspace installer for Windows
        $installerUrl = "https://agents.hyper.space/api/install?platform=windows"
        $installerPath = Join-Path $env:TEMP "hyperspace-install.ps1"
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
        & powershell -ExecutionPolicy Bypass -File $installerPath
        Write-Log "Hyperspace installed"
    }
    catch {
        Write-Warn "Hyperspace installation failed. Visit https://hyper.space for manual install."
    }
}

# Start Hyperspace daemon
Write-Info "Starting Hyperspace daemon..."
try {
    & hyperspace start 2>$null
}
catch {
    Write-Warn "Hyperspace start failed (may already be running)"
}
Start-Sleep -Seconds 3
try {
    & hyperspace status 2>$null
}
catch {
    Write-Warn "Hyperspace daemon may need attention"
}

# ── 3. Install ComfyUI ───────────────────────────────────────────
Write-StepBanner "STEP 3/6: ComfyUI (image/video generation)"

$ComfyUIDir = if ($env:COMFYUI_DIR) { $env:COMFYUI_DIR } else { Join-Path $env:USERPROFILE "comfyui" }

if (Test-Path (Join-Path $ComfyUIDir ".git")) {
    Write-Log "ComfyUI already installed at $ComfyUIDir"
    try {
        Push-Location $ComfyUIDir
        & git pull --ff-only 2>$null
        Pop-Location
    }
    catch {
        Pop-Location
        # Ignore pull failures
    }
}
else {
    Write-Info "Cloning ComfyUI to $ComfyUIDir..."
    try {
        & git clone https://github.com/comfyanonymous/ComfyUI.git $ComfyUIDir
        Write-Log "ComfyUI cloned"
    }
    catch {
        Write-Err "Failed to clone ComfyUI. Ensure git is installed and accessible."
    }
}

# Install ComfyUI-Distributed extension
$DistExtDir = Join-Path $ComfyUIDir "custom_nodes\ComfyUI-Distributed"
if (Test-Path (Join-Path $DistExtDir ".git")) {
    Write-Log "ComfyUI-Distributed already installed"
    try {
        Push-Location $DistExtDir
        & git pull --ff-only 2>$null
        Pop-Location
    }
    catch {
        Pop-Location
    }
}
else {
    Write-Info "Cloning ComfyUI-Distributed extension..."
    try {
        & git clone https://github.com/robertvoy/ComfyUI-Distributed.git $DistExtDir
        Write-Log "ComfyUI-Distributed extension installed"
    }
    catch {
        Write-Warn "Failed to clone ComfyUI-Distributed extension."
    }
}

# Install ComfyUI dependencies
$reqFile = Join-Path $ComfyUIDir "requirements.txt"
if (Test-Path $reqFile) {
    Write-Info "Installing ComfyUI Python dependencies..."
    try {
        & pip install -r $reqFile 2>$null
    }
    catch {
        Write-Warn "pip install had warnings"
    }
}

# ── 4. Configure ComfyUI for this machine ────────────────────────
Write-StepBanner "STEP 4/6: Configure ComfyUI role"

# Determine GPU label for config
$gpuLabel = if ($HAS_NVIDIA) { $GPU_NAME } else { "CPU" }

# Determine master URL placeholder
$masterIP = if ($env:MASTER_IP) { $env:MASTER_IP } else { "SET_MASTER_IP" }

# Create config directory and settings file
$configDir = Join-Path $ComfyUIDir "user\default"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

$configContent = @"
{
  "Comfy.Distributed.Role": "$Role",
  "Comfy.Distributed.MasterURL": "http://${masterIP}:8188",
  "Comfy.Distributed.WorkerName": "$Name",
  "Comfy.Distributed.WorkerGPU": "$gpuLabel"
}
"@

$configPath = Join-Path $configDir "comfy.settings.json"
Set-Content -Path $configPath -Value $configContent -Encoding UTF8

if ($Role -eq "coordinator") {
    Write-Log "Configured as ComfyUI MASTER (coordinator)"
    # Create coordinator marker
    New-Item -ItemType File -Path ".pod-coordinator" -Force | Out-Null
}
else {
    Write-Log "Configured as ComfyUI WORKER"
    Write-Warn "Set MASTER_IP environment variable to the coordinator's Tailscale IP"
}

# ── 5. Install Dispatcher (coordinator only) ─────────────────────
Write-StepBanner "STEP 5/6: Dispatcher service"

if ($Role -eq "coordinator") {
    Write-Info "Installing dispatcher dependencies..."
    try {
        & pip install fastapi uvicorn httpx pyyaml pydantic 2>$null
        Write-Log "Dispatcher ready to run: python dispatcher\main.py"
    }
    catch {
        Write-Warn "pip install had warnings"
    }
}
else {
    Write-Log "Skipping dispatcher (worker-only machine)"
}

# ── 6. Install Odysseus MCP servers ──────────────────────────────
Write-StepBanner "STEP 6/6: Odysseus MCP integration"

$OdysseusBase = if ($env:ODYSSEUS_DIR) { $env:ODYSSEUS_DIR } else { Join-Path $env:USERPROFILE "odysseus" }
$MCPDir = Join-Path $OdysseusBase "mcp_servers"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

if (Test-Path $MCPDir) {
    Write-Info "Copying MCP servers to $MCPDir..."
    try {
        Copy-Item (Join-Path $ScriptDir "mcp_servers\video_gen_server.py") $MCPDir -Force -ErrorAction SilentlyContinue
        Copy-Item (Join-Path $ScriptDir "mcp_servers\pod_manager_server.py") $MCPDir -Force -ErrorAction SilentlyContinue
        Write-Log "MCP servers installed. Add to your Odysseus config:"
        Write-Host ""
        Write-Host "  mcp_servers:"
        Write-Host "    video_gen:"
        Write-Host "      command: python"
        Write-Host "      args: [mcp_servers/video_gen_server.py]"
        Write-Host "    pod_manager:"
        Write-Host "      command: python"
        Write-Host "      args: [mcp_servers/pod_manager_server.py]"
    }
    catch {
        Write-Warn "Failed to copy some MCP server files."
    }
}
else {
    Write-Warn "Odysseus directory not found at $MCPDir"
    Write-Info "After installing Odysseus, copy mcp_servers\ into its mcp_servers\ directory"
}

# ── Summary ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor White
Write-Host "  SETUP COMPLETE" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor White
Write-Host ""
Write-Host "Machine role:  $Role"
Write-Host "Tailscale IP:  $TailscaleIP"
Write-Host "GPU:           $GPU_NAME ($GPU_VRAM_GB GB)"
Write-Host ""
Write-Host "--- Next steps --------------------------------------------" -ForegroundColor Cyan
Write-Host ""
Write-Host "For the COORDINATOR:" -ForegroundColor Green
Write-Host "  1. Create the Hyperspace pod:"
Write-Host "       hyperspace pod create `"our-cluster`""
Write-Host "       hyperspace pod invite --role member"
Write-Host "  2. Start ComfyUI master:"
Write-Host "       cd `$env:USERPROFILE\comfyui; python main.py --enable-cors-header --listen"
Write-Host "  3. Start the dispatcher:"
Write-Host "       python dispatcher\main.py"
Write-Host ""
Write-Host "For WORKERS:" -ForegroundColor Green
Write-Host "  1. Join the Hyperspace pod:"
Write-Host "       hyperspace pod join <invite-code>"
Write-Host "  2. Start ComfyUI worker:"
Write-Host "       cd `$env:USERPROFILE\comfyui; python main.py --enable-cors-header --listen"
Write-Host ""
Write-Host "All members:" -ForegroundColor Green
Write-Host "  1. Start Odysseus"
Write-Host "  2. The MCP tools (pod_manager, video_gen) will be available in chat"
Write-Host ""
Write-Host "Test the pod:" -ForegroundColor Green
Write-Host "  hyperspace pod status"
Write-Host "  hyperspace pod models"
Write-Host "  hyperspace pod infer -p `"Hello from the pod!`""

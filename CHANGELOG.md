# Changelog

All notable changes to OpenMind will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-07-13

### Added - Production Features
- **Auto-Discovery Service** (`discovery/`): mDNS/Bonjour-based automatic worker discovery
  - Zero-config worker registration on local networks
  - Hyperspace peer discovery integration for remote workers
  - Automatic GPU capability detection and reporting
  
- **GPU Profiler** (`gpu_profiler/`): Automatic hardware detection
  - NVIDIA GPU detection via `nvidia-smi`
  - AMD GPU detection via `rocm-smi`
  - Apple Silicon detection via Metal
  - VRAM, compute capability, and driver version reporting
  - Model compatibility scoring based on hardware

- **Intelligent Router** (`dispatcher/router.py`): Advanced load balancing
  - Multi-factor scoring: VRAM, GPU architecture, network latency, current load
  - Model-aware routing (different models have different optimal GPUs)
  - Affinity scoring for model reuse (avoid reloading)
  - Network latency measurement between nodes
  - Load prediction based on job history

- **Health Monitoring Dashboard** (`dashboard/`): Web UI for cluster management
  - Real-time cluster status visualization
  - GPU utilization charts (Prometheus/Grafana integration)
  - Job queue monitoring with progress tracking
  - Worker health status with auto-refresh
  - Alert management and notification system
  - Mobile-responsive design

- **Fault Tolerance** (`dispatcher/fault_tolerance.py`):
  - ComfyUI master failover with leader election
  - Job checkpointing for long-running video generation
  - Automatic worker recovery and re-registration
  - Circuit breaker pattern for failing workers
  - Graceful degradation when workers go offline

- **CI/CD Pipeline** (`.github/workflows/`):
  - Automated testing on push/PR
  - Multi-Python version testing (3.10, 3.11, 3.12)
  - Docker image building and publishing
  - Security scanning with Trivy
  - Automated release creation
  - Code coverage reporting

- **More Video Models** (`comfyui_workflows/`):
  - Stable Video Diffusion (SVD) workflow
  - ModelScope text-to-video workflow
  - Open-Sora text-to-video workflow
  - Zeroscope XL workflow
  - Model-specific VRAM requirements and recommendations

- **Audio Generation Support** (`mcp_servers/audio_gen_server.py`):
  - Bark TTS integration
  - Tortoise TTS integration
  - MusicGen music generation
  - Audio upscaling and enhancement
  - Voice cloning support

- **Batch Processing API** (`dispatcher/batch_api.py`):
  - Multi-user queue management
  - Priority lanes (urgent, normal, low)
  - Batch job submission
  - Progress tracking for batch operations
  - Rate limiting per user

- **Security Hardening**:
  - TLS support for all inter-component communication
  - Role-based access control (RBAC) with user management
  - Audit logging for all operations
  - API key rotation support
  - IP whitelisting option

### Changed
- Dispatcher version bumped to 3.0.0
- Improved worker selection algorithm with multi-factor scoring
- Enhanced error handling and retry logic
- Updated documentation with production deployment guide

### Fixed
- Worker discovery now works across subnets
- GPU memory reporting accuracy improved
- Job queue ordering now respects priorities
- WebSocket reconnection logic improved

## [2.0.0] - 2026-06-27

### Added
- Initial integration of Odysseus + Hyperspace AGI + ComfyUI-Distributed
- Dispatcher service with job routing and load balancing
- MCP servers for video generation and pod management
- Docker Compose deployment configuration
- Setup scripts for Linux/macOS/Windows
- Basic worker registration and heartbeat system
- Redis persistence with graceful fallback

### Fixed
- Replaced non-existent Docker images with local builds
- Removed docker.sock security vulnerability
- Unified Docker networking to host mode
- Added missing workflow files
- Fixed CORS configuration to be environment-configurable
- Implemented timing-attack-safe API key comparison
- Corrected Odysseus license attribution

## [1.0.0] - 2026-06-25

### Added
- Initial project structure
- Basic architecture documentation
- Proof of concept integration

---

## Version Numbering Guide

- **Major (X.0.0)**: Breaking changes, major feature additions
- **Minor (0.X.0)**: New features, backward compatible
- **Patch (0.0.X)**: Bug fixes, minor improvements

## Upgrade Instructions

### From 2.x to 3.x

1. **Backup your configuration**:
   ```bash
   cp dispatcher/config.yaml dispatcher/config.yaml.backup
   ```

2. **Update the code**:
   ```bash
   git pull origin main
   ```

3. **Install new dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run database migrations** (if using Redis):
   ```bash
   python scripts/migrate_v3.py
   ```

5. **Update configuration** (see `dispatcher/config.yaml.example` for new options)

6. **Restart services**:
   ```bash
   docker compose down
   docker compose --profile coordinator up -d
   ```

### New Configuration Options (v3.0)

```yaml
# dispatcher/config.yaml

# Auto-discovery settings
discovery:
  enabled: true
  mdns_enabled: true
  hyperspace_integration: true
  discovery_interval: 30  # seconds

# GPU profiling
gpu_profiler:
  enabled: true
  cache_duration: 300  # seconds
  nvidia_smi_path: "nvidia-smi"  # or full path

# Intelligent routing
routing:
  algorithm: "multi_factor"  # options: round_robin, least_loaded, multi_factor
  factors:
    vram_weight: 0.3
    latency_weight: 0.2
    load_weight: 0.3
    affinity_weight: 0.2
  latency_measurement:
    enabled: true
    interval: 60  # seconds

# Health monitoring dashboard
dashboard:
  enabled: true
  port: 9001
  prometheus_enabled: true
  grafana_enabled: true

# Fault tolerance
fault_tolerance:
  master_failover: true
  leader_election_timeout: 30
  checkpointing_enabled: true
  checkpoint_interval: 60
  circuit_breaker:
    enabled: true
    failure_threshold: 5
    recovery_timeout: 60

# Audio generation
audio:
  enabled: true
  models:
    - bark
    - tortoise
    - musicgen

# Batch processing
batch:
  enabled: true
  max_batch_size: 10
  priority_levels: ["urgent", "normal", "low"]
  rate_limit_per_user: 10  # jobs per minute

# Security
security:
  tls_enabled: false
  tls_cert_path: ""
  tls_key_path: ""
  rbac_enabled: false
  audit_logging: true
  ip_whitelist: []
```

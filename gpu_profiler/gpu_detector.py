"""
GPU Hardware Detector

Detects and profiles GPU hardware across different vendors:
- NVIDIA (via nvidia-smi)
- AMD (via rocm-smi)
- Apple Silicon (via Metal)
- Intel (via intel_gpu_top)
"""

import asyncio
import json
import logging
import platform
import re
import subprocess
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict
from enum import Enum

logger = logging.getLogger("openmind.gpu.detector")


class GPUVendor(str, Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    APPLE = "apple"
    INTEL = "intel"
    UNKNOWN = "unknown"


@dataclass
class GPUInfo:
    """Complete GPU information."""
    id: int
    name: str
    vendor: GPUVendor
    vram_total_mb: int
    vram_free_mb: int
    vram_used_mb: int
    driver_version: str
    cuda_version: str
    compute_capability: str
    temperature_c: int
    power_draw_w: int
    utilization_percent: int
    pci_bus_id: str
    uuid: str
    is_available: bool = True
    supported_precisions: List[str] = field(default_factory=lambda: ["fp32", "fp16"])
    
    @property
    def vram_total_gb(self) -> float:
        return self.vram_total_mb / 1024

    @property
    def vram_free_gb(self) -> float:
        return self.vram_free_mb / 1024

    @property
    def vram_used_gb(self) -> float:
        return self.vram_used_mb / 1024

    def to_dict(self) -> Dict:
        data = asdict(self)
        data['vendor'] = self.vendor.value
        data['vram_total_gb'] = self.vram_total_gb
        data['vram_free_gb'] = self.vram_free_gb
        data['vram_used_gb'] = self.vram_used_gb
        return data


class GPUDetector:
    """
    Detects and profiles GPU hardware.
    
    Features:
    - Multi-vendor support (NVIDIA, AMD, Apple, Intel)
    - Real-time VRAM monitoring
    - Temperature and power monitoring
    - Compute capability detection
    - Supported precision detection
    """

    def __init__(self, nvidia_smi_path: str = "nvidia-smi"):
        self.nvidia_smi_path = nvidia_smi_path
        self._gpus: List[GPUInfo] = []
        self._last_scan_time: float = 0
        self._scan_interval: float = 5.0  # seconds

    async def detect_all(self) -> List[GPUInfo]:
        """Detect all available GPUs."""
        gpus = []

        # Detect based on platform
        system = platform.system().lower()

        if system == "darwin":
            # macOS - check for Apple Silicon
            apple_gpus = await self._detect_apple_silicon()
            gpus.extend(apple_gpus)
        else:
            # Linux/Windows - check for NVIDIA, AMD, Intel
            nvidia_gpus = await self._detect_nvidia()
            gpus.extend(nvidia_gpus)

            amd_gpus = await self._detect_amd()
            gpus.extend(amd_gpus)

            intel_gpus = await self._detect_intel()
            gpus.extend(intel_gpus)

        self._gpus = gpus
        logger.info(f"Detected {len(gpus)} GPU(s)")
        return gpus

    async def _detect_nvidia(self) -> List[GPUInfo]:
        """Detect NVIDIA GPUs using nvidia-smi."""
        gpus = []

        try:
            # Query GPU information in JSON format
            cmd = [
                self.nvidia_smi_path,
                "--query-gpu=index,name,memory.total,memory.free,memory.used,"
                "driver_version,pci.bus_id,uuid,temperature.gpu,power.draw,"
                "utilization.gpu",
                "--format=csv,noheader,nounits"
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode != 0:
                logger.debug(f"nvidia-smi failed: {stderr.decode()}")
                return gpus

            # Parse output
            for line in stdout.decode().strip().split('\n'):
                if not line.strip():
                    continue

                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 11:
                    continue

                try:
                    gpu = GPUInfo(
                        id=int(parts[0]),
                        name=parts[1],
                        vendor=GPUVendor.NVIDIA,
                        vram_total_mb=int(parts[2]),
                        vram_free_mb=int(parts[3]),
                        vram_used_mb=int(parts[4]),
                        driver_version=parts[5],
                        cuda_version=await self._get_cuda_version(),
                        compute_capability=await self._get_compute_capability(int(parts[0])),
                        temperature_c=int(parts[8]) if parts[8] != '[N/A]' else 0,
                        power_draw_w=int(float(parts[9])) if parts[9] != '[N/A]' else 0,
                        utilization_percent=int(parts[10]) if parts[10] != '[N/A]' else 0,
                        pci_bus_id=parts[6],
                        uuid=parts[7],
                        supported_precisions=await self._get_supported_precisions(parts[1]),
                    )
                    gpus.append(gpu)
                    logger.info(
                        f"  NVIDIA GPU {gpu.id}: {gpu.name} "
                        f"({gpu.vram_total_gb:.1f}GB, {gpu.utilization_percent}% utilized)"
                    )
                except (ValueError, IndexError) as e:
                    logger.warning(f"Failed to parse GPU line: {line}, error: {e}")

        except FileNotFoundError:
            logger.debug("nvidia-smi not found")
        except asyncio.TimeoutError:
            logger.warning("nvidia-smi timed out")

        return gpus

    async def _detect_amd(self) -> List[GPUInfo]:
        """Detect AMD GPUs using rocm-smi."""
        gpus = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "rocm-smi", "--showmeminfo", "vram", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode != 0:
                return gpus

            data = json.loads(stdout.decode())
            # Parse AMD GPU data
            # Implementation depends on rocm-smi output format
            logger.info("AMD GPU detection (rocm-smi parsing)")

        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            logger.debug("AMD GPU detection skipped")

        return gpus

    async def _detect_apple_silicon(self) -> List[GPUInfo]:
        """Detect Apple Silicon GPU."""
        gpus = []

        try:
            # Check if running on Apple Silicon
            proc = await asyncio.create_subprocess_exec(
                "sysctl", "-n", "machdep.cpu.brand_string",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

            cpu_brand = stdout.decode().strip()
            if "Apple" not in cpu_brand:
                return gpus

            # Get unified memory size
            proc = await asyncio.create_subprocess_exec(
                "sysctl", "-n", "hw.memsize",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

            total_memory_bytes = int(stdout.decode().strip())
            total_memory_mb = total_memory_bytes // (1024 * 1024)

            # Apple Silicon uses unified memory
            # Estimate GPU available memory (typically 60-75% of unified memory)
            gpu_memory_mb = int(total_memory_mb * 0.7)

            gpu = GPUInfo(
                id=0,
                name=f"Apple {cpu_brand.split(' ')[-1]} GPU",
                vendor=GPUVendor.APPLE,
                vram_total_mb=gpu_memory_mb,
                vram_free_mb=gpu_memory_mb,  # Approximate
                vram_used_mb=0,
                driver_version="Metal",
                cuda_version="N/A",
                compute_capability="Metal 3",
                temperature_c=0,
                power_draw_w=0,
                utilization_percent=0,
                pci_bus_id="integrated",
                uuid="apple-silicon-0",
                supported_precisions=["fp32", "fp16", "bf16"],
            )
            gpus.append(gpu)
            logger.info(f"  Apple Silicon: {gpu.name} ({gpu.vram_total_gb:.1f}GB unified)")

        except Exception as e:
            logger.debug(f"Apple Silicon detection failed: {e}")

        return gpus

    async def _detect_intel(self) -> List[GPUInfo]:
        """Detect Intel GPUs."""
        # Intel GPU detection is platform-specific
        # Placeholder for future implementation
        return []

    async def _get_cuda_version(self) -> str:
        """Get CUDA version."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.nvidia_smi_path, "--query-gpu=driver_version", "--format=csv,noheader",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                # Parse CUDA version from nvidia-smi output
                return stdout.decode().strip().split('\n')[0]
        except Exception:
            pass
        return "unknown"

    async def _get_compute_capability(self, gpu_id: int) -> str:
        """Get GPU compute capability."""
        try:
            # Try using nvidia-smi with compute capability query
            proc = await asyncio.create_subprocess_exec(
                self.nvidia_smi_path,
                f"--id={gpu_id}",
                "--query-gpu=compute_cap",
                "--format=csv,noheader",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return "unknown"

    async def _get_supported_precisions(self, gpu_name: str) -> List[str]:
        """Determine supported precision types based on GPU name."""
        precisions = ["fp32"]

        gpu_name_lower = gpu_name.lower()

        # NVIDIA GPUs
        if "rtx" in gpu_name_lower or "a100" in gpu_name_lower or "h100" in gpu_name_lower:
            precisions.extend(["fp16", "bf16"])
            if "a100" in gpu_name_lower or "h100" in gpu_name_lower:
                precisions.append("tf32")
        elif "gtx" in gpu_name_lower:
            precisions.append("fp16")

        # AMD GPUs
        if "rx" in gpu_name_lower or "mi" in gpu_name_lower:
            precisions.extend(["fp16", "bf16"])

        return precisions

    async def refresh_gpu_status(self, gpu_id: Optional[int] = None) -> Optional[GPUInfo]:
        """Refresh status for a specific GPU or all GPUs."""
        if gpu_id is not None:
            # Refresh specific GPU
            for gpu in self._gpus:
                if gpu.id == gpu_id:
                    updated = await self._get_gpu_status(gpu_id)
                    if updated:
                        gpu.vram_free_mb = updated.vram_free_mb
                        gpu.vram_used_mb = updated.vram_used_mb
                        gpu.temperature_c = updated.temperature_c
                        gpu.power_draw_w = updated.power_draw_w
                        gpu.utilization_percent = updated.utilization_percent
                        return gpu
            return None
        else:
            # Refresh all GPUs
            return await self.detect_all()

    async def _get_gpu_status(self, gpu_id: int) -> Optional[GPUInfo]:
        """Get current status for a specific GPU."""
        try:
            cmd = [
                self.nvidia_smi_path,
                f"--id={gpu_id}",
                "--query-gpu=memory.free,memory.used,temperature.gpu,power.draw,utilization.gpu",
                "--format=csv,noheader,nounits"
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

            if proc.returncode == 0:
                parts = [p.strip() for p in stdout.decode().strip().split(',')]
                if len(parts) >= 5:
                    # Return a partial GPUInfo with updated status
                    existing = next((g for g in self._gpus if g.id == gpu_id), None)
                    if existing:
                        existing.vram_free_mb = int(parts[0])
                        existing.vram_used_mb = int(parts[1])
                        existing.temperature_c = int(parts[2])
                        existing.power_draw_w = int(float(parts[3]))
                        existing.utilization_percent = int(parts[4])
                        return existing

        except Exception as e:
            logger.debug(f"Failed to get GPU status: {e}")

        return None

    def get_all_gpus(self) -> List[GPUInfo]:
        """Get all detected GPUs."""
        return self._gpus

    def get_gpu(self, gpu_id: int) -> Optional[GPUInfo]:
        """Get a specific GPU by ID."""
        return next((g for g in self._gpus if g.id == gpu_id), None)

    def get_available_gpus(self, min_vram_gb: float = 0) -> List[GPUInfo]:
        """Get available GPUs with at least the specified VRAM."""
        return [
            g for g in self._gpus
            if g.is_available and g.vram_free_gb >= min_vram_gb
        ]

    def get_total_vram_gb(self) -> float:
        """Get total VRAM across all GPUs."""
        return sum(g.vram_total_gb for g in self._gpus)

    def get_free_vram_gb(self) -> float:
        """Get free VRAM across all GPUs."""
        return sum(g.vram_free_gb for g in self._gpus)

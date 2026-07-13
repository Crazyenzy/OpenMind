"""
GPU Profiler - Higher-level GPU profiling with caching and model recommendations.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from .gpu_detector import GPUDetector, GPUInfo, GPUVendor

logger = logging.getLogger("openmind.gpu.profiler")


@dataclass
class ModelRequirement:
    """VRAM and compute requirements for a model."""
    name: str
    min_vram_gb: float
    recommended_vram_gb: float
    supported_vendors: List[GPUVendor]
    supported_precisions: List[str]
    min_compute_capability: str  # e.g., "7.0" for Turing+
    description: str


class GPUProfiler:
    """
    High-level GPU profiling with caching and model compatibility checking.
    
    Features:
    - Cached GPU detection (configurable refresh interval)
    - Model compatibility scoring
    - VRAM requirement estimation
    - Performance tier classification
    """

    # Model VRAM requirements database
    MODEL_REQUIREMENTS: Dict[str, ModelRequirement] = {
        # Video Models
        "wan2.2": ModelRequirement(
            name="Wan 2.2",
            min_vram_gb=16,
            recommended_vram_gb=24,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="High quality text-to-video generation"
        ),
        "wan2.2-i2v": ModelRequirement(
            name="Wan 2.2 Image-to-Video",
            min_vram_gb=20,
            recommended_vram_gb=24,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="Image-to-video generation with Wan 2.2"
        ),
        "ltx-video": ModelRequirement(
            name="LTX-Video",
            min_vram_gb=8,
            recommended_vram_gb=12,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD, GPUVendor.APPLE],
            supported_precisions=["fp16", "bf16", "fp32"],
            min_compute_capability="6.0",
            description="Fast video generation, good for lower VRAM GPUs"
        ),
        "hunyuan": ModelRequirement(
            name="Hunyuan Video",
            min_vram_gb=32,
            recommended_vram_gb=48,
            supported_vendors=[GPUVendor.NVIDIA],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="8.0",
            description="High quality video generation (Ampere+ recommended)"
        ),
        "cogvideox": ModelRequirement(
            name="CogVideoX-5B",
            min_vram_gb=12,
            recommended_vram_gb=16,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="5B parameter video model"
        ),
        "animatediff": ModelRequirement(
            name="AnimateDiff",
            min_vram_gb=6,
            recommended_vram_gb=8,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD, GPUVendor.APPLE],
            supported_precisions=["fp16", "bf16", "fp32"],
            min_compute_capability="6.0",
            description="Animated image generation"
        ),
        "stable-video-diffusion": ModelRequirement(
            name="Stable Video Diffusion",
            min_vram_gb=10,
            recommended_vram_gb=16,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="Stability AI's video diffusion model"
        ),
        "open-sora": ModelRequirement(
            name="Open-Sora",
            min_vram_gb=16,
            recommended_vram_gb=24,
            supported_vendors=[GPUVendor.NVIDIA],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="Open-source text-to-video model"
        ),
        
        # Image Models
        "sdxl": ModelRequirement(
            name="Stable Diffusion XL",
            min_vram_gb=6,
            recommended_vram_gb=8,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD, GPUVendor.APPLE],
            supported_precisions=["fp16", "bf16", "fp32"],
            min_compute_capability="6.0",
            description="High quality image generation"
        ),
        "flux": ModelRequirement(
            name="Flux",
            min_vram_gb=12,
            recommended_vram_gb=16,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="Fast high-quality image generation"
        ),
        "flux-schnell": ModelRequirement(
            name="Flux Schnell",
            min_vram_gb=8,
            recommended_vram_gb=12,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "bf16"],
            min_compute_capability="7.0",
            description="Fast image generation (schnell variant)"
        ),
        
        # Audio Models
        "bark": ModelRequirement(
            name="Bark TTS",
            min_vram_gb=4,
            recommended_vram_gb=8,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD, GPUVendor.APPLE],
            supported_precisions=["fp16", "fp32"],
            min_compute_capability="6.0",
            description="Text-to-speech with voice cloning"
        ),
        "tortoise-tts": ModelRequirement(
            name="Tortoise TTS",
            min_vram_gb=6,
            recommended_vram_gb=12,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD],
            supported_precisions=["fp16", "fp32"],
            min_compute_capability="6.0",
            description="High quality text-to-speech"
        ),
        "musicgen": ModelRequirement(
            name="MusicGen",
            min_vram_gb=4,
            recommended_vram_gb=8,
            supported_vendors=[GPUVendor.NVIDIA, GPUVendor.AMD, GPUVendor.APPLE],
            supported_precisions=["fp16", "fp32"],
            min_compute_capability="6.0",
            description="Music generation from text prompts"
        ),
    }

    def __init__(self, cache_duration: int = 300):
        self._detector = GPUDetector()
        self._cache_duration = cache_duration
        self._last_scan: float = 0
        self._cached_gpus: List[GPUInfo] = []
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the profiler and perform initial GPU detection."""
        try:
            self._cached_gpus = await self._detector.detect_all()
            self._last_scan = time.time()
            self._initialized = True
            return len(self._cached_gpus) > 0
        except Exception as e:
            logger.error(f"GPU profiler initialization failed: {e}")
            return False

    async def get_gpus(self, force_refresh: bool = False) -> List[GPUInfo]:
        """Get detected GPUs with caching."""
        if not self._initialized:
            await self.initialize()

        # Check if cache is stale
        if force_refresh or (time.time() - self._last_scan > self._cache_duration):
            self._cached_gpus = await self._detector.detect_all()
            self._last_scan = time.time()

        return self._cached_gpus

    async def refresh_gpu_status(self, gpu_id: Optional[int] = None) -> Optional[GPUInfo]:
        """Refresh status for a specific GPU."""
        return await self._detector.refresh_gpu_status(gpu_id)

    def check_model_compatibility(
        self,
        model_name: str,
        gpu: GPUInfo,
    ) -> Tuple[bool, float, List[str]]:
        """
        Check if a model can run on a specific GPU.
        
        Returns:
            Tuple of (can_run, compatibility_score, reasons)
        """
        requirements = self.MODEL_REQUIREMENTS.get(model_name)
        if not requirements:
            return False, 0.0, [f"Unknown model: {model_name}"]

        reasons = []
        score = 100.0

        # Check VRAM
        if gpu.vram_free_gb < requirements.min_vram_gb:
            return False, 0.0, [
                f"Insufficient VRAM: {gpu.vram_free_gb:.1f}GB free, "
                f"need {requirements.min_vram_gb:.1f}GB minimum"
            ]
        
        if gpu.vram_free_gb < requirements.recommended_vram_gb:
            score -= 20
            reasons.append(
                f"VRAM below recommended: {gpu.vram_free_gb:.1f}GB free, "
                f"recommended {requirements.recommended_vram_gb:.1f}GB"
            )

        # Check vendor support
        if gpu.vendor not in requirements.supported_vendors:
            return False, 0.0, [
                f"GPU vendor '{gpu.vendor.value}' not supported for {model_name}"
            ]

        # Check precision support
        has_precision = any(p in gpu.supported_precisions for p in requirements.supported_precisions)
        if not has_precision:
            score -= 30
            reasons.append("Missing required precision support")

        # Check compute capability (NVIDIA only)
        if gpu.vendor == GPUVendor.NVIDIA and requirements.min_compute_capability != "0.0":
            try:
                gpu_cc = float(gpu.compute_capability.split('.')[0] + '.' + gpu.compute_capability.split('.')[1])
                min_cc = float(requirements.min_compute_capability)
                if gpu_cc < min_cc:
                    return False, 0.0, [
                        f"Compute capability {gpu.compute_capability} below minimum {requirements.min_compute_capability}"
                    ]
            except (ValueError, IndexError):
                pass

        # Check utilization (prefer less utilized GPUs)
        if gpu.utilization_percent > 80:
            score -= 15
            reasons.append(f"GPU heavily utilized ({gpu.utilization_percent}%)")

        return True, max(0, score), reasons

    def get_compatible_models(
        self,
        gpu: GPUInfo,
        category: Optional[str] = None,
    ) -> List[Tuple[str, float, ModelRequirement]]:
        """
        Get all models compatible with a GPU, sorted by compatibility score.
        
        Args:
            gpu: GPU to check compatibility for
            category: Optional filter (video, image, audio)
        """
        compatible = []

        for model_name, req in self.MODEL_REQUIREMENTS.items():
            # Filter by category if specified
            if category:
                if category == "video" and model_name not in [
                    "wan2.2", "wan2.2-i2v", "ltx-video", "hunyuan", "cogvideox",
                    "animatediff", "stable-video-diffusion", "open-sora"
                ]:
                    continue
                elif category == "image" and model_name not in [
                    "sdxl", "flux", "flux-schnell"
                ]:
                    continue
                elif category == "audio" and model_name not in [
                    "bark", "tortoise-tts", "musicgen"
                ]:
                    continue

            can_run, score, reasons = self.check_model_compatibility(model_name, gpu)
            if can_run:
                compatible.append((model_name, score, req))

        # Sort by score (higher is better)
        compatible.sort(key=lambda x: x[1], reverse=True)
        return compatible

    def get_best_gpu_for_model(
        self,
        model_name: str,
        gpus: Optional[List[GPUInfo]] = None,
    ) -> Optional[Tuple[GPUInfo, float]]:
        """
        Find the best GPU for a specific model.
        
        Returns:
            Tuple of (best_gpu, compatibility_score) or None
        """
        if gpus is None:
            gpus = self._cached_gpus

        if not gpus:
            return None

        candidates = []
        for gpu in gpus:
            if not gpu.is_available:
                continue
            can_run, score, reasons = self.check_model_compatibility(model_name, gpu)
            if can_run:
                candidates.append((gpu, score))

        if not candidates:
            return None

        # Sort by score
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0]

    def estimate_model_vram(self, model_name: str, precision: str = "fp16") -> float:
        """Estimate VRAM requirement for a model in GB."""
        requirements = self.MODEL_REQUIREMENTS.get(model_name)
        if not requirements:
            return 0.0

        # Base requirement from database
        base_vram = requirements.recommended_vram_gb

        # Adjust for precision
        if precision == "fp32":
            base_vram *= 2
        elif precision == "int8":
            base_vram *= 0.5
        elif precision == "int4":
            base_vram *= 0.25

        return base_vram

    def get_performance_tier(self, gpu: GPUInfo) -> str:
        """Classify GPU into performance tier."""
        vram = gpu.vram_total_gb
        compute_cap = gpu.compute_capability

        if vram >= 80:
            return "ultra"  # A100, H100
        elif vram >= 48:
            return "high"   # A6000, RTX 4090
        elif vram >= 24:
            return "medium" # RTX 3090, RTX 4080
        elif vram >= 12:
            return "low"    # RTX 3060, RTX 4060
        else:
            return "minimal" # GTX 1650, etc.

    def get_gpu_summary(self) -> Dict:
        """Get a summary of all detected GPUs."""
        gpus = self._cached_gpus
        
        return {
            "total_gpus": len(gpus),
            "total_vram_gb": sum(g.vram_total_gb for g in gpus),
            "free_vram_gb": sum(g.vram_free_gb for g in gpus),
            "gpus": [
                {
                    "id": g.id,
                    "name": g.name,
                    "vendor": g.vendor.value,
                    "vram_total_gb": g.vram_total_gb,
                    "vram_free_gb": g.vram_free_gb,
                    "utilization_percent": g.utilization_percent,
                    "performance_tier": self.get_performance_tier(g),
                }
                for g in gpus
            ],
            "compatible_models": {
                model_name: any(
                    self.check_model_compatibility(model_name, g)[0]
                    for g in gpus
                )
                for model_name in self.MODEL_REQUIREMENTS
            }
        }

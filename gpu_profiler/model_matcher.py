"""
Model Matcher - Intelligent model-to-GPU matching based on requirements and capabilities.
"""

import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from .gpu_detector import GPUInfo, GPUVendor
from .gpu_profiler import GPUProfiler, ModelRequirement

logger = logging.getLogger("openmind.gpu.matcher")


@dataclass
class MatchResult:
    """Result of model-GPU matching."""
    gpu: GPUInfo
    model_name: str
    score: float
    estimated_vram_gb: float
    estimated_time_seconds: float
    precision: str
    reasons: List[str]


class ModelMatcher:
    """
    Intelligent model-to-GPU matching system.
    
    Features:
    - Multi-factor scoring (VRAM, latency, affinity, utilization)
    - Automatic precision selection
    - Performance estimation
    - Load balancing across GPUs
    """

    # Scoring weights
    WEIGHTS = {
        'vram': 0.30,
        'utilization': 0.25,
        'affinity': 0.20,
        'precision': 0.15,
        'tier': 0.10,
    }

    # Performance estimates (relative speed multipliers)
    PRECISION_SPEED = {
        'fp32': 1.0,
        'fp16': 1.8,
        'bf16': 1.7,
        'int8': 2.5,
        'int4': 3.5,
    }

    TIER_SPEED = {
        'ultra': 4.0,
        'high': 3.0,
        'medium': 2.0,
        'low': 1.0,
        'minimal': 0.5,
    }

    def __init__(self, profiler: GPUProfiler):
        self.profiler = profiler
        self._affinity_cache: Dict[str, Dict[int, float]] = {}  # model -> {gpu_id: affinity_score}
        self._job_history: List[Dict] = []

    async def find_best_match(
        self,
        model_name: str,
        job_type: str = "video",
        exclude_gpu_ids: Optional[set] = None,
        prefer_gpu_id: Optional[int] = None,
    ) -> Optional[MatchResult]:
        """
        Find the best GPU for a model/job combination.
        
        Args:
            model_name: Name of the model to run
            job_type: Type of job (video, image, audio)
            exclude_gpu_ids: GPU IDs to exclude (e.g., failed GPUs)
            prefer_gpu_id: Preferred GPU ID (for affinity)
            
        Returns:
            MatchResult or None if no suitable GPU found
        """
        gpus = await self.profiler.get_gpus()
        if not gpus:
            logger.warning("No GPUs available")
            return None

        exclude_gpu_ids = exclude_gpu_ids or set()
        candidates = []

        for gpu in gpus:
            if gpu.id in exclude_gpu_ids:
                continue
            if not gpu.is_available:
                continue

            # Check basic compatibility
            can_run, compat_score, reasons = self.profiler.check_model_compatibility(
                model_name, gpu
            )
            if not can_run:
                continue

            # Calculate detailed score
            score = self._calculate_score(
                gpu, model_name, compat_score, prefer_gpu_id
            )

            # Estimate performance
            precision = self._select_precision(gpu, model_name)
            estimated_vram = self.profiler.estimate_model_vram(model_name, precision)
            estimated_time = self._estimate_time(model_name, gpu, precision, job_type)

            candidates.append(MatchResult(
                gpu=gpu,
                model_name=model_name,
                score=score,
                estimated_vram_gb=estimated_vram,
                estimated_time_seconds=estimated_time,
                precision=precision,
                reasons=reasons,
            ))

        if not candidates:
            logger.warning(f"No compatible GPU found for model {model_name}")
            return None

        # Sort by score (highest first)
        candidates.sort(key=lambda x: x.score, reverse=True)
        best = candidates[0]

        logger.info(
            f"Best match for {model_name}: GPU {best.gpu.id} ({best.gpu.name}) "
            f"with score {best.score:.1f}"
        )

        return best

    def _calculate_score(
        self,
        gpu: GPUInfo,
        model_name: str,
        compat_score: float,
        prefer_gpu_id: Optional[int],
    ) -> float:
        """Calculate multi-factor score for GPU-model pairing."""
        score = 0.0

        # VRAM score (prefer GPUs with more free VRAM, but not excessively)
        vram_req = self.profiler.MODEL_REQUIREMENTS.get(model_name)
        if vram_req:
            vram_ratio = gpu.vram_free_gb / vram_req.recommended_vram_gb
            vram_score = min(vram_ratio * 50, 100)  # Cap at 100
            score += vram_score * self.WEIGHTS['vram']

        # Utilization score (prefer less utilized GPUs)
        util_score = 100 - gpu.utilization_percent
        score += util_score * self.WEIGHTS['utilization']

        # Affinity score (prefer GPUs that previously ran this model efficiently)
        affinity_score = self._get_affinity_score(model_name, gpu.id)
        score += affinity_score * self.WEIGHTS['affinity']

        # Precision support score
        precision_score = self._get_precision_score(gpu, model_name)
        score += precision_score * self.WEIGHTS['precision']

        # Performance tier score
        tier = self.profiler.get_performance_tier(gpu)
        tier_score = self.TIER_SPEED.get(tier, 0.5) * 25  # Normalize to 0-100
        score += tier_score * self.WEIGHTS['tier']

        # Preference bonus
        if prefer_gpu_id is not None and gpu.id == prefer_gpu_id:
            score += 10

        return score

    def _get_affinity_score(self, model_name: str, gpu_id: int) -> float:
        """Get affinity score based on job history."""
        if model_name not in self._affinity_cache:
            return 50.0  # Neutral score for new model-GPU pairs

        affinity = self._affinity_cache[model_name].get(gpu_id, 50.0)
        return affinity

    def _get_precision_score(self, gpu: GPUInfo, model_name: str) -> float:
        """Score based on precision support."""
        req = self.profiler.MODEL_REQUIREMENTS.get(model_name)
        if not req:
            return 50.0

        # Check if GPU supports required precisions
        supported = set(gpu.supported_precisions)
        required = set(req.supported_precisions)

        if required.issubset(supported):
            return 100.0
        elif supported.intersection(required):
            return 70.0
        else:
            return 30.0

    def _select_precision(self, gpu: GPUInfo, model_name: str) -> str:
        """Select the best precision for a GPU-model combination."""
        req = self.profiler.MODEL_REQUIREMENTS.get(model_name)
        if not req:
            return "fp16"

        # Prefer faster precisions that are supported
        preferred_order = ["int4", "int8", "bf16", "fp16", "fp32"]
        
        for precision in preferred_order:
            if precision in gpu.supported_precisions and precision in req.supported_precisions:
                return precision

        # Fallback to fp16 if available, otherwise fp32
        if "fp16" in gpu.supported_precisions:
            return "fp16"
        return "fp32"

    def _estimate_time(
        self,
        model_name: str,
        gpu: GPUInfo,
        precision: str,
        job_type: str,
    ) -> float:
        """Estimate job completion time in seconds."""
        # Base times per model (in seconds, for reference GPU)
        base_times = {
            # Video models (per second of output)
            "wan2.2": 30,
            "wan2.2-i2v": 35,
            "ltx-video": 15,
            "hunyuan": 60,
            "cogvideox": 25,
            "animatediff": 10,
            "stable-video-diffusion": 20,
            "open-sora": 40,
            
            # Image models
            "sdxl": 5,
            "flux": 3,
            "flux-schnell": 1,
            
            # Audio models
            "bark": 5,
            "tortoise-tts": 30,
            "musicgen": 10,
        }

        base_time = base_times.get(model_name, 20)

        # Adjust for GPU tier
        tier = self.profiler.get_performance_tier(gpu)
        tier_multiplier = 1.0 / self.TIER_SPEED.get(tier, 1.0)

        # Adjust for precision
        precision_multiplier = 1.0 / self.PRECISION_SPEED.get(precision, 1.0)

        # Adjust for utilization
        util_multiplier = 1.0 + (gpu.utilization_percent / 100)

        estimated_time = base_time * tier_multiplier * precision_multiplier * util_multiplier

        return round(estimated_time, 1)

    def update_affinity(
        self,
        model_name: str,
        gpu_id: int,
        success: bool,
        actual_time: Optional[float] = None,
        estimated_time: Optional[float] = None,
    ):
        """Update affinity score based on job outcome."""
        if model_name not in self._affinity_cache:
            self._affinity_cache[model_name] = {}

        current_affinity = self._affinity_cache[model_name].get(gpu_id, 50.0)

        if success:
            # Increase affinity on success
            if actual_time and estimated_time:
                # Bonus for being faster than estimated
                speedup = estimated_time / actual_time
                bonus = min(speedup * 10, 30)  # Cap bonus at 30
                current_affinity = min(100, current_affinity + bonus)
            else:
                current_affinity = min(100, current_affinity + 10)
        else:
            # Decrease affinity on failure
            current_affinity = max(0, current_affinity - 20)

        self._affinity_cache[model_name][gpu_id] = current_affinity

        # Record in job history
        self._job_history.append({
            'model': model_name,
            'gpu_id': gpu_id,
            'success': success,
            'actual_time': actual_time,
            'estimated_time': estimated_time,
            'affinity': current_affinity,
        })

        # Keep history manageable
        if len(self._job_history) > 1000:
            self._job_history = self._job_history[-500:]

    def get_match_stats(self) -> Dict:
        """Get matching statistics."""
        return {
            'total_jobs': len(self._job_history),
            'success_rate': (
                sum(1 for j in self._job_history if j['success']) / len(self._job_history)
                if self._job_history else 0
            ),
            'average_speedup': self._calculate_average_speedup(),
            'affinity_cache_size': sum(
                len(gpus) for gpus in self._affinity_cache.values()
            ),
        }

    def _calculate_average_speedup(self) -> float:
        """Calculate average speedup from job history."""
        speedups = []
        for job in self._job_history:
            if job.get('actual_time') and job.get('estimated_time'):
                speedup = job['estimated_time'] / job['actual_time']
                speedups.append(speedup)

        return sum(speedups) / len(speedups) if speedups else 1.0

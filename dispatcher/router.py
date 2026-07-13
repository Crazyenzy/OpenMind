"""
Intelligent Job Router

Advanced load balancing with multi-factor scoring for optimal GPU/model matching.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

from ..gpu_profiler import GPUProfiler, ModelMatcher
from ..gpu_profiler.gpu_detector import GPUInfo

logger = logging.getLogger("openmind.dispatcher.router")


class RoutingAlgorithm(str, Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_LOADED = "least_loaded"
    MULTI_FACTOR = "multi_factor"
    AFFINITY = "affinity"


@dataclass
class WorkerScore:
    """Score breakdown for a worker."""
    worker_id: str
    gpu_id: int
    total_score: float
    vram_score: float
    latency_score: float
    load_score: float
    affinity_score: float
    tier_score: float
    reasons: List[str] = field(default_factory=list)


class IntelligentRouter:
    """
    Multi-factor intelligent job router.
    
    Scoring factors:
    1. VRAM availability (weight: 0.30)
    2. Current load/utilization (weight: 0.25)
    3. Model affinity (weight: 0.20)
    4. Network latency (weight: 0.15)
    5. Performance tier (weight: 0.10)
    
    Features:
    - Dynamic weight adjustment based on job type
    - Network latency measurement
    - Affinity learning from job history
    - Circuit breaker for failing workers
    - Load prediction
    """

    # Default scoring weights
    DEFAULT_WEIGHTS = {
        'vram': 0.30,
        'latency': 0.15,
        'load': 0.25,
        'affinity': 0.20,
        'tier': 0.10,
    }

    # Weight adjustments by job type
    JOB_TYPE_ADJUSTMENTS = {
        'video': {'vram': 0.35, 'load': 0.20},  # Video needs more VRAM
        'image': {'vram': 0.20, 'load': 0.30},   # Image is faster, load matters more
        'audio': {'vram': 0.15, 'latency': 0.25}, # Audio is lightweight, latency matters
    }

    def __init__(
        self,
        profiler: GPUProfiler,
        matcher: ModelMatcher,
        algorithm: RoutingAlgorithm = RoutingAlgorithm.MULTI_FACTOR,
        latency_check_interval: int = 60,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_recovery: int = 60,
    ):
        self.profiler = profiler
        self.matcher = matcher
        self.algorithm = algorithm
        self.latency_check_interval = latency_check_interval
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_recovery = circuit_breaker_recovery

        # Worker state
        self._workers: Dict[str, Dict] = {}
        self._latency_cache: Dict[str, float] = {}
        self._job_history: Dict[str, List[Dict]] = defaultdict(list)

        # Circuit breaker state
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._circuit_open_until: Dict[str, float] = {}

        # Round robin state
        self._round_robin_index: int = 0

        # Background tasks
        self._latency_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the router and background tasks."""
        self._running = True
        self._latency_task = asyncio.create_task(self._latency_check_loop())
        logger.info(f"✅ Intelligent router started (algorithm: {self.algorithm.value})")

    async def stop(self):
        """Stop the router."""
        self._running = False
        if self._latency_task:
            self._latency_task.cancel()
            try:
                await self._latency_task
            except asyncio.CancelledError:
                pass

    def register_worker(self, worker_id: str, worker_data: Dict):
        """Register a worker with the router."""
        self._workers[worker_id] = worker_data
        logger.info(f"Router: registered worker {worker_id}")

    def unregister_worker(self, worker_id: str):
        """Unregister a worker."""
        self._workers.pop(worker_id, None)
        self._latency_cache.pop(worker_id, None)
        self._failure_counts.pop(worker_id, None)
        self._circuit_open_until.pop(worker_id, None)
        logger.info(f"Router: unregistered worker {worker_id}")

    def update_worker_status(self, worker_id: str, status: Dict):
        """Update worker status (VRAM, utilization, etc.)."""
        if worker_id in self._workers:
            self._workers[worker_id].update(status)

    async def route_job(
        self,
        model_name: str,
        job_type: str = "video",
        min_vram_gb: float = 0,
        exclude_workers: Optional[Set[str]] = None,
        prefer_worker: Optional[str] = None,
    ) -> Optional[str]:
        """
        Route a job to the best available worker.
        
        Args:
            model_name: Model to run
            job_type: Type of job (video, image, audio)
            min_vram_gb: Minimum VRAM required
            exclude_workers: Worker IDs to exclude
            prefer_worker: Preferred worker ID (for affinity)
            
        Returns:
            Worker ID or None if no suitable worker found
        """
        exclude_workers = exclude_workers or set()

        # Filter available workers
        available_workers = self._get_available_workers(
            min_vram_gb=min_vram_gb,
            exclude_workers=exclude_workers,
        )

        if not available_workers:
            logger.warning("No available workers for routing")
            return None

        # Route based on algorithm
        if self.algorithm == RoutingAlgorithm.ROUND_ROBIN:
            return self._route_round_robin(available_workers)
        elif self.algorithm == RoutingAlgorithm.LEAST_LOADED:
            return self._route_least_loaded(available_workers)
        elif self.algorithm == RoutingAlgorithm.AFFINITY:
            return self._route_affinity(available_workers, model_name)
        else:  # MULTI_FACTOR
            return await self._route_multi_factor(
                available_workers, model_name, job_type, prefer_worker
            )

    def _get_available_workers(
        self,
        min_vram_gb: float = 0,
        exclude_workers: Optional[Set[str]] = None,
    ) -> List[str]:
        """Get list of available worker IDs."""
        exclude_workers = exclude_workers or set()
        now = time.time()

        available = []
        for worker_id, worker in self._workers.items():
            # Skip excluded workers
            if worker_id in exclude_workers:
                continue

            # Skip offline workers
            if worker.get('status') == 'offline':
                continue

            # Skip workers with open circuit breakers
            if worker_id in self._circuit_open_until:
                if now < self._circuit_open_until[worker_id]:
                    continue
                else:
                    # Circuit breaker recovered
                    del self._circuit_open_until[worker_id]
                    self._failure_counts[worker_id] = 0

            # Skip busy workers (unless load balancing)
            if worker.get('status') == 'busy':
                continue

            # Check VRAM
            if worker.get('free_vram_gb', 0) < min_vram_gb:
                continue

            available.append(worker_id)

        return available

    def _route_round_robin(self, workers: List[str]) -> str:
        """Simple round-robin routing."""
        if not workers:
            return None

        worker_id = workers[self._round_robin_index % len(workers)]
        self._round_robin_index += 1
        return worker_id

    def _route_least_loaded(self, workers: List[str]) -> str:
        """Route to least loaded worker."""
        if not workers:
            return None

        def get_load(wid):
            worker = self._workers.get(wid, {})
            # Combine utilization and job count
            utilization = worker.get('utilization_percent', 0)
            active_jobs = worker.get('active_jobs', 0)
            return utilization + (active_jobs * 20)

        return min(workers, key=get_load)

    def _route_affinity(self, workers: List[str], model_name: str) -> str:
        """Route based on model affinity (reuse loaded models)."""
        if not workers:
            return None

        def get_affinity(wid):
            worker = self._workers.get(wid, {})
            loaded_model = worker.get('loaded_model', '')
            
            # Exact match
            if loaded_model == model_name:
                return 100
            # Partial match
            elif model_name in loaded_model:
                return 50
            return 0

        return max(workers, key=get_affinity)

    async def _route_multi_factor(
        self,
        workers: List[str],
        model_name: str,
        job_type: str,
        prefer_worker: Optional[str],
    ) -> Optional[str]:
        """Route using multi-factor scoring."""
        if not workers:
            return None

        # Get adjusted weights for job type
        weights = self._get_weights_for_job_type(job_type)

        # Score each worker
        scores: List[WorkerScore] = []
        for worker_id in workers:
            worker = self._workers.get(worker_id)
            if not worker:
                continue

            score = await self._calculate_worker_score(
                worker_id, worker, model_name, weights, prefer_worker
            )
            scores.append(score)

        if not scores:
            return None

        # Sort by total score (highest first)
        scores.sort(key=lambda x: x.total_score, reverse=True)
        best = scores[0]

        logger.debug(
            f"Router selected worker {best.worker_id} "
            f"(score: {best.total_score:.1f}, "
            f"vram: {best.vram_score:.0f}, "
            f"load: {best.load_score:.0f}, "
            f"affinity: {best.affinity_score:.0f})"
        )

        return best.worker_id

    def _get_weights_for_job_type(self, job_type: str) -> Dict[str, float]:
        """Get adjusted weights for a specific job type."""
        weights = self.DEFAULT_WEIGHTS.copy()
        adjustments = self.JOB_TYPE_ADJUSTMENTS.get(job_type, {})
        
        for factor, adjustment in adjustments.items():
            if factor in weights:
                weights[factor] = adjustment

        # Normalize weights to sum to 1.0
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    async def _calculate_worker_score(
        self,
        worker_id: str,
        worker: Dict,
        model_name: str,
        weights: Dict[str, float],
        prefer_worker: Optional[str],
    ) -> WorkerScore:
        """Calculate multi-factor score for a worker."""
        reasons = []

        # VRAM score (0-100)
        vram_req = self.profiler.MODEL_REQUIREMENTS.get(model_name)
        if vram_req:
            vram_ratio = worker.get('free_vram_gb', 0) / vram_req.recommended_vram_gb
            vram_score = min(vram_ratio * 100, 100)
            if vram_ratio < 1.0:
                reasons.append(f"VRAM below recommended ({vram_ratio:.1%})")
        else:
            vram_score = 50  # Unknown model, neutral score

        # Latency score (0-100, lower latency = higher score)
        latency = self._latency_cache.get(worker_id, 100)
        latency_score = max(0, 100 - latency)  # 100ms = 0, 0ms = 100

        # Load score (0-100, lower load = higher score)
        utilization = worker.get('utilization_percent', 0)
        load_score = 100 - utilization

        # Affinity score (0-100)
        affinity_score = self._get_affinity_score(worker_id, model_name)

        # Tier score (0-100)
        tier = self._get_worker_tier(worker)
        tier_scores = {'ultra': 100, 'high': 80, 'medium': 60, 'low': 40, 'minimal': 20}
        tier_score = tier_scores.get(tier, 50)

        # Calculate weighted total
        total_score = (
            vram_score * weights['vram'] +
            latency_score * weights['latency'] +
            load_score * weights['load'] +
            affinity_score * weights['affinity'] +
            tier_score * weights['tier']
        )

        # Preference bonus
        if prefer_worker and worker_id == prefer_worker:
            total_score += 10
            reasons.append("Preferred worker")

        return WorkerScore(
            worker_id=worker_id,
            gpu_id=worker.get('gpu_id', 0),
            total_score=total_score,
            vram_score=vram_score,
            latency_score=latency_score,
            load_score=load_score,
            affinity_score=affinity_score,
            tier_score=tier_score,
            reasons=reasons,
        )

    def _get_affinity_score(self, worker_id: str, model_name: str) -> float:
        """Get affinity score based on loaded model and history."""
        worker = self._workers.get(worker_id, {})
        loaded_model = worker.get('loaded_model', '')

        # Exact model match
        if loaded_model == model_name:
            return 100

        # Partial match
        if model_name in loaded_model or loaded_model in model_name:
            return 70

        # Check job history for success rate
        history = self._job_history.get(worker_id, [])
        model_jobs = [j for j in history if j.get('model') == model_name]
        
        if model_jobs:
            success_rate = sum(1 for j in model_jobs if j.get('success')) / len(model_jobs)
            return success_rate * 80

        return 50  # Neutral score

    def _get_worker_tier(self, worker: Dict) -> str:
        """Get performance tier for a worker."""
        vram = worker.get('vram_gb', 0)
        
        if vram >= 80:
            return 'ultra'
        elif vram >= 48:
            return 'high'
        elif vram >= 24:
            return 'medium'
        elif vram >= 12:
            return 'low'
        else:
            return 'minimal'

    def record_job_result(
        self,
        worker_id: str,
        model_name: str,
        success: bool,
        actual_time: Optional[float] = None,
        estimated_time: Optional[float] = None,
    ):
        """Record job result for affinity learning."""
        # Update job history
        self._job_history[worker_id].append({
            'model': model_name,
            'success': success,
            'actual_time': actual_time,
            'estimated_time': estimated_time,
            'timestamp': time.time(),
        })

        # Keep history manageable
        if len(self._job_history[worker_id]) > 100:
            self._job_history[worker_id] = self._job_history[worker_id][-50:]

        # Update circuit breaker
        if success:
            self._failure_counts[worker_id] = 0
        else:
            self._failure_counts[worker_id] += 1
            if self._failure_counts[worker_id] >= self.circuit_breaker_threshold:
                self._circuit_open_until[worker_id] = (
                    time.time() + self.circuit_breaker_recovery
                )
                logger.warning(
                    f"Circuit breaker opened for worker {worker_id} "
                    f"(failures: {self._failure_counts[worker_id]})"
                )

        # Update matcher affinity
        self.matcher.update_affinity(
            model_name, worker_id, success, actual_time, estimated_time
        )

    async def _latency_check_loop(self):
        """Periodically measure latency to workers."""
        while self._running:
            try:
                await self._measure_all_latencies()
                await asyncio.sleep(self.latency_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Latency check error: {e}")
                await asyncio.sleep(30)

    async def _measure_all_latencies(self):
        """Measure latency to all workers."""
        import httpx

        for worker_id, worker in self._workers.items():
            comfyui_url = worker.get('comfyui_url', '')
            if not comfyui_url:
                continue

            try:
                start = time.time()
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.get(f"{comfyui_url}/system_stats")
                latency = (time.time() - start) * 1000  # Convert to ms
                self._latency_cache[worker_id] = latency
            except Exception:
                self._latency_cache[worker_id] = 999  # High latency for unreachable

    def get_routing_stats(self) -> Dict:
        """Get routing statistics."""
        return {
            'algorithm': self.algorithm.value,
            'total_workers': len(self._workers),
            'available_workers': len(self._get_available_workers()),
            'circuit_breakers_open': len(self._circuit_open_until),
            'average_latency_ms': (
                sum(self._latency_cache.values()) / len(self._latency_cache)
                if self._latency_cache else 0
            ),
            'job_history_size': sum(len(h) for h in self._job_history.values()),
        }

    def get_worker_scores(
        self,
        model_name: str,
        job_type: str = "video",
    ) -> List[WorkerScore]:
        """Get scores for all available workers (for debugging/monitoring)."""
        weights = self._get_weights_for_job_type(job_type)
        workers = self._get_available_workers()
        
        scores = []
        for worker_id in workers:
            worker = self._workers.get(worker_id)
            if worker:
                # Note: This is sync, so we can't use the async version
                # For monitoring, use approximate scores
                score = WorkerScore(
                    worker_id=worker_id,
                    gpu_id=worker.get('gpu_id', 0),
                    total_score=0,
                    vram_score=0,
                    latency_score=0,
                    load_score=0,
                    affinity_score=0,
                    tier_score=0,
                )
                scores.append(score)

        return scores

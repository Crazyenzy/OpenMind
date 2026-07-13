"""
Fault Tolerance Module

Provides:
- ComfyUI master failover with leader election
- Job checkpointing for long-running jobs
- Automatic worker recovery
- Circuit breaker pattern
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import aiofiles

logger = logging.getLogger("openmind.dispatcher.fault_tolerance")


class CircuitState(str, Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """Circuit breaker for a worker."""
    worker_id: str
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0
    next_retry_time: float = 0
    success_count: int = 0

    def record_success(self):
        """Record a successful operation."""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 3:  # Require 3 successes to close
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                logger.info(f"Circuit breaker CLOSED for worker {self.worker_id}")
        elif self.state == CircuitState.CLOSED:
            self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self):
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.success_count = 0

        if self.failure_count >= 5:  # Threshold
            self.state = CircuitState.OPEN
            self.next_retry_time = time.time() + 60  # 60 second recovery
            logger.warning(f"Circuit breaker OPENED for worker {self.worker_id}")

    def can_execute(self) -> bool:
        """Check if requests can be executed."""
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            if time.time() >= self.next_retry_time:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit breaker HALF_OPEN for worker {self.worker_id}")
                return True
            return False
        else:  # HALF_OPEN
            return True


@dataclass
class JobCheckpoint:
    """Checkpoint for a long-running job."""
    job_id: str
    job_type: str
    model_name: str
    status: str
    progress: float
    worker_id: str
    comfyui_prompt_id: str
    parameters: Dict
    created_at: float
    updated_at: float
    checkpoint_data: Dict = field(default_factory=dict)


class MasterFailover:
    """
    ComfyUI master failover with leader election.
    
    Features:
    - Health monitoring of master node
    - Automatic failover to backup master
    - Leader election among available nodes
    - State synchronization
    """

    def __init__(
        self,
        master_urls: List[str],
        health_check_interval: int = 30,
        failover_timeout: int = 60,
    ):
        self.master_urls = master_urls
        self.health_check_interval = health_check_interval
        self.failover_timeout = failover_timeout

        self._current_master: Optional[str] = None
        self._master_health: Dict[str, bool] = {url: True for url in master_urls}
        self._master_latency: Dict[str, float] = {url: 0 for url in master_urls}
        self._leader: Optional[str] = None
        self._failover_in_progress: bool = False

        self._health_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_failover = None
        self._on_master_change = None

    async def start(self):
        """Start master health monitoring."""
        self._running = True
        
        # Set initial master
        if self.master_urls:
            self._current_master = self.master_urls[0]
            self._leader = self._current_master

        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info(f"Master failover started, current master: {self._current_master}")

    async def stop(self):
        """Stop master failover."""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

    def on_failover(self, callback):
        """Register failover callback."""
        self._on_failover = callback

    def on_master_change(self, callback):
        """Register master change callback."""
        self._on_master_change = callback

    async def _health_check_loop(self):
        """Periodically check master health."""
        while self._running:
            try:
                await self._check_all_masters()
                await asyncio.sleep(self.health_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                await asyncio.sleep(10)

    async def _check_all_masters(self):
        """Check health of all master nodes."""
        import httpx

        for url in self.master_urls:
            try:
                start = time.time()
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{url}/system_stats")
                    resp.raise_for_status()

                latency = (time.time() - start) * 1000
                self._master_health[url] = True
                self._master_latency[url] = latency

            except Exception as e:
                self._master_health[url] = False
                logger.warning(f"Master {url} health check failed: {e}")

        # Check if current master is healthy
        if self._current_master and not self._master_health.get(self._current_master, False):
            await self._initiate_failover()

    async def _initiate_failover(self):
        """Initiate failover to a healthy master."""
        if self._failover_in_progress:
            return

        self._failover_in_progress = True
        old_master = self._current_master

        logger.warning(f"Initiating failover from {old_master}")

        # Find best alternative master
        healthy_masters = [
            url for url, healthy in self._master_health.items()
            if healthy and url != old_master
        ]

        if not healthy_masters:
            logger.error("No healthy masters available for failover!")
            self._failover_in_progress = False
            return

        # Select master with lowest latency
        new_master = min(healthy_masters, key=lambda u: self._master_latency.get(u, 999))

        # Perform leader election
        elected = await self._elect_leader(healthy_masters)
        if elected:
            new_master = elected

        # Switch to new master
        self._current_master = new_master
        self._leader = new_master
        self._failover_in_progress = False

        logger.info(f"Failover complete: {old_master} -> {new_master}")

        # Notify callbacks
        if self._on_failover:
            await self._on_failover(old_master, new_master)
        if self._on_master_change:
            await self._on_master_change(new_master)

    async def _elect_leader(self, candidates: List[str]) -> Optional[str]:
        """Elect a leader among candidate masters."""
        if not candidates:
            return None

        # Simple election: lowest latency wins
        # In production, use a proper consensus algorithm (Raft, Paxos)
        return min(candidates, key=lambda u: self._master_latency.get(u, 999))

    def get_current_master(self) -> Optional[str]:
        """Get the current active master URL."""
        return self._current_master

    def get_master_status(self) -> Dict:
        """Get status of all masters."""
        return {
            'current_master': self._current_master,
            'leader': self._leader,
            'failover_in_progress': self._failover_in_progress,
            'masters': {
                url: {
                    'healthy': self._master_health.get(url, False),
                    'latency_ms': self._master_latency.get(url, 0),
                    'is_current': url == self._current_master,
                }
                for url in self.master_urls
            }
        }


class JobCheckpointer:
    """
    Job checkpointing for fault tolerance.
    
    Saves job state periodically so jobs can be resumed after failures.
    """

    def __init__(
        self,
        checkpoint_dir: str = "/tmp/openmind/checkpoints",
        checkpoint_interval: int = 60,
    ):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_interval = checkpoint_interval

        self._checkpoints: Dict[str, JobCheckpoint] = {}
        self._checkpoint_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start checkpointing service."""
        self._running = True
        self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())
        
        # Load existing checkpoints
        await self._load_checkpoints()
        
        logger.info(f"Job checkpointer started (dir: {self.checkpoint_dir})")

    async def stop(self):
        """Stop checkpointing service."""
        self._running = False
        if self._checkpoint_task:
            self._checkpoint_task.cancel()
            try:
                await self._checkpoint_task
            except asyncio.CancelledError:
                pass

        # Save final checkpoints
        await self._save_all_checkpoints()

    async def create_checkpoint(
        self,
        job_id: str,
        job_type: str,
        model_name: str,
        status: str,
        progress: float,
        worker_id: str,
        comfyui_prompt_id: str,
        parameters: Dict,
        checkpoint_data: Optional[Dict] = None,
    ):
        """Create or update a job checkpoint."""
        checkpoint = JobCheckpoint(
            job_id=job_id,
            job_type=job_type,
            model_name=model_name,
            status=status,
            progress=progress,
            worker_id=worker_id,
            comfyui_prompt_id=comfyui_prompt_id,
            parameters=parameters,
            created_at=time.time(),
            updated_at=time.time(),
            checkpoint_data=checkpoint_data or {},
        )

        self._checkpoints[job_id] = checkpoint
        await self._save_checkpoint(checkpoint)

    async def update_checkpoint(
        self,
        job_id: str,
        progress: Optional[float] = None,
        status: Optional[str] = None,
        checkpoint_data: Optional[Dict] = None,
    ):
        """Update an existing checkpoint."""
        checkpoint = self._checkpoints.get(job_id)
        if not checkpoint:
            return

        if progress is not None:
            checkpoint.progress = progress
        if status is not None:
            checkpoint.status = status
        if checkpoint_data is not None:
            checkpoint.checkpoint_data.update(checkpoint_data)

        checkpoint.updated_at = time.time()

    async def get_checkpoint(self, job_id: str) -> Optional[JobCheckpoint]:
        """Get a job checkpoint."""
        return self._checkpoints.get(job_id)

    async def remove_checkpoint(self, job_id: str):
        """Remove a job checkpoint."""
        self._checkpoints.pop(job_id, None)
        
        checkpoint_file = self.checkpoint_dir / f"{job_id}.json"
        if checkpoint_file.exists():
            checkpoint_file.unlink()

    async def get_incomplete_jobs(self) -> List[JobCheckpoint]:
        """Get all incomplete jobs (for recovery)."""
        return [
            cp for cp in self._checkpoints.values()
            if cp.status not in ('completed', 'failed')
        ]

    async def _checkpoint_loop(self):
        """Periodically save checkpoints."""
        while self._running:
            try:
                await asyncio.sleep(self.checkpoint_interval)
                await self._save_all_checkpoints()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Checkpoint loop error: {e}")

    async def _save_checkpoint(self, checkpoint: JobCheckpoint):
        """Save a single checkpoint to disk."""
        try:
            checkpoint_file = self.checkpoint_dir / f"{checkpoint.job_id}.json"
            
            data = {
                'job_id': checkpoint.job_id,
                'job_type': checkpoint.job_type,
                'model_name': checkpoint.model_name,
                'status': checkpoint.status,
                'progress': checkpoint.progress,
                'worker_id': checkpoint.worker_id,
                'comfyui_prompt_id': checkpoint.comfyui_prompt_id,
                'parameters': checkpoint.parameters,
                'created_at': checkpoint.created_at,
                'updated_at': checkpoint.updated_at,
                'checkpoint_data': checkpoint.checkpoint_data,
            }

            async with aiofiles.open(checkpoint_file, 'w') as f:
                await f.write(json.dumps(data, indent=2))

        except Exception as e:
            logger.error(f"Failed to save checkpoint {checkpoint.job_id}: {e}")

    async def _save_all_checkpoints(self):
        """Save all checkpoints to disk."""
        for checkpoint in self._checkpoints.values():
            await self._save_checkpoint(checkpoint)

    async def _load_checkpoints(self):
        """Load checkpoints from disk."""
        try:
            for checkpoint_file in self.checkpoint_dir.glob("*.json"):
                try:
                    async with aiofiles.open(checkpoint_file, 'r') as f:
                        data = json.loads(await f.read())

                    checkpoint = JobCheckpoint(**data)
                    self._checkpoints[checkpoint.job_id] = checkpoint

                except Exception as e:
                    logger.warning(f"Failed to load checkpoint {checkpoint_file}: {e}")

            logger.info(f"Loaded {len(self._checkpoints)} checkpoints")

        except Exception as e:
            logger.error(f"Failed to load checkpoints: {e}")


class WorkerRecovery:
    """
    Automatic worker recovery system.
    
    Features:
    - Detects failed workers
    - Attempts automatic recovery
    - Re-registers recovered workers
    - Migrates jobs from failed workers
    """

    def __init__(
        self,
        recovery_interval: int = 30,
        max_recovery_attempts: int = 3,
    ):
        self.recovery_interval = recovery_interval
        self.max_recovery_attempts = max_recovery_attempts

        self._failed_workers: Dict[str, Dict] = {}
        self._recovery_attempts: Dict[str, int] = {}

        self._recovery_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_worker_recovered = None
        self._on_worker_failed = None
        self._on_job_migration = None

    async def start(self):
        """Start worker recovery system."""
        self._running = True
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        logger.info("Worker recovery system started")

    async def stop(self):
        """Stop worker recovery."""
        self._running = False
        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

    def on_worker_recovered(self, callback):
        """Register callback for worker recovery."""
        self._on_worker_recovered = callback

    def on_worker_failed(self, callback):
        """Register callback for worker failure."""
        self._on_worker_failed = callback

    def on_job_migration(self, callback):
        """Register callback for job migration."""
        self._on_job_migration = callback

    async def mark_worker_failed(self, worker_id: str, worker_data: Dict):
        """Mark a worker as failed and start recovery."""
        self._failed_workers[worker_id] = {
            'data': worker_data,
            'failed_at': time.time(),
            'last_attempt': 0,
        }
        self._recovery_attempts[worker_id] = 0

        logger.warning(f"Worker marked as failed: {worker_id}")

        if self._on_worker_failed:
            await self._on_worker_failed(worker_id, worker_data)

    async def _recovery_loop(self):
        """Periodically attempt worker recovery."""
        while self._running:
            try:
                await self._attempt_recovery()
                await asyncio.sleep(self.recovery_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Recovery loop error: {e}")
                await asyncio.sleep(10)

    async def _attempt_recovery(self):
        """Attempt to recover failed workers."""
        import httpx

        for worker_id in list(self._failed_workers.keys()):
            worker_info = self._failed_workers[worker_id]
            attempts = self._recovery_attempts.get(worker_id, 0)

            if attempts >= self.max_recovery_attempts:
                logger.error(f"Worker {worker_id} recovery failed after {attempts} attempts")
                del self._failed_workers[worker_id]
                continue

            # Try to connect to worker
            comfyui_url = worker_info['data'].get('comfyui_url', '')
            if not comfyui_url:
                continue

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{comfyui_url}/system_stats")
                    resp.raise_for_status()

                # Worker is back online!
                logger.info(f"Worker {worker_id} recovered!")
                del self._failed_workers[worker_id]
                del self._recovery_attempts[worker_id]

                if self._on_worker_recovered:
                    await self._on_worker_recovered(worker_id, worker_info['data'])

            except Exception:
                self._recovery_attempts[worker_id] = attempts + 1
                logger.debug(
                    f"Worker {worker_id} recovery attempt {attempts + 1} failed"
                )

    def get_failed_workers(self) -> List[Dict]:
        """Get list of failed workers."""
        return [
            {
                'worker_id': wid,
                'failed_at': info['failed_at'],
                'recovery_attempts': self._recovery_attempts.get(wid, 0),
            }
            for wid, info in self._failed_workers.items()
        ]

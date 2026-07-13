"""
Batch Processing API

Provides multi-user queue management with priority lanes for batch job processing.
"""

import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("openmind.dispatcher.batch")


class Priority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"


class BatchStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BatchJob:
    """Individual job within a batch."""
    job_id: str
    job_type: str
    model: str
    parameters: Dict
    status: str = "pending"
    result_url: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass
class Batch:
    """A batch of jobs submitted together."""
    batch_id: str
    user_id: str
    priority: Priority
    jobs: List[BatchJob]
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    total_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0


# Pydantic models for API
class BatchJobRequest(BaseModel):
    job_type: str = "video"
    model: str = "auto"
    parameters: Dict = {}


class BatchSubmitRequest(BaseModel):
    jobs: List[BatchJobRequest]
    priority: Priority = Priority.NORMAL
    callback_url: Optional[str] = None
    max_concurrent: int = Field(default=5, ge=1, le=20)


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: str
    priority: str
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    progress_percent: float
    created_at: float
    started_at: Optional[float]
    completed_at: Optional[float]
    jobs: List[Dict]


class BatchManager:
    """
    Manages batch job processing with priority queues.
    
    Features:
    - Priority lanes (urgent, normal, low)
    - Per-user rate limiting
    - Concurrent job execution within batches
    - Progress tracking
    - Callback notifications
    """

    def __init__(
        self,
        max_concurrent_global: int = 20,
        max_concurrent_per_user: int = 10,
        rate_limit_per_user: int = 10,  # jobs per minute
    ):
        self.max_concurrent_global = max_concurrent_global
        self.max_concurrent_per_user = max_concurrent_per_user
        self.rate_limit_per_user = rate_limit_per_user

        # Storage
        self._batches: Dict[str, Batch] = {}
        self._user_batches: Dict[str, List[str]] = defaultdict(list)

        # Rate limiting
        self._user_job_counts: Dict[str, List[float]] = defaultdict(list)

        # Priority queues
        self._urgent_queue: List[str] = []
        self._normal_queue: List[str] = []
        self._low_queue: List[str] = []

        # Active processing
        self._active_batches: Set[str] = set()
        self._active_jobs: int = 0

        # Processing task
        self._process_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_batch_complete = None
        self._on_job_complete = None

    async def start(self):
        """Start the batch manager."""
        self._running = True
        self._process_task = asyncio.create_task(self._process_loop())
        logger.info("Batch manager started")

    async def stop(self):
        """Stop the batch manager."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass

    def on_batch_complete(self, callback):
        """Register callback for batch completion."""
        self._on_batch_complete = callback

    def on_job_complete(self, callback):
        """Register callback for job completion."""
        self._on_job_complete = callback

    async def submit_batch(
        self,
        user_id: str,
        jobs: List[Dict],
        priority: Priority = Priority.NORMAL,
        callback_url: Optional[str] = None,
        max_concurrent: int = 5,
    ) -> str:
        """
        Submit a new batch of jobs.
        
        Returns:
            Batch ID
        """
        # Check rate limit
        if not self._check_rate_limit(user_id):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Max {self.rate_limit_per_user} jobs per minute."
            )

        # Create batch
        batch_id = f"batch_{uuid.uuid4().hex[:12]}"

        batch_jobs = []
        for job_req in jobs:
            job = BatchJob(
                job_id=f"{batch_id}_{uuid.uuid4().hex[:8]}",
                job_type=job_req.get("job_type", "video"),
                model=job_req.get("model", "auto"),
                parameters=job_req.get("parameters", {}),
            )
            batch_jobs.append(job)

        batch = Batch(
            batch_id=batch_id,
            user_id=user_id,
            priority=priority,
            jobs=batch_jobs,
            total_jobs=len(batch_jobs),
        )

        # Store batch
        self._batches[batch_id] = batch
        self._user_batches[user_id].append(batch_id)

        # Add to priority queue
        if priority == Priority.URGENT:
            self._urgent_queue.append(batch_id)
        elif priority == Priority.NORMAL:
            self._normal_queue.append(batch_id)
        else:
            self._low_queue.append(batch_id)

        logger.info(
            f"Batch submitted: {batch_id} ({len(batch_jobs)} jobs, "
            f"priority: {priority.value}, user: {user_id})"
        )

        return batch_id

    async def get_batch_status(self, batch_id: str) -> Optional[BatchStatusResponse]:
        """Get status of a batch."""
        batch = self._batches.get(batch_id)
        if not batch:
            return None

        completed = sum(1 for j in batch.jobs if j.status == "completed")
        failed = sum(1 for j in batch.jobs if j.status == "failed")
        progress = (completed / batch.total_jobs * 100) if batch.total_jobs > 0 else 0

        return BatchStatusResponse(
            batch_id=batch.batch_id,
            status=batch.status,
            priority=batch.priority.value,
            total_jobs=batch.total_jobs,
            completed_jobs=completed,
            failed_jobs=failed,
            progress_percent=round(progress, 2),
            created_at=batch.created_at,
            started_at=batch.started_at,
            completed_at=batch.completed_at,
            jobs=[
                {
                    "job_id": j.job_id,
                    "job_type": j.job_type,
                    "model": j.model,
                    "status": j.status,
                    "result_url": j.result_url,
                    "error": j.error,
                }
                for j in batch.jobs
            ],
        )

    async def cancel_batch(self, batch_id: str, user_id: str) -> bool:
        """Cancel a batch."""
        batch = self._batches.get(batch_id)
        if not batch:
            return False

        if batch.user_id != user_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        if batch.status in ("completed", "cancelled"):
            return False

        batch.status = "cancelled"
        for job in batch.jobs:
            if job.status == "pending":
                job.status = "cancelled"

        # Remove from queues
        for queue in [self._urgent_queue, self._normal_queue, self._low_queue]:
            if batch_id in queue:
                queue.remove(batch_id)

        return True

    async def get_user_batches(self, user_id: str) -> List[BatchStatusResponse]:
        """Get all batches for a user."""
        batch_ids = self._user_batches.get(user_id, [])
        batches = []

        for batch_id in batch_ids:
            status = await self.get_batch_status(batch_id)
            if status:
                batches.append(status)

        return batches

    def _check_rate_limit(self, user_id: str) -> bool:
        """Check if user is within rate limits."""
        now = time.time()
        window = 60  # 1 minute

        # Clean old entries
        self._user_job_counts[user_id] = [
            t for t in self._user_job_counts[user_id]
            if now - t < window
        ]

        if len(self._user_job_counts[user_id]) >= self.rate_limit_per_user:
            return False

        self._user_job_counts[user_id].append(now)
        return True

    async def _process_loop(self):
        """Main processing loop."""
        while self._running:
            try:
                await self._process_next_batch()
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                await asyncio.sleep(5)

    async def _process_next_batch(self):
        """Process the next batch from the queue."""
        # Check if we can process more
        if self._active_jobs >= self.max_concurrent_global:
            return

        # Get next batch from priority queue
        batch_id = None
        for queue in [self._urgent_queue, self._normal_queue, self._low_queue]:
            if queue:
                batch_id = queue.pop(0)
                break

        if not batch_id:
            return

        batch = self._batches.get(batch_id)
        if not batch or batch.status != "pending":
            return

        # Start processing
        batch.status = "processing"
        batch.started_at = time.time()
        self._active_batches.add(batch_id)

        # Process jobs concurrently
        max_concurrent = min(5, self.max_concurrent_global - self._active_jobs)
        await self._process_batch_jobs(batch, max_concurrent)

    async def _process_batch_jobs(self, batch: Batch, max_concurrent: int):
        """Process jobs in a batch concurrently."""
        import httpx

        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_job(job: BatchJob):
            async with semaphore:
                self._active_jobs += 1
                try:
                    # Submit job to dispatcher
                    async with httpx.AsyncClient(timeout=600) as client:
                        response = await client.post(
                            f"http://localhost:9000/api/v1/jobs/{job.job_type}",
                            json={
                                "prompt": job.parameters.get("prompt", ""),
                                "model": job.model,
                                **job.parameters,
                            },
                        )
                        response.raise_for_status()
                        result = response.json()

                        job_id = result.get("job_id")
                        job.status = "processing"

                        # Poll for completion
                        await self._poll_job_completion(client, job_id, job)

                except Exception as e:
                    job.status = "failed"
                    job.error = str(e)
                    batch.failed_jobs += 1
                    logger.error(f"Job {job.job_id} failed: {e}")
                finally:
                    self._active_jobs -= 1

        # Process all jobs
        tasks = [process_job(job) for job in batch.jobs if job.status == "pending"]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Update batch status
        completed = sum(1 for j in batch.jobs if j.status == "completed")
        failed = sum(1 for j in batch.jobs if j.status == "failed")

        if failed == batch.total_jobs:
            batch.status = "failed"
        elif completed + failed == batch.total_jobs:
            batch.status = "completed"
        
        batch.completed_at = time.time()
        batch.completed_jobs = completed
        batch.failed_jobs = failed

        self._active_batches.discard(batch.batch_id)

        logger.info(
            f"Batch {batch.batch_id} finished: "
            f"{completed} completed, {failed} failed"
        )

        # Notify
        if self._on_batch_complete:
            await self._on_batch_complete(batch)

    async def _poll_job_completion(
        self,
        client: "httpx.AsyncClient",
        job_id: str,
        job: BatchJob,
    ):
        """Poll for individual job completion."""
        max_attempts = 120
        for _ in range(max_attempts):
            try:
                response = await client.get(f"http://localhost:9000/api/v1/jobs/{job_id}")
                result = response.json()

                if result.get("status") == "completed":
                    job.status = "completed"
                    job.result_url = result.get("result_url")
                    job.completed_at = time.time()
                    return
                elif result.get("status") == "failed":
                    job.status = "failed"
                    job.error = result.get("error", "Unknown error")
                    job.completed_at = time.time()
                    return

                await asyncio.sleep(5)

            except Exception:
                await asyncio.sleep(5)

        job.status = "failed"
        job.error = "Job timed out"

    def get_stats(self) -> Dict:
        """Get batch processing statistics."""
        return {
            "total_batches": len(self._batches),
            "active_batches": len(self._active_batches),
            "active_jobs": self._active_jobs,
            "queue_sizes": {
                "urgent": len(self._urgent_queue),
                "normal": len(self._normal_queue),
                "low": len(self._low_queue),
            },
            "by_status": {
                "pending": sum(1 for b in self._batches.values() if b.status == "pending"),
                "processing": sum(1 for b in self._batches.values() if b.status == "processing"),
                "completed": sum(1 for b in self._batches.values() if b.status == "completed"),
                "failed": sum(1 for b in self._batches.values() if b.status == "failed"),
                "cancelled": sum(1 for b in self._batches.values() if b.status == "cancelled"),
            },
        }

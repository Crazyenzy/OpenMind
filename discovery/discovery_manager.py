"""
Discovery Manager - Coordinates all discovery mechanisms.

Manages mDNS and Hyperspace discovery services, aggregates discovered workers,
and provides a unified interface for the dispatcher.
"""

import asyncio
import logging
from typing import Optional, Dict, List, Callable
from datetime import datetime, timedelta

from .mdns_discovery import MDNSDiscovery, WorkerAdvertisement
from .hyperspace_discovery import HyperspaceDiscovery, HyperspacePeer

logger = logging.getLogger("openmind.discovery.manager")


class DiscoveryManager:
    """
    Unified discovery manager for OpenMind workers.
    
    Coordinates:
    - mDNS/Bonjour for local network discovery
    - Hyperspace P2P for remote network discovery
    - Manual worker registration (fallback)
    
    Provides:
    - Aggregated worker list from all sources
    - Worker health monitoring
    - Automatic cleanup of stale workers
    """

    def __init__(
        self,
        worker_id: str,
        worker_name: str,
        port: int = 8188,
        gpu_info: Optional[Dict] = None,
        vram_gb: float = 0.0,
        comfyui_url: str = "",
        capabilities: Optional[List[str]] = None,
        mdns_enabled: bool = True,
        hyperspace_enabled: bool = True,
        hyperspace_bin: str = "hyperspace",
        cleanup_interval: int = 60,
        stale_threshold: int = 300,  # 5 minutes
    ):
        self.worker_id = worker_id
        self.worker_name = worker_name
        self.port = port
        self.gpu_info = gpu_info or {}
        self.vram_gb = vram_gb
        self.comfyui_url = comfyui_url
        self.capabilities = capabilities or ["image", "video"]

        # Discovery services
        self._mdns: Optional[MDNSDiscovery] = None
        self._hyperspace: Optional[HyperspaceDiscovery] = None

        # Configuration
        self._mdns_enabled = mdns_enabled
        self._hyperspace_enabled = hyperspace_enabled
        self._cleanup_interval = cleanup_interval
        self._stale_threshold = stale_threshold

        # Aggregated worker list
        self._workers: Dict[str, Dict] = {}
        self._callbacks: List[Callable] = []
        self._running = False
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> bool:
        """Start all discovery services."""
        self._running = True
        success = False

        # Initialize mDNS discovery
        if self._mdns_enabled:
            self._mdns = MDNSDiscovery(
                worker_id=self.worker_id,
                name=self.worker_name,
                port=self.port,
                gpu_info=self.gpu_info,
                vram_gb=self.vram_gb,
                comfyui_url=self.comfyui_url,
                capabilities=self.capabilities,
            )
            self._mdns.on_worker_discovered(self._on_mdns_worker_discovered)
            if await self._mdns.start():
                success = True
                logger.info("✅ mDNS discovery started")
            else:
                logger.warning("⚠️ mDNS discovery failed to start")

        # Initialize Hyperspace discovery
        if self._hyperspace_enabled:
            self._hyperspace = HyperspaceDiscovery()
            self._hyperspace.on_peer_discovered(self._on_hyperspace_peer_discovered)
            if await self._hyperspace.start():
                success = True
                logger.info("✅ Hyperspace discovery started")
            else:
                logger.warning("⚠️ Hyperspace discovery not available")

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        if success:
            logger.info("🔍 Discovery manager started successfully")
        else:
            logger.warning("⚠️ No discovery services available")

        return success

    async def stop(self):
        """Stop all discovery services."""
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._mdns:
            await self._mdns.stop()

        if self._hyperspace:
            await self._hyperspace.stop()

        self._workers.clear()
        logger.info("🛑 Discovery manager stopped")

    def on_worker_discovered(self, callback: Callable):
        """Register a callback for worker discovery events."""
        self._callbacks.append(callback)

    async def _on_mdns_worker_discovered(self, advertisement: Optional[WorkerAdvertisement], **kwargs):
        """Handle mDNS worker discovery."""
        if advertisement is None:
            # Worker removed
            worker_id = kwargs.get('worker_id')
            if worker_id and worker_id in self._workers:
                del self._workers[worker_id]
                logger.info(f"👋 Worker removed (mDNS): {worker_id}")
                await self._notify_callbacks('removed', worker_id)
            return

        worker_data = {
            'id': advertisement.worker_id,
            'name': advertisement.name,
            'host': advertisement.host,
            'port': advertisement.port,
            'gpu_info': advertisement.gpu_info,
            'vram_gb': advertisement.vram_gb,
            'comfyui_url': advertisement.comfyui_url,
            'capabilities': advertisement.capabilities,
            'source': 'mdns',
            'last_seen': datetime.now(),
            'latency_ms': 0,  # Local network, assume low latency
        }

        self._workers[advertisement.worker_id] = worker_data
        await self._notify_callbacks('discovered', advertisement.worker_id)
        logger.info(f"✅ Worker registered (mDNS): {advertisement.name}")

    async def _on_hyperspace_peer_discovered(self, peer: HyperspacePeer):
        """Handle Hyperspace peer discovery."""
        worker_data = {
            'id': peer.peer_id,
            'name': peer.name,
            'host': peer.host,
            'port': peer.port,
            'gpu_info': peer.gpu_info,
            'vram_gb': peer.vram_gb,
            'comfyui_url': peer.comfyui_url,
            'capabilities': peer.capabilities,
            'source': 'hyperspace',
            'last_seen': datetime.now(),
            'latency_ms': peer.latency_ms,
        }

        self._workers[peer.peer_id] = worker_data
        await self._notify_callbacks('discovered', peer.peer_id)
        logger.info(f"✅ Worker registered (Hyperspace): {peer.name} (latency: {peer.latency_ms:.1f}ms)")

    async def _notify_callbacks(self, event: str, worker_id: str):
        """Notify all registered callbacks."""
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event, worker_id, self._workers.get(worker_id))
                else:
                    callback(event, worker_id, self._workers.get(worker_id))
            except Exception as e:
                logger.error(f"Callback error: {e}")

    async def _cleanup_loop(self):
        """Periodically clean up stale workers."""
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_stale_workers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_stale_workers(self):
        """Remove workers that haven't been seen recently."""
        now = datetime.now()
        stale_threshold = timedelta(seconds=self._stale_threshold)

        stale_workers = []
        for worker_id, worker in self._workers.items():
            last_seen = worker.get('last_seen', now)
            if now - last_seen > stale_threshold:
                stale_workers.append(worker_id)

        for worker_id in stale_workers:
            worker = self._workers.pop(worker_id)
            logger.warning(
                f"⚠️ Worker stale (removed): {worker.get('name', worker_id)} "
                f"(last seen: {worker.get('last_seen')})"
            )
            await self._notify_callbacks('removed', worker_id)

    def register_worker_manual(
        self,
        worker_id: str,
        name: str,
        host: str,
        port: int = 8188,
        gpu_info: Optional[Dict] = None,
        vram_gb: float = 0.0,
        comfyui_url: str = "",
        capabilities: Optional[List[str]] = None,
    ):
        """Manually register a worker (fallback when discovery fails)."""
        worker_data = {
            'id': worker_id,
            'name': name,
            'host': host,
            'port': port,
            'gpu_info': gpu_info or {},
            'vram_gb': vram_gb,
            'comfyui_url': comfyui_url or f"http://{host}:{port}",
            'capabilities': capabilities or ["image", "video"],
            'source': 'manual',
            'last_seen': datetime.now(),
            'latency_ms': 0,
        }

        self._workers[worker_id] = worker_data
        logger.info(f"✅ Worker registered (manual): {name}")

    def get_all_workers(self) -> List[Dict]:
        """Get list of all discovered workers."""
        return list(self._workers.values())

    def get_worker(self, worker_id: str) -> Optional[Dict]:
        """Get a specific worker by ID."""
        return self._workers.get(worker_id)

    def get_workers_by_capability(self, capability: str) -> List[Dict]:
        """Get workers that support a specific capability."""
        return [
            w for w in self._workers.values()
            if capability in w.get('capabilities', [])
        ]

    def get_workers_by_vram(self, min_vram_gb: float) -> List[Dict]:
        """Get workers with at least the specified VRAM."""
        return [
            w for w in self._workers.values()
            if w.get('vram_gb', 0) >= min_vram_gb
        ]

    def get_best_worker(
        self,
        capability: str = "video",
        min_vram_gb: float = 0,
        prefer_low_latency: bool = True,
    ) -> Optional[Dict]:
        """Get the best worker based on criteria."""
        candidates = self.get_workers_by_capability(capability)
        candidates = [w for w in candidates if w.get('vram_gb', 0) >= min_vram_gb]

        if not candidates:
            return None

        # Sort by latency (if preferred) and VRAM
        if prefer_low_latency:
            candidates.sort(key=lambda w: (w.get('latency_ms', 999), -w.get('vram_gb', 0)))
        else:
            candidates.sort(key=lambda w: -w.get('vram_gb', 0))

        return candidates[0]

    def get_stats(self) -> Dict:
        """Get discovery statistics."""
        return {
            'total_workers': len(self._workers),
            'by_source': {
                'mdns': sum(1 for w in self._workers.values() if w.get('source') == 'mdns'),
                'hyperspace': sum(1 for w in self._workers.values() if w.get('source') == 'hyperspace'),
                'manual': sum(1 for w in self._workers.values() if w.get('source') == 'manual'),
            },
            'by_capability': {
                'image': len(self.get_workers_by_capability('image')),
                'video': len(self.get_workers_by_capability('video')),
                'audio': len(self.get_workers_by_capability('audio')),
            },
            'total_vram_gb': sum(w.get('vram_gb', 0) for w in self._workers.values()),
            'average_latency_ms': (
                sum(w.get('latency_ms', 0) for w in self._workers.values()) / len(self._workers)
                if self._workers else 0
            ),
        }

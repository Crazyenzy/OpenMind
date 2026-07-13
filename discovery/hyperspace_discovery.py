"""
Hyperspace AGI peer discovery integration.

Discovers workers through the Hyperspace P2P network for remote discovery
beyond the local network.
"""

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional, Dict, List, Callable
from datetime import datetime

logger = logging.getLogger("openmind.discovery.hyperspace")


@dataclass
class HyperspacePeer:
    """Represents a peer discovered through Hyperspace network."""
    peer_id: str
    name: str
    host: str
    port: int
    gpu_info: Dict
    vram_gb: float
    comfyui_url: str
    capabilities: List[str]
    latency_ms: float
    last_seen: str


class HyperspaceDiscovery:
    """
    Integration with Hyperspace AGI P2P network for remote worker discovery.
    
    Features:
    - Discovers workers beyond local network via Hyperspace mesh
    - Measures network latency to remote workers
    - Integrates with Hyperspace pod management
    - Supports NAT traversal via Hyperspace relay nodes
    """

    def __init__(
        self,
        hyperspace_bin: str = "hyperspace",
        pod_name: str = "openmind-cluster",
        discovery_interval: int = 60,
    ):
        self.hyperspace_bin = hyperspace_bin
        self.pod_name = pod_name
        self.discovery_interval = discovery_interval

        self._peers: Dict[str, HyperspacePeer] = {}
        self._callbacks: List[Callable] = []
        self._running = False
        self._discovery_task: Optional[asyncio.Task] = None

    async def start(self) -> bool:
        """Start Hyperspace peer discovery."""
        # Check if Hyperspace CLI is available
        if not await self._check_hyperspace_available():
            logger.warning(
                "Hyperspace CLI not found. Remote discovery disabled. "
                "Install with: curl -fsSL https://agents.hyper.space/api/install | bash"
            )
            return False

        self._running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        logger.info("✅ Hyperspace peer discovery started")
        return True

    async def stop(self):
        """Stop Hyperspace peer discovery."""
        self._running = False
        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
        self._peers.clear()

    async def _check_hyperspace_available(self) -> bool:
        """Check if Hyperspace CLI is installed and accessible."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.hyperspace_bin, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                version = stdout.decode().strip()
                logger.info(f"Hyperspace CLI found: {version}")
                return True
            return False
        except (FileNotFoundError, asyncio.TimeoutError):
            return False

    async def _discovery_loop(self):
        """Periodically discover peers through Hyperspace network."""
        while self._running:
            try:
                await self._discover_peers()
                await asyncio.sleep(self.discovery_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Discovery loop error: {e}")
                await asyncio.sleep(30)

    async def _discover_peers(self):
        """Discover peers in the Hyperspace pod."""
        try:
            # Get pod members
            members = await self._run_hyperspace_command(["pod", "members"])
            if not members:
                return

            # Get detailed info for each member
            for member in members:
                peer_id = member.get("id")
                if not peer_id:
                    continue

                # Check if this peer has OpenMind capabilities
                if not self._is_openmind_worker(member):
                    continue

                # Measure latency
                host = member.get("host", "")
                latency = await self._measure_latency(host) if host else 0

                peer = HyperspacePeer(
                    peer_id=peer_id,
                    name=member.get("name", peer_id[:8]),
                    host=host,
                    port=member.get("port", 8188),
                    gpu_info=member.get("gpu", {}),
                    vram_gb=member.get("vram_gb", 0),
                    comfyui_url=member.get("comfyui_url", f"http://{host}:8188"),
                    capabilities=member.get("capabilities", ["image", "video"]),
                    latency_ms=latency,
                    last_seen=datetime.now().isoformat(),
                )

                # Update or add peer
                if peer_id not in self._peers:
                    logger.info(
                        f"🆕 Discovered Hyperspace peer: {peer.name} "
                        f"({host}) - Latency: {latency:.1f}ms"
                    )
                    # Trigger callbacks
                    for callback in self._callbacks:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(peer)
                            else:
                                callback(peer)
                        except Exception as e:
                            logger.error(f"Callback error: {e}")

                self._peers[peer_id] = peer

        except Exception as e:
            logger.error(f"Peer discovery failed: {e}")

    async def _run_hyperspace_command(self, args: List[str]) -> Optional[List[Dict]]:
        """Run a Hyperspace CLI command and return parsed output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.hyperspace_bin, *args, "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                logger.warning(f"Hyperspace command failed: {stderr.decode()}")
                return None

            return json.loads(stdout.decode())

        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error(f"Hyperspace command error: {e}")
            return None

    def _is_openmind_worker(self, member: Dict) -> bool:
        """Check if a Hyperspace member is an OpenMind worker."""
        # Check for OpenMind-specific tags or capabilities
        tags = member.get("tags", [])
        capabilities = member.get("capabilities", [])

        if "openmind" in tags or "comfyui" in tags:
            return True

        # Check if they have ComfyUI running
        if member.get("comfyui_url"):
            return True

        return False

    async def _measure_latency(self, host: str) -> float:
        """Measure network latency to a host in milliseconds."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "3", "-W", "2", host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                # Parse average latency from ping output
                output = stdout.decode()
                for line in output.split('\n'):
                    if 'avg' in line:
                        # Format: rtt min/avg/max/mdev = 1.234/2.345/3.456/0.123 ms
                        parts = line.split('=')[1].strip().split('/')
                        if len(parts) >= 2:
                            return float(parts[1])

            return 0.0

        except (asyncio.TimeoutError, Exception):
            return 0.0

    def on_peer_discovered(self, callback: Callable):
        """Register a callback for when a new peer is discovered."""
        self._callbacks.append(callback)

    def get_discovered_peers(self) -> List[HyperspacePeer]:
        """Get list of all discovered peers."""
        return list(self._peers.values())

    def get_peer(self, peer_id: str) -> Optional[HyperspacePeer]:
        """Get a specific peer by ID."""
        return self._peers.get(peer_id)

    async def get_pod_status(self) -> Optional[Dict]:
        """Get the current Hyperspace pod status."""
        return await self._run_hyperspace_command(["pod", "status"])

    async def get_pod_models(self) -> Optional[List[Dict]]:
        """Get available models across the pod."""
        return await self._run_hyperspace_command(["pod", "models"])

"""
mDNS/Bonjour-based worker discovery for local network.

Uses zeroconf to advertise and discover OpenMind workers on the local network
without any manual configuration.
"""

import asyncio
import json
import logging
import socket
from dataclasses import dataclass, asdict
from typing import Optional, Callable, Dict, List
from datetime import datetime

try:
    from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo, ServiceStateChange
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

logger = logging.getLogger("openmind.discovery.mdns")


@dataclass
class WorkerAdvertisement:
    """Worker service advertisement data."""
    worker_id: str
    name: str
    host: str
    port: int
    gpu_info: Dict
    vram_gb: float
    comfyui_url: str
    capabilities: List[str]
    version: str
    timestamp: str

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'WorkerAdvertisement':
        return cls(**data)


class MDNSDiscovery:
    """
    mDNS/Bonjour-based discovery for OpenMind workers.
    
    Features:
    - Zero-configuration worker discovery on local network
    - Automatic GPU capability advertisement
    - Real-time worker status updates
    - Cross-platform support (Linux, macOS, Windows)
    """

    SERVICE_TYPE = "_openmind._tcp.local."
    SERVICE_NAME = "OpenMind Worker"

    def __init__(
        self,
        worker_id: str,
        name: str,
        port: int = 8188,
        gpu_info: Optional[Dict] = None,
        vram_gb: float = 0.0,
        comfyui_url: str = "",
        capabilities: Optional[List[str]] = None,
        version: str = "3.0.0",
    ):
        self.worker_id = worker_id
        self.name = name
        self.port = port
        self.gpu_info = gpu_info or {}
        self.vram_gb = vram_gb
        self.comfyui_url = comfyui_url
        self.capabilities = capabilities or ["image", "video"]
        self.version = version

        self._zeroconf: Optional[AsyncZeroconf] = None
        self._browser: Optional[AsyncServiceBrowser] = None
        self._service_info: Optional[AsyncServiceInfo] = None
        self._discovered_workers: Dict[str, WorkerAdvertisement] = {}
        self._callbacks: List[Callable] = []
        self._running = False

        if not ZEROCONF_AVAILABLE:
            logger.warning(
                "zeroconf not installed. mDNS discovery disabled. "
                "Install with: pip install zeroconf"
            )

    async def start(self) -> bool:
        """Start advertising and discovering workers."""
        if not ZEROCONF_AVAILABLE:
            logger.error("Cannot start mDNS discovery: zeroconf not installed")
            return False

        try:
            self._zeroconf = AsyncZeroconf()
            await self._zeroconf.zeroconf.async_wait_for_start()

            # Register our service
            hostname = socket.gethostname()
            local_ip = self._get_local_ip()

            properties = {
                'worker_id': self.worker_id,
                'name': self.name,
                'gpu': json.dumps(self.gpu_info),
                'vram_gb': str(self.vram_gb),
                'comfyui_url': self.comfyui_url,
                'capabilities': json.dumps(self.capabilities),
                'version': self.version,
                'timestamp': datetime.now().isoformat(),
            }

            self._service_info = AsyncServiceInfo(
                self.SERVICE_TYPE,
                f"{self.worker_id}.{self.SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=self.port,
                properties=properties,
                server=f"{hostname}.local.",
            )

            await self._zeroconf.async_register_service(self._service_info)
            logger.info(f"✅ Registered worker '{self.name}' on mDNS ({local_ip}:{self.port})")

            # Browse for other workers
            self._browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf,
                self.SERVICE_TYPE,
                handlers=[self._on_service_state_change],
            )

            self._running = True
            logger.info("🔍 mDNS discovery started, browsing for workers...")
            return True

        except Exception as e:
            logger.error(f"Failed to start mDNS discovery: {e}")
            return False

    async def stop(self):
        """Stop advertising and discovering."""
        self._running = False

        if self._browser:
            await self._browser.async_cancel()

        if self._zeroconf and self._service_info:
            try:
                await self._zeroconf.async_unregister_service(self._service_info)
                logger.info("🛑 Unregistered worker from mDNS")
            except Exception as e:
                logger.warning(f"Error unregistering service: {e}")

        if self._zeroconf:
            await self._zeroconf.async_close()

        self._discovered_workers.clear()

    def on_worker_discovered(self, callback: Callable):
        """Register a callback for when a new worker is discovered."""
        self._callbacks.append(callback)

    async def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ):
        """Handle service state changes."""
        if state_change == ServiceStateChange.Added:
            await self._handle_service_added(zeroconf, service_type, name)
        elif state_change == ServiceStateChange.Removed:
            await self._handle_service_removed(name)

    async def _handle_service_added(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
    ):
        """Handle a new worker discovery."""
        try:
            info = await zeroconf.async_get_service_info(service_type, name)
            if not info or not info.properties:
                return

            # Parse properties
            props = {k.decode(): v.decode() for k, v in info.properties.items() if v}

            worker_id = props.get('worker_id', name.split('.')[0])
            worker_name = props.get('name', worker_id)

            # Skip our own advertisement
            if worker_id == self.worker_id:
                return

            # Get IP address
            if info.addresses:
                host = socket.inet_ntoa(info.addresses[0])
            else:
                host = "unknown"

            # Parse GPU info
            gpu_info = {}
            try:
                gpu_info = json.loads(props.get('gpu', '{}'))
            except json.JSONDecodeError:
                pass

            # Parse capabilities
            capabilities = ["image", "video"]
            try:
                capabilities = json.loads(props.get('capabilities', '["image", "video"]'))
            except json.JSONDecodeError:
                pass

            advertisement = WorkerAdvertisement(
                worker_id=worker_id,
                name=worker_name,
                host=host,
                port=info.port,
                gpu_info=gpu_info,
                vram_gb=float(props.get('vram_gb', 0)),
                comfyui_url=props.get('comfyui_url', f"http://{host}:{info.port}"),
                capabilities=capabilities,
                version=props.get('version', 'unknown'),
                timestamp=props.get('timestamp', ''),
            )

            # Store and notify
            self._discovered_workers[worker_id] = advertisement
            logger.info(
                f"🆕 Discovered worker: {worker_name} ({host}:{info.port}) "
                f"- GPU: {gpu_info.get('name', 'unknown')}, VRAM: {advertisement.vram_gb}GB"
            )

            # Trigger callbacks
            for callback in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(advertisement)
                    else:
                        callback(advertisement)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        except Exception as e:
            logger.warning(f"Error handling service addition: {e}")

    async def _handle_service_removed(self, name: str):
        """Handle worker removal."""
        worker_id = name.split('.')[0]
        if worker_id in self._discovered_workers:
            worker = self._discovered_workers.pop(worker_id)
            logger.info(f"👋 Worker left: {worker.name} ({worker.host})")

            # Notify callbacks with None to indicate removal
            for callback in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(None, worker_id=worker_id)
                    else:
                        callback(None, worker_id=worker_id)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    def get_discovered_workers(self) -> List[WorkerAdvertisement]:
        """Get list of all discovered workers."""
        return list(self._discovered_workers.values())

    def get_worker(self, worker_id: str) -> Optional[WorkerAdvertisement]:
        """Get a specific worker by ID."""
        return self._discovered_workers.get(worker_id)

    @staticmethod
    def _get_local_ip() -> str:
        """Get the local IP address."""
        try:
            # Connect to a public DNS server to determine local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def update_advertisement(self, **kwargs):
        """Update the service advertisement (e.g., after GPU status change)."""
        if not self._zeroconf or not self._service_info:
            return

        # Update properties
        for key, value in kwargs.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif isinstance(value, (int, float)):
                value = str(value)
            self._service_info.properties[key.encode()] = value.encode()

        # Re-register with updated properties
        await self._zeroconf.async_update_service(self._service_info)
        logger.debug("Updated mDNS advertisement")

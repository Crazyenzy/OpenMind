"""
OpenMind Auto-Discovery Service

Provides automatic worker discovery using:
1. mDNS/Bonjour for local network discovery
2. Hyperspace peer integration for remote workers
3. GPU capability detection and reporting
"""

from .mdns_discovery import MDNSDiscovery
from .hyperspace_discovery import HyperspaceDiscovery
from .discovery_manager import DiscoveryManager

__all__ = ["MDNSDiscovery", "HyperspaceDiscovery", "DiscoveryManager"]

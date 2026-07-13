"""
OpenMind Health Monitoring Dashboard

Web UI for cluster management, GPU monitoring, and job tracking.
"""

from .dashboard_app import create_dashboard_app

__all__ = ["create_dashboard_app"]

"""
Health Monitoring Dashboard Application

Provides a web UI for:
- Cluster status visualization
- GPU utilization monitoring
- Job queue management
- Worker health tracking
- Alert management
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

logger = logging.getLogger("openmind.dashboard")


def create_dashboard_app(
    dispatcher_url: str = "http://localhost:9000",
    refresh_interval: int = 5,
) -> FastAPI:
    """
    Create the dashboard FastAPI application.
    
    Args:
        dispatcher_url: URL of the OpenMind dispatcher
        refresh_interval: Auto-refresh interval in seconds
    """
    app = FastAPI(title="OpenMind Dashboard", version="3.0.0")

    # WebSocket connections for real-time updates
    connected_clients: List[WebSocket] = []

    # Dashboard HTML template
    DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenMind Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 0;
            border-bottom: 1px solid #1e293b;
            margin-bottom: 30px;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .logo h1 {
            font-size: 24px;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .status-badge {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }
        
        .status-online { background: #059669; color: white; }
        .status-offline { background: #dc2626; color: white; }
        .status-warning { background: #d97706; color: white; }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: #1e293b;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #334155;
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        .card-title {
            font-size: 14px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .card-value {
            font-size: 36px;
            font-weight: 700;
            color: #f1f5f9;
        }
        
        .card-subtitle {
            font-size: 12px;
            color: #64748b;
            margin-top: 5px;
        }
        
        .gpu-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 15px;
        }
        
        .gpu-card {
            background: #0f172a;
            border-radius: 8px;
            padding: 15px;
            border: 1px solid #1e293b;
        }
        
        .gpu-name {
            font-weight: 600;
            margin-bottom: 10px;
            color: #3b82f6;
        }
        
        .gpu-stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        
        .gpu-stat {
            font-size: 12px;
        }
        
        .gpu-stat-label {
            color: #64748b;
        }
        
        .gpu-stat-value {
            font-weight: 600;
            color: #e2e8f0;
        }
        
        .progress-bar {
            height: 8px;
            background: #334155;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #3b82f6, #8b5cf6);
            transition: width 0.3s ease;
        }
        
        .progress-fill.warning { background: #d97706; }
        .progress-fill.danger { background: #dc2626; }
        
        .job-list {
            max-height: 400px;
            overflow-y: auto;
        }
        
        .job-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            border-bottom: 1px solid #1e293b;
        }
        
        .job-item:last-child {
            border-bottom: none;
        }
        
        .job-info {
            flex: 1;
        }
        
        .job-id {
            font-family: monospace;
            font-size: 12px;
            color: #64748b;
        }
        
        .job-prompt {
            font-size: 14px;
            margin-top: 4px;
            color: #e2e8f0;
        }
        
        .job-status {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
        
        .job-status-queued { background: #1e3a5f; color: #60a5fa; }
        .job-status-running { background: #1e3a5f; color: #3b82f6; }
        .job-status-completed { background: #064e3b; color: #34d399; }
        .job-status-failed { background: #450a0a; color: #f87171; }
        
        .worker-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        
        .worker-item {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 12px;
            background: #0f172a;
            border-radius: 8px;
        }
        
        .worker-status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }
        
        .worker-status-dot.online { background: #34d399; }
        .worker-status-dot.busy { background: #f59e0b; }
        .worker-status-dot.offline { background: #ef4444; }
        
        .worker-info {
            flex: 1;
        }
        
        .worker-name {
            font-weight: 600;
        }
        
        .worker-gpu {
            font-size: 12px;
            color: #64748b;
        }
        
        .alert-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        
        .alert-item {
            padding: 12px;
            border-radius: 8px;
            font-size: 13px;
        }
        
        .alert-info { background: #1e3a5f; border-left: 3px solid #3b82f6; }
        .alert-warning { background: #422006; border-left: 3px solid #f59e0b; }
        .alert-error { background: #450a0a; border-left: 3px solid #ef4444; }
        .alert-success { background: #064e3b; border-left: 3px solid #34d399; }
        
        .refresh-indicator {
            font-size: 12px;
            color: #64748b;
        }
        
        @media (max-width: 768px) {
            .grid {
                grid-template-columns: 1fr;
            }
            
            .gpu-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo">
                <h1>🧠 OpenMind Dashboard</h1>
            </div>
            <div>
                <span class="status-badge status-online" id="cluster-status">Cluster Online</span>
                <span class="refresh-indicator">Auto-refresh: <span id="refresh-timer">5</span>s</span>
            </div>
        </header>
        
        <!-- Stats Cards -->
        <div class="grid">
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Workers</span>
                </div>
                <div class="card-value" id="workers-total">0</div>
                <div class="card-subtitle"><span id="workers-idle">0</span> idle / <span id="workers-busy">0</span> busy</div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Jobs</span>
                </div>
                <div class="card-value" id="jobs-total">0</div>
                <div class="card-subtitle"><span id="jobs-queued">0</span> queued / <span id="jobs-active">0</span> active</div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <span class="card-title">GPU VRAM</span>
                </div>
                <div class="card-value" id="vram-total">0 GB</div>
                <div class="card-subtitle"><span id="vram-free">0 GB</span> free</div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Uptime</span>
                </div>
                <div class="card-value" id="uptime">0:00:00</div>
                <div class="card-subtitle">Since <span id="start-time">-</span></div>
            </div>
        </div>
        
        <!-- GPUs -->
        <div class="card" style="margin-bottom: 20px;">
            <div class="card-header">
                <span class="card-title">GPUs</span>
            </div>
            <div class="gpu-grid" id="gpu-list">
                <!-- Populated by JavaScript -->
            </div>
        </div>
        
        <!-- Jobs & Workers -->
        <div class="grid">
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Recent Jobs</span>
                </div>
                <div class="job-list" id="job-list">
                    <!-- Populated by JavaScript -->
                </div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Workers</span>
                </div>
                <div class="worker-list" id="worker-list">
                    <!-- Populated by JavaScript -->
                </div>
            </div>
        </div>
        
        <!-- Alerts -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Alerts</span>
            </div>
            <div class="alert-list" id="alert-list">
                <!-- Populated by JavaScript -->
            </div>
        </div>
    </div>
    
    <script>
        const dispatcherUrl = '""" + dispatcher_url + """';
        let refreshInterval = """ + str(refresh_interval) + """;
        let uptimeStart = Date.now();
        
        // WebSocket for real-time updates
        let ws = null;
        function connectWebSocket() {
            ws = new WebSocket(`ws://${window.location.host}/ws`);
            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                updateDashboard(data);
            };
            ws.onclose = function() {
                setTimeout(connectWebSocket, 5000);
            };
        }
        
        // Fetch dashboard data
        async function fetchDashboardData() {
            try {
                const [health, workers, jobs, gpus, alerts] = await Promise.all([
                    fetch(`${dispatcherUrl}/api/v1/health`).then(r => r.json()),
                    fetch(`${dispatcherUrl}/api/v1/workers`).then(r => r.json()),
                    fetch(`${dispatcherUrl}/api/v1/jobs?limit=20`).then(r => r.json()),
                    fetch(`${dispatcherUrl}/api/v1/gpus`).then(r => r.json()).catch(() => ({gpus: []})),
                    fetch(`${dispatcherUrl}/api/v1/alerts`).then(r => r.json()).catch(() => ({alerts: []})),
                ]);
                
                updateDashboard({health, workers, jobs, gpus, alerts});
            } catch (error) {
                console.error('Failed to fetch dashboard data:', error);
                document.getElementById('cluster-status').className = 'status-badge status-offline';
                document.getElementById('cluster-status').textContent = 'Cluster Offline';
            }
        }
        
        function updateDashboard(data) {
            const {health, workers, jobs, gpus, alerts} = data;
            
            // Update cluster status
            if (health) {
                const statusEl = document.getElementById('cluster-status');
                statusEl.className = 'status-badge status-online';
                statusEl.textContent = 'Cluster Online';
                
                document.getElementById('workers-total').textContent = health.workers_registered || 0;
                document.getElementById('workers-idle').textContent = health.workers_idle || 0;
                document.getElementById('workers-busy').textContent = (health.workers_registered - health.workers_idle) || 0;
                document.getElementById('jobs-total').textContent = health.jobs_total || 0;
                document.getElementById('jobs-queued').textContent = health.jobs_queued || 0;
                document.getElementById('jobs-active').textContent = health.jobs_active || 0;
            }
            
            // Update GPUs
            if (gpus && gpus.gpus) {
                const gpuList = document.getElementById('gpu-list');
                gpuList.innerHTML = gpus.gpus.map(gpu => `
                    <div class="gpu-card">
                        <div class="gpu-name">${gpu.name}</div>
                        <div class="gpu-stats">
                            <div class="gpu-stat">
                                <div class="gpu-stat-label">VRAM</div>
                                <div class="gpu-stat-value">${gpu.vram_free_gb.toFixed(1)} / ${gpu.vram_total_gb.toFixed(1)} GB</div>
                            </div>
                            <div class="gpu-stat">
                                <div class="gpu-stat-label">Utilization</div>
                                <div class="gpu-stat-value">${gpu.utilization_percent}%</div>
                            </div>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill ${gpu.utilization_percent > 80 ? 'danger' : gpu.utilization_percent > 60 ? 'warning' : ''}" 
                                 style="width: ${gpu.utilization_percent}%"></div>
                        </div>
                    </div>
                `).join('');
                
                const totalVram = gpus.gpus.reduce((sum, g) => sum + g.vram_total_gb, 0);
                const freeVram = gpus.gpus.reduce((sum, g) => sum + g.vram_free_gb, 0);
                document.getElementById('vram-total').textContent = `${totalVram.toFixed(0)} GB`;
                document.getElementById('vram-free').textContent = `${freeVram.toFixed(0)} GB`;
            }
            
            // Update jobs
            if (jobs && jobs.jobs) {
                const jobList = document.getElementById('job-list');
                jobList.innerHTML = jobs.jobs.map(job => `
                    <div class="job-item">
                        <div class="job-info">
                            <div class="job-id">${job.job_id}</div>
                            <div class="job-prompt">${job.prompt || 'N/A'}</div>
                        </div>
                        <span class="job-status job-status-${job.status}">${job.status}</span>
                    </div>
                `).join('') || '<div style="padding: 20px; text-align: center; color: #64748b;">No jobs</div>';
            }
            
            // Update workers
            if (workers && workers.workers) {
                const workerList = document.getElementById('worker-list');
                workerList.innerHTML = workers.workers.map(worker => `
                    <div class="worker-item">
                        <div class="worker-status-dot ${worker.status}"></div>
                        <div class="worker-info">
                            <div class="worker-name">${worker.name}</div>
                            <div class="worker-gpu">${worker.gpu} • ${worker.free_vram_gb.toFixed(1)}GB free</div>
                        </div>
                    </div>
                `).join('') || '<div style="padding: 20px; text-align: center; color: #64748b;">No workers</div>';
            }
            
            // Update alerts
            if (alerts && alerts.alerts) {
                const alertList = document.getElementById('alert-list');
                alertList.innerHTML = alerts.alerts.map(alert => `
                    <div class="alert-item alert-${alert.type}">
                        ${alert.message}
                        <div style="font-size: 11px; color: #64748b; margin-top: 4px;">${alert.timestamp}</div>
                    </div>
                `).join('') || '<div style="padding: 20px; text-align: center; color: #64748b;">No alerts</div>';
            }
            
            // Update uptime
            const uptime = Math.floor((Date.now() - uptimeStart) / 1000);
            const hours = Math.floor(uptime / 3600);
            const minutes = Math.floor((uptime % 3600) / 60);
            const seconds = uptime % 60;
            document.getElementById('uptime').textContent = 
                `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        }
        
        // Initialize
        connectWebSocket();
        fetchDashboardData();
        setInterval(fetchDashboardData, refreshInterval * 1000);
        
        // Update timer
        let timer = refreshInterval;
        setInterval(() => {
            timer = timer > 0 ? timer - 1 : refreshInterval;
            document.getElementById('refresh-timer').textContent = timer;
        }, 1000);
    </script>
</body>
</html>
"""

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Serve the dashboard HTML."""
        return DASHBOARD_HTML

    @app.get("/api/dashboard/health")
    async def dashboard_health():
        """Dashboard health check."""
        return {"status": "ok", "service": "dashboard"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time updates."""
        await websocket.accept()
        connected_clients.append(websocket)

        try:
            while True:
                # Keep connection alive
                await websocket.receive_text()
        except WebSocketDisconnect:
            connected_clients.remove(websocket)

    async def broadcast_update(data: Dict):
        """Broadcast update to all connected clients."""
        for client in connected_clients:
            try:
                await client.send_json(data)
            except Exception:
                connected_clients.remove(client)

    @app.get("/api/dashboard/stats")
    async def get_stats():
        """Get dashboard statistics."""
        try:
            async with httpx.AsyncClient() as client:
                health = await client.get(f"{dispatcher_url}/api/v1/health")
                return health.json()
        except Exception as e:
            return {"error": str(e)}

    return app


# Standalone runner
if __name__ == "__main__":
    import uvicorn
    
    app = create_dashboard_app()
    uvicorn.run(app, host="0.0.0.0", port=9001)

"""
===============================================================================
MÓDULO: Dashboard Matriz Multiagente em Tempo Real (:8085) - Dia 6
CAMINHO: src/dashboard/multiagent_dashboard.py
===============================================================================

OBJETIVO:
Servir o painel web de observabilidade na porta 8085 (http://localhost:8085/)
adaptado para múltiplos agentes concorrentes, exibindo:
- Cards individuais por Worker (Worker #1..#N + Worker P1 Interativo Chrome).
- Fila Persistente dividida por prioridades P1, P2, P3, P4 e P5.
- Status do Pool de LLMs (Ollama GPU Local GPU Local, LAN Node, Cloud Gemini
  com trava CLOUD_FALLBACK_MAX_PRIORITY = 3).
- Auditoria de Privacidade ([PRIVADO: dominio]) e Monitores de MD5 (P5).

FLUXO DA ARQUITETURA:
┌─────────────────────────────────────────────────────────────────────────────┐
│ [PostgreSQL 18: WebWorker_db] + [Go Gateway :8000] + [WorkerFleet States]   │
│                               │                                             │
│                               ▼                                             │
│             [MultiAgentDashboardServer (HTTP :8085)]                        │
│               ├── GET /            (HTML5 Dark Mode Matrix UI)              │
│               └── GET /api/metrics (JSON Telemetria Unificada)              │
└─────────────────────────────────────────────────────────────────────────────┘
===============================================================================
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Any, Optional
import urllib.request

from src.db.webworker_db_manager import WebWorkerDBManager


class MultiAgentDashboardServer:
    """
    Servidor HTTP leve na porta 8085 para visualização em tempo real do cluster
    Multiagente (Go Gateway + Python Worker Fleet + PostgreSQL WebWorker_db).
    """

    def __init__(
        self,
        port: int = 8085,
        go_gateway_url: str = "http://127.0.0.1:8000",
        fleet_ref: Optional[Any] = None,
    ):
        self.port = port
        self.go_gateway_url = go_gateway_url.rstrip("/")
        self.fleet_ref = fleet_ref
        self.db = WebWorkerDBManager()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def collect_cluster_snapshot(self) -> Dict[str, Any]:
        """Coleta métricas consolidadas do WebWorker_db, Go Gateway e WorkerFleet."""
        db_metrics = {}
        try:
            db_metrics = self.db.get_dashboard_metrics()
        except Exception as e:
            db_metrics = {"error": str(e)}

        go_status = {"online": False}
        try:
            with urllib.request.urlopen(f"{self.go_gateway_url}/api/metrics", timeout=1.5) as resp:
                go_status = json.loads(resp.read().decode("utf-8"))
                go_status["online"] = True
        except Exception:
            go_status = {"online": False, "cloud_fallback_max_priority": 3}

        workers = {}
        if self.fleet_ref is not None:
            workers = self.fleet_ref.worker_states
        else:
            workers = {
                "worker_p1_interactive": {
                    "worker_id": "worker_p1_interactive",
                    "role": "USER_CHROME_P1",
                    "status": "STANDBY_P1",
                    "url": "[AGUARDANDO EXTENSÃO]",
                    "dom_md5": "-",
                },
                "worker_1": {"worker_id": "worker_1", "role": "HEADLESS_POOL", "status": "IDLE", "url": "-", "dom_md5": "-"},
                "worker_2": {"worker_id": "worker_2", "role": "HEADLESS_POOL", "status": "IDLE", "url": "-", "dom_md5": "-"},
                "worker_3": {"worker_id": "worker_3", "role": "HEADLESS_POOL", "status": "IDLE", "url": "-", "dom_md5": "-"},
            }

        return {
            "service": "WebWorker Distributed Multi-Agent Matrix (Day 6)",
            "database": "WebWorker_db",
            "cloud_fallback_max_priority": 3,
            "go_gateway": go_status,
            "workers": workers,
            "db_telemetry": db_metrics,
        }

    def render_html(self) -> str:
        snapshot = self.collect_cluster_snapshot()
        workers_html = ""
        for wid, w in snapshot["workers"].items():
            badge_color = "#0284c7" if w.get("role") == "USER_CHROME_P1" else "#10b981"
            if "ABORTED" in str(w.get("status", "")):
                badge_color = "#dc2626"
            workers_html += f"""
            <div class="card">
              <div class="card-header">
                <strong>{wid}</strong>
                <span class="badge" style="background:{badge_color}">{w.get('status', 'IDLE')}</span>
              </div>
              <div class="meta">Role: <code>{w.get('role', '-')}</code></div>
              <div class="meta">URL: <code>{w.get('url', '-')}</code></div>
              <div class="meta">DOM MD5: <code>{str(w.get('dom_md5', '-'))[:12]}</code></div>
              <div class="meta">LLM Backend: <code>{w.get('llm_backend', 'ollama_local_gpu0')}</code></div>
            </div>
            """

        recent_rows = ""
        for t in snapshot.get("db_telemetry", {}).get("recent_tasks", []):
            recent_rows += f"""
            <tr>
              <td><code>{t.get('task_uuid')}</code></td>
              <td><span class="prio p{t.get('priority')}">P{t.get('priority')}</span></td>
              <td>{t.get('task_type')}</td>
              <td><strong>{t.get('status')}</strong></td>
              <td><code>{t.get('target_url')}</code></td>
              <td>{round(float(t.get('execution_time_seconds') or 0) * 1000, 1)} ms</td>
            </tr>
            """

        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="5" />
  <title>WebWorker_db Matrix Dashboard (:8085) - Dia 6</title>
  <style>
    body {{ background:#0b1120; color:#f8fafc; font-family:system-ui,sans-serif; margin:0; padding:24px; }}
    h1 {{ margin-top:0; color:#38bdf8; font-size:22px; }}
    .top-bar {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; border-bottom:1px solid #1e293b; padding-bottom:12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:14px; margin-bottom:24px; }}
    .card {{ background:#1e293b; border:1px solid #334155; border-radius:8px; padding:14px; }}
    .card-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }}
    .badge {{ padding:3px 8px; border-radius:99px; font-size:11px; font-weight:700; color:#fff; }}
    .meta {{ font-size:12.5px; color:#cbd5e1; margin-bottom:4px; word-break:break-all; }}
    table {{ width:100%; border-collapse:collapse; background:#1e293b; border-radius:8px; overflow:hidden; font-size:13px; }}
    th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #334155; }}
    th {{ background:#0f172a; color:#38bdf8; }}
    .prio {{ padding:2px 7px; border-radius:4px; font-weight:700; font-size:11px; }}
    .p1 {{ background:#dc2626; color:#fff; }}
    .p2 {{ background:#ea580c; color:#fff; }}
    .p3 {{ background:#2563eb; color:#fff; }}
    .p4 {{ background:#475569; color:#fff; }}
    .p5 {{ background:#7c3aed; color:#fff; }}
  </style>
</head>
<body>
  <div class="top-bar">
    <div>
      <h1>⚡ WebWorker Distributed Matrix (Dia 6) — Porta :8085</h1>
      <div style="font-size:13px; color:#94a3b8;">
        Banco Oficial: <code>WebWorker_db</code> | Go Gateway (:8000): <strong>{'ONLINE' if snapshot['go_gateway'].get('online') else 'STANDBY'}</strong> |
        Teto Cloud Fallback: <strong>Prioridade &le; P{snapshot['cloud_fallback_max_priority']} (P4/P5 bloqueados na nuvem)</strong>
      </div>
    </div>
  </div>

  <h3>Frota de Workers Concorrentes (Playwright Pool + Ponte Interativa P1)</h3>
  <div class="grid">{workers_html}</div>

  <h3>Fila Persistente & Auditoria de Tarefas (PostgreSQL 18: WebWorker_db)</h3>
  <table>
    <thead>
      <tr>
        <th>Task ID</th>
        <th>Prioridade</th>
        <th>Tipo</th>
        <th>Status</th>
        <th>URL (Anonimizada se P1 Privado)</th>
        <th>Latência</th>
      </tr>
    </thead>
    <tbody>{recent_rows}</tbody>
  </table>
</body>
</html>"""

    def start_background(self) -> None:
        dashboard_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/metrics"):
                    data = json.dumps(dashboard_ref.collect_cluster_snapshot(), ensure_ascii=False, default=str).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    html = dashboard_ref.render_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html)

            def do_POST(self):
                if self.path.startswith("/api/worker/dispatch"):
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode("utf-8")
                    try:
                        data = json.loads(body)
                        task_dict = data.get("task", {})
                        if dashboard_ref.fleet_ref:
                            accepted = dashboard_ref.fleet_ref.handle_pushed_task(task_dict)
                            self.send_response(200 if accepted else 429)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            self.wfile.write(json.dumps({"accepted": accepted}).encode("utf-8"))
                        else:
                            self.send_response(503)
                            self.end_headers()
                    except Exception as e:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def log_message(self, format, *args):
                return

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

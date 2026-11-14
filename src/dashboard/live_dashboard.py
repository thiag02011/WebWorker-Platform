"""
MÓDULO: REAL-TIME HTTP DASHBOARD SERVER (LiveDashboard)
=============================================================================
OBJETIVO:
Servir uma interface web em tempo real (porta 8085) para inspeção ao vivo do agente,
mostrando screenshot do navegador, entrada (DOM), saída (decisão) e dados do banco.

FLUXO DA ARQUITETURA:
1. Thread HTTP Server: Roda servidor embutido (http.server) sem travar o crawler principal.
   -> 2. API /api/telemetry: Expõe JSON com estado atual (URL, prompt, decisão, DOM podado).
   -> 3. API /api/screenshot: Fornece o screenshot PNG capturado no turno atual.
   -> 4. API /api/db-rows: Consulta o PostgreSQL e lista os animes mais recentes gravados.
   -> 5. Frontend Dark Mode: Página HTML com polling JS a cada 1 segundo atualizando o painel.
=============================================================================
"""
import os
import json
import base64
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Any, Optional

import psycopg2

class DashboardState:
    """Armazena o estado compartilhado da telemetria ao vivo."""
    def __init__(self):
        self.lock = threading.Lock()
        self.current_url: str = "Aguardando inicialização..."
        self.current_title: str = ""
        self.current_season: str = "Winter 2025"
        self.current_anime_name: str = ""
        self.agent_status: str = "Inicializando"
        self.step_index: int = 0
        self.what_ai_saw: str = "Aguardando primeiro snapshot do DOM..."
        self.current_prompt: str = ""
        self.what_ai_decided: str = "Nenhuma ação registrada ainda."
        self.last_tool_call: Dict[str, Any] = {}
        self.last_extracted_record: Dict[str, Any] = {}
        self.screenshot_base64: str = ""
        self.start_time: float = time.time()
        self.db_count: int = 0

    def update(
        self,
        url: Optional[str] = None,
        title: Optional[str] = None,
        season: Optional[str] = None,
        anime_name: Optional[str] = None,
        status: Optional[str] = None,
        ai_saw: Optional[str] = None,
        prompt: Optional[str] = None,
        ai_decided: Optional[str] = None,
        tool_call: Optional[Dict[str, Any]] = None,
        extracted_record: Optional[Dict[str, Any]] = None,
        screenshot_bytes: Optional[bytes] = None
    ):
        with self.lock:
            self.step_index += 1
            if url is not None:
                self.current_url = url
            if title is not None:
                self.current_title = title
            if season is not None:
                self.current_season = season
            if anime_name is not None:
                self.current_anime_name = anime_name
            if status is not None:
                self.agent_status = status
            if ai_saw is not None:
                self.what_ai_saw = ai_saw
            if prompt is not None:
                self.current_prompt = prompt
            if ai_decided is not None:
                self.what_ai_decided = ai_decided
            if tool_call is not None:
                self.last_tool_call = tool_call
            if extracted_record is not None:
                self.last_extracted_record = extracted_record
            if screenshot_bytes:
                self.screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            elapsed = time.time() - self.start_time
            return {
                "step_index": self.step_index,
                "current_url": self.current_url,
                "current_title": self.current_title,
                "current_season": self.current_season,
                "current_anime_name": self.current_anime_name,
                "agent_status": self.agent_status,
                "elapsed_seconds": int(elapsed),
                "what_ai_saw": self.what_ai_saw,
                "current_prompt": self.current_prompt,
                "what_ai_decided": self.what_ai_decided,
                "last_tool_call": self.last_tool_call,
                "last_extracted_record": self.last_extracted_record,
                "screenshot_base64": self.screenshot_base64,
                "db_count": self.db_count
            }

GLOBAL_STATE = DashboardState()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel de Controle ao Vivo | Agente Web AnimeSchedule 2025</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;800&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border-color: #30363d;
            --accent: #58a6ff;
            --accent-green: #3fb950;
            --accent-purple: #bc8cff;
            --accent-orange: #d29922;
            --accent-red: #f85149;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-primary);
            color: var(--text-primary);
            font-family: 'Inter', sans-serif;
            padding: 20px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px 24px;
            margin-bottom: 20px;
        }
        .header-title h1 {
            font-size: 1.4rem;
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .header-title p {
            color: var(--text-secondary);
            font-size: 0.85rem;
            margin-top: 4px;
        }
        .header-stats {
            display: flex;
            gap: 20px;
        }
        .stat-card {
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 8px 16px;
            text-align: center;
        }
        .stat-card .val {
            font-size: 1.2rem;
            font-weight: 700;
            color: var(--accent);
            font-family: 'JetBrains Mono', monospace;
        }
        .stat-card .lbl {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
        }

        .grid-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }

        .card {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            height: 480px;
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 10px;
            margin-bottom: 12px;
        }
        .card-header h2 {
            font-size: 1rem;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .badge {
            font-size: 0.75rem;
            padding: 3px 8px;
            border-radius: 12px;
            background: rgba(88, 166, 255, 0.15);
            color: var(--accent);
            font-family: 'JetBrains Mono', monospace;
        }

        .pre-box {
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 12px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            overflow-y: auto;
            flex-grow: 1;
            white-space: pre-wrap;
            word-break: break-all;
            color: #79c0ff;
        }

        .screenshot-img {
            max-width: 100%;
            max-height: 380px;
            object-fit: contain;
            border-radius: 6px;
            border: 1px solid var(--border-color);
            background: #000;
            display: block;
            margin: auto;
        }

        .db-section {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
        }
        .db-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
            margin-top: 12px;
        }
        .db-table th, .db-table td {
            border: 1px solid var(--border-color);
            padding: 8px 12px;
            text-align: left;
        }
        .db-table th {
            background: var(--bg-tertiary);
            color: #fff;
            font-weight: 600;
        }
        .db-table tr:hover {
            background: rgba(255, 255, 255, 0.02);
        }
    </style>
</head>
<body>

    <div class="header">
        <div class="header-title">
            <h1>🌐 Painel de Acompanhamento ao Vivo | Agente Web</h1>
            <p>Alvo: <span id="target-url" style="color: var(--accent); font-family: 'JetBrains Mono';">https://animeschedule.net/seasons?year=2025</span></p>
        </div>
        <div class="header-stats">
            <div class="stat-card">
                <div class="val" id="status-val">Iniciando</div>
                <div class="lbl">Status Agente</div>
            </div>
            <div class="stat-card">
                <div class="val" id="db-count-val">0</div>
                <div class="lbl">Animes no PostgreSQL</div>
            </div>
            <div class="stat-card">
                <div class="val" id="time-val">0s</div>
                <div class="lbl">Tempo Decorrido</div>
            </div>
        </div>
    </div>

    <div class="grid-container">
        <!-- Ponto do Site em que se Encontra -->
        <div class="card">
            <div class="card-header">
                <h2>📍 Ponto do Site em que se Encontra</h2>
                <span class="badge" id="current-url-badge">URL</span>
            </div>
            <p id="page-title-display" style="font-size: 0.85rem; color: #fff; margin-bottom: 8px; font-weight: 600;"></p>
            <div style="flex-grow: 1; display: flex; align-items: center; justify-content: center; overflow: hidden;">
                <img id="live-screenshot" class="screenshot-img" src="" alt="Screenshot da Página Atual" />
            </div>
        </div>

        <!-- Saída da IA / Tool Calls -->
        <div class="card">
            <div class="card-header">
                <h2>⚡ Saída da IA (Tool Calls & Extração)</h2>
                <span class="badge" style="color: var(--accent-green); background: rgba(63, 185, 80, 0.15);" id="step-badge">Turno 0</span>
            </div>
            <div class="pre-box" id="ai-decided-box" style="color: #7ee787;">Aguardando ação da IA...</div>
        </div>

        <!-- Entrada da IA (O que a IA está vendo no DOM) -->
        <div class="card">
            <div class="card-header">
                <h2>👀 Entrada da IA (Snapshot Podado do DOM)</h2>
                <span class="badge" style="color: var(--accent-purple); background: rgba(188, 140, 255, 0.15);">Accessibility Tree</span>
            </div>
            <div class="pre-box" id="ai-saw-box">Aguardando snapshot...</div>
        </div>

        <!-- Prompt Atual da Rodada -->
        <div class="card">
            <div class="card-header">
                <h2>📝 Instrução e Objetivo da Rodada</h2>
                <span class="badge" style="color: var(--accent-orange); background: rgba(210, 153, 34, 0.15);">Prompt Atual</span>
            </div>
            <div class="pre-box" id="prompt-box" style="color: #e3b341;">Aguardando prompt...</div>
        </div>
    </div>

    <!-- Tabela ao Vivo do PostgreSQL -->
    <div class="db-section">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2 style="font-size: 1.1rem; color: #fff;">🗄️ Dados Persistidos no PostgreSQL (`animeschedule_db.animes_2025`)</h2>
            <span class="badge" style="color: var(--accent); background: rgba(88, 166, 255, 0.15);">Auto-Refresh (1s)</span>
        </div>
        <table class="db-table">
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Título</th>
                    <th>Tipo</th>
                    <th>Temporada</th>
                    <th>Lançamento</th>
                    <th>Status</th>
                    <th>Fonte</th>
                    <th>Eps</th>
                    <th>Score</th>
                    <th>Estúdios</th>
                </tr>
            </thead>
            <tbody id="db-rows">
                <tr><td colspan="10" style="text-align: center; color: var(--text-secondary);">Nenhum registro ainda no banco.</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        async function updateDashboard() {
            try {
                const res = await fetch('/api/telemetry');
                if (!res.ok) return;
                const data = await res.json();

                document.getElementById('status-val').innerText = data.agent_status;
                document.getElementById('time-val').innerText = data.elapsed_seconds + 's';
                document.getElementById('db-count-val').innerText = data.db_count;
                document.getElementById('current-url-badge').innerText = data.current_url.substring(0, 45) + '...';
                document.getElementById('page-title-display').innerText = data.current_title || 'Navegador Ativo';
                document.getElementById('step-badge').innerText = 'Passo #' + data.step_index;

                document.getElementById('ai-saw-box').innerText = data.what_ai_saw;
                document.getElementById('prompt-box').innerText = data.current_prompt;
                
                let decidedFormatted = data.what_ai_decided;
                if (data.last_extracted_record && Object.keys(data.last_extracted_record).length > 0) {
                    decidedFormatted += '\\n\\n=== REGISTRO EXTRAÍDO ===\\n' + JSON.stringify(data.last_extracted_record, null, 2);
                }
                document.getElementById('ai-decided-box').innerText = decidedFormatted;

                if (data.screenshot_base64) {
                    document.getElementById('live-screenshot').src = 'data:image/png;base64,' + data.screenshot_base64;
                }

                // Carrega linhas do DB
                const dbRes = await fetch('/api/db-rows');
                if (dbRes.ok) {
                    const rows = await dbRes.json();
                    if (rows.length > 0) {
                        const tbody = document.getElementById('db-rows');
                        tbody.innerHTML = '';
                        rows.forEach(r => {
                            const tr = document.createElement('tr');
                            tr.innerHTML = `
                                <td>${r.id}</td>
                                <td style="font-weight: 600; color: #fff;">${r.title}</td>
                                <td><span class="badge">${r.anime_type || '-'}</span></td>
                                <td>${r.season || '-'}</td>
                                <td>${r.release_date || '-'}</td>
                                <td>${r.status || '-'}</td>
                                <td>${r.source || '-'}</td>
                                <td>${r.episodes || '-'}</td>
                                <td style="color: var(--accent-orange); font-weight: 600;">${r.score || '-'}</td>
                                <td>${r.studios || '-'}</td>
                            `;
                            tbody.appendChild(tr);
                        });
                    }
                }
            } catch (e) {
                console.error("Erro atualizando dashboard:", e);
            }
        }

        setInterval(updateDashboard, 1000);
        updateDashboard();
    </script>
</body>
</html>
"""


class DashboardHTTPHandler(BaseHTTPRequestHandler):
    """Handler leve para servir o painel de telemetria."""

    def log_message(self, format, *args):
        # Silencia logs de console para não sujar o terminal
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif self.path == "/api/telemetry":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = GLOBAL_STATE.to_dict()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path == "/api/db-rows":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            rows = []
            try:
                from src.config_manager import get_config
                cfg = get_config()
                conn = psycopg2.connect(
                    host=str(cfg.get("database.host", "127.0.0.1")),
                    port=int(cfg.get("database.port", 5432)),
                    user=str(cfg.get("database.user", "postgres")),
                    password=os.environ.get("WEBWORKER_DB_PASSWORD") or str(cfg.get("database.password", "")),
                    dbname=os.environ.get("WEBWORKER_LEGACY_DB_NAME", "animeschedule_db"),
                    connect_timeout=2,
                )
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, title, anime_type, season, release_date, status, source, episodes, score, studios
                    FROM animes_2025 ORDER BY id DESC LIMIT 20;
                """)
                for r in cur.fetchall():
                    rows.append({
                        "id": r[0],
                        "title": r[1],
                        "anime_type": r[2],
                        "season": r[3],
                        "release_date": r[4],
                        "status": r[5],
                        "source": r[6],
                        "episodes": r[7],
                        "score": r[8],
                        "studios": r[9]
                    })
                # Atualiza contagem total no estado
                cur.execute("SELECT COUNT(*) FROM animes_2025;")
                GLOBAL_STATE.db_count = cur.fetchone()[0]
                cur.close()
                conn.close()
            except Exception as e:
                pass
            self.wfile.write(json.dumps(rows).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def start_dashboard_server(port: int = 8085) -> HTTPServer:
    """Inicia o servidor da dashboard em background em uma thread daemon."""
    server = HTTPServer(("0.0.0.0", port), DashboardHTTPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"📊 Dashboard ao Vivo rodando em: http://localhost:{port}")
    return server

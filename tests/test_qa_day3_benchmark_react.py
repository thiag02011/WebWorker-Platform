"""
===============================================================================
SUITE DE QA E2E: BENCHMARK DO DIA 3 NO MOTOR REACT ATERRADO
CAMINHO: tests/test_qa_day3_benchmark_react.py
===============================================================================
Valida os 5 cenarios reais do Dia 3 (LGPD + Busca, Checkout, Portal KPIs,
Async Spinner e Curriculo) contra o motor ReAct aterrado (TriagedWebAgent),
Playwright Chromium e Ollama local.
===============================================================================
"""
import http.server
import socketserver
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ollama_manager import OllamaManager
from src.web.triaged_web_agent import TriagedWebAgent

PAGES = {
    "/": "<html><body><h1>Index</h1></body></html>",
    "/lgpd": """<!DOCTYPE html><html><head><title>Busca</title></head><body>
        <div id="modal" style="border:1px solid #000;padding:12px;">
            <p>Usamos cookies (LGPD).</p>
            <button id="btn-accept" onclick="document.getElementById('modal').style.display='none';document.getElementById('search-area').style.display='block';">Aceitar tudo</button>
        </div>
        <div id="search-area" style="display:none;">
            <input id="q" type="text" placeholder="Pesquisar">
            <button id="btn-go" onclick="document.getElementById('results').style.display='block';">Buscar</button>
        </div>
        <div id="results" style="display:none;">
            <h3>Resultado Oficial: Alerta Lobo (2019)</h3>
            <p>Filme de suspense submarino.</p>
        </div>
    </body></html>""",
    "/checkout": """<!DOCTYPE html><html><head><title>Checkout</title></head><body>
        <h1>Finalizar Pedido</h1>
        <form onsubmit="event.preventDefault();document.getElementById('out').innerText='PEDIDO CONFIRMADO #9981';">
            <input id="nome" type="text" placeholder="Nome completo">
            <input id="email" type="email" placeholder="E-mail">
            <button id="btn-pagar" type="submit">Confirmar Pedido</button>
        </form>
        <div id="out"></div>
    </body></html>""",
    "/portal": """<!DOCTYPE html><html><head><title>Portal Corporativo</title></head><body>
        <h1>Painel Financeiro Q4</h1>
        <table>
            <tr><th>Indicador</th><th>Valor</th></tr>
            <tr><td>Faturamento</td><td>R$ 4.850.000</td></tr>
            <tr><td>Usuarios Ativos</td><td>128.400</td></tr>
            <tr><td>SLA</td><td>99.98%</td></tr>
        </table>
    </body></html>""",
    "/async": """<!DOCTYPE html><html><head><title>Consulta Assincrona</title></head><body>
        <h1>Status do Servidor</h1>
        <button id="btn-check" onclick="document.getElementById('status').innerText='Carregando...'; setTimeout(()=>{document.getElementById('status').innerText='CONCLUIDO: Latencia 14ms';}, 800);">Verificar</button>
        <div id="status">Aguardando...</div>
    </body></html>""",
    "/cv": """<!DOCTYPE html><html><head><title>Candidatura</title></head><body>
        <h1>Enviar Curriculo</h1>
        <form onsubmit="event.preventDefault();document.getElementById('cv-out').innerText='CANDIDATURA ENVIADA';">
            <input id="c_nome" type="text" placeholder="Nome">
            <input id="c_cargo" type="text" placeholder="Cargo">
            <button id="btn-enviar" type="submit">Enviar</button>
        </form>
        <div id="cv-out"></div>
    </body></html>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        html = PAGES.get(self.path, PAGES["/"])
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass


class TestQADay3BenchmarkReact(unittest.TestCase):
    """Suite de QA E2E reproduzindo os 5 cenarios do Dia 3 no motor ReAct aterrado."""

    @classmethod
    def setUpClass(cls):
        cls.ollama_online = OllamaManager().is_server_online()
        if not cls.ollama_online:
            return
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        cls.agent = TriagedWebAgent(model="qwen2.5:3b-instruct-q8_0", headless=True, max_turns=12)
        cls.agent.start()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "ollama_online", False):
            cls.agent.stop()
            cls.httpd.shutdown()

    def setUp(self):
        if not self.ollama_online:
            self.skipTest("Ollama local offline; teste E2E de QA pulado.")

    def test_qa_day3_01_lgpd_and_search(self):
        res = self.agent.run_task(
            f"{self.base_url}/lgpd",
            "Aceite os cookies, busque 'Alerta Lobo' e extraia o titulo h3 do resultado.",
        )
        self.assertTrue(res["success"], res.get("error"))
        self.assertIn("Alerta Lobo", (res.get("final_result") or {}).get("summary", ""))

    def test_qa_day3_02_checkout_form(self):
        res = self.agent.run_task(
            f"{self.base_url}/checkout",
            "Preencha nome 'Carlos Silva', email 'carlos@teste.com', confirme o pedido e extraia o codigo de confirmacao.",
        )
        self.assertTrue(res["success"], res.get("error"))
        self.assertIn("9981", (res.get("final_result") or {}).get("summary", ""))

    def test_qa_day3_03_portal_kpis(self):
        res = self.agent.run_task(
            f"{self.base_url}/portal",
            "Extraia o Faturamento, Usuarios Ativos e SLA da tabela.",
        )
        self.assertTrue(res["success"], res.get("error"))
        summary = (res.get("final_result") or {}).get("summary", "")
        self.assertIn("4.850.000", summary)

    def test_qa_day3_04_async_spinner(self):
        res = self.agent.run_task(
            f"{self.base_url}/async",
            "Clique em Verificar, aguarde o carregamento e extraia a latencia.",
        )
        self.assertTrue(res["success"], res.get("error"))
        self.assertIn("14ms", (res.get("final_result") or {}).get("summary", ""))

    def test_qa_day3_05_cv_form(self):
        res = self.agent.run_task(
            f"{self.base_url}/cv",
            "Preencha nome 'Ana Souza', cargo 'Dev Python', envie e extraia a confirmacao.",
        )
        self.assertTrue(res["success"], res.get("error"))


if __name__ == "__main__":
    unittest.main()

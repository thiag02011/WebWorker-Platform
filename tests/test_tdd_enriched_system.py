"""
===============================================================================
MÓDULO: Suíte Enriquecida de Testes TDD & Mocks Herméticos
CAMINHO: tests/test_tdd_enriched_system.py
===============================================================================

OBJETIVO:
Bateria de testes TDD hermética e determinística com Mocks para validar:
- TDD-01: Extrator da Visão Geral Criada por IA (Google AI Overview).
- TDD-02: Extrator de Ficha Técnica / Knowledge Panel de Filmes (Alerta Lobo).
- TDD-03: DOM Simplifier & Detecção de Desafios Bot / Captcha (/sorry/index).
- TDD-04: Triagem Cognitiva de Contexto (Fase 0 -> Fase 1 -> Extração Direta).
- TDD-05: Watchdog Anti-Loop por MD5 (Streak = 3 força submit_error_reflection).
- TDD-06: Fila de Prioridades Min-Heap (Preempção P1 sobre P2..P5 e Default P3).
- TDD-07: Trava de Custo Cloud Fallback (Teto Máximo <= P3 | Bloqueio P4 e P5).
- TDD-08: Kill Switch & Protocolo de UX (Som 'Pop' + Toast Esmaecível).
===============================================================================
"""

import asyncio
import json
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.mocks.mock_html_fixtures import (
    GOOGLE_AI_OVERVIEW_HTML,
    GOOGLE_MOVIE_KNOWLEDGE_PANEL_HTML,
    GOOGLE_CAPTCHA_HTML,
    DYNAMIC_PORTAL_V1_HTML,
    DYNAMIC_PORTAL_V2_HTML,
)
from tests.mocks.mock_llm_service import MockOllamaManager
from src.web.search_extractors import GoogleAIOverviewExtractor, GoogleMovieKnowledgeExtractor
from src.web.dom_simplifier import DOMSimplifier
from src.web.triaged_web_agent import TriagedWebAgent
from src.multiagent.worker_fleet import MultiAgentWorkerFleet, MultiLLMRouterClient
from src.db.webworker_db_manager import WebWorkerDBManager


class TestEnrichedTDDSuite(unittest.TestCase):
    """Bateria de testes TDD com Mocks determinísticos para o ecossistema multiagente."""

    @classmethod
    def setUpClass(cls):
        cls.simplifier = DOMSimplifier()

    def test_tdd_01_google_ai_overview_extractor(self):
        """TDD-01: Valida a extração estruturada do bloco de Visão Geral por IA do Google."""
        # 1. Página contendo o bloco AI Overview
        res = GoogleAIOverviewExtractor.extract(GOOGLE_AI_OVERVIEW_HTML)
        self.assertTrue(res["found"], "Deveria encontrar a Visão Geral de IA")
        self.assertEqual(res["badge"], "Visão geral criada por IA")
        self.assertIn("usina nuclear", res["overview_text"].lower())
        self.assertIn("absorver e dissipar a descarga elétrica", res["overview_text"].lower())

        # Pontos-chave extraídos em lista
        self.assertGreaterEqual(res["points_count"], 4)
        points_joined = " ".join(res["key_points"]).lower()
        self.assertIn("pára-raios", points_joined)
        self.assertIn("desligamento automático", points_joined)
        self.assertIn("geradores de emergência", points_joined)
        self.assertIn("estrutura de contenção", points_joined)

        # 2. Página sem bloco de IA (ex: Captcha)
        res_no_ai = GoogleAIOverviewExtractor.extract(GOOGLE_CAPTCHA_HTML)
        self.assertFalse(res_no_ai["found"])

    def test_tdd_02_google_movie_knowledge_extractor(self):
        """TDD-02: Valida a extração dos campos solicitados para filmes (Alerta Lobo)."""
        res = GoogleMovieKnowledgeExtractor.extract(GOOGLE_MOVIE_KNOWLEDGE_PANEL_HTML)
        self.assertTrue(res["found"])
        self.assertIn("Alerta Lobo", res["nome"])
        self.assertEqual(res["lancamento"], "2019")
        self.assertEqual(res["duracao"], "1h 55m")

        # Tags / Gêneros
        tags = [t.lower() for t in res["tags"]]
        self.assertTrue(any("thriller" in t or "ação" in t for t in tags))

        # Notas / Avaliações
        notas_str = json.dumps(res["nota"])
        self.assertTrue("7,2/10" in notas_str or "4,5/5" in notas_str or "6,9/10" in notas_str)

        # Descrição / Sinopse
        self.assertIn("submarino", res["descricao"].lower())
        self.assertIn("acústica", res["descricao"].lower())

    def test_tdd_03_dom_simplifier_and_bot_detection(self):
        """TDD-03: Valida a poda semântica e identificação de bloqueio de bot/captcha."""
        # 1. Poda do DOM simplificado
        snapshot = self.simplifier.simplify(DYNAMIC_PORTAL_V1_HTML)
        self.assertIn("Lançamentos da Semana", snapshot.pruned_text)
        self.assertIn("Episódio 10 lançado", snapshot.pruned_text)
        self.assertGreater(len(snapshot.selector_map), 0)

        # 2. Detecção de bloqueio bot / captcha
        snap_captcha = self.simplifier.simplify(GOOGLE_CAPTCHA_HTML)
        is_blocked = "unusual traffic" in snap_captcha.pruned_text.lower() or "robot" in snap_captcha.pruned_text.lower()
        self.assertTrue(is_blocked, "Deveria detectar o bloqueio de bot do Google")

    def test_tdd_04_grounded_agent_answers_in_first_turn(self):
        """TDD-04: Dado presente na pagina -> resposta direta no 1o turno, sem fases de triagem."""
        mock_llm = MockOllamaManager()
        mock_llm.queue_response("browser_extract_data", {
            "answer": "Alerta Lobo (2019) - 7.2/10",
            "evidence": "Alerta Lobo 2019"
        })

        agent = TriagedWebAgent(max_turns=3)
        agent.ollama = mock_llm

        class MockEngine:
            def start(self): pass
            def stop(self): pass
            def get_url(self): return "https://www.google.com/search?q=filme+alerta+lobo"
            def navigate(self, url):
                snap = DOMSimplifier().simplify(GOOGLE_MOVIE_KNOWLEDGE_PANEL_HTML)
                return {"title": "filme alerta lobo - Pesquisa Google", "dom_snapshot": snap}
            def _get_current_snapshot(self):
                return DOMSimplifier().simplify(GOOGLE_MOVIE_KNOWLEDGE_PANEL_HTML)

        agent.engine = MockEngine()
        result = agent.run_task(
            target_url="https://www.google.com/search?q=filme+alerta+lobo",
            user_goal="Extrair ficha tecnica de Alerta Lobo"
        )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["turns"]), 1, "Deveria responder em exatamente 1 turno!")
        self.assertEqual(result["final_result"]["summary"], "Alerta Lobo (2019) - 7.2/10")
        self.assertEqual(len(mock_llm.call_history), 1, "Nao deve haver chamadas extras de triagem")

    def test_tdd_05_hallucinated_selectors_rejected_without_touching_browser(self):
        """TDD-05: Seletores inventados sao rejeitados antes do navegador; 3 erros seguidos encerram a tarefa."""
        mock_llm = MockOllamaManager()
        mock_llm.queue_response("browser_fill", {"selector": "searchInput", "value": "ID"})
        mock_llm.queue_response("browser_fill", {"selector": "id", "value": "ID"})
        mock_llm.queue_response("browser_click", {"selector": "#nao-existe"})

        agent = TriagedWebAgent(max_turns=6)
        agent.ollama = mock_llm
        calls = {"fill": 0, "click": 0}

        class MockStaticEngine:
            def start(self): pass
            def stop(self): pass
            def get_url(self): return "https://portal.exemplo.com"
            def navigate(self, url):
                return {"title": "Portal", "dom_snapshot": DOMSimplifier().simplify(DYNAMIC_PORTAL_V1_HTML)}
            def _get_current_snapshot(self):
                return DOMSimplifier().simplify(DYNAMIC_PORTAL_V1_HTML)
            def fill(self, sel, val, press_enter=False):
                calls["fill"] += 1
                return {"success": True}
            def click(self, sel):
                calls["click"] += 1
                return {"success": True}

        agent.engine = MockStaticEngine()
        result = agent.run_task(target_url="https://portal.exemplo.com", user_goal="Buscar titulo inexistente")

        self.assertFalse(result["success"])
        self.assertIn("erros consecutivos", result["error"])
        self.assertEqual(calls, {"fill": 0, "click": 0}, "Nenhum seletor inventado pode chegar ao navegador")

    def test_tdd_06_worker_fleet_preemption_and_privacy(self):
        """TDD-06: Valida preempção de prioridade P1 e anonimização de URLs no banco."""
        db = WebWorkerDBManager(auto_init=True)
        db.clear_all_for_test()

        # Enfileira P4 (Lote), P3 (Default quando omitido) e P1 (Interativo)
        t_p4 = db.enqueue_task("t_p4", "Crawler", "https://exemplo.com/lote", priority=4)
        t_p3 = db.enqueue_task("t_p3", "Rede", "https://exemplo.com/padrao")  # priority None -> assume 3
        t_p1 = db.enqueue_task(
            "t_p1",
            "Clique Usuário",
            "https://meu-banco.com.br/extrato?token=xyz",
            is_interactive=True,
            save_private_data=False
        )

        self.assertEqual(t_p3["priority"], 3, "Prioridade omitida deve assumir P3 por padrão")
        self.assertEqual(t_p1["priority"], 1, "Interatividade do usuário deve ser P1")
        self.assertEqual(t_p1["target_url"], "[PRIVADO: meu-banco.com.br]", "URL deve ser mascarada para privacidade")

        # Busca próxima tarefa no banco por ordem de prioridade
        next_task = db.fetch_next_task_from_db("worker_test")
        self.assertIsNotNone(next_task)
        self.assertEqual(next_task["task_uuid"], "t_p1", "P1 deve furar a fila e ser processada primeiro!")

    def test_tdd_07_cloud_fallback_ceiling_trava(self):
        """TDD-07: Valida trava de custo CLOUD_FALLBACK_MAX_PRIORITY = 3."""
        db = WebWorkerDBManager(auto_init=True)
        router = MultiLLMRouterClient(db)

        # Prioridades 1, 2 e 3 têm permissão para usar Cloud
        self.assertTrue(router.select_backend_for_priority(1, force_cloud=True)["allowed"])
        self.assertTrue(router.select_backend_for_priority(2, force_cloud=True)["allowed"])
        self.assertTrue(router.select_backend_for_priority(3, force_cloud=True)["allowed"])

        # Prioridades 4 e 5 NUNCA devem ter acesso à Cloud (economia estrita de custos)
        with self.assertRaises(PermissionError):
            router.select_backend_for_priority(4, force_cloud=True)

        with self.assertRaises(PermissionError):
            router.select_backend_for_priority(5, force_cloud=True)

    def test_tdd_08_kill_switch_and_ux_feedback(self):
        """TDD-08: Valida interrupção imediata pelo Kill Switch e retorno sonoro/toast."""
        fleet = MultiAgentWorkerFleet(num_workers=2)
        task_uuid = "task_test_abort_123"

        # Dispara o Kill Switch
        abort_res = fleet.trigger_kill_switch(task_uuid, reason="Usuário acionou o botão de pânico")

        self.assertEqual(abort_res["status"], "ABORTED")
        self.assertEqual(abort_res["task_uuid"], task_uuid)

        # UX Feedback: Som Pop e Toast com fade-out de 5s
        ux = abort_res["ux_feedback"]
        self.assertTrue(ux["play_pop_sound"])
        self.assertEqual(ux["fade_out_ms"], 5000)
        self.assertEqual(ux["toast_type"], "error")
        self.assertIn("Kill Switch", ux["toast_message"])

    def test_tdd_09_config_manager_and_google_base_url(self):
        """TDD-09: Valida auto-criação de config.json, eliminação do herokuapp e default Google.com."""
        import tempfile
        from src.config_manager import ConfigManager, get_config
        from src.web.browser_engine import BrowserEngine

        cfg = get_config()
        # 1. Valida valores padrão no config
        self.assertEqual(cfg.get("browser.default_base_url"), "https://www.google.com")
        self.assertEqual(cfg.get("servers.gateway_port"), 8000)
        self.assertEqual(cfg.get("servers.dashboard_port"), 8085)
        self.assertEqual(cfg.get("llm_backends.cloud_fallback.max_allowed_priority"), 3)

        # 2. Valida que BrowserEngine não tem mais o herokuapp e usa https://www.google.com
        engine = BrowserEngine(headless=True)
        self.assertEqual(engine.base_url, "https://www.google.com")
        self.assertNotIn("herokuapp", str(engine.base_url))

        # 3. Valida auto-criação resiliente caso o arquivo não exista
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_cfg_path = pathlib.Path(tmp_dir) / "config.json"
            self.assertFalse(tmp_cfg_path.exists())
            mgr = ConfigManager(custom_path=tmp_cfg_path)
            self.assertTrue(tmp_cfg_path.exists(), "Deveria auto-criar o arquivo de configuracao!")
            self.assertEqual(mgr.get("browser.default_base_url"), "https://www.google.com")

    def test_tdd_10_search_extractors_and_batch_reflection_enrichment(self):
        """TDD-10: Valida stop markers fracionados no extrator de IA, suporte a batch em busca e reflexão com escopo."""
        from src.web.search_extractors import GoogleAIOverviewExtractor
        from src.web.triaged_tools_schema import (
            get_phase2_tools,
            get_batch_discovery_tool,
            get_error_reflection_tool
        )

        # 1. Valida GoogleAIOverviewExtractor com disclaimers fracionados e quebra de linha
        html_with_fractioned_disclaimer = """
        <html>
        <body>
            <div class="MGB6P">
                <h3>Visão geral criada por IA</h3>
                <p>O Alerta Lobo é um filme francês de suspense e guerra submarina dirigido por Antonin Baudry.</p>
                <li>Lançamento em 2019</li>
                <li>Duração: 1h 55m</li>
                <div class="disclaimer-area">
                    <span>Resposta gerada por IA.</span>
                    <span>Por favor, verifique os fatos críticos.</span>
                </div>
            </div>
            <div class="regular-results">
                <h3>Resultados da web indesejados que nao devem ser lidos</h3>
                <p>Conteudo poluente da pagina.</p>
            </div>
        </body>
        </html>
        """
        extracted = GoogleAIOverviewExtractor.extract(html_with_fractioned_disclaimer)
        self.assertTrue(extracted["found"])
        self.assertIn("Alerta Lobo", extracted["overview_text"])
        self.assertNotIn("Resultados da web indesejados", extracted["overview_text"])
        self.assertNotIn("Conteudo poluente", extracted["overview_text"])

        # 2. Valida disponibilidade de browser_discover_batch_links quando scope='batch' mesmo em use_search_bar
        tools_single_search = get_phase2_tools(action="use_search_bar", scope="single")
        names_single = [t["function"]["name"] for t in tools_single_search]
        self.assertNotIn("browser_discover_batch_links", names_single)

        tools_batch_search = get_phase2_tools(action="use_search_bar", scope="batch")
        names_batch = [t["function"]["name"] for t in tools_batch_search]
        self.assertIn("browser_discover_batch_links", names_batch)

        # 3. Valida batch tool schema com suporte a coletar todos (0 ou omitido)
        batch_tool = get_batch_discovery_tool()
        desc = batch_tool["function"]["parameters"]["properties"]["max_items"]["description"]
        self.assertIn("TODOS", desc)

        # 4. Valida novos campos de reflexão (change_scope, change_strategy, restart_triage)
        refl_tool = get_error_reflection_tool()
        props = refl_tool["function"]["parameters"]["properties"]
        self.assertIn("change_scope", props)
        self.assertIn("change_strategy", props)
        self.assertIn("restart_triage", props)
        self.assertIn("collect_batch_links", props["next_action"]["enum"])


def run_tdd_suite():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestEnrichedTDDSuite)
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


if __name__ == "__main__":
    run_tdd_suite()

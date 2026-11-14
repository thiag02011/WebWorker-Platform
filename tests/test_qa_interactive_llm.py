"""
===============================================================================
SUITE DE QA COMPLETA: INTERATIVIDADE P1, LLM LOCAL E RESILIENCIA
CAMINHO: tests/test_qa_interactive_llm.py
===============================================================================
OBJETIVOS DE QA:
1. Inferencia Real / Fallback da LLM Local (Ollama) na tarefa interativa P1.
2. Tratamento Gracioso quando o Ollama esta Offline (sem crash/excecao).
3. Ativacao e Eficacia do Kill Switch (interrupcao imediata e audio de panico).
4. Modo de Privacidade: Anonimizacao estrita de URLs sensiveis em P1.
5. Resiliencia contra HTML Vazio, Nulo ou Malformado.
6. Ciclo de Vida do Worker P1 sem Vazamento de Slot / Concorrencia Limpa.
7. Filtragem Estrita de Scripts / XSS no Snapshot DOM.
8. Prevencao de Loop Infinito no Push Dispatch do Go Gateway.
===============================================================================
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.multiagent.worker_fleet import MultiAgentWorkerFleet
from src.ollama_manager import OllamaManager
from src.web.dom_simplifier import DOMSimplifier


class TestQAInteractiveLLM(unittest.TestCase):
    """Suite de QA intensivo para fluxo interativo P1 e integracao com Ollama."""

    def setUp(self):
        self.fleet = MultiAgentWorkerFleet(num_workers=1)

    def tearDown(self):
        self.fleet.stop()

    def test_qa_01_interactive_p1_extracts_id_with_llm(self):
        """QA 1: Valida que execute_interactive_p1_task consulta o conteudo e extrai o ID corretamente."""
        html_sample = (
            "<html><head><title>Produto Teste</title></head>"
            "<body><h1>Mouse Gamer Pro</h1>"
            "<p class='sku'>ID do item: 987654</p>"
            "<p>Preco: R$ 150</p></body></html>"
        )

        ollama = OllamaManager()
        is_online = ollama.is_server_online()

        async def _run():
            return await self.fleet.execute_interactive_p1_task(
                url="http://exemplo.local/item/1",
                objective="qual o ID do item?",
                html_snapshot_override=html_sample,
            )

        res = asyncio.run(_run())

        self.assertEqual(res.get("status"), "COMPLETED")
        self.assertIsNotNone(res.get("summary"))

        if is_online:
            self.assertIn("987654", res.get("summary"))
        else:
            self.assertIn("analisada", res.get("summary"))

    def test_qa_02_ollama_offline_graceful_fallback(self):
        """QA 2: Valida comportamento resiliente quando Ollama esta offline."""
        html_sample = "<html><body><h1>Dashboard</h1><p>Status: Operacional</p></body></html>"

        with patch.object(OllamaManager, "is_server_online", return_value=False):
            async def _run():
                return await self.fleet.execute_interactive_p1_task(
                    url="http://meusite.com/painel",
                    objective="Qual o status do sistema?",
                    html_snapshot_override=html_sample,
                )

            res = asyncio.run(_run())

            self.assertEqual(res.get("status"), "COMPLETED")
            self.assertIn("indisponivel", res.get("summary"))
            # Como a pagina tem <h1>Dashboard</h1>, o fallback extrai Dashboard como titulo
            self.assertIn("Dashboard", res.get("summary") or res.get("agent_response"))
            # Worker deve estar liberado (IDLE)
            self.assertEqual(self.fleet.worker_states["worker_p1_interactive"]["status"], "IDLE")

    def test_qa_03_kill_switch_mid_execution(self):
        """QA 3: Valida interrupcao imediata pelo Kill Switch e feedback sonoro de alerta."""
        html_sample = "<html><body><h1>Dados Criticos</h1></body></html>"

        async def _run():
            return await self.fleet.execute_interactive_p1_task(
                url="http://meusite.com/segredo",
                objective="extrair dados",
                simulate_abort_mid_execution=True,
                html_snapshot_override=html_sample,
            )

        res = asyncio.run(_run())

        self.assertEqual(res.get("status"), "ABORTED")
        ux = res.get("ux_feedback", {})
        self.assertTrue(ux.get("play_pop_sound"))
        # Frequencia de 320Hz indica alerta/panico do Kill Switch
        self.assertEqual(ux.get("sound_frequency_hz"), 320)
        self.assertEqual(ux.get("toast_type"), "error")

        # Verifica persistencia no DB em memoria
        task_in_db = self.fleet.db.get_task_by_uuid(res.get("task_uuid"))
        self.assertIsNotNone(task_in_db)
        self.assertEqual(task_in_db.get("status"), "ABORTED")

    def test_qa_04_privacy_mode_url_anonymization(self):
        """QA 4: Valida anonimizacao de credenciais e tokens em URLs quando privacidade esta ativa."""
        raw_url = "https://banco.local/dashboard?user_token=secret_xyz987&session=active#profile"

        async def _run():
            return await self.fleet.execute_interactive_p1_task(
                url=raw_url,
                objective="consultar saldo",
                user_explicitly_requested_save=False,
                html_snapshot_override="<html><body><h1>Conta Corrente</h1></body></html>",
            )

        res = asyncio.run(_run())

        self.assertTrue(res.get("privacy_mode_active"))
        stored_url = res.get("stored_url_in_db")
        # Token nao deve estar presente na URL persistida no banco
        self.assertNotIn("secret_xyz987", stored_url)
        self.assertEqual(stored_url, "[PRIVADO: banco.local]")

    def test_qa_05_empty_and_malformed_html_resilience(self):
        """QA 5: Valida que HTML nulo, vazio ou malformado nao quebra o motor."""
        test_cases = [
            ("", "vazio"),
            ("   \n\t  ", "espacos"),
            ("<div><span><b><i><u>teste quebrado</div>", "malformado"),
        ]

        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", return_value={"message": {"content": "Analise concluida"}}):
            for html_input, case_name in test_cases:
                with self.subTest(case=case_name):
                    async def _run():
                        return await self.fleet.execute_interactive_p1_task(
                            url="http://teste.local/broken",
                            objective="analisar",
                            html_snapshot_override=html_input,
                        )

                    res = asyncio.run(_run())
                    self.assertEqual(res.get("status"), "COMPLETED")
                    self.assertIsNotNone(res.get("summary"))

    def test_qa_06_sequential_lifecycle_and_no_slot_leak(self):
        """QA 6: Valida 3 tarefas consecutivas verificando liberacao correta de slots."""
        self.assertEqual(self.fleet.worker_states["worker_p1_interactive"]["status"], "STANDBY_P1")

        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", return_value={"message": {"content": "Resposta OK"}}):
            for i in range(1, 4):
                async def _run(idx):
                    return await self.fleet.execute_interactive_p1_task(
                        url=f"http://site.local/{idx}",
                        objective=f"pergunta {idx}",
                        html_snapshot_override=f"<html><body>Item {idx}</body></html>",
                    )

                res = asyncio.run(_run(i))
                self.assertEqual(res.get("status"), "COMPLETED")
                # Apos concluir, worker deve ter resetado estado
                self.assertEqual(self.fleet.worker_states["worker_p1_interactive"]["status"], "IDLE")
                self.assertIsNone(self.fleet.worker_states["worker_p1_interactive"]["current_task_uuid"])

    def test_qa_07_xss_and_script_injection_filtered(self):
        """QA 7: Valida que tags <script>, <style> e links maliciosos javascript: sao purgados do DOM."""
        malicious_html = (
            "<html><head>"
            "<script>alert('MALICIOUS_JS_EXECUTION'); window.location='http://attacker.com';</script>"
            "<style>body { display: none; background: red; }</style>"
            "</head><body>"
            "<h1>Area do Cliente</h1>"
            "<a href='javascript:exploit()'>Clique Aqui</a>"
            "<p>Numero da Conta: 12345-6</p>"
            "</body></html>"
        )

        simplifier = DOMSimplifier()
        snapshot = simplifier.simplify(malicious_html)

        # Scripts e estilos devem ser completamente descartados
        self.assertNotIn("MALICIOUS_JS_EXECUTION", snapshot.pruned_text)
        self.assertNotIn("display: none", snapshot.pruned_text)
        self.assertNotIn("javascript:exploit", str(snapshot.selector_map))
        # Conteudo legitimo deve ser preservado
        self.assertIn("Area do Cliente", snapshot.pruned_text)
        self.assertIn("12345-6", snapshot.pruned_text)

    @patch("urllib.request.build_opener")
    def test_qa_08_push_dispatch_no_infinite_loop_and_passes_llm_summary(self, mock_opener_builder):
        """QA 8: Valida que _execute_dispatched_job nao reenfileira tarefa e envia o resumo ao Go Gateway."""
        calls_made = []

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"updated": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        def fake_open(req, *args, **kwargs):
            calls_made.append(req.full_url)
            return mock_resp

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        mock_opener_builder.return_value = mock_opener

        task_spec = {
            "task_uuid": "task-qa-anti-loop-123",
            "target_url": "http://loja.local/prod/42",
            "user_goal": "qual o preco?",
            "priority": 1,
            "is_interactive": True,
            "save_private_data": False,
            "html_snapshot": "<html><body>Preco: R$ 89,90</body></html>",
        }

        # Mock Ollama para retornar resposta previsivel instantanea
        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", return_value={"message": {"content": "O preco e R$ 89,90"}}):
            self.fleet._execute_dispatched_job(task_spec)

        # Valida que JAMAIS chamou /api/task (o que causava o loop infinito)
        api_task_calls = [url for url in calls_made if url.endswith("/api/task")]
        self.assertEqual(len(api_task_calls), 0, "Loop infinito detectado: chamada recursiva a /api/task!")

        # Valida que chamou /api/worker/complete
        complete_calls = [url for url in calls_made if url.endswith("/api/worker/complete")]
        self.assertEqual(len(complete_calls), 1)

        # Worker_p1_interactive deve voltar para STANDBY_P1
        self.assertEqual(self.fleet.worker_states["worker_p1_interactive"]["status"], "STANDBY_P1")

    def test_qa_09_interactive_action_click_emission_and_selector_resolution(self):
        """QA 9: Valida que a LLM pode emitir ACTION: click e o worker resolve o seletor CSS no DOM."""
        html_page = (
            "<html><head><title>Loja Virtual</title></head>"
            "<body><h1>Mouse Gamer</h1>"
            "<button id='btn-add-cart' class='btn-primary'>Adicionar ao Carrinho</button>"
            "</body></html>"
        )

        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", return_value={"message": {"content": "ACTION: click @btn-add-cart [Adicionando produto]"}}):
            async def _run():
                return await self.fleet.execute_interactive_p1_task(
                    url="http://loja.local/mouse",
                    objective="clique em adicionar ao carrinho",
                    html_snapshot_override=html_page
                )

            res = asyncio.run(_run())

            self.assertEqual(res.get("status"), "COMPLETED")
            plan = res.get("action_plan", {})
            self.assertEqual(plan.get("type"), "action")
            self.assertEqual(plan.get("action"), "click")
            self.assertEqual(plan.get("target_id"), "@btn-add-cart")
            # Seletor resolvido pelo DOMSimplifier deve ser #btn-add-cart
            self.assertEqual(plan.get("selector"), "#btn-add-cart")

            # Telemetria do DOM deve estar presente
            dom_tel = res.get("dom_telemetry", {})
            self.assertGreaterEqual(dom_tel.get("elements_count", 0), 1)
            self.assertIn("@btn-add-cart", dom_tel.get("selector_map", {}))

    def test_qa_10_interactive_action_fill_emission_and_selector_resolution(self):
        """QA 10: Valida que a LLM pode emitir ACTION: fill e o worker empacota o valor a preencher."""
        html_page = (
            "<html><head><title>Busca de Produtos</title></head>"
            "<body><form><input id='search-input' name='q' type='text' placeholder='Pesquisar...' /></form></body></html>"
        )

        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", return_value={"message": {"content": "ACTION: fill @search-input 'teclado mecanico' [Digitando busca]"}}):
            async def _run():
                return await self.fleet.execute_interactive_p1_task(
                    url="http://loja.local/busca",
                    objective="digite teclado mecanico no campo de busca",
                    html_snapshot_override=html_page
                )

            res = asyncio.run(_run())

            self.assertEqual(res.get("status"), "COMPLETED")
            plan = res.get("action_plan", {})
            self.assertEqual(plan.get("type"), "action")
            self.assertEqual(plan.get("action"), "fill")
            self.assertEqual(plan.get("target_id"), "@search-input")
            self.assertEqual(plan.get("selector"), "#search-input")
            self.assertEqual(plan.get("value"), "teclado mecanico")

    def test_qa_11_viewport_visible_text_priority_and_no_private_url_hallucination(self):
        """QA 11: Valida foco na tela atual (viewport) e ausencia de [PRIVADO: ...] no prompt da LLM."""
        html_page = (
            "<title>Scaffolding - Minecraft Wiki</title>\n"
            "<div class='mw-body'><a href='/w/Block'>Block</a><button id='edit-btn'>Edit</button>"
            "<p>Scaffolding is a climbable block.</p></div>"
        )
        visible_viewport = (
            "<h2>ID</h2>\n"
            "| Name | Identifier | Numeric ID |\n"
            "| Scaffolding | scaffolding | 418 |"
        )

        captured_messages = []

        def fake_chat(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return {"message": {"content": "O identificador do item e 'scaffolding' (Numeric ID: 418)."}}

        with patch.object(OllamaManager, "is_server_online", return_value=True), \
             patch.object(OllamaManager, "chat", side_effect=fake_chat):
            async def _run():
                return await self.fleet.execute_interactive_p1_task(
                    url="[PRIVADO: minecraft.wiki]",
                    objective="qual o id do item?",
                    html_snapshot_override=html_page,
                    visible_text=visible_viewport,
                )

            res = asyncio.run(_run())

            self.assertEqual(res.get("status"), "COMPLETED")
            self.assertIn("scaffolding", res.get("summary"))

            # Garante que [PRIVADO: minecraft.wiki] nao vazou para o prompt da LLM
            user_prompt = next((m["content"] for m in captured_messages if m["role"] == "user"), "")
            self.assertNotIn("[PRIVADO:", user_prompt)
            self.assertIn("Conteúdo Visível na Tela Atual", user_prompt)
            self.assertIn("| Scaffolding | scaffolding | 418 |", user_prompt)

            # Telemetria do DOM deve conter os elementos e o preview combinado
            dom_tel = res.get("dom_telemetry", {})
            self.assertGreaterEqual(dom_tel.get("elements_count", 0), 2)
            self.assertIn("[TELA ATUAL / VIEWPORT]", dom_tel.get("pruned_text_preview", ""))


    def test_qa_12_non_interactive_dispatched_job_no_name_error_and_no_unbound_res(self):
        """QA 12: Regressao TDD: Valida que tarefas nao-interativas (P2..P5) despachadas para worker_2
        nao disparam NameError: name 'cfg' is not defined nem UnboundLocalError em 'res'."""
        task_uuid = "task-tdd-craft-test-123"
        task_spec = {
            "task_uuid": task_uuid,
            "user_goal": "como se faz o craft do item?",
            "target_url": "https://minecraft.wiki/w/Scaffolding",
            "priority": 3,
            "task_type": "web_agent",
            "is_interactive": False,
        }

        mock_agent = MagicMock()
        mock_agent.run_task.return_value = {
            "success": True,
            "turns": [{"turn": 1, "action": "browser_extract_data"}],
            "final_result": {"crafting": "6 bamboo + 1 string"},
            "summary": "Crafting: 6 Bambus e 1 Linha geram 6 Andaimes."
        }

        with patch("src.multiagent.worker_fleet.TriagedWebAgent", return_value=mock_agent):
            # Executa diretamente o job despachado
            self.fleet._execute_dispatched_job(task_spec)

            # Valida que o status no banco foi atualizado para COMPLETED sem quebrar o thread
            row = self.fleet.db.get_task_by_uuid(task_uuid)
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "COMPLETED")
            self.assertTrue(row.get("assigned_worker_id", "").startswith("worker_"))
            self.assertIsNone(row["error_details"])
            self.assertIn("6 bamboo", str(row.get("result_data", {})))

    def test_qa_13_audit_pyramid_stage2_executive_refiner(self):
        """QA 13: Valida a Piramide de Auditoria Cognitiva (Estagio 1 rascunho bruto -> Estagio 2 refinamento limpo)."""
        html_page = "<title>Scaffolding - Minecraft Wiki</title><p>Scaffolding block</p>"
        visible_text = "Data values: Bedrock Edition ID is -165, Java Edition is scaffolding."

        raw_stage1_output = (
            "De acordo com a seção de dados (Data values), para o bloco de scaffolding na versão Java Edition, "
            "o ID do item é -165. Aqui está o trecho relevante da documentação: "
            "[link @169 /w/Bedrock_Edition] Bedrock Edition : Name [link @170 /w/Identifier] Identifier Numeric ID Form [link @171]..."
        )
        stage2_refined_output = "ID do Bloco: scaffolding (Java Edition) | ID Numérico: -165 (Bedrock Edition)"

        calls = []
        def fake_chat(*args, **kwargs):
            messages = kwargs.get("messages", [])
            calls.append(messages)
            if len(calls) == 1:
                return {"message": {"content": raw_stage1_output}}
            return {"message": {"content": stage2_refined_output}}

        with patch.object(OllamaManager, "is_server_online", return_value=True),              patch.object(OllamaManager, "chat", side_effect=fake_chat):
            async def _run():
                return await self.fleet.execute_interactive_p1_task(
                    url="https://minecraft.wiki/w/Scaffolding",
                    objective="qual a id do item?",
                    html_snapshot_override=html_page,
                    visible_text=visible_text,
                )

            res = asyncio.run(_run())

            self.assertEqual(res.get("status"), "COMPLETED")
            # A resposta final entregue a HUD deve ser a saida limpa do Estagio 2
            self.assertEqual(res.get("summary"), stage2_refined_output)
            self.assertNotIn("[link @169", res.get("summary"))
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

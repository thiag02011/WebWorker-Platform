"""
===============================================================================
SUITE DE TESTES TDD: RESILIENCIA ASSINCRONA E CONCORRENCIA (test_tdd_concurrency_and_async_resilience.py)
CAMINHO: tests/test_tdd_concurrency_and_async_resilience.py
===============================================================================
OBJETIVO:
Garantir a prevencao de regressao para os 3 problemas criticos identificados:
1. Prevencao de travamento por criacao desnecessaria de loopback sockets (asyncio.new_event_loop)
   em tarefas sincronas ou mocks de teste.
2. Prevencao de bloqueio por transformacao automatica em AsyncMock pelo unittest.mock.
3. Isolamento da thread de heartbeat em suites de teste via parametro start_heartbeat=False.
4. Encerramento deterministico e liberacao de recursos com join gracioso em fleet.stop().
5. Conformidade do protocolo de leitura de respostas HTTP usando context manager (with opener.open).
===============================================================================
"""

import asyncio
import json
import pathlib
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Garante path correto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.multiagent.worker_fleet import MultiAgentWorkerFleet


class TestTDDConcurrencyAndAsyncResilience(unittest.TestCase):
    """Testes TDD para garantia de resiliencia assincrona, isolamento de threads e context managers."""

    def setUp(self):
        self.fleet = MultiAgentWorkerFleet(num_workers=2, go_gateway_url="http://127.0.0.1:8000")

    def tearDown(self):
        self.fleet.stop()

    def test_01_dispatched_job_does_not_create_event_loop_when_sync(self):
        """Valida que _execute_dispatched_job nao aloca loopback socket quando o retorno e sincrono."""
        task_spec = {
            "task_uuid": "task-resilience-01",
            "priority": 1,
            "user_goal": "Acao de teste de socket",
            "target_url": "https://exemplo.com",
            "is_interactive": True,
            "save_private_data": False
        }

        # Simula retorno imediato de dicionario
        mock_p1 = MagicMock(return_value={"status": "COMPLETED", "turns_used": 1, "result_data": {"ok": True}})
        
        # Garante que asyncio.new_event_loop NUNCA e invocado para tarefas sincronas
        with patch("asyncio.new_event_loop", side_effect=AssertionError("asyncio.new_event_loop nao deveria ser chamado para retorno sincrono!")):
            with patch.object(self.fleet, "execute_interactive_p1_task", new=mock_p1):
                with patch("urllib.request.build_opener") as mock_opener_builder:
                    mock_resp = MagicMock()
                    mock_resp.read.return_value = b"{}"
                    mock_resp.__enter__.return_value = mock_resp
                    mock_opener = MagicMock()
                    mock_opener.open.return_value = mock_resp
                    mock_opener_builder.return_value = mock_opener

                    self.fleet._execute_dispatched_job(task_spec)

        saved = self.fleet.db.get_task_by_uuid("task-resilience-01")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "COMPLETED")

    def test_02_dispatched_job_prevents_involuntary_asyncmock_lock(self):
        """Valida que o tratamento de retorno detecta se e corotina real antes de chamar run_until_complete."""
        task_spec = {
            "task_uuid": "task-resilience-02",
            "priority": 1,
            "user_goal": "Acao com mock síncrono",
            "target_url": "https://exemplo.com",
            "is_interactive": True,
            "save_private_data": False
        }

        # Quando usamos new=MagicMock, execute_interactive_p1_task retorna um dict
        # e asyncio.iscoroutine(...) e False, evitando deadlock em ambiente sandbox
        mock_p1 = MagicMock(return_value={"status": "COMPLETED", "turns_used": 2})
        with patch.object(self.fleet, "execute_interactive_p1_task", new=mock_p1):
            with patch("urllib.request.build_opener") as mock_opener_builder:
                mock_resp = MagicMock()
                mock_resp.read.return_value = b"{}"
                mock_resp.__enter__.return_value = mock_resp
                mock_opener = MagicMock()
                mock_opener.open.return_value = mock_resp
                mock_opener_builder.return_value = mock_opener

                self.fleet._execute_dispatched_job(task_spec)

        mock_p1.assert_called_once()
        saved = self.fleet.db.get_task_by_uuid("task-resilience-02")
        self.assertEqual(saved["turns_used"], 2)

    @patch("urllib.request.build_opener")
    def test_03_heartbeat_isolation_flag_prevents_spawning_daemon_thread(self, mock_opener_builder):
        """Valida que start_heartbeat=False impede criacao de thread paralela concorrente."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"registered": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        # Registra explicitamente desativando o heartbeat continuo
        success = self.fleet.register_with_gateway("http://127.0.0.1:8085/api/worker/dispatch", start_heartbeat=False)
        self.assertTrue(success)

        # A thread de heartbeat deve permanecer nula e nenhuma chamada concorrente ao open deve ocorrer
        self.assertIsNone(self.fleet._heartbeat_thread)

    @patch("urllib.request.build_opener")
    def test_04_fleet_stop_deterministically_joins_heartbeat_thread(self, mock_opener_builder):
        """Valida que com start_heartbeat=True a thread inicia e e finalizada de forma limpa por stop()."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"registered": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        success = self.fleet.register_with_gateway("http://127.0.0.1:8085/api/worker/dispatch", start_heartbeat=True)
        self.assertTrue(success)
        self.assertIsNotNone(self.fleet._heartbeat_thread)
        self.assertTrue(self.fleet._heartbeat_thread.is_alive())

        # Executa parada graciosa
        self.fleet.stop()

        # Thread deve ter sido unida e limpa
        self.assertTrue(self.fleet._stop_heartbeat)
        self.assertIsNone(self.fleet._heartbeat_thread)

    @patch("urllib.request.build_opener")
    def test_05_http_context_manager_mock_contract(self, mock_opener_builder):
        """Valida que os metodos de rede usam o gerenciador de contexto com __enter__ no opener."""
        # Se __enter__ nao estivesse mockado, json.loads receberia MagicMock e falharia com TypeError
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "OK", "registered": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        success = self.fleet.register_with_gateway("http://127.0.0.1:8085/api/worker/dispatch", start_heartbeat=False)
        self.assertTrue(success)

        # Valida que __enter__ foi de fato invocado como parte do contrato 'with opener.open(...) as resp:'
        mock_resp.__enter__.assert_called()


if __name__ == "__main__":
    unittest.main()

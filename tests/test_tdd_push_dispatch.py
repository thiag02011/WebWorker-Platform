"""
===============================================================================
SUITE DE TESTES TDD: PUSH DISPATCH & BACKPRESSURE (test_tdd_push_dispatch.py)
CAMINHO: tests/test_tdd_push_dispatch.py
===============================================================================
OBJETIVO:
Validar o fluxo orientado a eventos por Push Dispatch entre o Gateway Go e a
Frota de Workers Python:
1. Leitura de workers_available (capacidade real ativa, ex: 1 na GPU Local)
2. Registro de listener push via POST /api/worker/register
3. Heartbeat periodico de 500ms
4. Recepcao de tarefa via POST /api/worker/dispatch no Dashboard
5. Backpressure: rejeicao com HTTP 429 / False quando active_tasks >= workers_available
6. Execucao de tarefa com persistencia no banco e notificacao de conclusao ao Go
===============================================================================
"""

import json
import os
import pathlib
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# Garante path correto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_manager import ConfigManager, get_config
from src.multiagent.worker_fleet import MultiAgentWorkerFleet
from src.dashboard.multiagent_dashboard import MultiAgentDashboardServer


class TestTDDPushDispatch(unittest.TestCase):
    """Suite de testes TDD para o mecanismo de Push Dispatch e Backpressure."""

    def setUp(self):
        self.fleet = MultiAgentWorkerFleet(num_workers=3, go_gateway_url="http://127.0.0.1:8000")
        self.dashboard = MultiAgentDashboardServer(port=8085, go_gateway_url="http://127.0.0.1:8000", fleet_ref=self.fleet)

    def tearDown(self):
        self.fleet.stop()
        self.dashboard.stop()

    def test_01_workers_available_reads_from_config(self):
        """Valida que a frota identifica a capacidade real de workers_available (ex: 1)."""
        cfg = get_config()
        expected = int(cfg.get("fleet.workers_available") or 1)
        self.assertEqual(self.fleet.workers_available, expected)
        self.assertLessEqual(self.fleet.workers_available, self.fleet.num_workers)

    @patch("urllib.request.build_opener")
    def test_02_register_with_gateway_posts_metadata(self, mock_opener_builder):
        """Valida que register_with_gateway envia a URL de escuta e workers_available ao Go."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"registered": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        listen_url = "http://127.0.0.1:8085/api/worker/dispatch"
        success = self.fleet.register_with_gateway(listen_url, start_heartbeat=False)

        self.assertTrue(success)
        mock_opener.open.assert_called()
        req_sent = mock_opener.open.call_args[0][0]
        self.assertIn("/api/worker/register", req_sent.full_url)
        payload = json.loads(req_sent.data.decode("utf-8"))
        self.assertEqual(payload["listen_url"], listen_url)
        self.assertEqual(payload["workers_available"], self.fleet.workers_available)

    def test_03_handle_pushed_task_respects_backpressure(self):
        """Valida que tarefas sao aceitas ate o limite de workers_available e rejeitadas quando cheio."""
        self.fleet.workers_available = 1
        self.fleet.active_tasks_count = 0

        # Tarefa 1: Deve ser aceita pois active_tasks (0) < workers_available (1)
        with patch.object(self.fleet, "_execute_dispatched_job"):
            accepted_1 = self.fleet.handle_pushed_task({
                "task_uuid": "task-001",
                "priority": 1,
                "user_goal": "Acao 1",
                "target_url": "https://google.com"
            })
            self.assertTrue(accepted_1, "Primeira tarefa deveria ser aceita.")
            self.assertEqual(self.fleet.active_tasks_count, 1)

            # Tarefa 2: Deve ser REJEITADA pois atingiu o limite de capacidade
            accepted_2 = self.fleet.handle_pushed_task({
                "task_uuid": "task-002",
                "priority": 3,
                "user_goal": "Acao 2",
                "target_url": "https://google.com"
            })
            self.assertFalse(accepted_2, "Segunda tarefa deveria ser rejeitada por backpressure.")
            self.assertEqual(self.fleet.active_tasks_count, 1)

    @patch("urllib.request.build_opener")
    def test_04_execution_updates_db_and_notifies_go_completion(self, mock_opener_builder):
        """Valida que a execucao salva a tarefa no banco e envia POST /api/worker/complete."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        task_spec = {
            "task_uuid": "task-test-complete-123",
            "priority": 1,
            "user_goal": "Pesquisar clima",
            "target_url": "https://google.com",
            "is_interactive": True,
            "save_private_data": False
        }

        # Simula execucao
        mock_p1 = MagicMock(return_value={"status": "COMPLETED", "turns_used": 1})
        with patch.object(self.fleet, "execute_interactive_p1_task", new=mock_p1):
            self.fleet._execute_dispatched_job(task_spec)

        # 1. Verifica persistencia no banco
        saved = self.fleet.db.get_task_by_uuid("task-test-complete-123")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["priority"], 1)

        # 2. Verifica notificacao de complete ao Go Gateway
        req_sent = mock_opener.open.call_args[0][0]
        self.assertIn("/api/worker/complete", req_sent.full_url)
        payload = json.loads(req_sent.data.decode("utf-8"))
        self.assertEqual(payload["task_uuid"], "task-test-complete-123")
        self.assertEqual(payload["status"], "COMPLETED")

    def test_05_hardware_agnostic_backend_sanitization(self):
        """Valida que nenhum identificador de hardware especifico (ex: rtx, 5060) esta fixado no sistema."""
        cfg = get_config()
        local_backend = cfg.get("llm_backends.local_gpu")
        self.assertIsNotNone(local_backend)
        self.assertEqual(local_backend.get("id"), "local_gpu")
        
        # Garante que nenhum campo de backend possui strings acopladas a placas especificas
        dumped_backends = json.dumps(cfg.get("llm_backends", {})).lower()
        self.assertNotIn("5060", dumped_backends)
        self.assertNotIn("rtx", dumped_backends)

        # Valida que o router da frota usa o no generico local_gpu
        self.assertIn("local_gpu", self.fleet.llm_router.nodes)



if __name__ == "__main__":
    unittest.main()

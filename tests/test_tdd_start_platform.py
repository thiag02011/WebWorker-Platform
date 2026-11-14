"""
===============================================================================
SUITE DE TESTES TDD: INICIALIZADOR INTEGRADO (start_platform.py)
CAMINHO: tests/test_tdd_start_platform.py
===============================================================================
OBJETIVO:
Bateria de testes TDD para validar o ciclo de vida, verificacoes de seguranca,
integracao com o gerenciador de configuracoes (config.json) e resiliencia de
inicializacao do launcher mestre start_platform.py:
1. Leitura dinamica da versao a partir do ConfigManager
2. Deteccao acurada de portas livres e ocupadas (is_port_in_use)
3. Verificacao de pre-requisitos (binario Go, portas :8000 e :8085)
4. Falha graciosa se o binario Go estiver ausente (SEM tentar compilar)
5. Inicializacao e deteccao de processo do Gateway Go
6. Tolerancia a reuso de Gateway Go ja em execucao
7. Inicializacao da frota com quantidade configurada de workers
8. Ausencia estrita de chamadas a webbrowser.open
9. Ausencia estrita de emojis nos prints/logs
===============================================================================
"""

import io
import os
import re
import socket
import sys
import pathlib
import unittest
from unittest.mock import patch, MagicMock

# Garante path correto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import start_platform
from src.config_manager import ConfigManager, get_config


class TestTDDStartPlatform(unittest.TestCase):
    """Suite de testes unitarios e integrados para o start_platform.py."""

    def setUp(self):
        self.mock_config = ConfigManager.get_instance()

    # -------------------------------------------------------------------------
    # 1. VERSAO DINAMICA DA CONFIGURACAO
    # -------------------------------------------------------------------------
    def test_01_version_loaded_dynamically_from_config(self):
        """Valida que a versao do sistema exibida e obtida dinamicamente do config.json sem hardcode."""
        with patch.object(self.mock_config, "get", return_value="1.1") as mock_get:
            version = start_platform.get_platform_version()
            self.assertEqual(version, "1.1")
            mock_get.assert_called_with("version")

        # Testa se mudanca na config reflete imediatamente no launcher
        with patch.object(self.mock_config, "get", return_value="2.0-custom"):
            version_updated = start_platform.get_platform_version()
            self.assertEqual(version_updated, "2.0-custom")

        # Valida que rejeita versao ausente em vez de usar fallback hardcoded
        with patch.object(self.mock_config, "get", return_value=None):
            with self.assertRaises(ValueError):
                start_platform.get_platform_version()

    # -------------------------------------------------------------------------
    # 2. DETECCAO DE PORTAS LIVRES E OCUPADAS
    # -------------------------------------------------------------------------
    def test_02_is_port_in_use_accuracy(self):
        """Valida deteccao precisa de porta livre e porta ocupada usando socket real."""
        # Cria um socket temporario para simular uma porta ocupada
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.bind(("127.0.0.1", 0))  # Porta aleatoria do SO
        assigned_port = test_sock.getsockname()[1]

        try:
            # Enquanto o socket esta aberto, a porta deve constar como ocupada
            self.assertTrue(
                start_platform.is_port_in_use(assigned_port),
                f"Porta {assigned_port} deveria ser detectada como ocupada."
            )
        finally:
            test_sock.close()

        # Apos fechar o socket, a porta deve constar como livre
        self.assertFalse(
            start_platform.is_port_in_use(assigned_port),
            f"Porta {assigned_port} deveria ser detectada como livre apos close()."
        )

    # -------------------------------------------------------------------------
    # 3. PING AO GATEWAY GO
    # -------------------------------------------------------------------------
    @patch("urllib.request.build_opener")
    def test_03_ping_go_gateway_success_and_failure(self, mock_opener_builder):
        """Valida ping de saude do Gateway Go em cenarios de sucesso e falha."""
        # Cenario 1: Resposta HTTP 200
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.status = 200
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_builder.return_value = mock_opener

        self.assertTrue(start_platform.ping_go_gateway(8000))

        # Cenario 2: Falha de conexao / timeout
        mock_opener.open.side_effect = Exception("Connection refused")
        self.assertFalse(start_platform.ping_go_gateway(8000))

    # -------------------------------------------------------------------------
    # 4. PRE-REQUISITOS: BINARIO GO AUSENTE (SEM AUTO-BUILD)
    # -------------------------------------------------------------------------
    def test_04_verify_prerequisites_fails_if_binary_missing_without_compiling(self):
        """Valida que a falta do binario Go aborta a inicializacao sem tentar compilar."""
        fake_path = pathlib.Path("caminho_inexistente/webworker_gateway.exe")

        with patch("subprocess.run") as mock_subproc_run:
            stdout_trap = io.StringIO()
            with patch("sys.stdout", stdout_trap):
                result = start_platform.verify_prerequisites(8000, 8085, fake_path)

            self.assertFalse(result, "Deveria retornar False quando o binario Go esta ausente.")
            # Garante que NENHUM comando 'go build' foi executado
            mock_subproc_run.assert_not_called()
            output = stdout_trap.getvalue()
            self.assertIn("[ERRO]", output)
            self.assertIn("Compile previamente", output)

    # -------------------------------------------------------------------------
    # 5. PRE-REQUISITOS: CONFLITO DE PORTAS
    # -------------------------------------------------------------------------
    @patch("start_platform.is_port_in_use")
    @patch("start_platform.ping_go_gateway")
    def test_05_verify_prerequisites_dashboard_port_occupied(self, mock_ping, mock_port_in_use):
        """Valida rejeicao quando a porta do Dashboard (:8085) ja esta ocupada."""
        mock_exe = MagicMock()
        mock_exe.exists.return_value = True
        mock_exe.name = "webworker_gateway.exe"

        # Gateway livre, mas dashboard ocupado
        mock_port_in_use.side_effect = lambda port, host="127.0.0.1": port == 8085

        stdout_trap = io.StringIO()
        with patch("sys.stdout", stdout_trap):
            result = start_platform.verify_prerequisites(8000, 8085, mock_exe)

        self.assertFalse(result)
        self.assertIn("A porta 8085 (Dashboard Web) ja esta em uso", stdout_trap.getvalue())

    @patch("start_platform.is_port_in_use")
    @patch("start_platform.ping_go_gateway")
    def test_06_verify_prerequisites_gateway_occupied_by_other_process(self, mock_ping, mock_port_in_use):
        """Valida rejeicao quando a porta 8000 esta ocupada por outro processo que nao e o Go."""
        mock_exe = MagicMock()
        mock_exe.exists.return_value = True
        mock_exe.name = "webworker_gateway.exe"

        mock_port_in_use.side_effect = lambda port, host="127.0.0.1": port == 8000
        mock_ping.return_value = False  # Nao responde ao healthcheck do Go

        stdout_trap = io.StringIO()
        with patch("sys.stdout", stdout_trap):
            result = start_platform.verify_prerequisites(8000, 8085, mock_exe)

        self.assertFalse(result)
        self.assertIn("A porta 8000 ja esta em uso por outro processo que nao responde", stdout_trap.getvalue())

    @patch("start_platform.is_port_in_use")
    @patch("start_platform.ping_go_gateway")
    def test_07_verify_prerequisites_passes_when_gateway_already_active(self, mock_ping, mock_port_in_use):
        """Valida que se o Gateway Go ja estiver ativo e respondendo na porta 8000, prossegue com sucesso."""
        mock_exe = MagicMock()
        mock_exe.exists.return_value = True
        mock_exe.name = "webworker_gateway.exe"

        # Porta 8000 ocupada, porta 8085 livre
        mock_port_in_use.side_effect = lambda port, host="127.0.0.1": port == 8000
        mock_ping.return_value = True  # Responde ao healthcheck do Go

        stdout_trap = io.StringIO()
        with patch("sys.stdout", stdout_trap):
            result = start_platform.verify_prerequisites(8000, 8085, mock_exe)

        self.assertTrue(result)
        self.assertIn("Gateway Go ja esta ativo e respondendo", stdout_trap.getvalue())

    # -------------------------------------------------------------------------
    # 6. INICIALIZACAO DO GATEWAY GO
    # -------------------------------------------------------------------------
    @patch("start_platform.ping_go_gateway")
    def test_08_start_go_gateway_returns_none_if_already_healthy(self, mock_ping):
        """Valida que se o Go Gateway ja esta ativo, nao cria um segundo subprocesso."""
        mock_ping.return_value = True
        fake_exe = pathlib.Path("go_services/webworker_gateway.exe")

        with patch("subprocess.Popen") as mock_popen:
            proc = start_platform.start_go_gateway(8000, fake_exe)
            self.assertIsNone(proc)
            mock_popen.assert_not_called()

    @patch("start_platform.ping_go_gateway")
    @patch("start_platform.is_port_in_use")
    @patch("subprocess.Popen")
    def test_09_start_go_gateway_spawns_process_with_correct_args(self, mock_popen, mock_in_use, mock_ping):
        """Valida que start_go_gateway inicia o subprocesso com a flag de porta correta."""
        # Inicialmente nao responde, depois responde
        mock_ping.side_effect = [False, True]
        mock_in_use.return_value = True

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        mock_popen.return_value = fake_proc

        fake_exe = pathlib.Path("go_services/webworker_gateway.exe")
        proc = start_platform.start_go_gateway(8000, fake_exe, timeout_sec=2.0)

        self.assertIsNotNone(proc)
        mock_popen.assert_called_once()
        cmd_args = mock_popen.call_args[0][0]
        self.assertEqual(cmd_args[0], str(fake_exe))
        self.assertIn("--port=8000", cmd_args)

    @patch("start_platform.ping_go_gateway", return_value=False)
    @patch("start_platform.is_port_in_use", return_value=False)
    @patch("subprocess.Popen")
    def test_10_start_go_gateway_handles_immediate_crash(self, mock_popen, mock_in_use, mock_ping):
        """Valida deteccao de falha imediata caso o binario Go encerre com erro."""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 1
        fake_proc.returncode = 1
        fake_proc.stderr.read.return_value = b"bind: address already in use"
        mock_popen.return_value = fake_proc

        fake_exe = pathlib.Path("go_services/webworker_gateway.exe")
        stdout_trap = io.StringIO()
        with patch("sys.stdout", stdout_trap):
            proc = start_platform.start_go_gateway(8000, fake_exe, timeout_sec=1.0)

        self.assertIsNone(proc)
        self.assertIn("O processo Go encerrou imediatamente com codigo 1", stdout_trap.getvalue())

    # -------------------------------------------------------------------------
    # 7. REGRA DE PRIVACIDADE E CONTROLE: SEM ABERTURA AUTOMATICA DE NAVEGADOR
    # -------------------------------------------------------------------------
    def test_11_no_webbrowser_open_in_launcher(self):
        """Valida que o launcher NÃO chama webbrowser.open em nenhuma hipotese."""
        launcher_file = PROJECT_ROOT / "start_platform.py"
        content = launcher_file.read_text(encoding="utf-8")

        self.assertNotIn("webbrowser.open", content, "start_platform.py nao deve chamar webbrowser.open")
        self.assertNotIn("import webbrowser", content, "import webbrowser deve ser evitado no launcher")

    # -------------------------------------------------------------------------
    # 8. PADRAO VISUAL: SEM EMOJIS EM LOGS E PRINTS
    # -------------------------------------------------------------------------
    def test_12_no_emojis_in_launcher_code_and_messages(self):
        """Valida que o codigo de start_platform.py nao contem caracteres de emojis."""
        launcher_file = PROJECT_ROOT / "start_platform.py"
        content = launcher_file.read_text(encoding="utf-8")

        # Expressao regular para deteccao de caracteres Unicode comuns de emojis
        emoji_pattern = re.compile(
            "[\U0001F600-\U0001F64F"  # Emoticons
            "\U0001F300-\U0001F5FF"  # Simbolos e pictogramas
            "\U0001F680-\U0001F6FF"  # Transporte e mapas
            "\U0001F1E0-\U0001F1FF"  # Bandeiras
            "\U00002702-\U000027B0"  # Dingbats
            "\U000024C2-\U0001F251"
            "\U0001F900-\U0001F9FF"
            "\U0001FA70-\U0001FAFF"
            "]+", flags=re.UNICODE
        )

        matches = emoji_pattern.findall(content)
        self.assertEqual(len(matches), 0, f"Emojis detectados no codigo de start_platform.py: {matches}")

    # -------------------------------------------------------------------------
    # 9. INTEGRACAO FROTA MULTIAGENTE E DASHBOARD
    # -------------------------------------------------------------------------
    def test_13_worker_fleet_config_and_dashboard_instantiation(self):
        """Valida que a frota e instanciada com num_workers da configuracao e o dashboard e vinculado."""
        from src.multiagent.worker_fleet import MultiAgentWorkerFleet
        from src.dashboard.multiagent_dashboard import MultiAgentDashboardServer

        fleet = MultiAgentWorkerFleet(num_workers=3, go_gateway_url="http://127.0.0.1:8000")
        self.assertEqual(fleet.num_workers, 3)
        self.assertIn("worker_1", fleet.worker_states)
        self.assertIn("worker_2", fleet.worker_states)
        self.assertIn("worker_3", fleet.worker_states)
        self.assertIn("worker_p1_interactive", fleet.worker_states)

        dashboard = MultiAgentDashboardServer(port=8085, go_gateway_url="http://127.0.0.1:8000", fleet_ref=fleet)
        self.assertEqual(dashboard.port, 8085)
        self.assertEqual(dashboard.fleet_ref, fleet)

    # -------------------------------------------------------------------------
    # 10. PADRAO CWD: FORCA DIRETORIO RAIZ DO PROJETO
    # -------------------------------------------------------------------------
    def test_14_forces_cwd_to_project_root(self):
        """Valida que start_platform.py forca explicitamente o os.chdir para o diretorio do script."""
        launcher_file = PROJECT_ROOT / "start_platform.py"
        content = launcher_file.read_text(encoding="utf-8")

        self.assertIn("os.chdir(BASE_DIR)", content, "start_platform.py deve conter 'os.chdir(BASE_DIR)'")
        self.assertEqual(pathlib.Path(os.getcwd()).resolve(), PROJECT_ROOT.resolve())


if __name__ == "__main__":
    unittest.main()

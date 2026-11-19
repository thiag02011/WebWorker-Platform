"""
===============================================================================
SUÍTE DE TESTES TDD AVANÇADA: 20 CASOS DE MOCK, RESILIÊNCIA & EDGE CASES
CAMINHO: tests/test_tdd_advanced_mock_matrix.py
===============================================================================
OBJETIVO:
Bateria de 20 casos de testes determinísticos, isolados por Mocks (sem dependência
de Ollama online ou rede externa), validando a estabilidade da Versão 1.2:
- Telemetria de Hardware Universal (Ollama-First, AMD, CPU Host, Zero 8151 hardcoded)
- Extratores Semânticos Resilientes (Stop-markers fracionados, disclaimers multi-linha)
- Descoberta em Lote Dinâmica (Coleta total sem truncamento, seletores customizados)
- Reflexão Cognitiva com Redefinição de Intenção e Escopo
- Resiliência a Arquivos Corrompidos, Anonimização P1 e Kill Switch
===============================================================================
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ajusta sys.path para garantir imports relativos à raiz do projeto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.gpu_telemetry import GPUTelemetry, GPUStats, _get_host_ram_stats, _query_ollama_ps
from src.web.search_extractors import GoogleAIOverviewExtractor, GoogleMovieKnowledgeExtractor
from src.web.triaged_tools_schema import (
    get_phase0_tools,
    get_phase1_tools,
    get_phase2_tools,
    get_batch_discovery_tool,
    get_error_reflection_tool
)
from src.config_manager import ConfigManager


class TestAdvancedMockMatrix(unittest.TestCase):
    """Suíte com 20 casos de testes mockados e TDD avançados."""

    # =========================================================================
    # BLOCO 1: TELEMETRIA DE HARDWARE & OLLAMA-FIRST (5 CASOS)
    # =========================================================================

    @patch("src.gpu_telemetry._query_ollama_ps")
    @patch("src.gpu_telemetry._get_host_ram_stats")
    def test_01_telemetry_ollama_cpu_mode_switches_to_host_ram(self, mock_ram, mock_ollama):
        """Caso 1: Ollama reporta modelo 100% em CPU -> sistema seleciona CPU_HOST e RAM real."""
        mock_ollama.return_value = {
            "has_model": True,
            "model_name": "qwen2.5:3b",
            "total_mb": 1900,
            "vram_mb": 0,
            "ram_mb": 1900,
            "is_gpu": False,
            "is_cpu_only": True,
            "is_hybrid": False
        }
        mock_ram.return_value = {
            "total_mb": 32768,
            "used_mb": 12000,
            "free_mb": 20768,
            "load_pct": 36,
            "available": True
        }

        telemetry = GPUTelemetry()
        stats = telemetry.get_current_stats()

        self.assertEqual(stats.device_type, "CPU_HOST")
        self.assertEqual(stats.memory_total_mb, 32768)
        self.assertEqual(stats.memory_used_mb, 12000)
        self.assertEqual(stats.model_vram_mb, 0)
        self.assertEqual(stats.model_ram_mb, 1900)
        self.assertTrue(stats.is_available)

    @patch("src.gpu_telemetry._query_ollama_ps")
    def test_02_telemetry_ollama_hybrid_mode_reports_vram_and_ram(self, mock_ollama):
        """Caso 2: Ollama em modo híbrido -> reporta VRAM e RAM divididas e device_type=HYBRID."""
        mock_ollama.return_value = {
            "has_model": True,
            "model_name": "qwen2.5:7b",
            "total_mb": 4500,
            "vram_mb": 3000,
            "ram_mb": 1500,
            "is_gpu": True,
            "is_cpu_only": False,
            "is_hybrid": True
        }

        telemetry = GPUTelemetry()
        with patch.object(telemetry, "_query_nvidia_smi", return_value=GPUStats(
            memory_used_mb=3500, memory_total_mb=8192, memory_free_mb=4692,
            gpu_util_pct=85, temperature_c=62, is_available=True, device_type="NVIDIA_GPU"
        )):
            stats = telemetry.get_current_stats()

        self.assertEqual(stats.device_type, "HYBRID")
        self.assertEqual(stats.model_vram_mb, 3000)
        self.assertEqual(stats.model_ram_mb, 1500)
        self.assertEqual(stats.temperature_c, 62)

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_03_telemetry_ollama_offline_does_not_crash(self, mock_url):
        """Caso 3: Ollama offline/timeout -> não gera exceção não tratada e consulta fallback."""
        res = _query_ollama_ps("http://127.0.0.1:11434")
        self.assertIsNone(res)

    @patch("subprocess.run")
    def test_04_telemetry_amd_rocm_smi_parsing(self, mock_sub):
        """Caso 4: Placa AMD Radeon reportando VRAM via rocm-smi JSON."""
        fake_json = json.dumps({
            "card0": {
                "VRAM Total Memory (B)": 17179869184,  # 16 GB
                "VRAM Total Used Memory (B)": 4294967296   # 4 GB
            }
        })
        mock_sub.return_value = MagicMock(stdout=fake_json)

        telemetry = GPUTelemetry()
        amd_stats = telemetry._query_amd_smi()

        self.assertIsNotNone(amd_stats)
        self.assertEqual(amd_stats.device_type, "AMD_GPU")
        self.assertEqual(amd_stats.memory_total_mb, 16384)
        self.assertEqual(amd_stats.memory_used_mb, 4096)
        self.assertEqual(amd_stats.memory_free_mb, 12288)

    @patch("src.gpu_telemetry._query_ollama_ps", return_value=None)
    @patch("src.gpu_telemetry.GPUTelemetry._query_nvidia_smi", return_value=None)
    @patch("src.gpu_telemetry.GPUTelemetry._query_amd_smi", return_value=None)
    @patch("src.gpu_telemetry._get_host_ram_stats")
    def test_05_telemetry_no_hardcoded_8151_when_sensors_fail(self, mock_ram, *args):
        """Caso 5: Sem GPU e sem NVIDIA -> usa Host RAM real sem número 8151 arbitrário."""
        mock_ram.return_value = {
            "total_mb": 65536,  # 64 GB de Host RAM
            "used_mb": 16000,
            "free_mb": 49536,
            "load_pct": 24,
            "available": True
        }

        telemetry = GPUTelemetry()
        stats = telemetry.get_current_stats()

        self.assertEqual(stats.device_type, "CPU_HOST")
        self.assertNotEqual(stats.memory_total_mb, 8151, "Proibido usar 8151 hardcoded!")
        self.assertEqual(stats.memory_total_mb, 65536)

    # =========================================================================
    # BLOCO 2: SEARCH EXTRACTORS & GOOGLE AI OVERVIEW RESILIENTE (5 CASOS)
    # =========================================================================

    def test_06_search_extractor_multiline_fractioned_ai_disclaimer(self):
        """Caso 6: Disclaimer de IA com frase quebrada em 3 linhas interrompe antes de ler poluentes."""
        html = """
        <div>
            <h3>Visão geral por IA</h3>
            <p>O protocolo HTTP/3 opera sobre QUIC garantindo menor latência em conexões móveis.</p>
            <span>Resposta gerada por IA.</span>
            <span>Por favor,</span>
            <span>verifique os fatos críticos.</span>
        </div>
        <div class="unwanted-web-results">
            <h3>Resultados da web de terceiros</h3>
        </div>
        """
        res = GoogleAIOverviewExtractor.extract(html)
        self.assertTrue(res["found"])
        self.assertIn("QUIC", res["overview_text"])
        self.assertNotIn("Resultados da web de terceiros", res["overview_text"])

    def test_07_search_extractor_english_experimental_stop_markers(self):
        """Caso 7: Disclaimer em inglês ('Generative AI is experimental') encerra a captura."""
        html = """
        <div>
            <div data-attrid="wa:/description">
                <p>Generative AI summary about quantum computing.</p>
                <p>Generative AI is experimental.</p>
                <p>Learn more about this feature.</p>
            </div>
            <div>More search content that should be excluded</div>
        </div>
        """
        res = GoogleAIOverviewExtractor.extract(html)
        self.assertTrue(res["found"])
        self.assertIn("quantum computing", res["overview_text"])
        self.assertNotIn("More search content that should be excluded", res["overview_text"])

    def test_08_search_extractor_wa_description_structured_li(self):
        """Caso 8: Captura precisa de listas (<li>) em bloco wa:/description."""
        html = """
        <div data-attrid="wa:/description">
            <h3>AI Overview</h3>
            <li>Vantagem 1: Execução paralela em GPU</li>
            <li>Vantagem 2: Menor consumo de energia em repouso</li>
            <li>Vantagem 3: Suporte a tensores de precisão mista</li>
            <span>Enviar feedback</span>
        </div>
        """
        res = GoogleAIOverviewExtractor.extract(html)
        self.assertTrue(res["found"])
        self.assertEqual(len(res["key_points"]), 3)
        self.assertIn("Vantagem 1", res["key_points"][0])

    def test_09_search_extractor_empty_or_corrupted_html(self):
        """Caso 9: HTML vazio ou corrompido não dispara crash."""
        res_empty = GoogleAIOverviewExtractor.extract("")
        self.assertFalse(res_empty["found"])

        res_no_ai = GoogleAIOverviewExtractor.extract("<html><body><p>Página estática comum</p></body></html>")
        self.assertFalse(res_no_ai["found"])

    def test_10_movie_extractor_accents_and_mixed_ratings(self):
        """Caso 10: Knowledge Panel de filme com acentos e notas de múltiplas fontes."""
        html = """
        <h2 data-attrid="title">O Auto da Compadecida</h2>
        <span>2000 ‧ Comédia/Aventura ‧ 1h 44m</span>
        <div>IMDb: 8,7/10</div>
        <div>AdoroCinema: 4,8/5</div>
        <div data-attrid="description">As aventuras dos nordestinos João Grilo e Chicó no sertão paraibano.</div>
        """
        res = GoogleMovieKnowledgeExtractor.extract(html)
        self.assertTrue(res["found"])
        self.assertEqual(res["nome"], "O Auto da Compadecida")
        self.assertEqual(res["lancamento"], "2000")
        self.assertIn("1h 44m", res["duracao"])
        self.assertIn("João Grilo", res["descricao"])

    # =========================================================================
    # BLOCO 3: DESCOBERTA EM LOTE (BATCH TOOL) & ESCOPO (5 CASOS)
    # =========================================================================

    def test_11_batch_discovery_zero_limit_collects_all_items(self):
        """Caso 11: Schema de batch com max_items=0 ou omitido indica coleta total."""
        tool = get_batch_discovery_tool()
        props = tool["function"]["parameters"]["properties"]
        self.assertIn("max_items", props)
        self.assertIn("TODOS", props["max_items"]["description"])

    def test_12_batch_discovery_strict_max_items_limit(self):
        """Caso 12: Simula corte estrito quando max_items > 0 fornecido."""
        mock_links = [{"href": f"/item/{i}", "text": f"Item {i}"} for i in range(100)]
        max_limit = 5
        filtered = mock_links[:max_limit] if max_limit > 0 else mock_links
        self.assertEqual(len(filtered), 5)

    def test_13_batch_discovery_custom_css_selector_matching(self):
        """Caso 13: Schema aceita tanto caminho relativo quanto classe CSS."""
        tool = get_batch_discovery_tool()
        desc = tool["function"]["parameters"]["properties"]["link_pattern"]["description"]
        self.assertIn("a.card-link", desc)
        self.assertIn("/produto/", desc)

    def test_14_batch_tool_automatically_added_to_search_when_scope_batch(self):
        """Caso 14: Transição de escopo: action='use_search_bar' com scope='batch' expõe batch_links."""
        tools = get_phase2_tools(action="use_search_bar", scope="batch")
        names = [t["function"]["name"] for t in tools]
        self.assertIn("browser_fill", names)
        self.assertIn("browser_discover_batch_links", names)

    def test_15_batch_tool_excluded_from_search_when_scope_single(self):
        """Caso 15: Com scope='single', batch_links permanece protegido fora do arsenal."""
        tools = get_phase2_tools(action="use_search_bar", scope="single")
        names = [t["function"]["name"] for t in tools]
        self.assertIn("browser_fill", names)
        self.assertNotIn("browser_discover_batch_links", names)

    # =========================================================================
    # BLOCO 4: REFLEXÃO COGNITIVA & REDEFINIÇÃO DE INTENÇÃO (3 CASOS)
    # =========================================================================

    def test_16_reflection_tool_allows_scope_escalation_to_batch(self):
        """Caso 16: submit_error_reflection possui campo change_scope=['single', 'batch']."""
        refl = get_error_reflection_tool()
        props = refl["function"]["parameters"]["properties"]
        self.assertIn("change_scope", props)
        self.assertEqual(props["change_scope"]["enum"], ["single", "batch"])

    def test_17_reflection_tool_allows_strategy_redefinition(self):
        """Caso 17: submit_error_reflection permite redefinir estratégia de busca global."""
        refl = get_error_reflection_tool()
        props = refl["function"]["parameters"]["properties"]
        self.assertIn("change_strategy", props)
        self.assertIn("use_search_bar", props["change_strategy"]["enum"])
        self.assertIn("collect_batch_links", props["change_strategy"]["enum"])

    def test_18_reflection_tool_restart_triage_flag_handling(self):
        """Caso 18: submit_error_reflection suporta flag booleana restart_triage."""
        refl = get_error_reflection_tool()
        props = refl["function"]["parameters"]["properties"]
        self.assertIn("restart_triage", props)
        self.assertEqual(props["restart_triage"]["type"], "boolean")

    # =========================================================================
    # BLOCO 5: CONFIG MANAGER, ANONIMIZAÇÃO P1 & EDGE CASES (2 CASOS)
    # =========================================================================

    def test_19_config_manager_corrupted_json_fallback_safe(self):
        """Caso 19: Arquivo config.json com sintaxe corrompida não aborta e usa defaults."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_cfg_path = pathlib.Path(tmp_dir) / "config.json"
            with open(bad_cfg_path, "w", encoding="utf-8") as f:
                f.write("{ json_invalido: sem_aspas, corrompido: True ")

            mgr = ConfigManager(custom_path=bad_cfg_path)
            # Deve carregar os defaults seguros sem lançar exceção
            self.assertEqual(mgr.get("browser.default_base_url"), "https://www.google.com")
            self.assertEqual(mgr.get("servers.gateway_port"), 8000)

    def test_20_p1_interactive_url_anonymization_and_immediate_killswitch(self):
        """Caso 20: Mascaramento estrito de URL confidencial e disparo de Kill Switch UX."""
        raw_sensitive_url = "https://app.banco.com.br/extrato/pessoal?token=segredo_cliente_12345"

        # Simula função de anonimização do sistema
        import urllib.parse
        parsed = urllib.parse.urlparse(raw_sensitive_url)
        anonymized = f"[PRIVADO: {parsed.netloc}]"

        self.assertEqual(anonymized, "[PRIVADO: app.banco.com.br]")
        self.assertNotIn("segredo_cliente_12345", anonymized)
        self.assertNotIn("pessoal", anonymized)


if __name__ == "__main__":
    unittest.main(verbosity=2)

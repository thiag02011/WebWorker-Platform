"""
===============================================================================
SUITE DE TESTES TDD: AUTO-CURA DE CONFIGURACOES (test_tdd_config_auto_cure.py)
CAMINHO: tests/test_tdd_config_auto_cure.py
===============================================================================
OBJETIVO:
Bateria de testes TDD para validar o comportamento de Auto-Cura (Auto-Sanitize)
do ConfigManager quando o usuario insere valores invalidos ou absurdos:
1. Portas negativas, strings ou fora do range TCP (1024-65535)
2. Conflito de portas entre Gateway e Dashboard
3. Quantidade absurda ou negativa de workers na frota
4. Timeouts negativos ou excessivamente baixos
5. URLs invalidas (ausencia de http/https)
6. Motores de banco desconhecidos (ex: oracle, redis)
7. Versao nula ou invalida
8. Persistencia em disco da correcao automatica
9. Preservacao integral de configuracoes validas
10. Ausencia de emojis nas mensagens de log/aviso
===============================================================================
"""

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest

# Garante path correto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_manager import ConfigManager, DEFAULT_CONFIG


class TestTDDConfigAutoCure(unittest.TestCase):
    """Suite TDD para auto-cura e sanitizacao semantica de configuracoes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg_file = pathlib.Path(self.temp_dir.name) / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_and_load(self, bad_data: dict) -> ConfigManager:
        self.cfg_file.write_text(json.dumps(bad_data, indent=2, ensure_ascii=False), encoding="utf-8")
        return ConfigManager(custom_path=self.cfg_file)

    # -------------------------------------------------------------------------
    # 1. AUTO-CURA DE PORTAS
    # -------------------------------------------------------------------------
    def test_01_auto_cure_negative_and_string_ports(self):
        """Valida que portas negativas ou strings sao auto-corrigidas para os defaults."""
        bad_config = {
            "servers": {
                "gateway_port": -5000,
                "dashboard_port": "abacaxi"
            },
            "database": {
                "port": 999999
            }
        }
        cfg = self._create_and_load(bad_config)

        self.assertEqual(cfg.get("servers.gateway_port"), DEFAULT_CONFIG["servers"]["gateway_port"])
        self.assertEqual(cfg.get("servers.dashboard_port"), DEFAULT_CONFIG["servers"]["dashboard_port"])
        self.assertEqual(cfg.get("database.port"), DEFAULT_CONFIG["database"]["port"])

    def test_02_auto_cure_port_collision_between_gateway_and_dashboard(self):
        """Valida que se o usuario configurar a mesma porta para gateway e dashboard, uma delas e ajustada."""
        bad_config = {
            "servers": {
                "gateway_port": 8000,
                "dashboard_port": 8000
            }
        }
        cfg = self._create_and_load(bad_config)

        gw = cfg.get("servers.gateway_port")
        dash = cfg.get("servers.dashboard_port")
        self.assertNotEqual(gw, dash, "Gateway e Dashboard nunca devem ter a mesma porta apos auto-cura.")
        self.assertEqual(gw, 8000)
        self.assertEqual(dash, 8085)

    # -------------------------------------------------------------------------
    # 2. AUTO-CURA DE WORKERS DA FROTA
    # -------------------------------------------------------------------------
    def test_03_auto_cure_nonsensical_workers_count(self):
        """Valida que workers <= 0, > 32 ou strings sao auto-corrigidos para o default."""
        bad_configs = [
            {"fleet": {"num_workers": -10}},
            {"fleet": {"num_workers": 1000}},
            {"fleet": {"num_workers": "muitos_workers"}},
            {"fleet": {"num_workers": 0}},
        ]
        for bad in bad_configs:
            cfg = self._create_and_load(bad)
            self.assertEqual(
                cfg.get("fleet.num_workers"),
                DEFAULT_CONFIG["fleet"]["num_workers"],
                f"Falha ao auto-corrigir frota para entrada: {bad}"
            )

    # -------------------------------------------------------------------------
    # 3. AUTO-CURA DE TIMEOUT DO NAVEGADOR
    # -------------------------------------------------------------------------
    def test_04_auto_cure_invalid_timeout(self):
        """Valida que timeout < 1000ms ou negativo e auto-corrigido para o default."""
        bad_config = {"browser": {"timeout_ms": -50}}
        cfg = self._create_and_load(bad_config)
        self.assertEqual(cfg.get("browser.timeout_ms"), DEFAULT_CONFIG["browser"]["timeout_ms"])

        bad_string_timeout = {"browser": {"timeout_ms": "cinco_segundos"}}
        cfg2 = self._create_and_load(bad_string_timeout)
        self.assertEqual(cfg2.get("browser.timeout_ms"), DEFAULT_CONFIG["browser"]["timeout_ms"])

    # -------------------------------------------------------------------------
    # 4. AUTO-CURA DE URL BASE DO NAVEGADOR
    # -------------------------------------------------------------------------
    def test_05_auto_cure_invalid_base_url(self):
        """Valida que URLs sem http/https ou formatos anomalos sao revertidas para o default seguro."""
        bad_urls = [
            {"browser": {"default_base_url": "ftp://servidor-invalido"}},
            {"browser": {"default_base_url": "isso_nao_e_uma_url"}},
            {"browser": {"default_base_url": ""}},
            {"browser": {"default_base_url": 12345}},
        ]
        for bad in bad_urls:
            cfg = self._create_and_load(bad)
            self.assertEqual(
                cfg.get("browser.default_base_url"),
                DEFAULT_CONFIG["browser"]["default_base_url"]
            )

    # -------------------------------------------------------------------------
    # 5. AUTO-CURA DE MOTOR DE BANCO DE DADOS
    # -------------------------------------------------------------------------
    def test_06_auto_cure_unknown_database_engine(self):
        """Valida que motores nao suportados (ex: oracle, redis) sao revertidos para o default."""
        bad_config = {"database": {"engine": "oracle_enterprise"}}
        cfg = self._create_and_load(bad_config)
        self.assertEqual(cfg.get("database.engine"), DEFAULT_CONFIG["database"]["engine"])

    # -------------------------------------------------------------------------
    # 6. AUTO-CURA DE VERSAO
    # -------------------------------------------------------------------------
    def test_07_auto_cure_invalid_version(self):
        """Valida que versao nula, vazia ou nao-string e auto-corrigida para o default."""
        bad_config = {"version": ""}
        cfg = self._create_and_load(bad_config)
        self.assertEqual(cfg.get("version"), DEFAULT_CONFIG["version"])

        bad_config2 = {"version": 999}
        cfg2 = self._create_and_load(bad_config2)
        self.assertEqual(cfg2.get("version"), DEFAULT_CONFIG["version"])

    # -------------------------------------------------------------------------
    # 7. PERSISTENCIA DA AUTO-CURA EM DISCO
    # -------------------------------------------------------------------------
    def test_08_auto_cure_persists_repaired_json_to_disk(self):
        """Valida que o arquivo em disco e imediatamente sobregravado com a versao corrigida."""
        bad_config = {
            "servers": {"gateway_port": -999},
            "fleet": {"num_workers": "zero"}
        }
        cfg = self._create_and_load(bad_config)

        # Le o arquivo diretamente do disco para checar a gravacao
        saved_disk_content = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_disk_content["servers"]["gateway_port"], DEFAULT_CONFIG["servers"]["gateway_port"])
        self.assertEqual(saved_disk_content["fleet"]["num_workers"], DEFAULT_CONFIG["fleet"]["num_workers"])

    # -------------------------------------------------------------------------
    # 8. PRESERVACAO DE CONFIGURACOES PERSONALIZADAS VALIDAS
    # -------------------------------------------------------------------------
    def test_09_valid_custom_values_are_preserved(self):
        """Valida que configuracoes validas fornecidas pelo usuario NUNCA sao modificadas."""
        custom_valid = {
            "version": "2.5-pro",
            "servers": {
                "gateway_port": 9000,
                "dashboard_port": 9090
            },
            "fleet": {
                "num_workers": 6
            },
            "browser": {
                "default_base_url": "https://www.bing.com",
                "timeout_ms": 35000
            },
            "database": {
                "engine": "sqlite"
            }
        }
        cfg = self._create_and_load(custom_valid)

        self.assertEqual(cfg.get("version"), "2.5-pro")
        self.assertEqual(cfg.get("servers.gateway_port"), 9000)
        self.assertEqual(cfg.get("servers.dashboard_port"), 9090)
        self.assertEqual(cfg.get("fleet.num_workers"), 6)
        self.assertEqual(cfg.get("browser.default_base_url"), "https://www.bing.com")
        self.assertEqual(cfg.get("browser.timeout_ms"), 35000)
        self.assertEqual(cfg.get("database.engine"), "sqlite")

    # -------------------------------------------------------------------------
    # 9. AUSENCIA DE EMOJIS NAS MENSAGENS DE AVISO
    # -------------------------------------------------------------------------
    def test_10_no_emojis_in_warning_messages(self):
        """Valida que todas as mensagens emitidas pela auto-cura sao livres de emojis."""
        bad_config = {
            "servers": {"gateway_port": -10},
            "fleet": {"num_workers": "invalido"},
            "browser": {"default_base_url": "invalido"}
        }
        self.cfg_file.write_text(json.dumps(bad_config), encoding="utf-8")
        cfg = ConfigManager(custom_path=self.cfg_file)
        warnings = cfg.validate_and_sanitize()

        emoji_pattern = re.compile(
            "[\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF"
            "\U00002702-\U000027B0"
            "]+", flags=re.UNICODE
        )

        for w in warnings:
            self.assertTrue(w.startswith("[AVISO]"))
            matches = emoji_pattern.findall(w)
            self.assertEqual(len(matches), 0, f"Emoji detectado no aviso de auto-cura: {w}")


if __name__ == "__main__":
    unittest.main()

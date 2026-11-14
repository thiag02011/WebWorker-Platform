"""
===============================================================================
SUITE DE TESTES TDD: EXTENSAO CHROME MV3 & FAIXA FIXA HUD (test_tdd_chrome_extension_hud.py)
CAMINHO: tests/test_tdd_chrome_extension_hud.py
===============================================================================
OBJETIVO:
Garantir a integridade da arquitetura da Extensao Chrome Manifest V3:
1. Validar manifest.json (Manifest V3, Service Worker background.js, permissoes).
2. Validar que background.js gerencia ciclo de vida, polling e notificacoes de aba.
3. Validar que content.js implementa Shadow DOM, Faixa Fixa HUD, som pop e Kill Switch [X].
4. Validar que popup.js delega tarefas ao background e permite fechamento imediato.
===============================================================================
"""

import json
import pathlib
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestTDDChromeExtensionHUD(unittest.TestCase):
    """Testes TDD para a arquitetura da Extensao Chrome e Faixa Fixa de Topo (HUD)."""

    def setUp(self):
        self.ext_dir = PROJECT_ROOT / "chrome_extension"

    def test_01_manifest_json_structure_and_service_worker(self):
        """Valida que o manifest.json esta no padrao MV3 com Service Worker background.js."""
        manifest_path = self.ext_dir / "manifest.json"
        self.assertTrue(manifest_path.exists(), "manifest.json deve existir.")

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest.get("manifest_version"), 3)
        self.assertEqual(manifest.get("background", {}).get("service_worker"), "background.js")
        self.assertIn("tabs", manifest.get("permissions", []))
        self.assertIn("activeTab", manifest.get("permissions", []))
        self.assertIn("storage", manifest.get("permissions", []))

        # Garante que todos os arquivos referenciados existem
        self.assertTrue((self.ext_dir / "background.js").exists())
        self.assertTrue((self.ext_dir / "content.js").exists())
        self.assertTrue((self.ext_dir / "popup.html").exists())
        self.assertTrue((self.ext_dir / "popup.js").exists())

    def test_02_background_service_worker_contract(self):
        """Valida que background.js contem a maquina de estados e os canais de comunicacao."""
        bg_path = self.ext_dir / "background.js"
        content = bg_path.read_text(encoding="utf-8")

        # Deve gerenciar START_TASK e ABORT_TASK
        self.assertIn('request.action === "START_TASK"', content)
        self.assertIn('request.action === "ABORT_TASK"', content)

        # Deve disparar eventos de HUD para a aba alvo
        self.assertIn('"HUD_SHOW"', content)
        self.assertIn('"HUD_UPDATE"', content)
        self.assertIn('"HUD_COMPLETE"', content)
        self.assertIn('"HUD_ABORTED"', content)

        # Deve implementar polling com status de LLM
        self.assertIn("Processando LLM", content)
        self.assertIn("/api/task/status", content)

    def test_03_content_script_shadow_dom_and_kill_switch(self):
        """Valida que content.js encapsula a faixa em Shadow DOM e implementa [X] como Kill Switch."""
        cs_path = self.ext_dir / "content.js"
        content = cs_path.read_text(encoding="utf-8")

        # Isolamento com Shadow DOM
        self.assertIn("attachShadow({ mode: \"closed\" })", content)
        self.assertIn("webworker-hud-host", content)

        # Elementos da Faixa
        self.assertIn("ww-hud-bar", content)
        self.assertIn("wwPromptText", content)
        self.assertIn("wwStatusBadge", content)
        self.assertIn("wwResponseBox", content)
        self.assertIn("wwCopyBtn", content)
        self.assertIn("wwCloseBtn", content)

        # Kill Switch no botao [X]
        self.assertIn("handleCloseAndAbort", content)
        self.assertIn('"ABORT_TASK"', content)

        # Audio pop e cancelamento sintetizados
        self.assertIn("playWebWorkerPopSound", content)
        self.assertIn("playWebWorkerCancelSound", content)

    def test_04_popup_delegation_to_background(self):
        """Valida que popup.js despacha a tarefa para background.js sem bloquear a janela."""
        popup_js_path = self.ext_dir / "popup.js"
        content = popup_js_path.read_text(encoding="utf-8")

        self.assertIn("chrome.runtime.sendMessage", content)
        self.assertIn('"START_TASK"', content)
        self.assertIn("tab_id", content)

    def test_05_content_script_dom_inspector_and_action_execution(self):
        """Valida que content.js contem o Inspetor de DOM e o motor de acoes in-page."""
        cs_path = self.ext_dir / "content.js"
        content = cs_path.read_text(encoding="utf-8")

        # Elementos do Inspetor de DOM no banner
        self.assertIn("wwInspectBtn", content)
        self.assertIn("wwHighlightBtn", content)
        self.assertIn("wwDomDrawer", content)
        self.assertIn("wwDrawerContent", content)
        self.assertIn("highlightAllMappedElements", content)

        # Motor de execucao de acoes na pagina
        self.assertIn("executeInPageAction", content)
        self.assertIn("flashElementHighlight", content)
        self.assertIn("actionPlan", content)
        self.assertIn("domTelemetry", content)
        self.assertIn("captureCleanBodyHTML", content)
        self.assertIn("captureVisibleViewportText", content)


if __name__ == "__main__":
    unittest.main()

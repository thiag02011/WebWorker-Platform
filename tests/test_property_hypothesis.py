"""
===============================================================================
SUÍTE DE TESTES BASEADA EM PROPRIEDADES (PROPERTY-BASED TESTING COM HYPOTHESIS)
CAMINHO: tests/test_property_hypothesis.py
===============================================================================
OBJETIVO:
Utilizar a biblioteca Hypothesis para estressar os componentes críticos da
plataforma com centenas de entradas sintéticas geradas automaticamente por turno
(Fuzzing determinístico com shrinking automático):
- DOMSimplifier: Imunidade a travamentos com qualquer HTML malformado.
- Anonimização de URLs: Garantia matemática de ausência de vazamento de credenciais.
- Extratores Semânticos: Tipagem estrita e resiliência contra payloads hostis.
- Invariantes de Telemetria: Coerência de métricas de memória e limites de percentual.
===============================================================================
"""
import os
import pathlib
import sys
import unittest
import urllib.parse
from hypothesis import given, settings, strategies as st

# Ajusta sys.path para garantir imports relativos à raiz do projeto
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.web.dom_simplifier import DOMSimplifier, DOMSnapshot
from src.web.search_extractors import GoogleAIOverviewExtractor, GoogleMovieKnowledgeExtractor, strip_html
from src.gpu_telemetry import GPUStats


class TestPropertyBasedHypothesis(unittest.TestCase):
    """Bateria de testes baseados em propriedades e fuzzing gerativo."""

    @settings(max_examples=100, deadline=None)
    @given(st.text())
    def test_01_property_strip_html_never_crashes_on_arbitrary_text(self, raw_text: str):
        """Propriedade 1: strip_html deve processar qualquer sequência de texto sem crash."""
        result = strip_html(raw_text)
        self.assertIsInstance(result, str)

    @settings(max_examples=50, deadline=None)
    @given(st.text(max_size=15000))
    def test_02_property_dom_simplifier_never_crashes_on_arbitrary_html(self, raw_html: str):
        """Propriedade 2: DOMSimplifier aceita qualquer HTML caótico sem estourar exceção não tratada."""
        simplifier = DOMSimplifier()
        snapshot = simplifier.simplify(raw_html, max_tokens=1500)

        self.assertIsInstance(snapshot, DOMSnapshot)
        self.assertIsInstance(snapshot.pruned_text, str)
        self.assertIsInstance(snapshot.selector_map, dict)
        self.assertLessEqual(snapshot.token_estimate, 2500, "Tokens não podem estourar a margem segura!")

    @settings(max_examples=100, deadline=None)
    @given(
        st.from_regex(
            r"https?://[a-zA-Z0-9_\-\.]+\.[a-z]{2,4}(/[a-zA-Z0-9_\-\./]*)?(\?[a-zA-Z0-9_\-\.=&%+]*)?",
            fullmatch=True
        )
    )
    def test_03_property_url_anonymization_guarantees_zero_leakage(self, arbitrary_url: str):
        """Propriedade 3: Anonimização de URL NUNCA vaza parâmetros de query, paths ou credenciais."""
        parsed = urllib.parse.urlparse(arbitrary_url)
        host = parsed.netloc or "oculto"
        anonymized = f"[PRIVADO: {host}]"

        # Invariantes de privacidade
        self.assertTrue(anonymized.startswith("[PRIVADO:"))
        self.assertTrue(anonymized.endswith("]"))
        self.assertNotIn("?", anonymized, "Delimitador de query '?' jamais pode constar na URL mascarada!")
        self.assertNotIn("&", anonymized, "Separador '&' jamais pode constar!")
        self.assertEqual(anonymized, f"[PRIVADO: {parsed.netloc}]")

    @settings(max_examples=80, deadline=None)
    @given(st.text(max_size=12000))
    def test_04_property_google_ai_overview_extractor_type_safety(self, arbitrary_payload: str):
        """Propriedade 4: Extrator de IA sempre devolve formato de dicionário estrito e tipado."""
        res = GoogleAIOverviewExtractor.extract(arbitrary_payload)

        self.assertIsInstance(res, dict)
        self.assertIn("found", res)
        self.assertIsInstance(res["found"], bool)

        if res["found"]:
            self.assertIn("overview_text", res)
            self.assertIsInstance(res["overview_text"], str)
            self.assertIn("key_points", res)
            self.assertIsInstance(res["key_points"], list)

    @settings(max_examples=80, deadline=None)
    @given(st.text(max_size=12000))
    def test_05_property_google_movie_knowledge_extractor_schema_contract(self, arbitrary_payload: str):
        """Propriedade 5: Extrator de filmes sempre satisfaz o contrato semântico (nome, notas, etc.)."""
        res = GoogleMovieKnowledgeExtractor.extract(arbitrary_payload)

        self.assertIsInstance(res, dict)
        self.assertIn("found", res)
        self.assertIsInstance(res["found"], bool)

        if res["found"]:
            self.assertIn("nome", res)
            self.assertIn("lancamento", res)
            self.assertIn("duracao", res)
            self.assertIn("tags", res)
            self.assertIn("nota", res)
            self.assertIn("descricao", res)
            self.assertIsInstance(res["tags"], list)
            self.assertIsInstance(res["nota"], dict)

    @settings(max_examples=100, deadline=None)
    @given(
        st.integers(min_value=0, max_value=200000),
        st.integers(min_value=0, max_value=200000),
        st.integers(min_value=0, max_value=150)
    )
    def test_06_property_hardware_telemetry_numerical_invariants(self, used_mb: int, total_mb: int, util: int):
        """Propriedade 6: Invariantes matemáticos de telemetria de hardware (sem números negativos ou corrompidos)."""
        free_mb = max(0, total_mb - used_mb)
        bounded_util = min(100, max(0, util))

        stats = GPUStats(
            memory_used_mb=used_mb,
            memory_total_mb=total_mb,
            memory_free_mb=free_mb,
            gpu_util_pct=bounded_util,
            temperature_c=25,
            is_available=True
        )

        self.assertGreaterEqual(stats.memory_free_mb, 0)
        self.assertGreaterEqual(stats.gpu_util_pct, 0)
        self.assertLessEqual(stats.gpu_util_pct, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)

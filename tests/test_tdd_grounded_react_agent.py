"""
===============================================================================
SUITE TDD: AGENTE WEB REACT ATERRADO
CAMINHO: tests/test_tdd_grounded_react_agent.py
===============================================================================
Regressoes cobertas (casos reais observados em minecraft.wiki/w/Scaffolding#ID):
- G-01: Pagina longa sem barra de busca: o agente NAO pode tentar browser_fill;
        observacao informa explicitamente que nao ha campos de texto.
- G-02: Ancora #ID posiciona a janela de leitura na secao, mesmo apos 10k chars.
- G-03: browser_find_in_page localiza dados alem da janela e permite responder.
- G-04: Acao identica repetida e rejeitada.
- G-05: Ultimo turno oferece apenas ferramentas de encerramento.
- G-06: Limite de turnos sem resposta gera erro explicito (nao "Cancelado").
- G-07: Clique aterrado em @id valido chega ao navegador.
===============================================================================
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.dom_simplifier import DOMSimplifier
from src.web.triaged_web_agent import TriagedWebAgent, PageView
from tests.mocks.mock_llm_service import MockOllamaManager

FILLER = "".join(f"<p>Paragrafo de historia numero {i} sobre andaimes e construcao.</p>" for i in range(400))
WIKI_HTML = (
    "<html><head><title>Scaffolding - Minecraft Wiki</title></head><body>"
    "<h1>Scaffolding</h1><p>Scaffolding is a climbable block.</p>"
    "<a href='/w/Bamboo'>Bamboo</a><a href='/w/Crafting'>Crafting</a>"
    f"{FILLER}"
    "<h2>Data values</h2><h3>ID</h3>"
    "<table><tr><th>Name</th><th>Identifier</th><th>Numeric ID</th></tr>"
    "<tr><td>Scaffolding</td><td>scaffolding</td><td>-165</td></tr></table>"
    "</body></html>"
)
URL = "https://minecraft.wiki/w/Scaffolding#ID"


class _WikiEngine:
    def __init__(self, html=WIKI_HTML, url=URL):
        self.html, self.url = html, url
        self.calls = {"fill": 0, "click": []}

    def start(self): pass
    def stop(self): pass
    def get_url(self): return self.url
    def navigate(self, url):
        return {"title": "Scaffolding - Minecraft Wiki", "dom_snapshot": DOMSimplifier().simplify(self.html)}
    def _get_current_snapshot(self):
        return DOMSimplifier().simplify(self.html)
    def fill(self, sel, val, press_enter=False):
        self.calls["fill"] += 1
        return {"success": True}
    def click(self, sel):
        self.calls["click"].append(sel)
        return {"success": True}
    def go_back(self):
        return {"success": True}


def _agent(max_turns=4, max_find_per_page=None):
    agent = TriagedWebAgent(max_turns=max_turns, max_find_per_page=max_find_per_page)
    agent.ollama = MockOllamaManager()
    agent.engine = _WikiEngine()
    return agent


class TestGroundedReactAgent(unittest.TestCase):

    def test_g01_no_search_bar_blocks_fill(self):
        agent = _agent()
        agent.ollama.queue_response("browser_fill", {"selector": "searchInput", "value": "ID"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "ID Numerico: -165"})
        res = agent.run_task(URL, "qual o id do item em numero?")
        self.assertTrue(res["success"])
        self.assertEqual(agent.engine.calls["fill"], 0)
        first_obs = agent.ollama.call_history[0]["messages"][1]["content"]
        self.assertIn("CAMPOS DE TEXTO: nenhum", first_obs)

    def test_g02_anchor_positions_reading_window(self):
        snap = DOMSimplifier().simplify(WIKI_HTML)
        self.assertGreater(len(snap.full_text), 10000)
        view = PageView(snap, URL, "Scaffolding")
        self.assertIn("-165", view.window())
        self.assertGreater(view.offset, 0)

    def test_g03_find_in_page_beyond_window(self):
        snap = DOMSimplifier().simplify(WIKI_HTML)
        view = PageView(snap, "https://minecraft.wiki/w/Scaffolding", "Scaffolding")
        self.assertEqual(view.offset, 0)
        self.assertNotIn("-165", view.window())
        snippets, best = view.find("Numeric ID")
        self.assertTrue(snippets)
        self.assertIn("-165", snippets[0])
        self.assertIsNotNone(best)

    def test_g03b_find_then_answer_flow(self):
        agent = _agent()
        agent.engine.url = "https://minecraft.wiki/w/Scaffolding"
        agent.ollama.queue_response("browser_find_in_page", {"query": "Numeric ID"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165", "evidence": "Numeric ID -165"})
        res = agent.run_task("https://minecraft.wiki/w/Scaffolding", "qual o id numerico?")
        self.assertTrue(res["success"])
        second_obs = agent.ollama.call_history[1]["messages"][1]["content"]
        self.assertIn("RESULTADO DA ULTIMA ACAO", second_obs)
        self.assertIn("-165", second_obs)

    def test_g04_identical_action_rejected(self):
        agent = _agent(max_turns=5)
        agent.ollama.queue_response("browser_read_more", {"direction": "up"})
        agent.ollama.queue_response("browser_read_more", {"direction": "up"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(URL, "id?")
        self.assertIn("identica", res["turns"][1]["result"]["message"])
        self.assertTrue(res["success"])

    def test_g05_last_turn_only_offers_closing_tools(self):
        agent = _agent(max_turns=2)
        agent.ollama.queue_response("browser_read_more", {"direction": "down"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        agent.run_task(URL, "id?")
        last_tools = {t["function"]["name"] for t in agent.ollama.call_history[-1]["tools"]}
        self.assertEqual(last_tools, {"browser_extract_data", "browser_report_failure"})

    def test_g06_turn_limit_reports_explicit_error(self):
        agent = _agent(max_turns=1)
        agent.ollama.queue_text_response("nao sei")
        agent.ollama.queue_text_response("nao sei")
        agent.ollama.queue_text_response("nao sei")
        res = agent.run_task(URL, "id?")
        self.assertFalse(res["success"])
        self.assertIn("turnos", res["error"])

    def test_g07_grounded_click_reaches_browser(self):
        agent = _agent()
        snap = DOMSimplifier().simplify(WIKI_HTML)
        link_id = next(k for k, v in snap.selector_map.items() if "Crafting" in v)
        agent.ollama.queue_response("browser_click", {"selector": f"[link {link_id}]"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "ok"})
        res = agent.run_task(URL, "abrir crafting")
        self.assertEqual(agent.engine.calls["click"], [link_id])
        self.assertTrue(res["success"])

    def test_g08_whole_word_find_ignores_substrings(self):
        noise = "<p>solid hidden video guide width Identifier</p>" * 300
        html_only_substrings = f"<html><body>{noise}</body></html>"
        view_empty = PageView(DOMSimplifier().simplify(html_only_substrings), URL, "Noise")
        snippets_none, _ = view_empty.find("ID")
        self.assertEqual(snippets_none, [], "Substrings como 'solid', 'hidden' ou 'Identifier' devem ser ignoradas ao buscar 'ID'")

        html_with_exact = f"<html><body>{noise}<h3>ID</h3><p>Numeric ID: -165</p></body></html>"
        view_exact = PageView(DOMSimplifier().simplify(html_with_exact), URL, "Exact")
        snippets_found, _ = view_exact.find("ID")
        self.assertTrue(snippets_found)
        self.assertIn("-165", snippets_found[0])

    def test_g09_ping_pong_find_queries_rejected(self):
        agent = _agent(max_turns=5, max_find_per_page=3)
        agent.ollama.queue_response("browser_find_in_page", {"query": "ID"})
        agent.ollama.queue_response("browser_find_in_page", {"query": "Numeric ID"})
        agent.ollama.queue_response("browser_find_in_page", {"query": "ID"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(URL, "qual o id do item?")
        self.assertFalse(res["turns"][2]["result"]["ok"])
        self.assertIn("Busca repetida rejeitada", res["turns"][2]["result"]["message"])
        self.assertTrue(res["success"])

    def test_g10_find_strips_links_by_default_and_includes_when_requested(self):
        snap = DOMSimplifier().simplify(WIKI_HTML)
        view = PageView(snap, URL, "Scaffolding")
        clean_snippets, _ = view.find("Bamboo")
        self.assertTrue(clean_snippets)
        self.assertNotIn("[link @", clean_snippets[0], "Por padrao (links=False) nao deve conter marcadores [link @...]")
        raw_snippets, _ = view.find("Bamboo", links=True)
        self.assertTrue(raw_snippets)
        self.assertIn("[link @", raw_snippets[0], "Com links=True deve manter os marcadores [link @...]")

    def test_g11_find_tool_disabled_after_max_find_per_page(self):
        agent = _agent(max_turns=5)
        self.assertEqual(agent.max_find_per_page, 1)
        snap = DOMSimplifier().simplify(WIKI_HTML)
        link_id = next(k for k, v in snap.selector_map.items() if "Crafting" in v)
        agent.ollama.queue_response("browser_find_in_page", {"query": "ID"})
        agent.ollama.queue_response("browser_click", {"selector": f"[link {link_id}]"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(URL, "qual o id do item?")
        self.assertTrue(res["success"])
        t1_tools = {t["function"]["name"] for t in agent.ollama.call_history[0]["tools"]}
        t2_tools = {t["function"]["name"] for t in agent.ollama.call_history[1]["tools"]}
        t3_tools = {t["function"]["name"] for t in agent.ollama.call_history[2]["tools"]}
        self.assertIn("browser_find_in_page", t1_tools)
        self.assertNotIn("browser_find_in_page", t2_tools, "Apos 1 uso na pagina, browser_find_in_page deve ser removida")
        self.assertIn("browser_find_in_page", t3_tools, "Apos mudar de pagina (click), browser_find_in_page deve voltar")

    def test_g12_extract_with_link_clicks_instead_of_answering(self):
        agent = _agent(max_turns=4)
        snap = DOMSimplifier().simplify(WIKI_HTML)
        link_id = next(k for k, v in snap.selector_map.items() if "Crafting" in v)
        agent.ollama.queue_response("browser_extract_data", {"answer": link_id})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(URL, "qual o id do item?")
        self.assertTrue(res["success"])
        self.assertEqual(agent.engine.calls["click"], [link_id], "Retornar @id em browser_extract_data deve clicar no link")
        self.assertEqual(res["final_result"]["extracted_value"], "-165")

    def test_g13_extract_with_missing_number_returns_error(self):
        agent = _agent(max_turns=4)
        agent.ollama.queue_response("browser_extract_data", {"answer": "162"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(URL, "qual o id do item?")
        self.assertFalse(res["turns"][0]["result"]["ok"])
        self.assertIn("162", res["turns"][0]["result"]["message"])
        self.assertTrue(res["success"])
        self.assertEqual(res["final_result"]["extracted_value"], "-165")

    def test_g14_pretriage_normalizes_title_only_find_query(self):
        agent = _agent(max_turns=4)
        agent.engine.url = "https://minecraft.wiki/w/Scaffolding"
        agent.ollama.queue_response("browser_find_in_page", {"query": "Scaffolding"})
        agent.ollama.queue_response("browser_extract_data", {"answer": "-165"})
        res = agent.run_task(
            "https://minecraft.wiki/w/Scaffolding",
            "Tell me the numeric ID of Scaffolding in Bedrock Edition.",
        )
        self.assertTrue(res["success"])
        self.assertIn("-165", res["turns"][0]["result"]["message"])
        self.assertEqual(res["final_result"]["extracted_value"], "-165")


if __name__ == "__main__":
    unittest.main()

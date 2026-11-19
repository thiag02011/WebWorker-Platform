"""
MODULO: GROUNDED REACT WEB AGENT (TriagedWebAgent)
=============================================================================
OBJETIVO:
Agente web autonomo baseado em um ciclo unico ReAct (Observar -> Decidir -> Agir)
com aterramento estrito nos elementos reais da pagina.

POR QUE NAO HA MAIS FASES 0/1/2:
O pipeline anterior classificava a "estrategia" olhando so a URL (Fase 0) antes
de abrir a pagina, e essa adivinhacao contaminava as fases seguintes (vies em
cascata): o modelo era instruido a usar uma barra de busca inexistente e
inventava seletores. Aqui toda decisao e tomada sobre a observacao real.

FLUXO:
1. Navega ate a URL e gera o snapshot do DOM (DOMSimplifier).
2. A cada turno, monta uma OBSERVACAO nova (sem acumular DOMs antigos no contexto):
   - janela de leitura do texto (posicionada na ancora #fragmento, se houver);
   - CAMPOS DE TEXTO realmente existentes (ou aviso explicito de que nao ha);
   - ELEMENTOS INTERATIVOS mais relevantes ao objetivo;
   - resultado da ultima acao e historico compacto das acoes anteriores.
3. O modelo escolhe UMA ferramenta (react_tools_schema).
4. Validacao de aterramento ANTES de tocar no navegador:
   - click/fill so aceitam @ids presentes no selector_map atual;
   - fill so aceita @ids de campos de texto;
   - acao identica repetida e rejeitada.
5. Encerramento: resposta (browser_extract_data), falha honesta
   (browser_report_failure), 3 erros consecutivos ou limite de turnos.
   No ultimo turno so sao oferecidas as ferramentas de encerramento.
=============================================================================
"""
import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from src.config_manager import get_config
from src.web.browser_engine import BrowserEngine
from src.web.dom_simplifier import DOMSnapshot
from src.ollama_manager import OllamaManager
from src.gpu_telemetry import GPUTelemetry
from src.web.react_tools_schema import get_react_tools, get_answer_tool, get_failure_tool


WINDOW_CHARS = 2000          # Tamanho da janela de leitura enviada ao modelo
SNIPPET_BEFORE = 40          # Contexto antes de cada ocorrencia em find_in_page
SNIPPET_AFTER = 1200         # Contexto depois de cada ocorrencia em find_in_page
MAX_SNIPPETS = 2
MAX_CONSECUTIVE_ERRORS = 3

_TEXT_INPUT_TYPES = {"text", "search", "email", "url", "tel", "number", "password"}
_INPUT_RE = re.compile(r"\[input:([a-z]+) (@[\w-]+)([^\]]*)\]")
_ELEMENT_RE = re.compile(r"\[(link|button|select) (@[\w-]+)([^\]]*)\]")
_LINK_MARKER_RE = re.compile(r"\[link @[\w-]+[^\]]*\]\s*")
_AT_ID_RE = re.compile(r"@[\w-]+")
_STOPWORDS = {
    "o", "a", "os", "as", "de", "do", "da", "dos", "das", "e", "em", "no", "na", "um", "uma",
    "qual", "quais", "que", "como", "se", "faz", "para", "por", "com", "item", "the", "of",
    "is", "what", "how", "me", "diga", "mostre", "pagina", "site",
}
_GOAL_NOISE_WORDS = _STOPWORDS | {
    "informe", "retorne", "extraia", "procure", "busque", "encontre", "localize", "passe",
    "quero", "saber", "preciso", "apenas", "formato", "desse", "deste", "este", "esse",
    "tabela", "secao", "coluna", "valor", "numero", "tell", "provide",
    "give", "find", "number", "format", "this", "in", "on", "for", "from", "to",
    "json", "array", "primeiras", "primeiros", "primeira", "primeiro", "clique", "clicando",
    "navegue", "faca", "preenchendo", "ordene", "aplique", "abra", "carregar", "exibida",
    "exibido", "tela", "botao", "depois", "aguarde",
}
_BATCH_GOAL_RE = re.compile(r"\b(lote|batch|todos os links|all links|lista de links|listar links)\b", re.I)
_FORM_GOAL_RE = re.compile(
    r"\b(login|logar|preencha|preencher|fill|usuario|username|password|senha|submet|submit)\b",
    re.I,
)
_COUNT_GOAL_RE = re.compile(
    r"\b(contagem|conte|contar|quantos|quantas|count|browser_count_items|total de blocos|total de citacoes|total de itens|numero de blocos|numero de citacoes)\b",
    re.I,
)


# =============================================================================
# Utilitarios de texto
# =============================================================================
def _fold(text: str) -> str:
    """Minusculas sem acentos preservando o comprimento (mapeamento 1:1 de offsets)."""
    out = []
    for ch in text:
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append((decomposed[0] if decomposed else ch).lower())
    return "".join(out)


def _keywords(text: str) -> List[str]:
    tokens = re.findall(r"[\w#-]+", _fold(text or ""))
    return [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]


def _page_identity_words(title: str = "", url: str = "") -> set:
    """Extrai dinamicamente palavras de identidade da pagina a partir do titulo e da URL."""
    words = set(_keywords(title))
    if url:
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path or "").replace("_", " ")
        words.update(t for t in _keywords(path) if len(t) >= 3)
    return words


def _triage_goal_query(goal: str, title: str = "", url: str = "") -> str:
    """Pre-triagem deterministica: extrai termos-alvo do objetivo descartando ruido e palavras da pagina."""
    title_words = _page_identity_words(title, url)
    tokens = re.findall(r"[\w#-]+", _fold(goal or ""))
    focused = [t for t in tokens if len(t) >= 2 and t not in _GOAL_NOISE_WORDS and t not in title_words]
    if not focused:
        focused = [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS and t not in title_words]
    if not focused:
        focused = [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]
    return " ".join(focused[:3])


def _normalize_find_query(raw_query: str, goal: str, title: str = "", url: str = "") -> str:
    """Se a query do LLM contiver apenas o titulo da pagina ou ruido, substitui pelos termos-alvo do objetivo."""
    title_words = _page_identity_words(title, url)
    q_tokens = [t for t in re.findall(r"[\w#-]+", _fold(raw_query or "")) if len(t) >= 2]
    specific = [t for t in q_tokens if t not in title_words and t not in _GOAL_NOISE_WORDS]
    if specific:
        return " ".join(specific[:3])
    fallback = _triage_goal_query(goal, title, url)
    return fallback if fallback else (raw_query or "").strip()


def _extract_dom_window(pruned_text: str, target_url: str = "", max_chars: int = 16000) -> str:
    """Compatibilidade: janela do texto posicionada na ancora (#fragmento) da URL."""
    if not pruned_text or len(pruned_text) <= max_chars:
        return pruned_text or ""
    start = PageView.anchor_offset(pruned_text, target_url)
    return pruned_text[start:start + max_chars]


def _strip_links(text: str) -> str:
    """Remove marcadores inline [link @id url] mantendo apenas o texto visivel."""
    if not text:
        return ""
    cleaned = _LINK_MARKER_RE.sub("", text)
    return "\n".join(" ".join(line.split()) for line in cleaned.splitlines())


# =============================================================================
# Visao da pagina (observacao estruturada sobre o snapshot)
# =============================================================================
class PageView:
    """Observacao estruturada derivada do DOMSnapshot, com janela de leitura movel."""

    def __init__(self, snapshot: Optional[DOMSnapshot], url: str, title: str = ""):
        self.snapshot = snapshot or DOMSnapshot(pruned_text="")
        self.url = url or ""
        self.title = title or ""
        self.raw_text = self.snapshot.full_text or self.snapshot.pruned_text or ""
        self.text = _strip_links(self.raw_text)
        self.selector_map = dict(self.snapshot.selector_map or {})
        self.offset = self.anchor_offset(self.text, self.url)
        self.text_inputs = self._parse_text_inputs()
        self.elements = self._parse_elements()

    def update_snapshot(self, snapshot: Optional[DOMSnapshot], url: str = "") -> None:
        if not snapshot:
            return
        self.snapshot = snapshot
        if url:
            self.url = url
        self.raw_text = self.snapshot.full_text or self.snapshot.pruned_text or ""
        self.text = _strip_links(self.raw_text)
        self.selector_map = dict(self.snapshot.selector_map or {})
        self.collapsed_sections = dict(getattr(self.snapshot, "collapsed_sections", None) or {})
        self.text_inputs = self._parse_text_inputs()
        self.elements = self._parse_elements()
        self.offset = min(self.offset, max(0, len(self.text) - 1))

    # ---------------------------------------------------------------- ancora
    @staticmethod
    def anchor_offset(text: str, url: str) -> int:
        """Posicao inicial de leitura: o navegador rola ate o #fragmento; reproduzimos isso no texto."""
        fragment = urllib.parse.unquote(urllib.parse.urlparse(url or "").fragment or "")
        fragment = fragment.replace("_", " ").strip()
        if len(fragment) < 2 or not text:
            return 0
        folded = _fold(text)
        needle = _fold(fragment)
        needle_re = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
        # Prioriza titulos de secao (<h2>, <h3>...) que contenham a palavra exata do fragmento
        for m in re.finditer(r"<h[1-6]>", folded):
            line_end = folded.find("\n", m.end())
            line = folded[m.end(): line_end if line_end != -1 else m.end() + 200]
            if needle_re.search(line):
                return max(0, m.start() - 200)
        m_any = needle_re.search(folded)
        if m_any:
            return max(0, m_any.start() - 500)
        pos = folded.find(needle)
        return max(0, pos - 500) if pos != -1 else 0

    # ------------------------------------------------------------- elementos
    def _label_after(self, end: int) -> str:
        tail = self.raw_text[end:end + 80]
        cut = re.split(r"[\[\n|]", tail, maxsplit=1)[0]
        return " ".join(cut.split())[:50]

    def _parse_text_inputs(self) -> List[Dict[str, str]]:
        found = []
        for m in _INPUT_RE.finditer(self.raw_text):
            itype, sid, desc = m.group(1), m.group(2), m.group(3).strip()
            if itype in _TEXT_INPUT_TYPES and sid in self.selector_map:
                found.append({"id": sid, "type": itype, "desc": desc})
        return found

    def _parse_elements(self) -> List[Dict[str, Any]]:
        found = []
        for m in _ELEMENT_RE.finditer(self.raw_text):
            kind, sid, extra = m.group(1), m.group(2), m.group(3).strip()
            if sid in self.selector_map:
                found.append({
                    "id": sid, "kind": kind, "target": extra,
                    "label": self._label_after(m.end()), "pos": m.start(),
                })
        return found

    def relevant_elements(self, goal: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Elementos relevantes ao objetivo primeiro, depois os da janela de leitura atual."""
        kws = set(_keywords(goal))
        scored, in_window, seen = [], [], set()
        for el in self.elements:
            hay = set(_keywords(f"{el['label']} {el['target']}"))
            score = len(kws & hay)
            if score:
                scored.append((score, el))
            elif self.offset <= el["pos"] < self.offset + WINDOW_CHARS:
                in_window.append(el)
        scored.sort(key=lambda x: -x[0])
        result = []
        for el in [e for _, e in scored] + in_window:
            if el["id"] not in seen:
                seen.add(el["id"])
                result.append(el)
            if len(result) >= limit:
                break
        return result

    # ----------------------------------------------------------------- leitura
    def window(self) -> str:
        chunk = self.text[self.offset:self.offset + WINDOW_CHARS]
        prefix = f"[... {self.offset} caracteres acima ...]\n" if self.offset > 0 else ""
        remaining = len(self.text) - (self.offset + WINDOW_CHARS)
        suffix = f"\n[... {remaining} caracteres abaixo; use browser_read_more ou browser_find_in_page ...]" if remaining > 0 else ""
        return prefix + chunk + suffix

    def read_more(self, direction: str) -> bool:
        step = int(WINDOW_CHARS * 0.85)
        new = self.offset + step if direction != "up" else self.offset - step
        new = max(0, min(new, max(0, len(self.text) - 1)))
        moved = new != self.offset
        self.offset = new
        return moved

    def find(self, query: str, links: bool = False) -> Tuple[List[str], Optional[int]]:
        """Busca exclusivamente por palavras inteiras no texto completo; por padrao sem [link @id]."""
        source_text = self.raw_text if links else self.text
        folded = _fold(source_text)
        terms = _keywords(query) or [t for t in re.findall(r"[\w#-]+", _fold(query)) if t]
        if not terms or not folded:
            return [], None
        phrase = _fold(query).strip()
        phrase_re = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)") if phrase else None
        term_regexes = [re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)") for t in terms]
        candidates = []
        for tr in term_regexes:
            for m in tr.finditer(folded):
                candidates.append(m.start())
        if not candidates:
            return [], None
        scored = []
        for pos in set(candidates):
            a, b = max(0, pos - SNIPPET_BEFORE), min(len(folded), pos + SNIPPET_AFTER)
            region = folded[a:b]
            score = sum(1 for tr in term_regexes if tr.search(region)) * 10
            if phrase_re and len(phrase) > 1 and phrase_re.search(region):
                score += 25
            if "<h" in folded[max(0, pos - 60):pos]:
                score += 15  # ocorrencia em titulo de secao
            scored.append((score, -pos, a, b))
        scored.sort(reverse=True)
        snippets, used, best = [], [], None
        for score, _neg, a, b in scored:
            if any(not (b <= ua or a >= ub) for ua, ub in used):
                continue
            used.append((a, b))
            if best is None:
                best = a
            snippets.append(" ".join(source_text[a:b].split()))
            if len(snippets) >= MAX_SNIPPETS:
                break
        return snippets, best


# =============================================================================
# Agente
# =============================================================================
SYSTEM_PROMPT = (
    "Voce e um agente web autonomo. A cada turno voce recebe a OBSERVACAO da pagina e escolhe UMA ferramenta.\n"
    "REGRAS:\n"
    "1. Se o dado pedido estiver no TEXTO DA PAGINA ou no RESULTADO DA ULTIMA ACAO, chame browser_extract_data "
    "imediatamente com o valor exato (answer) e o trecho que comprova (evidence).\n"
    "2. Se o dado nao estiver visivel, use browser_find_in_page com palavras-chave do objetivo "
    "(use os termos no idioma da pagina, ex: ingles) antes de clicar ou navegar.\n"
    "3. browser_click e browser_fill aceitam SOMENTE @ids listados na observacao. Nunca invente seletores.\n"
    "4. Use browser_fill apenas se houver CAMPOS DE TEXTO listados.\n"
    "5. Nao repita uma acao que ja falhou ou ja foi feita.\n"
    "6. Se o dado realmente nao existir apos procurar, chame browser_report_failure.\n"
    "Nunca responda com texto livre: sempre emita uma chamada de ferramenta."
)


class TriagedWebAgent:
    """Agente web ReAct aterrado. Mantem o nome e a API publica da versao anterior."""

    def __init__(
        self,
        model: str = "qwen2.5:3b",
        headless: bool = True,
        enable_adblock: bool = True,
        enable_triage: bool = True,
        max_turns: int = 8,
        max_find_per_page: Optional[int] = None,
    ):
        self.model = model
        self.headless = headless
        self.enable_adblock = enable_adblock
        # Mantido por compatibilidade de assinatura; o ciclo ReAct aterrado e sempre usado.
        self.enable_triage = enable_triage
        self.max_turns = max(1, int(max_turns))
        if max_find_per_page is None:
            cfg_val = get_config().get("fleet.max_find_per_page")
            max_find_per_page = int(cfg_val) if cfg_val is not None else 1
        self.max_find_per_page = max(0, int(max_find_per_page))

        self.engine = BrowserEngine(headless=headless, enable_adblock=enable_adblock)
        self.ollama = OllamaManager()
        self.gpu = GPUTelemetry()

    # ------------------------------------------------------------ ciclo de vida
    def start(self):
        """Inicializa o motor do navegador e valida conexao com o Ollama."""
        self.engine.start()
        if not self.ollama.is_server_online():
            self.ollama.try_start_server()
            time.sleep(2.0)
        if not self.ollama.is_server_online():
            raise RuntimeError("Ollama nao esta online em 127.0.0.1:11434")

    def stop(self):
        """Encerra a sessao do navegador."""
        self.engine.stop()

    @staticmethod
    def compute_dom_hash(snapshot: Optional[DOMSnapshot]) -> str:
        """Gera hash MD5 do texto podado da pagina para detectar estabilidade ou mudancas."""
        if not snapshot or not snapshot.pruned_text:
            return ""
        normalized = " ".join(snapshot.pruned_text.split())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    # --------------------------------------------------------------- LLM
    def _call_llm(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Chama o modelo e devolve a primeira tool call valida (com ate 2 nudges)."""
        msgs = list(messages)
        for _ in range(3):
            chat_res = self.ollama.chat(
                model=self.model,
                messages=msgs,
                tools=tools,
                options={"temperature": 0.0, "num_ctx": 8192},
            )
            message = chat_res.get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []
            allowed = {t["function"]["name"] for t in tools}
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if name in allowed:
                    return {"name": name, "args": args if isinstance(args, dict) else {}}
            msgs.append({"role": "assistant", "content": message.get("content", "") or ""})
            msgs.append({
                "role": "user",
                "content": "Responda emitindo UMA chamada de ferramenta valida dentre: " + ", ".join(sorted(allowed)) + ".",
            })
        return None

    # ---------------------------------------------------------- observacao
    def _build_observation(self, view: PageView, goal: str, last_result: str,
                           history: List[str], turn: int, final_turn: bool,
                           turn_tools: Optional[List[Dict[str, Any]]] = None,
                           filled_ids: Optional[set] = None,
                           did_submit: bool = False,
                           did_count: bool = False) -> str:
        parts = [
            f"OBJETIVO: {goal}",
            f"TURNO: {turn}/{self.max_turns}",
            f"PAGINA: {view.title} | {view.url}",
        ]
        if history:
            parts.append("ACOES ANTERIORES:\n" + "\n".join(history[-6:]))
        if last_result:
            parts.append("RESULTADO DA ULTIMA ACAO:\n" + last_result)

        filled_set = filled_ids or set()
        is_form_goal = bool(view.text_inputs and _FORM_GOAL_RE.search(_fold(goal or "")))
        is_count_goal = bool(_COUNT_GOAL_RE.search(_fold(goal or "")))
        unfilled_ids = [i["id"] for i in view.text_inputs if i["id"] not in filled_set]

        if view.text_inputs:
            lines = []
            for i in view.text_inputs[:10]:
                status_tag = ""
                if is_form_goal:
                    status_tag = " [PREENCHIDO]" if i["id"] in filled_set else " [PENDENTE]"
                lines.append(f"- {i['id']} ({i['type']}) {i['desc']}{status_tag}".rstrip())
            parts.append("CAMPOS DE TEXTO:\n" + "\n".join(lines))
        else:
            parts.append("CAMPOS DE TEXTO: nenhum nesta pagina (browser_fill indisponivel).")

        elements = view.relevant_elements(goal)
        if elements:
            lines = []
            for e in elements:
                lbl, tgt = e["label"], e["target"]
                if lbl and tgt and tgt not in ("#", "/", lbl):
                    lines.append(f"- {e['id']} [{e['kind']}] {lbl} ({tgt})")
                else:
                    lines.append(f"- {e['id']} [{e['kind']}] {lbl or tgt}")
            parts.append(f"ELEMENTOS INTERATIVOS (mais relevantes de {len(view.elements)}):\n" + "\n".join(lines))

        parts.append("TEXTO DA PAGINA:\n" + view.window())
        if final_turn:
            parts.append(
                "ULTIMO TURNO: responda com browser_extract_data usando o que ja foi encontrado, "
                "ou chame browser_report_failure."
            )
        elif is_form_goal and unfilled_ids:
            parts.append(
                f"INSTRUCAO FINAL: Preencha agora o campo pendente {unfilled_ids[0]} usando browser_fill."
            )
        elif is_form_goal and not did_submit:
            parts.append(
                "INSTRUCAO FINAL: Todos os campos foram preenchidos. Clique agora no botao de envio/Login "
                "usando browser_click para confirmar a mudanca de pagina."
            )
        elif is_count_goal and not did_count:
            parts.append(
                "INSTRUCAO FINAL: Chame browser_count_items agora para rolar a pagina ate o fim e obter a contagem exata dos grupos no DOM."
            )
        else:
            parts.append(
                "INSTRUCAO FINAL: Se o dado solicitado ja estiver visivel no TEXTO DA PAGINA ou no "
                "RESULTADO DA ULTIMA ACAO, finalize AGORA chamando browser_extract_data com 'answer' preenchido."
            )
        return "\n\n".join(parts)

    # ------------------------------------------------------------- execucao
    def _refresh_view(self, prev: Optional[PageView] = None) -> PageView:
        snap = self.engine._get_current_snapshot()
        url = self.engine.get_url()
        title = prev.title if prev else ""
        view = PageView(snap, url, title)
        # Mesma URL e mesmo conteudo: preserva a posicao de leitura
        if prev and prev.url == view.url and prev.text == view.text:
            view.offset = prev.offset
        return view

    def _ground_selector(self, raw: str, view: PageView) -> Optional[str]:
        m = _AT_ID_RE.search(raw or "")
        if m and m.group(0) in view.selector_map:
            return m.group(0)
        return None

    def _element_text_offset(self, sid: str, view: PageView) -> Optional[int]:
        for el in view.elements:
            if el["id"] == sid:
                clean_pos = len(_strip_links(view.raw_text[:el["pos"]]))
                return max(0, clean_pos - 400)
        return None

    def _click_selector(self, sid: str, view: PageView, from_extract: bool = False) -> Dict[str, Any]:
        if sid in getattr(view, "collapsed_sections", {}):
            raw_list = _strip_links(view.collapsed_sections[sid])
            return {
                "ok": True,
                "message": f"Lista de linguagens disponiveis em {sid}:\n{raw_list[:2500]}",
            }
        target_offset = self._element_text_offset(sid, view)
        res = self.engine.click(sid)
        if not res.get("success"):
            if target_offset is not None:
                view.offset = target_offset
                return {
                    "ok": True,
                    "message": f"Janela posicionada na secao de {sid}. Extraia o dado real do TEXTO DA PAGINA.",
                }
            return {"ok": False, "message": f"Falha ao clicar em {sid}: {res.get('error')}"}
        msg = (
            f"Link {sid} clicado (nao enviado como resposta). Extraia o valor real do TEXTO DA PAGINA."
            if from_extract
            else f"Clique em {sid} executado."
        )
        return {"ok": True, "changed_page": True, "target_offset": target_offset, "message": msg}

    def _execute(
        self,
        name: str,
        args: Dict[str, Any],
        view: PageView,
        last_result: str = "",
        goal: str = "",
        find_count_this_page: int = 0,
        unfilled_inputs: Optional[List[str]] = None,
        did_submit_this_page: bool = True,
        did_count_this_page: bool = True,
    ) -> Dict[str, Any]:
        """Valida o aterramento e executa a ferramenta. Retorna dict com ok/message/final/changed_page."""
        if name == "browser_extract_data":
            if view.text_inputs and _FORM_GOAL_RE.search(_fold(goal)):
                if unfilled_inputs:
                    next_id = unfilled_inputs[0]
                    return {
                        "ok": False,
                        "message": (
                            f"ERRO: Falta preencher o campo de texto {next_id}. "
                            f"Use browser_fill em {next_id} antes de clicar no botao de envio ou extrair."
                        ),
                    }
                if not did_submit_this_page:
                    return {
                        "ok": False,
                        "message": (
                            "ERRO: Todos os campos foram preenchidos, mas a pagina ainda nao mudou. "
                            "Use browser_click no botao de envio/Login para confirmar a mudanca de pagina antes de extrair."
                        ),
                    }
            if _COUNT_GOAL_RE.search(_fold(goal)) and not did_count_this_page:
                return {
                    "ok": False,
                    "message": (
                        "ERRO: Para objetivos de contagem, chame browser_count_items primeiro para rolar a pagina "
                        "e contar os blocos repetidos no DOM."
                    ),
                }
            answer = str(args.get("answer") or "").strip()
            if not answer or answer in ("[]", "{}"):
                # Pre-triagem deterministica: se tentou extrair vazio no Turno 1 sem buscar em pagina longa, executa a busca
                if not answer and find_count_this_page == 0 and len(view.text) > WINDOW_CHARS and self.max_find_per_page > 0:
                    auto_q = _triage_goal_query(goal, view.title, view.url)
                    if auto_q:
                        res_find = self._execute("browser_find_in_page", {"query": auto_q}, view, last_result, goal, find_count_this_page)
                        res_find["auto_find"] = True
                        return res_find
                return {
                    "ok": False,
                    "message": "ERRO: 'answer' vazio. Preencha 'answer' apenas com o valor exato lido na tabela de TEXTO DA PAGINA ou RESULTADO DA ULTIMA ACAO.",
                }
            # Se retornar um link (@id), clica nele em vez de enviar como resposta
            sid = self._ground_selector(answer, view)
            if sid:
                return self._click_selector(sid, view, from_extract=True)
            # Se a resposta contiver numero(s) que nao existem no texto DOM visivel atual (sem @id), ignora e retorna erro
            nums = re.findall(r"(?<!\w)-?\d+(?:\.\d+)?(?!\w)", answer)
            if nums:
                visible_dom = re.sub(r"@\w+", "", _strip_links(f"{view.window()}\n{last_result}"))
                visible_dom_compact = re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", visible_dom)
                combined_dom = f"{visible_dom}\n{visible_dom_compact}"
                missing = [
                    n for n in nums
                    if not (
                        re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", combined_dom)
                        or ("." in n and re.search(r"(?<!\w)" + re.escape(n.replace(".", ",")) + r"(?!\w)", combined_dom))
                    )
                ]
                if missing:
                    return {
                        "ok": False,
                        "message": (
                            f"ERRO: O numero '{missing[0]}' nao existe na pagina DOM atual (sem @id). "
                            "Nao use numeros de @id como resposta; use browser_find_in_page ou extraia o valor real do TEXTO DA PAGINA."
                        ),
                    }
            return {"ok": True, "final": "answer", "message": answer}

        if name == "browser_report_failure":
            return {"ok": True, "final": "failure", "message": str(args.get("reason") or "Dado nao encontrado.")}

        if name == "browser_find_in_page":
            raw_query = str(args.get("query") or "").strip()
            query = _normalize_find_query(raw_query, goal, view.title, view.url) if goal else raw_query
            include_links = bool(args.get("links", False))
            snippets, best = view.find(query, links=include_links)
            if not snippets and query != raw_query and raw_query:
                snippets, best = view.find(raw_query, links=include_links)
                query = raw_query
            if not snippets:
                return {"ok": False, "message": f"Nenhuma ocorrencia de '{raw_query}' no texto da pagina. Tente outros termos (sinonimos ou outro idioma)."}
            if best is not None and not include_links:
                view.offset = max(0, best)
            body = "\n".join(f"[{i + 1}] ...{s}..." for i, s in enumerate(snippets))
            hint = "\n-> Se o valor procurado aparece nos trechos acima, finalize AGORA chamando browser_extract_data com 'answer' preenchido."
            return {"ok": True, "message": f"Trechos encontrados para '{query}':\n{body}{hint}"}

        if name == "browser_read_more":
            direction = str(args.get("direction") or "down")
            m_scroll = re.search(r"\b(\d+)\s*(?:rolagen|scroll)", goal or "", re.I) if direction != "up" else None
            if m_scroll:
                n_scrolls = min(max(1, int(m_scroll.group(1))), 6)
                scroll_res: Dict[str, Any] = {}
                for _ in range(n_scrolls):
                    scroll_res = self.engine.scroll("down", 1200)
                if scroll_res.get("dom_snapshot"):
                    view.update_snapshot(scroll_res.get("dom_snapshot"), self.engine.get_url())
                rep = scroll_res.get("repeated_blocks") or {}
                rep_str = ", ".join(f"{k}: {v}" for k, v in rep.items()) if rep else "nenhum"
                return {
                    "ok": True,
                    "changed_page": True,
                    "message": f"Rolagem na pagina executada. Total agora: {len(view.text)} chars; blocos repetidos ({rep_str}).",
                }
            if view.read_more(direction):
                return {"ok": True, "message": f"Janela de leitura movida para {'baixo' if direction != 'up' else 'cima'}."}
            if direction != "up":
                prev_len = len(view.text)
                scroll_res = self.engine.scroll("down", 1000)
                if scroll_res.get("dom_snapshot"):
                    view.update_snapshot(scroll_res.get("dom_snapshot"), self.engine.get_url())
                if len(view.text) > prev_len:
                    view.read_more("down")
                    return {"ok": True, "changed_page": True, "message": "Pagina rolada e novos dados carregados."}
            return {"ok": False, "message": "Ja esta no limite do texto; nao ha mais conteudo nessa direcao."}

        if name == "browser_count_items":
            auto_scroll = bool(args.get("auto_scroll", True))
            prev_offset = view.offset
            res = self.engine.count_items(auto_scroll=auto_scroll)
            if not res.get("success"):
                return {"ok": False, "message": f"Falha ao contar itens: {res.get('error')}"}
            if res.get("dom_snapshot"):
                view.update_snapshot(res.get("dom_snapshot"), self.engine.get_url())
                view.offset = min(prev_offset, max(0, len(view.text) - WINDOW_CHARS))
            groups = res.get("groups") or []
            if not groups:
                return {
                    "ok": True,
                    "message": "Contagem concluida, mas nenhum grupo de itens repetidos (>=3) foi detectado no DOM.",
                }
            lines = [
                f"- Grupo {g['rank']}: {g['count']} itens ({g['selector']}) | Exemplo: \"{g['sample']}\""
                for g in groups
            ]
            return {
                "ok": True,
                "message": (
                    "Contagem concluida apos carregar a pagina ate o fim (posicao restaurada):\n"
                    + "\n".join(lines)
                    + "\n-> Chame browser_extract_data com o numero do grupo correspondente ao objetivo."
                ),
            }

        if name == "browser_click":
            sid = self._ground_selector(str(args.get("selector") or ""), view)
            if not sid:
                return {"ok": False, "message": f"Seletor '{args.get('selector')}' nao existe na tela. Use um @id da lista ELEMENTOS INTERATIVOS."}
            res = self.engine.click(sid)
            if not res.get("success"):
                return {"ok": False, "message": f"Falha ao clicar em {sid}: {res.get('error')}"}
            return {"ok": True, "changed_page": True, "message": f"Clique em {sid} executado."}

        if name == "browser_fill":
            if not view.text_inputs:
                return {"ok": False, "message": "Nao ha campos de texto nesta pagina. Procure o dado no texto ou clique em um link."}
            sid = self._ground_selector(str(args.get("selector") or ""), view)
            valid = {i["id"] for i in view.text_inputs}
            if sid not in valid:
                return {"ok": False, "message": f"Seletor '{args.get('selector')}' nao e um campo de texto. Campos validos: {', '.join(sorted(valid))}."}
            value = str(args.get("value") or "")
            res = self.engine.fill(sid, value, press_enter=bool(args.get("press_enter", False)))
            if not res.get("success"):
                return {"ok": False, "message": f"Falha ao preencher {sid}: {res.get('error')}"}
            return {"ok": True, "changed_page": True, "message": f"Texto '{value}' digitado em {sid}."}

        if name == "browser_go_back":
            res = self.engine.go_back()
            if not res.get("success", False):
                return {"ok": False, "message": f"Nao foi possivel voltar: {res.get('error')}"}
            return {"ok": True, "changed_page": True, "message": "Voltou para a pagina anterior."}

        if name == "browser_discover_batch_links":
            items = self.engine.discover_batch_links(args.get("link_pattern", ""), max_items=args.get("max_items", 0) or 0)
            return {"ok": True, "final": "batch", "items": items, "message": f"{len(items)} links coletados."}

        return {"ok": False, "message": f"Ferramenta desconhecida: {name}"}

    # ------------------------------------------------------------- tarefa
    def run_task(self, target_url: str, user_goal: str) -> Dict[str, Any]:
        """Executa o objetivo do usuario a partir da URL com o ciclo ReAct aterrado."""
        t0 = time.time()
        print("\n" + "=" * 80)
        print("[AGENTE WEB] Ciclo ReAct aterrado")
        print(f"URL alvo: {target_url}")
        print(f"Objetivo: {user_goal}")
        print("=" * 80, flush=True)

        telemetry: Dict[str, Any] = {
            "mode": "grounded_react",
            "target_url": target_url,
            "user_goal": user_goal,
            "turns": [],
            "intent_assessment": None,
            "dom_assessment": None,
            "reflections": [],
            "same_dom_streak_peaks": 0,
            "final_result": None,
            "success": False,
            "error": None,
        }

        nav = self.engine.navigate(target_url)
        time.sleep(0.5)
        snap = nav.get("dom_snapshot") or self.engine._get_current_snapshot()
        view = PageView(snap, self.engine.get_url() or target_url, nav.get("title", ""))
        print(f"Pagina aberta: '{view.title}' | {len(view.text)} chars | "
              f"{len(view.elements)} elementos | {len(view.text_inputs)} campos de texto", flush=True)

        include_batch = bool(_BATCH_GOAL_RE.search(user_goal or ""))
        include_count = bool(_COUNT_GOAL_RE.search(_fold(user_goal or "")))
        tools_all = get_react_tools(include_batch=include_batch, include_count=include_count)
        tools_no_find = [t for t in tools_all if t["function"]["name"] != "browser_find_in_page"]
        tools_final = [get_answer_tool(), get_failure_tool()]
        history: List[str] = []
        last_result = ""
        last_action_key = None
        seen_find_queries: set = set()
        find_count_this_page = 0
        filled_ids_this_page: set = set()
        did_submit_this_page = False
        did_count_this_page = False
        consecutive_errors = 0

        for turn in range(1, self.max_turns + 1):
            final_turn = (turn == self.max_turns)
            unfilled = [i["id"] for i in view.text_inputs if i["id"] not in filled_ids_this_page]
            is_form_goal = bool(view.text_inputs and _FORM_GOAL_RE.search(_fold(user_goal or "")))
            if final_turn:
                turn_tools = tools_final
            elif is_form_goal and unfilled:
                turn_tools = [t for t in tools_all if t["function"]["name"] in ("browser_fill", "browser_report_failure")]
            elif is_form_goal and not did_submit_this_page:
                turn_tools = [t for t in tools_all if t["function"]["name"] in ("browser_click", "browser_report_failure")]
            elif include_count and not did_count_this_page:
                turn_tools = [t for t in tools_all if t["function"]["name"] in ("browser_count_items", "browser_report_failure")]
            else:
                base_tools = tools_no_find if find_count_this_page >= self.max_find_per_page else tools_all
                if include_count and did_count_this_page:
                    turn_tools = [t for t in base_tools if t["function"]["name"] != "browser_count_items"]
                else:
                    turn_tools = base_tools
            observation = self._build_observation(
                view,
                user_goal,
                last_result,
                history,
                turn,
                final_turn,
                turn_tools,
                filled_ids=filled_ids_this_page,
                did_submit=did_submit_this_page,
                did_count=did_count_this_page,
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": observation},
            ]
            decision = self._call_llm(messages, turn_tools)
            if not decision:
                consecutive_errors += 1
                last_result = "ERRO: nenhuma ferramenta valida foi chamada."
                telemetry["turns"].append({"turn": turn, "tool": None, "error": "no_tool_call"})
                print(f"[Turno {turn}] modelo nao emitiu ferramenta valida", flush=True)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    break
                continue

            name, args = decision["name"], decision["args"]
            print(f"[Turno {turn}] {name} {args}", flush=True)

            action_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if (
                name == "browser_read_more"
                and str(args.get("direction") or "down").lower() == "down"
                and (view.offset + len(view.window()) < len(view.text))
            ):
                action_key = (name, "down", view.offset)
            q_norm = _fold(str(args.get("query") or "")).strip() if name == "browser_find_in_page" else ""
            if action_key == last_action_key and name != "browser_report_failure":
                result = {"ok": False, "message": "Acao identica a anterior rejeitada. Escolha uma acao diferente."}
            elif name == "browser_find_in_page" and q_norm and q_norm in seen_find_queries:
                result = {"ok": False, "message": f"Busca repetida rejeitada: '{args.get('query')}' ja foi buscado nesta pagina. Extraia o dado com browser_extract_data ou busque outro termo."}
            else:
                result = self._execute(
                    name, args, view, last_result, user_goal, find_count_this_page,
                    unfilled_inputs=unfilled,
                    did_submit_this_page=did_submit_this_page,
                    did_count_this_page=did_count_this_page,
                )
                if name == "browser_fill" and result.get("ok"):
                    sel_filled = self._ground_selector(str(args.get("selector") or ""), view) or str(args.get("selector") or "").strip()
                    if sel_filled:
                        filled_ids_this_page.add(sel_filled)
                elif name == "browser_click" and result.get("ok"):
                    did_submit_this_page = True
                elif name == "browser_count_items" and result.get("ok"):
                    did_count_this_page = True
                if name == "browser_find_in_page" or result.get("auto_find"):
                    find_count_this_page += 1
                    if q_norm:
                        seen_find_queries.add(q_norm)
            last_action_key = action_key

            telemetry["turns"].append({"turn": turn, "tool": name, "args": args, "result": {
                k: v for k, v in result.items() if k != "items"
            }})
            status = "OK" if result["ok"] else "ERRO"
            print(f"          -> {status}: {result['message']}", flush=True)

            final = result.get("final")
            if final == "answer":
                telemetry["final_result"] = {
                    "extracted_value": result["message"],
                    "summary": result["message"],
                    "evidence": args.get("evidence"),
                    "source_url": view.url,
                    "args": args,
                }
                telemetry["success"] = True
                break
            if final == "batch":
                items = result.get("items", [])
                telemetry["final_result"] = {"batch_count": len(items), "items": items,
                                             "summary": f"{len(items)} links coletados."}
                telemetry["success"] = True
                break
            if final == "failure":
                telemetry["error"] = f"Agente reportou falha: {result['message']}"
                break

            consecutive_errors = 0 if result["ok"] else consecutive_errors + 1
            history.append(f"T{turn} {name}({json.dumps(args, ensure_ascii=False)}) -> {status}: {result['message'][:120]}")
            last_result = result["message"]

            if result.get("changed_page"):
                time.sleep(0.5)
                prev_text = view.text
                target_offset = result.get("target_offset")
                view = self._refresh_view(view)
                if target_offset is not None and view.text == prev_text and view.offset == 0:
                    view.offset = target_offset
                if len(view.text) != len(prev_text):
                    last_action_key = None
                view.title = ""
                seen_find_queries.clear()
                find_count_this_page = 0
                if name not in ("browser_fill", "browser_click"):
                    filled_ids_this_page.clear()
                    did_submit_this_page = False
                    did_count_this_page = False

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                telemetry["error"] = f"{MAX_CONSECUTIVE_ERRORS} erros consecutivos. Ultimo: {result['message']}"
                break

        if not telemetry["success"] and not telemetry["error"]:
            telemetry["error"] = f"Limite de {self.max_turns} turnos atingido sem resposta."

        telemetry["total_time_seconds"] = round(time.time() - t0, 2)
        telemetry["total_turns_used"] = len(telemetry["turns"])
        print(f"[AGENTE WEB] {'Sucesso' if telemetry['success'] else 'Falha'} em "
              f"{telemetry['total_time_seconds']}s | {telemetry['error'] or ''}", flush=True)
        return telemetry

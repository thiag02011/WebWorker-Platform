"""
MÓDULO: DOM PRUNER & SEMANTIC MAPPING (DOMSimplifier)
=============================================================================
OBJETIVO:
Reduzir páginas web complexas (150k+ tokens de HTML) para um snapshot de texto 
compacto (<1.500 tokens) legível por SLMs locais (3B), sem estourar o contexto 
nem induzir a IA ao erro.

FLUXO DA ARQUITETURA:
1. HTML Bruto (Playwright) 
   -> 2. _HTMLPrunerParser: Descarta scripts/estilos/svgs e trata void tags HTML5 
         (<meta>, <link>, <input>) para evitar engolir textos subsequentes.
   -> 3. Mapeador Semântico: Converte botões, inputs e links em marcadores amigáveis 
         (ex: [1] [Input: @s], [2] [Botão: Pesquisar]).
   -> 4. DOMSnapshot: Gera o texto enxuto para o prompt e mantém o mapa "De-Para" 
         (ID amigável da IA -> Seletor CSS / XPath real do navegador).
=============================================================================
"""
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
from html.parser import HTMLParser


@dataclass
class DOMSnapshot:
    """Representação condensada do DOM sem ruídos para o modelo."""
    pruned_text: str
    selector_map: Dict[str, str] = field(default_factory=dict)
    token_estimate: int = 0
    # Texto podado completo, sem o corte por max_tokens. Usado pela busca na pagina
    # (browser_find_in_page) para localizar secoes alem da janela enviada ao modelo.
    full_text: str = ""
    collapsed_sections: Dict[str, str] = field(default_factory=dict)


_LANGUAGE_TERMS = {
    # Idiomas falados (EN / PT / Endonimos principais)
    "english", "ingles", "inglês", "french", "frances", "francês", "français", "francais",
    "spanish", "espanhol", "español", "espanol", "portuguese", "portugues", "português",
    "german", "alemao", "alemão", "deutsch", "italian", "italiano", "russian", "russo", "русский",
    "japanese", "japones", "japonês", "日本語", "chinese", "chines", "chinês", "中文", "简体中文", "繁體中文",
    "korean", "coreano", "한국어", "arabic", "arabe", "árabe", "العربية",
    "dutch", "holandes", "holandês", "nederlands", "polish", "polones", "polonês", "polski",
    "turkish", "turco", "türkçe", "turkce", "hindi", "swedish", "sueco", "svenska",
    "norwegian", "norsk", "danish", "dansk", "finnish", "suomi", "greek", "hebrew",
    "vietnamese", "thai", "indonesian", "malay", "ukrainian", "czech", "romanian", "hungarian",
    "catalan", "bulgarian", "croatian", "serbian", "slovak", "slovenian", "lithuanian",
    "latvian", "estonian", "persian", "bengali", "tamil", "telugu", "urdu", "swahili",
    "afrikaans", "albanian", "amharic", "armenian", "azerbaijani", "basque", "belarusian",
    "bosnian", "burmese", "esperanto", "filipino", "galician", "georgian", "gujarati",
    "haitian", "hausa", "icelandic", "igbo", "irish", "javanese", "kannada", "kazakh",
    "khmer", "kurdish", "kyrgyz", "lao", "latin", "luxembourgish", "macedonian", "malagasy",
    "malayalam", "maltese", "maori", "marathi", "mongolian", "nepali", "pashto", "punjabi",
    "quechua", "samoan", "scots", "shona", "sindhi", "sinhala", "somali", "sundanese",
    "tajik", "tatar", "tibetan", "tigrinya", "turkmen", "uyghur", "uzbek", "welsh",
    "xhosa", "yiddish", "yoruba", "zulu",
    # Linguagens de programacao (filtros de repositorios/codigo)
    "actionscript", "agda", "assembly", "AutoHotkey".lower(), "awk", "batchfile", "bison",
    "clojure", "cmake", "cobol", "coffeescript", "crystal", "cuda", "cython", "dart",
    "delphi", "dockerfile", "elixir", "elm", "erlang", "fortran", "gdscript", "glsl",
    "golang", "graphql", "groovy", "haskell", "haxe", "hcl", "idris", "java", "javascript",
    "jinja", "jsonnet", "julia", "kotlin", "latex", "livescript", "llvm", "lua", "makefile",
    "markdown", "matlab", "meson", "mlir", "modelica", "nim", "nix", "objective-c", "ocaml",
    "odin", "opencl", "pascal", "perl", "php", "plpgsql", "plsql", "postscript", "powershell",
    "prolog", "pug", "puppet", "purescript", "python", "qmake", "qml", "racket", "raku",
    "rescript", "roff", "ruby", "rust", "scala", "scheme", "scss", "smalltalk", "solidity",
    "sparql", "sql", "starlark", "stylus", "svelte", "swift", "systemverilog", "tcl", "tex",
    "thrift", "twig", "typescript", "typst", "vala", "vbscript", "vcl", "verilog", "vhdl",
    "vue", "vyper", "webassembly", "wgsl", "wikitext", "wolfram", "xslt", "yacc", "yaml", "zig",
}
_LANG_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(_LANGUAGE_TERMS, key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE,
)


def _count_distinct_languages(text: str) -> int:
    """Conta quantos idiomas/linguagens distintos aparecem como palavra inteira no trecho."""
    clean = re.sub(r"\[(?:link|button|input:[^\]]+|select)\s+@[^\]]+\]", " ", text)
    return len({m.group(1).lower() for m in _LANG_RE.finditer(clean)})


def _compact_url(url: str) -> str:
    """Simplifica URLs longas em representações limpas e compactas."""
    if not url:
        return ""
    if url.startswith("#"):
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        # Se for relativo (sem netloc / host)
        if not parsed.netloc:
            p = parsed.path
            return p if p else "/"
        # Se for absoluto, remove http://, https://, www. e encurta queries
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path
        if len(path) > 35:
            path = path[:18] + "..." + path[-14:]
        return f"{host}{path}"
    except Exception:
        return url[:30]


def _id_selector(element_id: str) -> str:
    """Gera seletor CSS seguro mesmo para IDs iniciados por digito (ex: id='2015')."""
    if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", element_id or ""):
        return f"#{element_id}"
    safe = (element_id or "").replace("'", "\\'")
    return f"[id='{safe}']"


def _slugify_label(text: str) -> str:
    """Converte rotulo em identificador seguro para @id sintetico."""
    fold = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", fold) if w not in {"selecione", "um", "uma", "de", "do", "da", "dos", "das", "o", "a", "os", "as", "por", "para", "select", "choose"}]
    slug = "_".join(words[:3])
    return slug[:32] if slug else "opcoes"


class _HTMLPrunerParser(HTMLParser):
    """Parser leve para extrair elementos semânticos e interativos sem dependências pesadas."""

    def __init__(self):
        super().__init__()
        self.output_lines: List[str] = []
        self.selector_map: Dict[str, str] = {}
        self.collapsed_sections: Dict[str, str] = {}
        self._counter = 1
        self._list_counter = 1
        self._block_ignored_tags = {"script", "style", "svg", "noscript"}
        self._void_ignored_tags = {"meta", "link", "param", "source"}
        self._ignored_depth = 0
        self._input_counts: Dict[str, int] = {}
        self._button_counts = 0
        self._current_text = []
        self._row_cells: List[int] = []
        self._in_cell_depth = 0
        self._ul_stack: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list):
        tag_lower = tag.lower()
        if tag_lower in self._block_ignored_tags:
            self._ignored_depth += 1
            return
        if tag_lower in self._void_ignored_tags:
            return
        if self._ignored_depth > 0:
            return

        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        element_id = attr_dict.get("id")
        element_name = attr_dict.get("name")
        input_type = attr_dict.get("type", "text").lower()

        # Trata elementos interativos
        if tag_lower == "input":
            # Ignora inputs mobile/ocultos específicos de sites como #ms para evitar bloqueios
            if input_type == "hidden" or element_id == "ms" or "hidden" in attr_dict.get("class", "") or "display:none" in attr_dict.get("style", "").replace(" ", "").lower():
                return

            syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
            self._counter += 1

            self._input_counts[input_type] = self._input_counts.get(input_type, 0) + 1
            type_idx = self._input_counts[input_type]

            placeholder = attr_dict.get("placeholder", "")
            val = attr_dict.get("value", "")

            if element_id:
                selector = _id_selector(element_id)
            elif element_name:
                selector = f"input[name='{element_name}']"
            elif input_type in ("submit", "button", "reset") and val and "'" not in val:
                selector = f"input[type='{input_type}'][value='{val}']"
            elif placeholder and "'" not in placeholder:
                selector = f"input[placeholder='{placeholder}']"
            elif type_idx == 1:
                selector = f"input[type='{input_type}']"
            else:
                selector = f"input[type='{input_type}']:nth-of-type({type_idx})"

            if input_type in ("checkbox", "radio"):
                is_checked = "checked" in attr_dict
                checked_str = "true" if is_checked else "false"
                self.selector_map[syn_id] = selector
                self.output_lines.append(f"[{input_type} {syn_id} checked={checked_str}]")
            elif input_type in ("submit", "button", "reset"):
                self.selector_map[syn_id] = selector
                label = val or input_type.capitalize()
                self.output_lines.append(f"[button {syn_id}] {label}")
            else:
                self.selector_map[syn_id] = selector
                desc = f"placeholder='{placeholder}'" if placeholder else (f"value='{val}'" if val else "")
                self.output_lines.append(f"[input:{input_type} {syn_id} {desc}]".strip())

        elif tag_lower == "button":
            syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
            self._counter += 1
            self._button_counts += 1
            css_class = attr_dict.get("class", "").strip()

            if element_id:
                selector = _id_selector(element_id)
            elif css_class:
                class_sel = "." + ".".join(css_class.split())
                selector = f"button{class_sel}"
            else:
                selector = f"button:nth-of-type({self._button_counts})"

            self.selector_map[syn_id] = selector
            self.output_lines.append(f"[button {syn_id}]")

        elif tag_lower == "select":
            syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
            self._counter += 1
            selector = _id_selector(element_id) if element_id else (f"select[name='{element_name}']" if element_name else "select")
            self.selector_map[syn_id] = selector
            self.output_lines.append(f"[select {syn_id}]")

        elif tag_lower == "a":
            href = attr_dict.get("href", "")
            # Ignora links de framing/marketing externo e ancoras de rodape/citacao que poluem a arvore
            is_boilerplate = any(d in href for d in (
                "github.com/saucelabs", "elementalselenium.com", "saucelabs.com",
                "#cite_note", "#cite_ref",
            ))
            if href and not href.startswith("javascript:") and not is_boilerplate:
                syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
                self._counter += 1
                selector = _id_selector(element_id) if element_id else f"a[href='{href}']"
                self.selector_map[syn_id] = selector
                compact_url = _compact_url(href)
                self.output_lines.append(f"[link {syn_id} {compact_url}]")

        elif tag_lower == "ul":
            hint = attr_dict.get("aria-label", "")
            if not hint:
                tail = "".join(self.output_lines[-12:])
                m_ph = re.findall(r"placeholder='([^']{2,60})'", tail)
                if m_ph:
                    hint = m_ph[-1]
                else:
                    clean_tail = re.sub(r"\[(?:input|button|link|select|checkbox|radio)[^\]]*\]", " ", tail)
                    clean_tail = " ".join(clean_tail.split())
                    if clean_tail:
                        hint = clean_tail[-50:]
            self._ul_stack.append({
                "start_idx": len(self.output_lines),
                "li_count": 0,
                "rich": False,
                "hint": hint,
            })

        elif tag_lower == "li":
            if self._ul_stack:
                self._ul_stack[-1]["li_count"] += 1
            self.output_lines.append(" " if self._in_cell_depth > 0 else "\n")

        elif tag_lower in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            if self._ul_stack:
                self._ul_stack[-1]["rich"] = True
            self.output_lines.append(f"\n<{tag_lower}>")

        elif tag_lower == "tr":
            if self._ul_stack:
                self._ul_stack[-1]["rich"] = True
            self.output_lines.append("\n")
            self._row_cells.append(0)

        elif tag_lower in {"td", "th"}:
            if self._row_cells:
                if self._row_cells[-1] > 0:
                    self.output_lines.append(" | ")
                self._row_cells[-1] += 1
            self._in_cell_depth += 1

        elif tag_lower in {"p", "article", "section", "details", "summary", "nav", "select"}:
            if tag_lower in {"p", "article"} and self._ul_stack:
                self._ul_stack[-1]["rich"] = True
            self.output_lines.append(" " if self._in_cell_depth > 0 else "\n")

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in self._block_ignored_tags:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth > 0:
            return
        if tag_lower == "ul" and self._ul_stack:
            ctx = self._ul_stack.pop()
            if ctx["li_count"] > 10 and not ctx["rich"]:
                inner_raw = "".join(self.output_lines[ctx["start_idx"]:])
                items = [
                    " ".join(re.sub(r"\[(?:link|button)\s+@[^\]]+\]", "", ln).split())
                    for ln in inner_raw.splitlines()
                ]
                items = [it for it in items if it]
                avg_len = (sum(len(it) for it in items) / len(items)) if items else 0
                if items and avg_len <= 90 and len(inner_raw) > 250:
                    slug = _slugify_label(ctx["hint"])
                    syn_id = f"@ver_{slug}_{self._list_counter}"
                    self._list_counter += 1
                    self.selector_map[syn_id] = f"#{syn_id[1:]}"
                    self.collapsed_sections[syn_id] = "\n".join(" ".join(ln.split()) for ln in inner_raw.splitlines() if ln.strip())
                    label_txt = ctx["hint"].strip() or slug.replace("_", " ")
                    self.output_lines[ctx["start_idx"]:] = [
                        f"\n[link {syn_id} #{syn_id[1:]}] Ver lista ({label_txt}: {ctx['li_count']} itens)\n"
                    ]
                    return
            self.output_lines.append("\n")
            return
        if tag_lower in {"td", "th"}:
            self._in_cell_depth = max(0, self._in_cell_depth - 1)
        elif tag_lower == "tr":
            if self._row_cells:
                self._row_cells.pop()
            self.output_lines.append("\n")
        elif tag_lower in {"details", "nav", "select", "article", "section"}:
            self.output_lines.append("\n")

    def handle_data(self, data: str):
        if self._ignored_depth > 0:
            return
        clean_text = data.strip()
        if clean_text:
            self.output_lines.append(f" {clean_text} ")


class DOMSimplifier:
    """Comprime e poda o DOM HTML para extrair apenas nós funcionais."""

    def simplify(self, html: str, max_tokens: int = 6000) -> DOMSnapshot:
        # 1. Remove comentários HTML
        cleaned_html = re.sub(r"<!--[\s\S]*?-->", "", html)

        parser = _HTMLPrunerParser()
        try:
            parser.feed(cleaned_html)
        except Exception:
            pass

        raw_text = "".join(parser.output_lines)
        # Normaliza espaços múltiplos e separadores de célula vazios
        normalized_lines = []
        for line in raw_text.splitlines():
            line_str = " ".join(line.split())
            if "|" in line_str:
                line_str = re.sub(r"(?:\s*\|\s*)+", " | ", line_str).strip(" |")
            if line_str:
                normalized_lines.append(line_str)

        pruned_text, collapsed_sections = self._collapse_language_lists(
            normalized_lines, parser.selector_map
        )
        if parser.collapsed_sections:
            merged = dict(parser.collapsed_sections)
            merged.update(collapsed_sections)
            collapsed_sections = merged
        full_text = pruned_text

        # Estimativa de tokens: ~4 caracteres por token
        est_tokens = len(pruned_text) // 4

        # Se ultrapassar limite, faz poda preservando nós interativos
        if est_tokens > max_tokens:
            pruned_text = pruned_text[: max_tokens * 4] + "\n... [DOM Truncado para caber na janela de contexto]"
            est_tokens = max_tokens

        return DOMSnapshot(
            pruned_text=pruned_text,
            selector_map=parser.selector_map,
            token_estimate=est_tokens,
            full_text=full_text,
            collapsed_sections=collapsed_sections,
        )

    @staticmethod
    def _collapse_language_lists(
        lines: List[str], selector_map: Dict[str, str]
    ) -> Tuple[str, Dict[str, str]]:
        """
        Substitui listas/selects com mais de 3 idiomas/linguagens separados entre si
        por um link sintetico '@escolher_linguagem_N' que exibe apenas essa lista quando clicado.
        """
        collapsed: Dict[str, str] = {}
        out_lines: List[str] = []
        idx_counter = 1
        i = 0
        n = len(lines)

        def _make_lang_link(raw_block: str, lang_count: int) -> str:
            nonlocal idx_counter
            syn_id = f"@escolher_linguagem_{idx_counter}"
            idx_counter += 1
            selector_map[syn_id] = "#escolher-linguagem"
            collapsed[syn_id] = raw_block
            return f"[link {syn_id} #escolher-linguagem] Escolher linguagem ({lang_count} opcoes)"

        while i < n:
            line = lines[i]
            lang_count = _count_distinct_languages(line)
            # Caso 1: Uma unica linha de filtro/select/menu contendo > 3 idiomas/linguagens
            if lang_count > 3:
                out_lines.append(_make_lang_link(line, lang_count))
                i += 1
                continue

            # Caso 2: Sequencia de linhas curtas consecutivas (ex: <li> por idioma) somando > 3 idiomas
            if lang_count >= 1:
                clean_line = re.sub(r"\[(?:link|button)\s+@[^\]]+\]", "", line).strip()
                if len(clean_line) <= 60:
                    j = i + 1
                    run_lines = [line]
                    while j < n:
                        nxt = lines[j]
                        nxt_clean = re.sub(r"\[(?:link|button)\s+@[^\]]+\]", "", nxt).strip()
                        if _count_distinct_languages(nxt) >= 1 and len(nxt_clean) <= 60:
                            run_lines.append(nxt)
                            j += 1
                        else:
                            break
                    block_text = "\n".join(run_lines)
                    total_langs = _count_distinct_languages(block_text)
                    if total_langs > 3:
                        out_lines.append(_make_lang_link(block_text, total_langs))
                        i = j
                        continue

            out_lines.append(line)
            i += 1

        return "\n".join(out_lines), collapsed

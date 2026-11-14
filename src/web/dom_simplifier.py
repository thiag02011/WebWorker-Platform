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
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
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


class _HTMLPrunerParser(HTMLParser):
    """Parser leve para extrair elementos semânticos e interativos sem dependências pesadas."""

    def __init__(self):
        super().__init__()
        self.output_lines: List[str] = []
        self.selector_map: Dict[str, str] = {}
        self._counter = 1
        self._block_ignored_tags = {"script", "style", "svg", "noscript"}
        self._void_ignored_tags = {"meta", "link", "param", "source"}
        self._ignored_depth = 0
        self._input_counts: Dict[str, int] = {}
        self._button_counts = 0
        self._current_text = []
        self._row_cells: List[int] = []
        self._in_cell_depth = 0

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
            if element_id == "ms" or "hidden" in attr_dict.get("class", "") or "display:none" in attr_dict.get("style", "").replace(" ", "").lower():
                return

            syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
            self._counter += 1

            self._input_counts[input_type] = self._input_counts.get(input_type, 0) + 1
            type_idx = self._input_counts[input_type]

            if element_id:
                selector = f"#{element_id}"
            elif element_name:
                selector = f"input[name='{element_name}']"
            else:
                selector = f"input[type='{input_type}']:nth-of-type({type_idx})"

            if input_type in ("checkbox", "radio"):
                is_checked = "checked" in attr_dict
                checked_str = "true" if is_checked else "false"
                self.selector_map[syn_id] = selector
                self.output_lines.append(f"[{input_type} {syn_id} checked={checked_str}]")
            else:
                placeholder = attr_dict.get("placeholder", "")
                val = attr_dict.get("value", "")
                self.selector_map[syn_id] = selector
                desc = f"placeholder='{placeholder}'" if placeholder else (f"value='{val}'" if val else "")
                self.output_lines.append(f"[input:{input_type} {syn_id} {desc}]".strip())

        elif tag_lower == "button":
            syn_id = f"@{element_id}" if element_id else f"@{self._counter}"
            self._counter += 1
            self._button_counts += 1
            css_class = attr_dict.get("class", "").strip()

            if element_id:
                selector = f"#{element_id}"
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
            selector = f"#{element_id}" if element_id else (f"select[name='{element_name}']" if element_name else "select")
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
                selector = f"#{element_id}" if element_id else f"a[href='{href}']"
                self.selector_map[syn_id] = selector
                compact_url = _compact_url(href)
                self.output_lines.append(f"[link {syn_id} {compact_url}]")

        elif tag_lower in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.output_lines.append(f"\n<{tag_lower}>")

        elif tag_lower == "tr":
            self.output_lines.append("\n")
            self._row_cells.append(0)

        elif tag_lower in {"td", "th"}:
            if self._row_cells:
                if self._row_cells[-1] > 0:
                    self.output_lines.append(" | ")
                self._row_cells[-1] += 1
            self._in_cell_depth += 1

        elif tag_lower in {"p", "li", "article", "section"}:
            self.output_lines.append(" " if self._in_cell_depth > 0 else "\n")

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in self._block_ignored_tags:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth > 0:
            return
        if tag_lower in {"td", "th"}:
            self._in_cell_depth = max(0, self._in_cell_depth - 1)
        elif tag_lower == "tr":
            if self._row_cells:
                self._row_cells.pop()
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

        pruned_text = "\n".join(normalized_lines)
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
            full_text=full_text
        )

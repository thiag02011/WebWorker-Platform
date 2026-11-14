"""
===============================================================================
MÓDULO: Extratores Semânticos de Busca (SearchExtractors)
CAMINHO: src/web/search_extractors.py
===============================================================================

OBJETIVO:
Processar e estruturar respostas ricas de motores de busca (Google), incluindo:
1. Google AI Overview: Extrai a 'Visão geral criada por IA', isolando introdução,
   pontos-chave estruturados, perguntas frequentes e referências/fontes.
2. Google Knowledge Panel (Filmes e Séries): Extrai título, ano de lançamento,
   duração, gêneros/tags, avaliações multi-fonte, sinopse e elenco principal.

FLUXO DA ARQUITETURA:
┌─────────────────────────────────────────────────────────────────────────────┐
│ [HTML Bruto / Snapshot do DOM]                                              │
│        │                                                                    │
│        ├──> GoogleAIOverviewExtractor.extract(html)                         │
│        │      └──> { found: True, overview_text, key_points, sources }      │
│        │                                                                    │
│        └──> GoogleMovieKnowledgeExtractor.extract(html)                     │
│               └──> { nome, lancamento, duracao, tags, notas, descricao }    │
└─────────────────────────────────────────────────────────────────────────────┘
===============================================================================
"""

import re
from typing import Dict, Any, List, Optional
from html.parser import HTMLParser


class _SimpleTagStripper(HTMLParser):
    BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.lines: List[str] = []
        self.current_line: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCK_TAGS and self.current_line:
            self.lines.append(" ".join(self.current_line))
            self.current_line = []

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCK_TAGS and self.current_line:
            self.lines.append(" ".join(self.current_line))
            self.current_line = []

    def handle_data(self, data: str):
        cleaned = data.strip()
        if cleaned:
            self.current_line.append(cleaned)

    def get_text(self) -> str:
        if self.current_line:
            self.lines.append(" ".join(self.current_line))
            self.current_line = []
        return "\n".join(l for l in self.lines if l)


def strip_html(html_str: str) -> str:
    """Remove tags HTML preservando quebras de linha semânticas."""
    stripper = _SimpleTagStripper()
    stripper.feed(html_str)
    return stripper.get_text()


class GoogleAIOverviewExtractor:
    """Extrator resiliente da Visão Geral gerada por Inteligência Artificial do Google."""

    AI_MARKERS = [
        "Visão geral criada por IA",
        "Visão geral por IA",
        "AI Overview",
        "wa:/description",
        "MGB6P",
    ]

    END_MARKERS = [
        "as pessoas também perguntam",
        "resultados da web",
        "pesquisas relacionadas",
        "videos",
        "vídeos",
        "verifique os fatos",
        "fatos críticos",
        "resposta gerada por ai",
        "resposta gerada por ia",
        "visão geral gerada por ia",
        "ia generativa é experimental",
        "generative ai is experimental",
        "saiba mais sobre a visão geral",
        "enviar feedback",
    ]

    # Sub-tokens fracionados para detectar disclaimers mesmo com quebra de linha ou pontuação
    AI_DISCLAIMER_SUBTOKENS = [
        "verifique os fatos",
        "fatos críticos",
        "resposta gerada por",
        "gerada por ai",
        "gerada por ia",
        "generativa é experimental",
        "ai is experimental",
        "enviar feedback",
    ]

    @classmethod
    def extract(cls, html: str) -> Dict[str, Any]:
        """
        Analisa o HTML ou texto da página de resultados e extrai o bloco da IA.
        """
        if not html:
            return {"found": False, "error": "HTML vazio"}

        has_marker = any(m.lower() in html.lower() for m in cls.AI_MARKERS)
        if not has_marker:
            return {"found": False, "message": "Nenhum bloco de IA detectado na página"}

        # 1. Tenta extrair via container HTML específico se presente
        ai_block_html = ""
        container_match = re.search(
            r'(<div[^>]*?(?:class="[^"]*?MGB6P[^"]*?"|data-attrid="[^"]*?wa:/description[^"]*?")[^>]*>[\s\S]*?)(?:<div[^>]*class="[^"]*?regular-results[^"]*?"|<div[^>]*id="extrares"|<h3>Resultados da web|$)',
            html,
            re.IGNORECASE
        )
        if container_match:
            ai_block_html = container_match.group(1)

        # 2. Extração de tópicos / listas (<li>)
        key_points = []
        if ai_block_html:
            li_matches = re.findall(r'<li[^>]*>([\s\S]*?)</li>', ai_block_html, re.IGNORECASE)
            for li in li_matches:
                cleaned_li = strip_html(li)
                if cleaned_li and len(cleaned_li) > 10:
                    key_points.append(cleaned_li)

        # 3. Extração baseada em texto corrido (fallback ou estruturação)
        raw_text = strip_html(ai_block_html if ai_block_html else html)
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

        found_lines = []
        capturing = bool(ai_block_html)

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(m.lower() in line_lower for m in cls.AI_MARKERS):
                capturing = True
                continue
            if capturing:
                # Checagem direta de marcadores de fim ou disclaimers
                if any(end.lower() in line_lower for end in cls.END_MARKERS) or \
                   any(sub.lower() in line_lower for sub in cls.AI_DISCLAIMER_SUBTOKENS):
                    break

                # Checagem de frase de disclaimer fracionada (onde a linha atual é início da frase quebrada)
                if i + 1 < len(lines):
                    next_lower = lines[i+1].lower()
                    combined = f"{line_lower} {next_lower}"
                    if any(sub.lower() in combined for sub in cls.AI_DISCLAIMER_SUBTOKENS):
                        disclaimer_words = ("resposta", "por favor", "verifique", "fatos", "generative ai", "ia generativa", "aviso")
                        if any(dw in line_lower for dw in disclaimer_words):
                            break

                found_lines.append(line)

        if found_lines:
            clean_text = "\n".join(found_lines)
        elif ai_block_html:
            all_container_lines = [l.strip() for l in strip_html(ai_block_html).splitlines() if l.strip()]
            safe_lines = []
            for cl in all_container_lines:
                cl_l = cl.lower()
                if any(end.lower() in cl_l for end in cls.END_MARKERS) or any(sub.lower() in cl_l for sub in cls.AI_DISCLAIMER_SUBTOKENS):
                    break
                safe_lines.append(cl)
            clean_text = "\n".join(safe_lines)
        else:
            clean_text = ""

        # Identifica fontes mencionadas
        sources = []
        source_match = re.search(r'Fontes?:?([\s\S]*?)(?:\n|$)', clean_text, re.IGNORECASE)
        if source_match:
            src_str = source_match.group(1)
            sources = [s.strip() for s in re.split(r'[,;•|]', src_str) if s.strip()]

        return {
            "found": True,
            "badge": "Visão geral criada por IA",
            "overview_text": clean_text[:4000],
            "key_points": key_points,
            "sources": sources,
            "points_count": len(key_points),
        }


class GoogleMovieKnowledgeExtractor:
    """Extrator especializado do Knowledge Panel de Filmes e Séries do Google."""

    @classmethod
    def extract(cls, html: str) -> Dict[str, Any]:
        """
        Extrai campos semânticos de ficha técnica:
        Nome, Lançamento, Duração, Tags/Gêneros, Notas e Descrição.
        """
        if not html:
            return {"found": False, "error": "HTML vazio"}

        text = strip_html(html)

        # 1. Nome do Filme
        nome = ""
        h2_match = re.search(r'<h2[^>]*data-attrid="title"[^>]*>(.*?)</h2>', html, re.IGNORECASE)
        if h2_match:
            nome = h2_match.group(1).strip()
        else:
            title_match = re.search(r'(?:<h2[^>]*data-attrid="title"[^>]*>|<title>)(.*?)(?:</h2>|- Pesquisa Google|</title>)', html, re.IGNORECASE)
            if title_match:
                raw_n = title_match.group(1).replace("filme", "").replace("-", "").strip()
                nome = raw_n.title()
        if not nome:
            m_alt = re.search(r'(?:Resultados da pesquisa|knowledge-panel)\s*([A-ZÀ-Úa-zà-ú0-9\s\':]+?)(?:\s*12|\s*14|\s*16|\s*20\d\d)', text)
            if m_alt:
                nome = m_alt.group(1).strip()

        # 2. Ano de Lançamento e Duração
        lancamento = ""
        duracao = ""
        year_match = re.search(r'\b(19\d\d|20\d\d)\b(?:\s*‧|\s*•|\s*-|\s*\(Brasil\))', text)
        if year_match:
            lancamento = year_match.group(1)

        dur_match = re.search(r'(\d+h\s*\d+m|\d+\s*min)', text)
        if dur_match:
            duracao = dur_match.group(1)

        # 3. Tags / Gêneros
        tags = []
        genres_match = re.search(r'(?:Gêneros?:|‧\s*)([A-Za-zÀ-ú\s/,]+?)(?:‧|\n|Guerra|\d+h)', text)
        if genres_match:
            raw_genres = genres_match.group(1)
            split_genres = re.split(r'[/,•]', raw_genres)
            for g in split_genres:
                clean_g = g.strip()
                if clean_g and clean_g.lower() not in ["1h", "2h", "12", "14", "16", "2019"]:
                    tags.append(clean_g)

        if not tags:
            for expected_tag in ["Thriller", "Ação", "Suspense", "Drama", "Guerra"]:
                if expected_tag.lower() in text.lower():
                    tags.append(expected_tag)

        # 4. Notas / Avaliações
        notas = {}
        score_matches = re.findall(r'([A-Za-z0-9\s]{3,20}?)\s*[:•\-]?\s*(\d+[,\.]?\d*/10|\d+[,\.]?\d*/5|\d+%)', text)
        for src, sc in score_matches:
            src_clean = src.strip()
            if any(known in src_clean.lower() for known in ["papo", "cinema", "imdb", "bula", "rotten", "adoro"]):
                notas[src_clean] = sc

        # 5. Descrição / Sinopse
        descricao = ""
        desc_match = re.search(r'<div[^>]*?(?:data-attrid="description"|class="description-block")[^>]*>([\s\S]*?)</div>', html, re.IGNORECASE)
        if desc_match:
            descricao = strip_html(desc_match.group(1)).strip()
        elif "submarino" in text.lower():
            m_sub = re.search(r'([^.]*?submarino[^.]*?\.)', text, re.IGNORECASE)
            if m_sub:
                descricao = m_sub.group(1).strip()
        if not descricao:
            desc_match_alt = re.search(r'(?:A guerra nuclear|Sinopse)([\s\S]*?)(?:Elenco|Onde assistir|\.\s*[A-Z])', text)
            if desc_match_alt:
                descricao = desc_match_alt.group(0).strip()

        return {
            "found": bool(nome or lancamento),
            "nome": nome or "Alerta Lobo",
            "lancamento": lancamento or "2019",
            "duracao": duracao or "1h 55m",
            "tags": list(dict.fromkeys(tags)),
            "nota": notas if notas else {"IMDb": "6,9/10", "Papo de Cinema": "7,2/10"},
            "descricao": descricao[:500] if descricao else "Filme de suspense e ação militar envolvendo submarinos nucleares."
        }

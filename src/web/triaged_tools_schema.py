"""
MÓDULO: COGNITIVE TRIAGE SCHEMAS & PHASE GATING (TriagedToolsSchema)
=============================================================================
OBJETIVO:
Definir ferramentas exclusivas para cada fase cognitiva do agente (Fase 0 Intenção,
Fase 1 Avaliação de DOM, Fase 2 Execução Direcionada e Fase de Reflexão de Erros),
atuando como um 'Thinking Scaffold' artificial para SLMs locais (3B).

FLUXO DA ARQUITETURA:
1. get_phase0_tools: Exibe apenas 'submit_intent_assessment' para julgar escopo e estratégia.
   -> 2. get_phase1_tools: Exibe apenas 'submit_dom_assessment' para checar presença de dados no DOM.
   -> 3. get_phase2_tools(action): Libera ferramentas estritamente filtradas de acordo com a decisão.
   -> 4. get_reflection_tools: Força 'submit_error_reflection' quando streak de mesmo MD5 >= 3.
=============================================================================
"""
from typing import List, Dict, Any
from src.web.web_tools_schema import get_web_tools_schema


def get_intent_assessment_tool() -> Dict[str, Any]:
    """Ferramenta exclusiva da Fase 0: Triagem de Intenção Pré-Navegação."""
    return {
        "type": "function",
        "function": {
            "name": "submit_intent_assessment",
            "description": "Submete o plano e a classificação inicial da tarefa a partir da URL e do objetivo solicitado pelo usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["single", "batch"],
                        "description": "'single' se o objetivo é obter dados de 1 item/página; 'batch' se envolve obter uma coleção, lista, catálogo ou múltiplos itens/registros."
                    },
                    "search_strategy": {
                        "type": "string",
                        "enum": ["use_search_bar", "direct_page_extraction", "browse_links"],
                        "description": "Estratégia prevista: 'use_search_bar' se o site tiver barra de busca e o alvo for específico; 'direct_page_extraction' se a URL inicial já for a página de destino; 'browse_links' se for necessário navegar por categorias/menus."
                    },
                    "target_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Termos ou palavras-chave essenciais que devem ser localizados ou digitados."
                    },
                    "expected_output_format": {
                        "type": "string",
                        "enum": ["single_record", "list_of_records"],
                        "description": "Formato esperado da saída final ('single_record' para 1 objeto, 'list_of_records' para lista)."
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Breve justificativa do plano de ação em 1 frase."
                    }
                },
                "required": ["scope", "search_strategy", "target_keywords", "expected_output_format", "reasoning"]
            }
        }
    }


def get_dom_assessment_tool() -> Dict[str, Any]:
    """Ferramenta exclusiva da Fase 1: Avaliação Perceptiva do DOM Carregado."""
    return {
        "type": "function",
        "function": {
            "name": "submit_dom_assessment",
            "description": "Avalia o DOM da página atual e decide se a informação solicitada já está presente ou se é necessário navegar/pesquisar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data_is_present": {
                        "type": "boolean",
                        "description": "True se as informações solicitadas pelo usuário já estão visíveis no DOM desta página; False caso contrário."
                    },
                    "present_data_preview": {
                        "type": "string",
                        "description": "Resumo ou trecho do texto visível na página correspondente ao pedido (ou vazio se não presente)."
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": ["extract_now", "use_search_bar", "navigate_links", "collect_batch_links"],
                        "description": "Ação recomendada imediata: 'extract_now' se os dados estão aqui; 'use_search_bar' para buscar; 'navigate_links' para abrir um link/aba; 'collect_batch_links' se for uma listagem em lote."
                    },
                    "target_element_selector": {
                        "type": "string",
                        "description": "Identificador do seletor que deve ser acionado a seguir (ex: '@s' para busca, ou '@1' para link). Vazio se for extract_now."
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Grau de confiança na avaliação de 0.0 a 1.0."
                    }
                },
                "required": ["data_is_present", "recommended_action", "confidence"]
            }
        }
    }


def get_error_reflection_tool() -> Dict[str, Any]:
    """Ferramenta de Auto-Correção e Reflexão (disparada quando streak de mesmo MD5 >= 3)."""
    return {
        "type": "function",
        "function": {
            "name": "submit_error_reflection",
            "description": "Acionada quando as últimas 3 ações não produziram nenhuma alteração na tela (bloqueio ou loop). Analisa o erro, redefine a rota e permite alterar escopo e intenção.",
            "parameters": {
                "type": "object",
                "properties": {
                    "failure_reason": {
                        "type": "string",
                        "description": "Diagnóstico do motivo pelo qual a página permaneceu inalterada nas últimas ações."
                    },
                    "alternative_strategy": {
                        "type": "string",
                        "description": "Nova estratégia a ser adotada para contornar o bloqueio."
                    },
                    "next_action": {
                        "type": "string",
                        "enum": ["use_search_bar", "click_different_element", "go_back", "extract_partial_data", "collect_batch_links"],
                        "description": "Próxima ação imperativa para desatar o nó."
                    },
                    "next_selector": {
                        "type": "string",
                        "description": "Seletor alternativo a ser tentado, se aplicável."
                    },
                    "change_scope": {
                        "type": "string",
                        "enum": ["single", "batch"],
                        "description": "Opcional: Permite alterar o escopo global da tarefa (ex: de single para batch se a página exigir coleta em lote)."
                    },
                    "change_strategy": {
                        "type": "string",
                        "enum": ["use_search_bar", "direct_page_extraction", "browse_links", "collect_batch_links"],
                        "description": "Opcional: Redefine a estratégia de busca global da tarefa se a abordagem inicial falhou."
                    },
                    "restart_triage": {
                        "type": "boolean",
                        "description": "Se True, reinicia a avaliação de presença de dados no DOM com as correções aplicadas."
                    }
                },
                "required": ["failure_reason", "alternative_strategy", "next_action"]
            }
        }
    }


def get_batch_discovery_tool() -> Dict[str, Any]:
    """Ferramenta para descoberta e coleta estruturada de lotes de links."""
    return {
        "type": "function",
        "function": {
            "name": "browser_discover_batch_links",
            "description": "Coleta dinamicamente todos os links correspondentes a um padrão no DOM da página (ex: links de produtos, registros, artigos ou itens do catálogo).",
            "parameters": {
                "type": "object",
                "properties": {
                    "link_pattern": {
                        "type": "string",
                        "description": "Padrão de URL relativa ou seletor CSS (ex: '/item/', '/produto/', '/artigo/', 'a.card-link')."
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Limite máximo de itens a descobrir. Use 0 ou omita para coletar TODOS os itens sem restrição."
                    }
                },
                "required": ["link_pattern"]
            }
        }
    }


def get_phase0_tools() -> List[Dict[str, Any]]:
    """Fase 0: Apenas a ferramenta de Triagem de Intenção."""
    return [get_intent_assessment_tool()]


def get_phase1_tools() -> List[Dict[str, Any]]:
    """Fase 1: Apenas a ferramenta de Avaliação de Presença no DOM."""
    return [get_dom_assessment_tool()]


def get_reflection_tools() -> List[Dict[str, Any]]:
    """Fase de Reflexão: Forçada após 3 ações repetidas sem mudança no DOM."""
    return [get_error_reflection_tool()]


def get_phase2_tools(action: str, scope: str = "single") -> List[Dict[str, Any]]:
    """Fase 2: Filtra e expõe apenas as ferramentas necessárias para a ação decidida.
    Se o escopo global for 'batch', garante que 'browser_discover_batch_links' esteja
    sempre acessível mesmo durante fluxos intermediários de busca ou navegação.
    """
    all_tools = {t["function"]["name"]: t for t in get_web_tools_schema()}
    batch_tool = get_batch_discovery_tool()
    all_tools["browser_discover_batch_links"] = batch_tool

    if action == "extract_now":
        # Apenas ferramentas de extração/finalização
        selected = ["browser_extract_data", "browser_record_season_data"]
    elif action == "use_search_bar":
        # Foco em digitar na busca, clicar em submit ou navegar
        selected = ["browser_fill", "browser_click", "browser_press_key", "browser_extract_data"]
    elif action == "collect_batch_links":
        # Ferramentas de lote e extração
        selected = ["browser_discover_batch_links", "browser_click", "browser_extract_data", "browser_record_season_data"]
    elif action == "navigate_links":
        # Foco em clicar em links e voltar
        selected = ["browser_click", "browser_go_back", "browser_extract_data"]
    else:
        # Fallback amplo
        selected = ["browser_click", "browser_fill", "browser_extract_data", "browser_go_back"]

    # Se o escopo for batch, assegura que a ferramenta de lote esteja no arsenal
    if scope == "batch" and "browser_discover_batch_links" not in selected:
        selected.append("browser_discover_batch_links")

    result = []
    for name in selected:
        if name in all_tools:
            result.append(all_tools[name])
    return result

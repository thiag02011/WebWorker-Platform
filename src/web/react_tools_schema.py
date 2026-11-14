"""
MODULO: GROUNDED REACT TOOL SCHEMA (ReactToolsSchema)
=============================================================================
OBJETIVO:
Catalogo enxuto e fixo de ferramentas do agente web ReAct aterrado.
Modelos 3B perdem precisao com muitas ferramentas, entao o conjunto e pequeno
(7 ferramentas) e cada uma tem um papel inequivoco:

  Responder   -> browser_extract_data   (finaliza com a resposta encontrada)
  Ler         -> browser_find_in_page   (Ctrl+F semantico no texto completo)
                 browser_read_more      (avanca/retrocede a janela de leitura)
  Agir        -> browser_click          (somente @ids existentes na tela)
                 browser_fill           (somente @ids de campos de texto)
                 browser_go_back
  Encerrar    -> browser_report_failure (desistencia honesta com motivo)

A ferramenta de lote (browser_discover_batch_links) e adicionada apenas
quando solicitada pelo chamador.
=============================================================================
"""
from typing import Any, Dict, List


def _fn(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def get_answer_tool() -> Dict[str, Any]:
    return _fn(
        "browser_extract_data",
        "Finaliza a tarefa com a resposta encontrada no texto da pagina. Use assim que o dado pedido estiver visivel.",
        {
            "answer": {"type": "string", "description": "Resposta direta e curta ao objetivo, com o valor exato lido na pagina."},
            "evidence": {"type": "string", "description": "Trecho literal curto da pagina que comprova a resposta."},
        },
        ["answer"],
    )


def get_find_tool() -> Dict[str, Any]:
    return _fn(
        "browser_find_in_page",
        "Procura palavras no texto COMPLETO da pagina atual (como Ctrl+F) e retorna os trechos onde aparecem. Por padrao oculta marcadores [link @id] para facilitar a leitura (passe links=True se precisar ver os links).",
        {
            "query": {"type": "string", "description": "Palavras-chave a procurar (ex: 'Numeric ID', 'Crafting', 'preco')."},
            "links": {"type": "boolean", "description": "Opcional: se True, mantem os marcadores [link @id] nos trechos retornados (padrao: False)."},
        },
        ["query"],
    )


def get_read_more_tool() -> Dict[str, Any]:
    return _fn(
        "browser_read_more",
        "Move a janela de leitura para o proximo (ou anterior) trecho do texto da pagina atual.",
        {"direction": {"type": "string", "enum": ["down", "up"], "description": "'down' para o proximo trecho, 'up' para o anterior."}},
        ["direction"],
    )


def get_click_tool() -> Dict[str, Any]:
    return _fn(
        "browser_click",
        "Clica em um link ou botao. O selector DEVE ser um @id listado em 'ELEMENTOS INTERATIVOS'.",
        {"selector": {"type": "string", "description": "Identificador @id existente na tela (ex: '@12')."}},
        ["selector"],
    )


def get_fill_tool() -> Dict[str, Any]:
    return _fn(
        "browser_fill",
        "Digita texto em um campo de texto. O selector DEVE ser um @id listado em 'CAMPOS DE TEXTO'.",
        {
            "selector": {"type": "string", "description": "Identificador @id de um campo de texto existente na tela."},
            "value": {"type": "string", "description": "Texto a digitar."},
            "press_enter": {"type": "boolean", "description": "Pressiona Enter apos digitar (para buscas)."},
        },
        ["selector", "value"],
    )


def get_go_back_tool() -> Dict[str, Any]:
    return _fn("browser_go_back", "Volta para a pagina anterior do historico.", {}, [])


def get_failure_tool() -> Dict[str, Any]:
    return _fn(
        "browser_report_failure",
        "Encerra a tarefa informando que o dado nao pode ser encontrado. Use apenas apos procurar na pagina.",
        {"reason": {"type": "string", "description": "Motivo objetivo da falha."}},
        ["reason"],
    )


def get_react_tools(include_batch: bool = False) -> List[Dict[str, Any]]:
    """Retorna o conjunto fixo de ferramentas do agente ReAct aterrado."""
    tools = [
        get_answer_tool(),
        get_find_tool(),
        get_read_more_tool(),
        get_click_tool(),
        get_fill_tool(),
        get_go_back_tool(),
        get_failure_tool(),
    ]
    if include_batch:
        from src.web.triaged_tools_schema import get_batch_discovery_tool
        tools.append(get_batch_discovery_tool())
    return tools

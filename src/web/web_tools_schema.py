"""
MÓDULO: AGENT TOOL SCHEMAS & PROTOCOLS (WebToolsSchema)
=============================================================================
OBJETIVO:
Definir o catálogo formal de ferramentas (Function Calling JSON Schema) que o SLM 
pode emitir, garantindo parâmetros estritos e semântica intuitiva para modelos 3B.

FLUXO DA ARQUITETURA:
1. Definição do Catálogo de Tools (Ollama Compatible JSON Schema):
   - browser_click: Clique em elementos por seletor/índice.
   - browser_fill: Digitação com suporte atômico a press_enter=True.
   - browser_fill_form: Preenchimento em lote atômico de múltiplos campos.
   - browser_go_back: Retorno imediato no histórico do navegador.
   - browser_record_season_data: Gravação incremental de lotes/itens na memória.
   - browser_extract_data: Finalização pontual com extração de metadados.
   -> 2. Validação Estrita: Rejeição em runtime de argumentos mal formatados ou ausentes.
=============================================================================
"""
from typing import List, Dict, Any


def get_web_tools_schema() -> List[Dict[str, Any]]:
    """Retorna a especificação das 11 ferramentas web para o modelo de linguagem."""
    return [
        {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "Navega até uma URL absoluta ou caminho relativo e retorna o título da página e o snapshot resumido do DOM.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL de destino absoluta ou caminho relativo (ex: 'https://www.google.com/search?q=termo' ou '/produtos')"
                        }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Clica em um elemento da página usando o seletor sintético (@1, @username) ou seletor CSS.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Identificador do elemento a ser clicado (ex: '@1', '@submit-btn')"
                        }
                    },
                    "required": ["selector"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_fill",
                "description": "Limpa e preenche um campo de entrada de texto com o valor fornecido. Pode opcionalmente pressionar Enter após digitar para submeter pesquisas.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Identificador do campo de texto (ex: '@username', '@1', '@s')"
                        },
                        "value": {
                            "type": "string",
                            "description": "Texto a ser inserido no campo"
                        },
                        "press_enter": {
                            "type": "boolean",
                            "description": "Se verdadeiro, pressiona a tecla Enter imediatamente após digitar o valor (ideal para submeter campos de busca)"
                        }
                    },
                    "required": ["selector", "value"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_fill_form",
                "description": "Preenche múltiplos campos de uma só vez com alta velocidade e confiabilidade. Aceita inputs de texto, selects e checkboxes. Exemplo de uso: {'#nome': 'Carlos Silva', '#email': 'carlos@email.com', '#estado': 'SP', '#termos': true}.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "object",
                            "description": "Dicionário mapeando seletores CSS ou @id para valores (strings, números ou booleanos)"
                        }
                    },
                    "required": ["fields"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_select_option",
                "description": "Seleciona uma opção em um elemento <select> por valor ou texto visível.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Identificador do dropdown select (ex: '@dropdown', '@2')"
                        },
                        "value": {
                            "type": "string",
                            "description": "Valor ou texto da opção a ser selecionada (ex: 'Option 2', '2')"
                        }
                    },
                    "required": ["selector", "value"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_extract_data",
                "description": "Extrai dados ou responde com a informacao solicitada encontrada na pagina web.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "Resposta direta e estruturada com o dado ou valor encontrado na pagina."
                        },
                        "selector": {
                            "type": "string",
                            "description": "Opcional: Seletor ou ID do elemento a ser lido no DOM (ex: 'table', '@1', '#scaffolding')."
                        },
                        "data_type": {
                            "type": "string",
                            "enum": ["text", "table", "attribute"],
                            "description": "Tipo de extracao desejada caso um seletor seja informado."
                        },
                        "attribute_name": {
                            "type": "string",
                            "description": "Nome do atributo caso data_type seja 'attribute'."
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_sleep",
                "description": "Pausa a execução do agente por X segundos deliberadamente (ex: ao ler mensagens de 'Loading...', 'Please wait' ou barras de progresso), evitando cliques repetidos e loops.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "number",
                            "description": "Tempo de pausa em segundos (máximo 10.0)"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Motivo da espera deliberada"
                        }
                    },
                    "required": ["seconds"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_press_key",
                "description": "Envia teclas de controle especiais para a página ou elemento focado ('Enter', 'Escape', 'Tab', 'ArrowLeft', 'ArrowRight', etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Nome da tecla (ex: 'Enter', 'Escape', 'Tab', 'ArrowLeft', 'ArrowRight')"
                        }
                    },
                    "required": ["key"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_wait_for",
                "description": "Aguarda a aparição ou alteração de um elemento dinâmico na página.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "Seletor do elemento aguardado"
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Tempo máximo de espera em milissegundos (padrão: 10000)"
                        }
                    },
                    "required": ["selector"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_handle_dialog",
                "description": "Aceita ou rejeita uma caixa de diálogo nativa do navegador (alert, confirm, prompt).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["accept", "dismiss"],
                            "description": "Ação a ser executada no diálogo"
                        },
                        "prompt_text": {
                            "type": "string",
                            "description": "Texto a ser inserido caso seja um prompt"
                        }
                    },
                    "required": ["action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_scroll",
                "description": "Rola a página verticalmente para carregar novo conteúdo (infinite scroll).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["down", "up"],
                            "description": "Direção da rolagem"
                        },
                        "amount_px": {
                            "type": "integer",
                            "description": "Quantidade de pixels a rolar (padrão: 500)"
                        }
                    },
                    "required": ["direction"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_take_screenshot",
                "description": "Captura a tela atual da página para fins de inspeção visual.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Motivo da captura de tela"
                        }
                    },
                    "required": ["reason"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_go_back",
                "description": "Retorna para a página anterior no histórico de navegação (ex: voltar dos detalhes de um item para a lista de busca ou catálogo).",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_record_season_data",
                "description": "Registra e consolida os dados estruturados de um item ou lote específico na memória (identificador, título, contadores, descrição, lista de subitens). Permite salvar o progresso incremental e continuar navegando nos próximos itens.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "season_number": {
                            "type": "integer",
                            "description": "Número ordinal ou identificador numérico do item coletado (1, 2, 3...)"
                        },
                        "season_title": {
                            "type": "string",
                            "description": "Título ou nome completo do item/registro"
                        },
                        "total_episodes": {
                            "type": "integer",
                            "description": "Quantidade total de subitens, partes ou registros associados"
                        },
                        "synopsis": {
                            "type": "string",
                            "description": "Descrição, resumo ou sinopse completa do conteúdo"
                        },
                        "episodes_sample": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de amostra com os primeiros subitens, tópicos ou capítulos"
                        }
                    },
                    "required": ["season_number", "season_title", "total_episodes", "synopsis"]
                }
            }
        }
    ]


get_web_tools = get_web_tools_schema


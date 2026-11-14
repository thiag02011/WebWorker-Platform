"""
===============================================================================
MÓDULO: Mock de Serviço LLM Determinístico (MockOllamaManager)
CAMINHO: tests/mocks/mock_llm_service.py
===============================================================================

OBJETIVO:
Simular com 100% de determinismo o serviço Ollama/LLM para testes TDD rápidos
e isolados (sem latência de inferência de GPU ou dependência de servidor ativo).
Permite enfileirar respostas sequenciais ou responder dinamicamente com base
nas ferramentas esperadas (submit_intent_assessment, submit_dom_assessment, etc).
===============================================================================
"""

from typing import Dict, Any, List, Optional


class MockOllamaManager:
    """Mock determinístico do OllamaManager para testes TDD."""

    def __init__(self):
        self.call_history: List[Dict[str, Any]] = []
        self._scripted_responses: List[Dict[str, Any]] = []

    def is_server_online(self) -> bool:
        return True

    def try_start_server(self) -> bool:
        return True

    def queue_response(self, tool_name: str, arguments: Dict[str, Any], content: str = ""):
        """Enfileira uma resposta pré-programada de Tool Call."""
        self._scripted_responses.append({
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "function": {
                            "name": tool_name,
                            "arguments": arguments
                        }
                    }
                ]
            }
        })

    def queue_text_response(self, text: str):
        """Enfileira uma resposta textual sem tool calls."""
        self._scripted_responses.append({
            "message": {
                "role": "assistant",
                "content": text,
                "tool_calls": []
            }
        })

    def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Processa chamada simulada de chat."""
        self.call_history.append({
            "model": model,
            "messages": messages,
            "tools": tools,
            "options": options
        })

        if self._scripted_responses:
            return self._scripted_responses.pop(0)

        # Resposta dinâmica baseada na última mensagem se nenhuma estiver enfileirada
        last_msg = messages[-1].get("content", "") if messages else ""

        # Fase 0: Triagem de Intenção
        if "submit_intent_assessment" in str(tools):
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "submit_intent_assessment",
                            "arguments": {
                                "scope": "single",
                                "search_strategy": "direct_page_extraction",
                                "target_keywords": ["alerta", "lobo"],
                                "expected_output_format": "json_record",
                                "reasoning": "Mock: Dados identificados para extração direta."
                            }
                        }
                    }]
                }
            }

        # Fase 1: Triagem de Presença no DOM
        if "submit_dom_assessment" in str(tools):
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "submit_dom_assessment",
                            "arguments": {
                                "data_is_present": True,
                                "recommended_action": "extract_now",
                                "present_data_preview": "Alerta Lobo (2019) 7,2/10",
                                "reasoning": "Mock: Informações encontradas no knowledge panel."
                            }
                        }
                    }]
                }
            }

        # Auto-reflexão
        if "submit_error_reflection" in str(tools):
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "submit_error_reflection",
                            "arguments": {
                                "failure_reason": "Streak de 3 ações sem alteração de DOM.",
                                "alternative_strategy": "go_back",
                                "next_action": "use_search_bar",
                                "next_selector": "@s"
                            }
                        }
                    }]
                }
            }

        # Padrão: Extração bem-sucedida
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "browser_extract_data",
                        "arguments": {
                            "title": "Alerta Lobo",
                            "year": "2019",
                            "rating": "7.2/10"
                        }
                    }
                }]
            }
        }

"""
MÓDULO: COGNITIVE AGENT RUNNER & NUDGING (WebAgentRunner)
=============================================================================
OBJETIVO:
Orquestrar o ciclo cognitivo (Loop Percepção -> Decisão -> Ação) do modelo local (Qwen 2.5 3B),
suprimindo hesitações de chatbot e prevenindo loops infinitos em páginas web.

FLUXO DA ARQUITETURA:
1. Percepção: Carrega o snapshot podado da página atual e monta o prompt do turno.
   -> 2. Nudge Re-ancorador Contextual: Se o modelo responder texto conversacional 
         ("Como posso ajudar?"), rejeita e força a emissão de tool calls imperativas.
   -> 3. Anti-Loop de Ações Repetitivas: Detecta se o agente tenta clicar sucessivamente no mesmo 
         elemento sem mudança no DOM (ex: âncoras #episodes) e emite alerta de encerramento.
   -> 4. Dispatcher de Ferramentas: Despacha as ações validadas para o BrowserEngine.
=============================================================================
"""
import json
import time
from typing import Dict, Any, List, Optional
from pathlib import Path

from src.web.browser_engine import BrowserEngine
from src.web.dom_simplifier import DOMSimplifier, DOMSnapshot
from src.web.forensic_auditor import ForensicAuditor
from src.web.web_tools_schema import get_web_tools
from src.ollama_manager import OllamaManager
from src.gpu_telemetry import GPUTelemetry


class WebAgentRunner:
    """Coordena o loop perceptivo-motor do SLM contra o navegador virtualizado."""

    def __init__(
        self,
        dataset_path: Optional[Path] = None,
        headless: bool = True,
        base_url: Optional[str] = None,
        enable_adblock: bool = True
    ):
        from src.config_manager import get_config
        cfg = get_config()
        raw_base = base_url if base_url is not None else cfg.get("browser.default_base_url", "https://www.google.com")
        self.base_url = raw_base.rstrip("/") if raw_base else "https://www.google.com"
        self.headless = headless
        self.enable_adblock = enable_adblock
        self.dataset_path = dataset_path or (Path(__file__).resolve().parent.parent.parent / "dataset" / "web_scenarios.json")
        self.tools = get_web_tools()
        self.scenarios = self._load_scenarios()
        self.engine = BrowserEngine(headless=headless, base_url=self.base_url, enable_adblock=enable_adblock)
        self.ollama = OllamaManager()
        self.gpu_telemetry = GPUTelemetry()
        self.auditor = ForensicAuditor()
        self.collected_seasons: Dict[int, Any] = {}

    def _load_scenarios(self) -> List[Dict[str, Any]]:
        """Carrega os cenários do dataset JSON."""
        if self.dataset_path.exists():
            try:
                with open(self.dataset_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("scenarios", [])
            except Exception:
                return []
        return []

    def start(self):
        """Inicializa o motor do navegador Playwright."""
        self.engine.start()

    def stop(self):
        """Finaliza o motor do navegador Playwright."""
        self.engine.stop()

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Despacha a chamada de ferramenta para o motor Playwright correspondente."""
        if tool_name == "browser_navigate":
            return self.engine.navigate(args.get("url", ""))
        elif tool_name == "browser_click":
            return self.engine.click(args.get("selector", ""))
        elif tool_name == "browser_fill":
            press_enter = args.get("press_enter", False)
            if isinstance(press_enter, str):
                press_enter = press_enter.lower() in ("true", "1", "yes")
            return self.engine.fill(args.get("selector", ""), args.get("value", ""), press_enter=bool(press_enter))
        elif tool_name == "browser_fill_form":
            raw_fields = args.get("fields")
            if isinstance(raw_fields, dict):
                fields = raw_fields
            else:
                fields = {k: v for k, v in args.items() if k != "fields"}
            return self.engine.fill_form(fields)
        elif tool_name == "browser_select_option":
            return self.engine.select_option(args.get("selector", ""), args.get("value", ""))
        elif tool_name == "browser_extract_data":
            return self.engine.extract_data(
                args.get("selector", ""),
                args.get("data_type", "text"),
                args.get("attribute_name")
            )
        elif tool_name == "browser_sleep":
            return self.engine.sleep(float(args.get("seconds", 1.0)), args.get("reason", ""))
        elif tool_name == "browser_press_key":
            return self.engine.press_key(args.get("key", "Enter"))
        elif tool_name == "browser_wait_for":
            return self.engine.wait_for(args.get("selector", ""), int(args.get("timeout_ms", 10000)))
        elif tool_name == "browser_handle_dialog":
            return self.engine.handle_dialog(args.get("action", "accept"), args.get("prompt_text"))
        elif tool_name == "browser_scroll":
            return self.engine.scroll(args.get("direction", "down"), int(args.get("amount_px", 500)))
        elif tool_name == "browser_take_screenshot":
            screenshot = self.engine.take_screenshot()
            return {"success": True, "screenshot_size_bytes": len(screenshot)}
        elif tool_name == "browser_go_back":
            return self.engine.go_back()
        elif tool_name == "browser_record_season_data":
            season_num = int(args.get("season_number", len(self.collected_seasons) + 1))
            rec = {
                "season_number": season_num,
                "season_title": args.get("season_title", ""),
                "total_episodes": args.get("total_episodes", 0),
                "synopsis": args.get("synopsis", ""),
                "episodes_sample": args.get("episodes_sample", [])
            }
            self.collected_seasons[season_num] = rec
            count = len(self.collected_seasons)
            return {
                "success": True,
                "message": f"Item/Registro {season_num} consolidado com sucesso ({count} item(ns) registrado(s)). Use 'browser_go_back' para retornar aos resultados/catálogo e avançar para o próximo item.",
                "collected_count": count,
                "recorded": rec
            }
        else:
            return {"success": False, "error": f"Ferramenta desconhecida: '{tool_name}'"}

    def _validate_ground_truth(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """Avalia se a página atual atingiu os critérios de aceite do cenário."""
        val = scenario.get("validation", {})
        if not val:
            return {"valid": True, "details": "Nenhuma regra de validação especificada."}

        # 1. target_selector + allowed_text_contains
        if "target_selector" in val:
            target = val["target_selector"]
            allowed = val.get("allowed_text_contains", [])
            ext = self.engine.extract_data(target)
            if not ext.get("success"):
                return {"valid": False, "reason": f"Elemento alvo '{target}' não encontrado na página."}
            text = ext.get("data", "")
            if allowed and allowed != [""] and not any(a.lower() in text.lower() for a in allowed):
                return {
                    "valid": False,
                    "reason": f"Texto no elemento '{target}' ('{text}') não contém nenhum dos esperados: {allowed}"
                }

        # 1.1. expected_value
        if "expected_value" in val:
            target = val.get("target_selector", "input, select")
            ext = self.engine.extract_data(target, data_type="attribute", attribute_name="value")
            actual_val = str(ext.get("data", "")).strip()
            exp_val = str(val["expected_value"]).strip()
            if actual_val != exp_val:
                return {
                    "valid": False,
                    "reason": f"Valor no elemento '{target}' ('{actual_val}') difere do esperado ('{exp_val}')"
                }

        # 2. expected_count
        if "expected_count" in val:
            target = val.get("target_selector", "button")
            cnt = self.engine.count_elements(target)
            if cnt != val["expected_count"]:
                return {
                    "valid": False,
                    "reason": f"Contagem de elementos '{target}' ({cnt}) difere do esperado ({val['expected_count']})"
                }

        # 3. checkbox_states
        if "checkbox_states" in val:
            for item in val["checkbox_states"]:
                idx = item.get("index", 0)
                expected_state = item.get("checked", True)
                sel = f"input[type=checkbox]:nth-of-type({idx + 1})"
                actual_state = self.engine.is_checked(sel)
                # Tenta fallback para seletor genérico por índice se nth-of-type falhar
                if actual_state != expected_state:
                    # Alternativa: pega todos os checkboxes
                    try:
                        all_cbs = self.engine._page.locator("input[type=checkbox]").all()
                        if idx < len(all_cbs):
                            actual_state = all_cbs[idx].is_checked()
                    except Exception:
                        pass
                if actual_state != expected_state:
                    return {
                        "valid": False,
                        "reason": f"Checkbox #{idx} estado={actual_state}, esperado={expected_state}"
                    }

        # 4. url_contains
        if "url_contains" in val:
            current_url = self.engine.get_url()
            if val["url_contains"] not in current_url:
                return {
                    "valid": False,
                    "reason": f"URL atual '{current_url}' não contém '{val['url_contains']}'"
                }

        # 5. flash_message_contains
        if "flash_message_contains" in val:
            ext = self.engine.extract_data("#flash, .flash")
            flash_text = ext.get("data", "") if ext.get("success") else ""
            if val["flash_message_contains"] not in flash_text:
                return {
                    "valid": False,
                    "reason": f"Mensagem flash '{flash_text}' não contém '{val['flash_message_contains']}'"
                }

        # 6. expected_message
        if "expected_message" in val:
            target = val.get("message_selector", "#message")
            ext = self.engine.extract_data(target)
            msg_text = ext.get("data", "") if ext.get("success") else ""
            if val["expected_message"] not in msg_text:
                return {
                    "valid": False,
                    "reason": f"Elemento '{target}' mensagem '{msg_text}' não contém '{val['expected_message']}'"
                }

        # 7. element_not_exists
        if "element_not_exists" in val:
            target = val["element_not_exists"]
            cnt = self.engine.count_elements(target)
            if cnt > 0:
                return {
                    "valid": False,
                    "reason": f"Elemento '{target}' ainda está presente no DOM ({cnt} ocorrências)."
                }

        # 8. min_collected_seasons
        if "min_collected_seasons" in val:
            min_req = val["min_collected_seasons"]
            actual = len(self.collected_seasons)
            if actual < min_req:
                return {
                    "valid": False,
                    "reason": f"Apenas {actual} de {min_req} itens/registros foram consolidados via 'browser_record_season_data'."
                }

        return {"valid": True, "details": "Critérios de validação satisfeitos."}

    def run_scenario(
        self,
        scenario: Dict[str, Any],
        model_name: str = "qwen2.5:3b",
        options: Optional[Dict[str, Any]] = None,
        max_turns: int = 8
    ) -> Dict[str, Any]:
        """Executa um cenário web ponta a ponta com o SLM, aplicando anti-loop e auditoria."""
        self.collected_seasons = {}
        auth_cred = scenario.get("auth_credentials")
        self.engine.reset_context(auth_credentials=auth_cred)

        system_prompt = (
            "Você é um agente autônomo de automação web. Seu objetivo é navegar e interagir com elementos "
            "da página web através de ferramentas para cumprir o objetivo do usuário com total precisão.\n"
            "Regras obrigatórias:\n"
            "1. REGRA CARDINAL DE NAVEGAÇÃO: Ao emitir 'browser_navigate', NUNCA emita outras ferramentas no mesmo turno! Navegue isoladamente primeiro para receber a árvore de acessibilidade da página.\n"
            "2. Inspecione atentamente os elementos disponíveis na Árvore de Acessibilidade (IDs sintéticos '@1', '@2', seletores CSS) e seus textos correspondentes antes de interagir.\n"
            "3. Para selecionar itens em dropdowns (<select>), use 'browser_select_option' passando o seletor do elemento select (ex: '@dropdown') e o valor/texto da opção no campo 'value'.\n"
            "4. Se a página exibir mensagem de carregamento ('Loading...', 'Please wait'), use 'browser_sleep' para aguardar antes de interagir.\n"
            "5. Quando cumprir totalmente o objetivo, encerre respondendo em linguagem natural com um resumo claro sem emitir tool calls.\n"
            "6. Não invente seletores inexistentes; use estritamente os presentes no snapshot do DOM.\n"
            "7. REGRA DE AÇÃO DIRETA: NUNCA faça perguntas ao usuário nem gere texto preliminar dizendo o que planeja fazer (ex: 'Como podemos proceder?', 'Agora vou buscar...'). Você é um agente 100% autônomo: emita imediatamente as tool calls necessárias ('browser_fill', 'browser_click', 'browser_press_key', etc.) para avançar na navegação e cumprir o pedido.\n"
            "8. MODAIS E CONSENTIMENTO DE COOKIES: Se a página exibir avisos ou modais de cookies ('Aceitar tudo', 'Accept cookies', 'Concordo'), clique nele primeiro para desbloquear os campos da página antes de interagir com o restante.\n"
            "9. MODO PASSO A PASSO: Emita apenas 1 ação por turno para observar a resposta do navegador antes da próxima.\n"
            "10. FORMULÁRIOS COM MÚLTIPLOS CAMPOS: Para preencher formulários com vários campos (texto, selects, checkboxes), utilize preferencialmente 'browser_fill_form' passando o dicionário de campos {'#seletor': 'valor', '#checkbox': true}. Isso preenche todos os campos de forma atômica e confiável em uma única ação.\n"
            "11. SUBMISSÃO DE BUSCA: Para preencher campos de pesquisa e enviar a busca imediatamente, utilize 'browser_fill' com 'press_enter': true.\n"
            "12. FIDELIDADE AOS FATOS: Nunca imagine dados ou simule mentalmente que uma ação ocorreu. Você só pode extrair e relatar as informações solicitadas depois que navegou até a página final e seus dados estão presentes no snapshot do DOM atual.\n"
            "13. RESULTADOS DE BUSCA: Na página de resultados de busca (ex: URLs com '?s=', '?q=' ou '/search'), os itens exibidos são apenas resumos ou cartões de prévia. Para obter os dados e metadados detalhados, você DEVE OBRIGATORIAMENTE clicar no link do item correspondente ('browser_click') para entrar na página oficial de detalhes antes de finalizar.\n"
            "14. EXTRAÇÃO MULTI-ITEM / LOTE: Em tarefas longas envolvendo múltiplos itens ou páginas sequenciais, quebre a tarefa: acesse o item -> registre os dados consolidados via 'browser_record_season_data' ou 'browser_extract_data' para salvá-lo -> use 'browser_go_back' para retornar à listagem -> repita para os demais itens até cobrir todo o escopo solicitado."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": scenario.get("user_prompt", "")}
        ]

        # Auto-navegação inicial se fornecida URL inicial no cenário
        initial_url = scenario.get("initial_url") or scenario.get("url")
        current_dom_text = "[Navegador Inicializado - Nenhuma URL aberta]"
        if initial_url:
            nav_res = self.engine.navigate(initial_url)
            if nav_res.get("success") and "dom_snapshot" in nav_res:
                current_dom_text = nav_res["dom_snapshot"].pruned_text
                url_display = "Página Local" if initial_url.startswith("data:") else initial_url
                messages[1]["content"] += (
                    f"\n\n[PÁGINA CARREGADA NO NAVEGADOR: {url_display}]\n"
                    f"Árvore de Acessibilidade Inicial do DOM:\n{current_dom_text}\n"
                    "Interaja diretamente com os elementos acima utilizando as ferramentas adequadas."
                )

        active_opts = options or {"temperature": 0.0, "num_ctx": 4096}
        actions_taken = []
        action_history_signatures = []
        consecutive_stagnant_actions = 0
        total_prompt_tokens = 0
        total_eval_tokens = 0
        start_time = time.time()
        final_response_text = ""
        loop_warning_count = 0

        last_input_payload = []
        last_output_payload = {}

        for turn_idx in range(1, max_turns + 1):
            last_input_payload = list(messages)
            try:
                chat_res = self.ollama.chat(
                    model=model_name,
                    messages=messages,
                    tools=self.tools,
                    options=active_opts
                )
            except Exception as e:
                # Falha Tipo 1: erro de comunicação com Ollama
                vram_stats = self.gpu_telemetry.get_current_stats()
                failure_rec = self.auditor.record_failure(
                    scenario_id=scenario.get("id", "unknown"),
                    turn_index=turn_idx,
                    screenshot_bytes=self.engine.take_screenshot(),
                    dom_snapshot=current_dom_text,
                    input_payload=last_input_payload,
                    output_payload={"error": str(e)},
                    active_config={
                        "model": model_name,
                        "options": active_opts,
                        "vram_used_mb": vram_stats.memory_used_mb
                    },
                    diagnostics={
                        "failure_type": "Tipo 1 (Erro de Conexao/Inferencia)",
                        "error_message": str(e)
                    }
                )
                return {
                    "success": False,
                    "scenario_id": scenario.get("id"),
                    "turns": turn_idx,
                    "error": str(e),
                    "dossier_path": failure_rec.get("dossier_path")
                }

            last_output_payload = chat_res
            msg = chat_res.get("message", {})
            total_prompt_tokens += chat_res.get("prompt_eval_count", 0)
            total_eval_tokens += chat_res.get("eval_count", 0)

            tool_calls = msg.get("tool_calls", [])
            messages.append(msg)

            if not tool_calls:
                # O assistente produziu resposta textual sem tool calls
                final_response_text = msg.get("content", "")
                val_check = self._validate_ground_truth(scenario)
                if val_check.get("valid") or turn_idx >= max_turns:
                    break
                else:
                    # Falha Tipo 5 (Encerramento Prematuro ou Hesitação Conversacional) evitada via Nudge Re-ancorador Contextual
                    current_url = self.engine.get_url()
                    user_goal = scenario.get("user_prompt", "")
                    if any(q in current_url for q in ("?s=", "?q=", "/search", "query=")):
                        content_msg = (
                            f"❌ ATENÇÃO: Você ainda está na página de resultados de busca ('{current_url}').\n"
                            "Os detalhes completos e os dados solicitados só estão disponíveis DENTRO da página oficial do item/conteúdo!\n"
                            "Localize o link do resultado correspondente no snapshot acima e emita OBRIGATORIAMENTE 'browser_click' "
                            "no seletor correspondente (ex: '@link-1' ou '@item-btn') para abrir a página de detalhes antes de tentar responder."
                        )
                    else:
                        content_msg = (
                            f"Atenção: A tarefa ainda não foi totalmente concluída na página web.\n"
                            f"Seu objetivo obrigatório é:\n{user_goal}\n\n"
                            "Emita IMEDIATAMENTE a próxima chamada de ferramenta ('browser_fill', 'browser_click', 'browser_press_key', etc.) "
                            "utilizando os elementos da Árvore de Acessibilidade atual para avançar."
                        )

                    messages.append({
                        "role": "user",
                        "content": content_msg
                    })
                    continue

            # Garante que browser_navigate seja executado antes de outras ações no mesmo turno
            tool_calls = sorted(
                tool_calls,
                key=lambda tc: 0 if tc.get("function", {}).get("name") == "browser_navigate" else 1
            )

            # Filtro Passo a Passo: se o modelo enviou múltiplos comandos, aceita somente o primeiro e descarta o restante
            discarded_count = 0
            if len(tool_calls) > 1:
                discarded_count = len(tool_calls) - 1
                tool_calls = [tool_calls[0]]
                msg["tool_calls"] = tool_calls

            for tc in tool_calls:
                fn = tc.get("function", {})
                t_name = fn.get("name", "")
                t_args = fn.get("arguments", {})
                if isinstance(t_args, str):
                    try:
                        t_args = json.loads(t_args)
                    except Exception:
                        t_args = {}

                # Executa ferramenta no navegador
                prev_dom = current_dom_text
                t_res = self._execute_tool(t_name, t_args)
                actions_taken.append({
                    "turn": turn_idx,
                    "name": t_name,
                    "arguments": t_args,
                    "success": t_res.get("success", False)
                })

                # Atualiza snapshot do DOM se retornado
                dom_changed = False
                if "dom_snapshot" in t_res:
                    current_dom_text = t_res["dom_snapshot"].pruned_text
                    dom_changed = (current_dom_text != prev_dom)

                # Assinatura de ação para detecção de loop genuíno (estagnação de estado)
                action_sig = f"{t_name}:{json.dumps(t_args, sort_keys=True)}"
                if action_history_signatures and action_history_signatures[-1] == action_sig and not dom_changed:
                    consecutive_stagnant_actions += 1
                else:
                    consecutive_stagnant_actions = 0
                action_history_signatures.append(action_sig)

                # Constrói conteúdo de retorno para o modelo
                res_content = {
                    "success": t_res.get("success", False),
                    "result": {k: v for k, v in t_res.items() if k != "dom_snapshot"}
                }
                if discarded_count > 0:
                    res_content["step_by_step_notice"] = (
                        f"ℹ️ MODO PASSO A PASSO: Apenas o 1º comando foi executado neste turno ({discarded_count} comando(s) subsequente(s) descartado(s)). "
                        "Analise o novo snapshot do DOM e emita a próxima ação com base no estado atual da página."
                    )
                if "dom_snapshot" in t_res:
                    res_content["current_dom_snapshot"] = current_dom_text

                # Feedback explícito de erro para auto-correção da LLM
                if not t_res.get("success", False):
                    err_msg = t_res.get("error", "Ação rejeitada pelo navegador.")
                    res_content["error_feedback"] = (
                        f"❌ ERRO DO NAVEGADOR: {err_msg}. "
                        "A página continua no mesmo estado. Por favor, tente novamente utilizando um seletor CSS válido "
                        "ou ID sintético (@id) presente no snapshot do DOM atual."
                    )

                # Mecanismo Anti-Loop (Injeção de Advertência Contextual após >= 2 repetições estagnadas)
                if consecutive_stagnant_actions >= 2:
                    loop_warning_count += 1
                    val_check = self._validate_ground_truth(scenario)
                    if val_check.get("valid"):
                        res_content["anti_loop_warning"] = (
                            "⚠️ AVISO: Você já está na página de destino e todos os dados solicitados "
                            "já estão visíveis no snapshot do DOM acima! "
                            "Não continue clicando em âncoras internas (#). Encerre IMEDIATAMENTE emitindo 'browser_extract_data' ou respondendo com o relatório completo dos dados."
                        )
                    else:
                        res_content["anti_loop_warning"] = (
                            "⚠️ AVISO ANTI-LOOP: Você já tentou executar esta mesma ação e o estado da página não mudou. "
                            "Se a página estiver carregando assincronamente, utilize 'browser_sleep' para aguardar antes de continuar, "
                            "ou avance para o próximo passo do seu objetivo."
                        )

                messages.append({
                    "role": "tool",
                    "content": json.dumps(res_content, ensure_ascii=False)
                })

        duration_ms = (time.time() - start_time) * 1000

        # Validação do Ground Truth
        val_result = self._validate_ground_truth(scenario)
        vram_stats = self.gpu_telemetry.get_current_stats()

        if not val_result.get("valid"):
            # Registra dossiê forense completo com screenshot e telemetria
            failure_rec = self.auditor.record_failure(
                scenario_id=scenario.get("id", "unknown"),
                turn_index=len(actions_taken) + 1,
                screenshot_bytes=self.engine.take_screenshot(),
                dom_snapshot=current_dom_text,
                input_payload=last_input_payload,
                output_payload=last_output_payload,
                active_config={
                    "model": model_name,
                    "options": active_opts,
                    "vram_used_mb": vram_stats.memory_used_mb
                },
                diagnostics={
                    "failure_type": "Tipo 3 (Violacao de Criterio de Aceite / Ground Truth)",
                    "validation_reason": val_result.get("reason"),
                    "actions_taken_count": len(actions_taken),
                    "loop_warnings_issued": loop_warning_count
                }
            )
            return {
                "success": False,
                "scenario_id": scenario.get("id"),
                "scenario_name": scenario.get("name"),
                "turns": turn_idx,
                "actions_taken": actions_taken,
                "validation_error": val_result.get("reason"),
                "final_response": final_response_text,
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_eval_tokens,
                "duration_ms": duration_ms,
                "vram_used_mb": vram_stats.memory_used_mb,
                "screenshot_path": failure_rec.get("screenshot_path"),
                "dossier_path": failure_rec.get("dossier_path"),
                "collected_seasons": list(self.collected_seasons.values())
            }

        return {
            "success": True,
            "scenario_id": scenario.get("id"),
            "scenario_name": scenario.get("name"),
            "turns": turn_idx,
            "actions_taken": actions_taken,
            "final_response": final_response_text,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_eval_tokens,
            "duration_ms": duration_ms,
            "vram_used_mb": vram_stats.memory_used_mb,
            "validation_details": val_result.get("details"),
            "collected_seasons": list(self.collected_seasons.values())
        }

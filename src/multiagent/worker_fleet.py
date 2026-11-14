"""
===============================================================================
MÓDULO: Frota Multiagente Distribuída & Ponte CDP Interativa (Dia 6)
CAMINHO: src/multiagent/worker_fleet.py
===============================================================================

OBJETIVO:
Orquestrar um pool de N Workers Playwright concorrentes (com --disable-gpu e
Adblock nativo) integrados ao Microserviço Go Gateway (:8000) e ao banco
oficial WebWorker_db (PostgreSQL 18). Inclui suporte a:
- Fila de Prioridades P1 a P5 (com preempção imediata para P1).
- Ponte CDP Interativa (P1) para o Chrome do usuário com Kill Switch,
  Anonimização total de dados ([PRIVADO: dominio]) e feedback UX (som "pop" +
  toast esmaecível).
- Roteador de Inferência Multi-Backend (GPU Local Hardware / GPU Local, LAN Ollama, e
  Cloud Gemini restrito ao teto CLOUD_FALLBACK_MAX_PRIORITY = 3).
- Monitor Recorrente de Mudança de MD5 (Prioridade 5).

FLUXO DA ARQUITETURA:
┌─────────────────────────────────────────────────────────────────────────────┐
│ [Go Gateway :8000 (Min-Heap P1..P5)] <──> [PostgreSQL 18: WebWorker_db]     │
│        │                                                                    │
│        ▼ (Claim / Dispatch por Prioridade)                                  │
│ [MultiAgentWorkerFleet (Pool Asyncio: Worker #1 ... Worker #N)]             │
│        ├──> P1 (User Chrome CDP): Anonimiza URL + KillSwitch + Pop/Toast    │
│        ├──> P2..P4 (Headless Workers): TriagedWebAgent + Multi-LLM Router   │
│        └──> P5 (MD5 Watcher): Compara Hash MD5 -> Atualiza WebWorker_db     │
└─────────────────────────────────────────────────────────────────────────────┘
===============================================================================
"""

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error

from src.config_manager import get_config
from src.db.webworker_db_manager import WebWorkerDBManager
from src.ollama_manager import OllamaManager
from src.web.browser_engine import BrowserEngine
from src.web.dom_simplifier import DOMSimplifier
from src.web.triaged_web_agent import TriagedWebAgent

logger = logging.getLogger("WorkerFleet")


def _extract_html_title(html: str) -> str:
    if not html:
        return "Página do Usuário"
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m and m.group(1).strip():
        return m.group(1).strip()
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
    if h1 and h1.group(1).strip():
        return re.sub(r"<[^>]+>", "", h1.group(1)).strip()
    return "Página do Usuário"


class MultiLLMRouterClient:
    """
    Roteador distribuído de inferência LLM com suporte a:
    - Local GPU (Ollama Hardware / GPU Local)
    - LAN Node (Ollama secundário na rede)
    - Cloud Fallback (Gemini API) protegido pela trava CLOUD_FALLBACK_MAX_PRIORITY = 3.
    """

    CLOUD_FALLBACK_MAX_PRIORITY = 3

    def __init__(self, db_manager: WebWorkerDBManager, go_gateway_url: str = "http://127.0.0.1:8000"):
        self.db = db_manager
        self.go_gateway_url = go_gateway_url
        self.nodes = {
            "local_gpu": {
                "type": "LOCAL_GPU",
                "endpoint": "http://127.0.0.1:11434",
                "model": "qwen2.5:3b",
                "busy": False,
            },
            "lan_node_gpu2": {
                "type": "LAN_OLLAMA",
                "endpoint": "http://127.0.0.1:11434",
                "model": "qwen2.5:3b",
                "busy": False,
            },
            "cloud_gemini_fallback": {
                "type": "CLOUD_GEMINI",
                "endpoint": "https://generativelanguage.googleapis.com",
                "model": "gemini-2.5-flash",
                "busy": False,
            },
        }
        self._register_initial_nodes()

    def _register_initial_nodes(self) -> None:
        for node_id, info in self.nodes.items():
            try:
                self.db.upsert_worker_telemetry(
                    worker_id=node_id,
                    worker_type=info["type"],
                    status="ONLINE",
                    current_url=info["endpoint"],
                )
            except Exception as e:
                logger.warning(f"Falha ao registrar nó {node_id}: {e}")

    def select_backend_for_priority(self, priority: int, force_cloud: bool = False) -> Dict[str, Any]:
        """
        Seleciona o backend respeitando a trava de custo:
        Se priority > CLOUD_FALLBACK_MAX_PRIORITY (ex: P4 ou P5), NUNCA usa Cloud.
        """
        if force_cloud:
            if priority > self.CLOUD_FALLBACK_MAX_PRIORITY:
                raise PermissionError(
                    f"TETO_CLOUD_BLOQUEADO: Tarefa de Prioridade {priority} excede o teto "
                    f"CLOUD_FALLBACK_MAX_PRIORITY={self.CLOUD_FALLBACK_MAX_PRIORITY}. "
                    f"Execução permitida apenas em nós locais/LAN."
                )
            return {
                "node_id": "cloud_gemini_fallback",
                "backend_type": "CLOUD_GEMINI",
                "allowed": True,
            }

        if not self.nodes["local_gpu"]["busy"]:
            return {
                "node_id": "local_gpu",
                "backend_type": "LOCAL_GPU",
                "allowed": True,
            }

        if not self.nodes["lan_node_gpu2"]["busy"]:
            return {
                "node_id": "lan_node_gpu2",
                "backend_type": "LAN_OLLAMA",
                "allowed": True,
            }

        if priority <= self.CLOUD_FALLBACK_MAX_PRIORITY:
            return {
                "node_id": "cloud_gemini_fallback",
                "backend_type": "CLOUD_GEMINI",
                "allowed": True,
            }

        raise PermissionError(
            f"TETO_CLOUD_BLOQUEADO: Todos os nós locais ocupados e tarefa P{priority} "
            f"não possui permissão para Cloud Fallback (Teto <= P{self.CLOUD_FALLBACK_MAX_PRIORITY})."
        )


class MultiAgentWorkerFleet:
    """
    Orquestrador da Frota de Workers Playwright e Ponte Interativa CDP.
    """

    def __init__(
        self,
        num_workers: int = 3,
        go_gateway_url: str = "http://127.0.0.1:8000",
        headless: bool = True,
    ):
        cfg = get_config()
        self.num_workers = max(1, num_workers)
        self.workers_available = max(1, int(cfg.get("fleet.workers_available") or 1))
        self.go_gateway_url = go_gateway_url.rstrip("/")
        self.headless = headless
        self.db = WebWorkerDBManager()
        self.llm_router = MultiLLMRouterClient(self.db, self.go_gateway_url)

        self.active_tasks_count = 0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_heartbeat = False

        self.worker_states: Dict[str, Dict[str, Any]] = {}
        for i in range(1, self.num_workers + 1):
            wid = f"worker_{i}"
            self.worker_states[wid] = {
                "worker_id": wid,
                "role": "HEADLESS_POOL",
                "status": "IDLE",
                "current_task_uuid": None,
                "priority": None,
                "url": "-",
                "dom_md5": "-",
                "llm_backend": "local_gpu",
                "updated_at": time.strftime("%H:%M:%S"),
            }

        self.worker_states["worker_p1_interactive"] = {
            "worker_id": "worker_p1_interactive",
            "role": "USER_CHROME_P1",
            "status": "STANDBY_P1",
            "current_task_uuid": None,
            "priority": 1,
            "url": "[AGUARDANDO EXTENSÃO CHROME]",
            "dom_md5": "-",
            "llm_backend": "local_gpu",
            "updated_at": time.strftime("%H:%M:%S"),
        }

        self._aborted_tasks: set = set()

    def register_with_gateway(self, listen_url: str = "http://127.0.0.1:8085/api/worker/dispatch", start_heartbeat: bool = True) -> bool:
        """Registra a frota no Gateway Go como listener orientado a push e inicia o heartbeat de 500ms."""
        payload = {
            "worker_id": "fleet_main",
            "listen_url": listen_url,
            "workers_available": self.workers_available
        }
        try:
            req = urllib.request.Request(
                f"{self.go_gateway_url}/api/worker/register",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                logger.info(f"[OK] Frota registrada no Go Gateway: {data}")
        except Exception as e:
            logger.warning(f"[AVISO] Falha ao registrar frota no Go Gateway: {e}")
            return False

        if start_heartbeat:
            self._stop_heartbeat = False
            if not self._heartbeat_thread or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._heartbeat_thread.start()
        return True

    def _heartbeat_loop(self):
        """Envia sinal de batimento cardiaco ao Go Gateway a cada 500ms."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while not self._stop_heartbeat:
            try:
                req = urllib.request.Request(
                    f"{self.go_gateway_url}/api/worker/heartbeat",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with opener.open(req, timeout=1.0) as resp:
                    pass
            except Exception:
                pass
            time.sleep(0.5)

    def handle_pushed_task(self, task_spec: Dict[str, Any]) -> bool:
        """
        Recebe tarefa empurrada via push pelo Go Gateway (:8085/api/worker/dispatch).
        Retorna True se aceitou a tarefa, False se rejeitou por capacidade (backpressure).
        """
        if self.active_tasks_count >= self.workers_available:
            return False

        self.active_tasks_count += 1
        threading.Thread(target=self._execute_dispatched_job, args=(task_spec,), daemon=True).start()
        return True

    def _execute_dispatched_job(self, task_spec: Dict[str, Any]):
        """Executa a tarefa despachada pelo Go, atualiza banco, dashboard e responde ao Go."""
        t0 = time.perf_counter()
        task_uuid = task_spec.get("task_uuid", f"task-{int(time.time()*1000)}")
        prio = int(task_spec.get("priority", 3))
        url = task_spec.get("target_url", "")
        obj = task_spec.get("user_goal", "")
        is_interactive = bool(task_spec.get("is_interactive", False))
        save_private = bool(task_spec.get("save_private_data", False))

        worker_id = "worker_p1_interactive" if is_interactive else f"worker_{(self.active_tasks_count % self.num_workers) + 1}"

        print(f"\n[TASK RECEBIDA] Tarefa P{prio} ({task_uuid[:8]}): '{obj[:50]}' -> {worker_id} assumindo...")

        # 1. Registra no banco para aparecer imediatamente no Dashboard
        self.db.enqueue_task(
            task_uuid=task_uuid,
            user_goal=obj,
            target_url=url,
            priority=prio,
            is_interactive=is_interactive,
            save_private_data=save_private
        )
        self.db.update_task_status(task_uuid, "RUNNING", worker_id=worker_id, backend_id="local_gpu")

        if worker_id in self.worker_states:
            self.worker_states[worker_id].update({
                "status": "RUNNING",
                "current_task_uuid": task_uuid,
                "priority": prio,
                "url": url,
                "updated_at": time.strftime("%H:%M:%S")
            })

        success = False
        result_data = {}
        turns_used = 1
        error_details = None
        res = None
        cfg = get_config()

        try:
            if is_interactive:
                res = self.execute_interactive_p1_task(
                    url=url,
                    objective=obj,
                    user_explicitly_requested_save=save_private,
                    task_uuid=task_uuid,
                    html_snapshot_override=task_spec.get("html_snapshot"),
                    visible_text=task_spec.get("visible_text"),
                )
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    res = loop.run_until_complete(res)
                success = (res.get("status") == "COMPLETED")
                result_data = res.get("result_data", {})
                turns_used = res.get("turns_used", 1)
            else:
                fleet_max_turns = int(cfg.get("fleet.max_turns") or 4)
                agent = TriagedWebAgent(headless=self.headless, max_turns=fleet_max_turns)
                agent.start()
                try:
                    res = agent.run_task(target_url=url, user_goal=obj)
                    success = bool(res.get("success"))
                    result_data = res.get("final_result") or {"completed": True}
                    turns_used = len(res.get("turns", []))
                    if not success and res.get("error"):
                        error_details = str(res.get("error"))
                finally:
                    agent.stop()
        except Exception as e:
            logger.error(f"Erro na execucao da tarefa {task_uuid}: {e}")
            error_details = str(e)
            success = False

        elapsed = round(time.perf_counter() - t0, 2)

        # 2. Atualiza status no banco oficial
        self.db.update_task_status(
            task_uuid=task_uuid,
            status="COMPLETED" if success else "FAILED",
            worker_id=worker_id,
            backend_id="local_gpu",
            turns_used=turns_used,
            execution_time=elapsed,
            result_data=result_data,
            error_details=error_details
        )

        # 3. Restaura estado do worker
        if worker_id in self.worker_states:
            self.worker_states[worker_id].update({
                "status": "STANDBY_P1" if is_interactive else "IDLE",
                "current_task_uuid": None,
                "updated_at": time.strftime("%H:%M:%S")
            })

        self.active_tasks_count = max(0, self.active_tasks_count - 1)

        # 4. Reporta conclusao ao Go Gateway (que dispara a proxima da fila se houver)
        summary_text = ""
        if isinstance(result_data, dict):
            summary_text = (
                result_data.get("summary") or
                result_data.get("agent_response") or
                result_data.get("extracted_value") or
                result_data.get("data") or ""
            )
            # Se for dict com campos diretos de extracao (ex: {"item_name": "...", "id": ...})
            if not summary_text:
                filtered_kvs = [f"{k}: {v}" for k, v in result_data.items() if k not in ("data_type", "attribute_name", "selector", "args") and v is not None]
                if filtered_kvs:
                    summary_text = " | ".join(filtered_kvs)

        if not summary_text and isinstance(res, dict):
            summary_text = res.get("agent_response") or res.get("summary") or res.get("result_data", {}).get("summary", "")

        # Se ainda assim nao tiver texto de resposta, aciona o Estagio 2 da Piramide de Auditoria
        if success and (not summary_text or summary_text.startswith("[OK]")):
            try:
                ollama = OllamaManager()
                if ollama.is_server_online():
                    refine_prompt = (
                        "Você é o auditor e sintetizador executivo do WebWorker AI.\n"
                        f"O usuário solicitou: '{obj}'\n"
                        f"Resultado bruto obtido na página: {result_data}\n\n"
                        "Gere uma resposta executiva, direta e estruturada em no máximo 1 a 2 linhas para o usuário final.\n"
                        "Resposta:"
                    )
                    ref_resp = ollama.chat(
                        model=str(cfg.get("llm_backends.local_gpu.model") or "qwen2.5:3b"),
                        messages=[{"role": "user", "content": refine_prompt}],
                        options={"temperature": 0.0, "num_ctx": 2048},
                        timeout=15
                    )
                    refined = ref_resp.get("message", {}).get("content", "").strip()
                    if refined:
                        summary_text = refined
            except Exception as e:
                logger.warning(f"[AVISO] Falha ao sintetizar resposta no dispatched job: {e}")

        if not summary_text:
            summary_text = f"[OK] Acao concluida em {elapsed:.2f}s!" if success else f"[FALHA] {error_details}"

        complete_payload = {
            "task_uuid": task_uuid,
            "worker_id": worker_id,
            "status": "COMPLETED" if success else "FAILED",
            "turns_used": turns_used,
            "execution_time_seconds": elapsed,
            "result_data": result_data,
            "error_details": error_details or "",
            "summary_text": summary_text
        }

        try:
            req = urllib.request.Request(
                f"{self.go_gateway_url}/api/worker/complete",
                data=json.dumps(complete_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3.0) as resp:
                pass
        except Exception as e:
            logger.warning(f"Aviso ao notificar conclusao ao Go Gateway: {e}")

        status_str = "concluida com sucesso" if success else "falhou"
        print(f"[TASK CONCLUIDA] Tarefa {task_uuid[:8]} {status_str} em {elapsed:.2f}s. Slot liberado no cluster.\n")

    def stop(self):
        """Encerra threads da frota."""
        self._stop_heartbeat = True
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None

    def trigger_kill_switch(self, task_uuid: str, reason: str = "Acionado pelo botão de pânico do usuário") -> Dict[str, Any]:
        """
        Aciona imediatamente o Kill Switch local, no Gateway Go e no PostgreSQL WebWorker_db.
        """
        self._aborted_tasks.add(task_uuid)
        self.db.update_task_status(
            task_uuid=task_uuid,
            status="ABORTED",
            worker_id="worker_p1_interactive",
            error_details=reason,
        )

        go_resp = {}
        try:
            req = urllib.request.Request(
                f"{self.go_gateway_url}/api/task/abort",
                data=json.dumps({"task_uuid": task_uuid}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                go_resp = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"Gateway Go notificação kill switch offline/direta: {e}")

        self.worker_states["worker_p1_interactive"].update({
            "status": "ABORTED_BY_KILL_SWITCH",
            "current_task_uuid": task_uuid,
            "updated_at": time.strftime("%H:%M:%S"),
        })

        return {
            "task_uuid": task_uuid,
            "status": "ABORTED",
            "go_gateway": go_resp,
            "ux_feedback": {
                "play_pop_sound": True,
                "sound_frequency_hz": 320,
                "toast_message": f"🛑 Kill Switch ativado: Acesso da LLM bloqueado imediatamente ({reason}).",
                "toast_type": "error",
                "fade_out_ms": 5000,
            },
        }

    async def execute_interactive_p1_task(
        self,
        url: str,
        objective: str,
        user_explicitly_requested_save: bool = False,
        simulate_abort_mid_execution: bool = False,
        html_snapshot_override: Optional[str] = None,
        task_uuid: Optional[str] = None,
        visible_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executa uma tarefa de Prioridade 1 (Interatividade Local no Navegador do Usuário).
        """
        start_t = time.perf_counter()
        task_uuid = task_uuid or f"p1-user-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

        existing_row = self.db.get_task_by_uuid(task_uuid)
        if existing_row:
            row = existing_row
        else:
            row = self.db.enqueue_task(
                task_uuid=task_uuid,
                user_goal=objective,
                target_url=url,
                task_type="web_agent",
                source="chrome_extension",
                priority=1,
                is_interactive=True,
                save_private_data=user_explicitly_requested_save,
            )

        masked_url = row["target_url"]

        self.worker_states["worker_p1_interactive"].update({
            "status": "RUNNING_P1",
            "current_task_uuid": task_uuid,
            "url": masked_url,
            "updated_at": time.strftime("%H:%M:%S"),
        })

        if simulate_abort_mid_execution or task_uuid in self._aborted_tasks:
            return self.trigger_kill_switch(task_uuid, "Interrompido pelo usuário na Extensão Chrome")

        simplifier = DOMSimplifier()
        if html_snapshot_override:
            snapshot = simplifier.simplify(html_snapshot_override)
            title = _extract_html_title(html_snapshot_override)
        else:
            def _fetch_sync():
                try:
                    eng = BrowserEngine(headless=self.headless)
                    eng.start()
                    try:
                        nav = eng.navigate(url)
                        snap = nav.get("dom_snapshot") or eng._get_current_snapshot()
                        return snap, nav.get("title", "Página do Usuário")
                    finally:
                        eng.stop()
                except Exception as e:
                    logger.warning(f"[AVISO] Falha ao navegar em '{url}': {e}")
                    return None, "Aba do Usuário"

            snapshot, title = await asyncio.to_thread(_fetch_sync)

        dom_md5 = TriagedWebAgent.compute_dom_hash(snapshot) if snapshot else ""
        self.worker_states["worker_p1_interactive"]["dom_md5"] = dom_md5

        if task_uuid in self._aborted_tasks:
            return self.trigger_kill_switch(task_uuid, "Bloqueado antes do envio para a LLM")

        elements_count = len(snapshot.selector_map) if (snapshot and hasattr(snapshot, "selector_map")) else 0
        context_text = snapshot.pruned_text[:24000] if (snapshot and hasattr(snapshot, "pruned_text")) else ""

        # Consulta a LLM local no Ollama com o conteúdo da página e o objetivo do usuário
        ollama = OllamaManager()
        cfg = get_config()
        model_name = str(cfg.get("llm_backends.local_gpu.model") or "qwen2.5:3b")

        system_prompt = (
            "Você é o WebWorker AI, um agente web autônomo operando diretamente na aba aberta do navegador do usuário.\n"
            "Você tem visibilidade do conteúdo da página e dos elementos interativos marcados no texto como [button @ID], [input @ID tipo] e [link @ID url].\n\n"
            "Diretrizes de Execução:\n"
            "1. Se o objetivo do usuário for uma PERGUNTA sobre dados visíveis (ex: 'qual o ID?', 'quanto custa?', 'qual o nome?'):\n"
            "   Responda de forma direta, precisa e concisa em português.\n"
            "2. Se o objetivo do usuário pedir uma AÇÃO DE INTERAÇÃO na página (ex: clicar, preencher, digitar, rolar):\n"
            "   - Se você localizar o elemento pelo @ID correspondente na lista, emita OBRIGATORIAMENTE uma das ações:\n"
            "     ACTION: click @ID [motivo]\n"
            "     ACTION: fill @ID 'texto a preencher' [motivo]\n"
            "     ACTION: scroll down [motivo]\n"
            "     ACTION: scroll up [motivo]\n"
            "   - Se o objetivo pedir para interagir com um elemento que NÃO estiver presente na lista de @IDs carregada:\n"
            "     Emita OBRIGATORIAMENTE:\n"
            "     ACTION: scroll down [procurar elemento na página]\n"
            "     Ou responda diretamente em texto: 'Elemento não localizado no trecho carregado da página.'\n"
            "3. PROIBIÇÃO ABSOLUTA: NUNCA gere blocos de código HTML, tutoriais de programação nem instruções de como criar links em websites. Você é um agente executor autônomo, não um assistente de tutoriais.\n"
            "Use apenas os @IDs reais fornecidos na página."
        )
        viewport_block = ""
        if visible_text:
            viewport_block = f"--- Conteúdo Visível na Tela Atual (Foco do Usuário) ---\n{visible_text[:6000]}\n--- Fim da Tela Atual ---\n\n"
        user_message = (
            f"Título da Página: '{title}'\n\n"
            f"{viewport_block}"
            f"--- Estrutura e Elementos Mapeados da Página ---\n{context_text}\n--- Fim da Estrutura ---\n\n"
            f"Objetivo do Usuário: {objective}"
        )

        llm_answer = ""
        try:
            if ollama.is_server_online():
                llm_resp = ollama.chat(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    options={"temperature": 0.1, "num_ctx": 8192},
                    timeout=45
                )
                llm_answer = llm_resp.get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning(f"[AVISO] Falha ao consultar LLM local no Ollama: {e}")

        action_plan = {"type": "answer", "action": None}
        final_summary = ""

        if llm_answer:
            click_match = re.search(r"ACTION:\s*click\s+(@\S+)(?:\s+\[?(.*?)\]?)?", llm_answer, re.IGNORECASE)
            fill_match = re.search(r'ACTION:\s*fill\s+(@\S+)\s+[\'"](.*?)[\'"](?:\s+\[?(.*?)\]?)?', llm_answer, re.IGNORECASE)
            scroll_match = re.search(r"ACTION:\s*scroll\s+(down|up)(?:\s+\[?(.*?)\]?)?", llm_answer, re.IGNORECASE)

            if click_match:
                target_id = click_match.group(1).strip()
                desc = (click_match.group(2) or "").strip()
                selector = snapshot.selector_map.get(target_id, target_id) if snapshot else target_id
                action_plan = {
                    "type": "action",
                    "action": "click",
                    "target_id": target_id,
                    "selector": selector,
                    "description": desc or f"Clicando em {target_id}"
                }
                final_summary = f"Acao: Clicando no elemento {target_id} ({selector})."
            elif fill_match:
                target_id = fill_match.group(1).strip()
                fill_val = fill_match.group(2).strip()
                desc = (fill_match.group(3) or "").strip()
                selector = snapshot.selector_map.get(target_id, target_id) if snapshot else target_id
                action_plan = {
                    "type": "action",
                    "action": "fill",
                    "target_id": target_id,
                    "selector": selector,
                    "value": fill_val,
                    "description": desc or f"Preenchendo {target_id} com '{fill_val}'"
                }
                final_summary = f"Acao: Preenchendo {target_id} com '{fill_val}'."
            elif scroll_match:
                direction = scroll_match.group(1).strip().lower()
                desc = (scroll_match.group(2) or "").strip()
                action_plan = {
                    "type": "action",
                    "action": "scroll",
                    "direction": direction,
                    "description": desc or f"Rolando pagina para {direction}"
                }
                final_summary = f"Acao: Rolando pagina para {direction}."
            else:
                # Estagio 2 da Piramide de Auditoria: Refinamento Executivo
                # Se for resposta textual, remove ruidos tecnicos ([link @...], preambulos, codigos)
                raw_draft = llm_answer
                try:
                    if ollama.is_server_online() and len(raw_draft) > 15:
                        refine_sys = (
                            "Você é o auditor e sintetizador executivo do WebWorker AI.\n"
                            "Sua única função é pegar uma resposta bruta extraída de uma página web e transformá-la em uma resposta final direta, limpa e estruturada para o usuário final.\n\n"
                            "Diretrizes Estritas:\n"
                            "1. Elimine qualquer marcador técnico como [link @...], [button @...], [hide], URLs, trechos brutos de HTML, seletores e referências internas.\n"
                            "2. Não use introduções prolixas (ex: 'De acordo com a seção...', 'Para atender ao objetivo...', 'Aqui está o trecho...').\n"
                            "3. Responda estritamente o que foi perguntado em no máximo 1 a 2 linhas, no formato 'Chave: Valor' ou tópicos objetivos.\n\n"
                            "Exemplo de Entrada:\n"
                            "Objetivo: qual a id do item?\n"
                            "Texto Bruto: De acordo com a seção de dados (Data values), para o bloco de scaffolding na versão Java Edition, o ID do item é -165. Aqui está o trecho relevante da documentação: [link @169 /w/Bedrock_Edition] Bedrock Edition : Name [link @170]...\n"
                            "Saída Refinada:\n"
                            "ID do Item: scaffolding (Java Edition) | ID Numérico: -165 (Bedrock Edition)"
                        )
                        refine_user = (
                            f"Objetivo do Usuário: {objective}\n"
                            f"Texto Bruto Extraído: {raw_draft}\n\n"
                            "Saída Refinada:"
                        )
                        ref_resp = ollama.chat(
                            model=model_name,
                            messages=[
                                {"role": "system", "content": refine_sys},
                                {"role": "user", "content": refine_user}
                            ],
                            options={"temperature": 0.0, "num_ctx": 2048},
                            timeout=15
                        )
                        refined_text = ref_resp.get("message", {}).get("content", "").strip()
                        if refined_text:
                            final_summary = refined_text
                        else:
                            final_summary = raw_draft
                    else:
                        final_summary = raw_draft
                except Exception as e:
                    logger.warning(f"[AVISO] Falha no Estagio 2 de refinamento: {e}")
                    final_summary = raw_draft
        else:
            final_summary = f"Aba '{title}' analisada ({elements_count} elementos mapeados). Resposta da LLM indisponivel para: '{objective}'."

        preview_parts = []
        if visible_text:
            preview_parts.append(f"[TELA ATUAL / VIEWPORT]\n{visible_text[:2500]}")
        if context_text:
            preview_parts.append(f"[DOM SIMPLIFICADO]\n{context_text[:2500]}")
        dom_telemetry = {
            "title": title,
            "elements_count": elements_count,
            "pruned_text_preview": "\n\n".join(preview_parts),
            "selector_map": dict(list(snapshot.selector_map.items())[:60]) if (snapshot and hasattr(snapshot, "selector_map")) else {}
        }

        result_payload = {
            "summary": final_summary,
            "title": title,
            "elements_count": elements_count,
            "action_plan": action_plan,
            "dom_telemetry": dom_telemetry
        }

        elapsed_sec = round(time.perf_counter() - start_t, 3)

        self.db.update_task_status(
            task_uuid=task_uuid,
            status="COMPLETED",
            worker_id="worker_p1_interactive",
            backend_id="local_gpu",
            turns_used=1,
            execution_time=elapsed_sec,
            result_data=result_payload,
        )

        self.worker_states["worker_p1_interactive"].update({
            "status": "IDLE",
            "current_task_uuid": None,
            "updated_at": time.strftime("%H:%M:%S"),
        })

        return {
            "task_uuid": task_uuid,
            "priority": 1,
            "status": "COMPLETED",
            "privacy_mode_active": not user_explicitly_requested_save,
            "stored_url_in_db": masked_url,
            "dom_md5": dom_md5,
            "execution_time_seconds": elapsed_sec,
            "agent_response": final_summary,
            "summary": final_summary,
            "action_plan": action_plan,
            "dom_telemetry": dom_telemetry,
            "result_data": result_payload,
            "ux_feedback": {
                "play_pop_sound": True,
                "sound_frequency_hz": 820,
                "sound_duration_ms": 60,
                "toast_message": final_summary,
                "toast_type": "success",
                "fade_out_ms": 5000,
                "action_plan": action_plan,
                "dom_telemetry": dom_telemetry
            },
        }

    async def execute_p5_md5_watcher_cycle(
        self,
        watcher_uuid: str,
        target_url: str,
        html_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executa a Triagem de Configuração de Pedidos Recorrentes (Prioridade 5).
        Verifica se o hash MD5 da janela/DOM mudou e atualiza o banco com os dados novos.
        """
        simplifier = DOMSimplifier()
        if html_content is not None:
            snapshot = simplifier.simplify(html_content)
            title = _extract_html_title(html_content)
        else:
            def _fetch_sync():
                eng = BrowserEngine(headless=self.headless)
                eng.start()
                try:
                    nav = eng.navigate(target_url)
                    snap = nav.get("dom_snapshot") or eng._get_current_snapshot()
                    return snap, nav.get("title", "")
                finally:
                    eng.stop()

            snapshot, title = await asyncio.to_thread(_fetch_sync)

        new_md5 = TriagedWebAgent.compute_dom_hash(snapshot)

        extracted_snapshot = {
            "title": title,
            "interactive_count": len(snapshot.selector_map),
            "markdown_preview": snapshot.pruned_text[:300],
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        check_result = self.db.update_watcher_check(
            watcher_uuid=watcher_uuid,
            new_md5=new_md5,
            extracted_data=extracted_snapshot,
        )

        return {
            "watcher_uuid": watcher_uuid,
            "priority": 5,
            "target_url": target_url,
            "md5_changed": check_result["changed"],
            "previous_md5": check_result["old_md5"],
            "current_md5": check_result["new_md5"],
            "db_updated_with_new_data": check_result["changed"],
            "snapshot": extracted_snapshot if check_result["changed"] else None,
        }

    async def run_parallel_worker_tasks(self, tasks_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Executa uma lista de tarefas concorrentemente distribuídas entre os N workers
        usando threads sincronizadas com o TriagedWebAgent real.
        """
        semaphore = asyncio.Semaphore(self.num_workers)

        async def _worker_job(idx: int, task_spec: Dict[str, Any]) -> Dict[str, Any]:
            worker_id = f"worker_{(idx % self.num_workers) + 1}"
            async with semaphore:
                t0 = time.perf_counter()
                prio = task_spec.get("priority", 3)
                url = task_spec["url"]
                obj = task_spec["objective"]
                task_uuid = f"fleet-p{prio}-{idx+1}-{uuid.uuid4().hex[:6]}"

                self.db.enqueue_task(
                    task_uuid=task_uuid,
                    user_goal=obj,
                    target_url=url,
                    task_type="web_agent",
                    priority=prio,
                )
                self.db.update_task_status(task_uuid, "RUNNING", worker_id=worker_id, backend_id="local_gpu")

                self.worker_states[worker_id].update({
                    "status": "RUNNING",
                    "current_task_uuid": task_uuid,
                    "priority": prio,
                    "url": url,
                    "updated_at": time.strftime("%H:%M:%S"),
                })

                def _run_sync_agent():
                    cfg = get_config()
                    fleet_max_turns = int(cfg.get("fleet.max_turns") or 4)
                    agent = TriagedWebAgent(headless=self.headless, max_turns=fleet_max_turns)
                    agent.start()
                    try:
                        return agent.run_task(target_url=url, user_goal=obj)
                    finally:
                        agent.stop()

                res = await asyncio.to_thread(_run_sync_agent)
                elapsed_sec = round(time.perf_counter() - t0, 2)

                self.db.update_task_status(
                    task_uuid=task_uuid,
                    status="COMPLETED" if res.get("success") else "FAILED",
                    worker_id=worker_id,
                    backend_id="local_gpu",
                    turns_used=len(res.get("turns", [])),
                    execution_time=elapsed_sec,
                    result_data=res.get("final_result") or {"completed": True},
                )

                self.worker_states[worker_id].update({
                    "status": "IDLE",
                    "current_task_uuid": None,
                    "updated_at": time.strftime("%H:%M:%S"),
                })

                return {
                    "worker_id": worker_id,
                    "task_uuid": task_uuid,
                    "priority": prio,
                    "url": url,
                    "success": res.get("success", False),
                    "execution_time_seconds": elapsed_sec,
                    "final_result": res.get("final_result"),
                }

        results = await asyncio.gather(*[_worker_job(i, t) for i, t in enumerate(tasks_list)])
        return list(results)

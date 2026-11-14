"""
===============================================================================
MÓDULO: Suíte de Testes End-to-End da Plataforma Distribuída (Dia 6)
CAMINHO: tests/test_day6_distributed_platform.py
===============================================================================

OBJETIVO:
Validar todos os Critérios de Aceite (AC-01 a AC-11) aprovados no plano de
arquitetura do Dia 6:
- T1 (Fila P1..P5 & Preempção): Enfileirar tarefas no Go Gateway (:8000) e no
  PostgreSQL (WebWorker_db), verificando que prioridade omitida assume P3 e que
  P1 fura a fila imediatamente.
- T2 (Router de Propósito Geral - Raw LLM): Testar POST /api/llm/chat e
  POST /api/llm/generate entregando inferência pura na fila sem abrir browser.
- T3 (Teto Cloud Fallback <= P3): Comprovar que P1..P3 têm permissão para usar
  Cloud Fallback quando necessário, enquanto P4 e P5 recebem bloqueio estrito
  (HTTP 403 / PermissionError: TETO_CLOUD_BLOQUEADO).
- T4 (Privacidade & Anonimização + Kill Switch + UX Pop/Toast): Comprovar que
  tarefas interativas do navegador do usuário (P1) mascaram a URL como
  [PRIVADO: dominio] e nunca salvam dados extraídos no banco, além de testar o
  acionamento do Kill Switch (POST /api/task/abort) e o payload de som "pop" e
  toast esmaecível.
- T5 (Crash Recovery da Fila no PostgreSQL): Simular queda de energia com
  tarefas em RUNNING, rodar o protocolo de Crash Recovery (que move para PAUSED)
  e comprovar a retomada ordenada por prioridade.
- T6 (Monitor Recorrente de MD5 - Prioridade 5): Testar o watcher P5 validando
  que quando o hash MD5 não muda ele ignora a atualização, e quando o MD5 muda
  ele atualiza automaticamente os dados novos no WebWorker_db.
- T7 (Dashboard Multiagente :8085 & Extensão Chrome MV3): Iniciar a Dashboard na
  porta 8085, validar o endpoint /api/metrics e os artefatos da Extensão Chrome.

FLUXO DA ARQUITETURA:
┌─────────────────────────────────────────────────────────────────────────────┐
│ [test_day6_distributed_platform.py]                                         │
│    ├──> Inicia Binário Nativo Go (go_services/webworker_gateway.exe :8000)  │
│    ├──> Conecta ao PostgreSQL 18 (WebWorker_db)                             │
│    ├──> Instancia MultiAgentWorkerFleet + MultiAgentDashboardServer (:8085) │
│    └──> Executa Baterias T1..T7 -> Salva Relatório JSON em results/         │
└─────────────────────────────────────────────────────────────────────────────┘
===============================================================================
"""

import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db.webworker_db_manager import WebWorkerDBManager
from src.multiagent.worker_fleet import MultiAgentWorkerFleet
from src.dashboard.multiagent_dashboard import MultiAgentDashboardServer


GO_BINARY_PATH = ROOT_DIR / "go_services" / "webworker_gateway.exe"
GO_GATEWAY_URL = "http://127.0.0.1:8000"
DASHBOARD_URL = "http://127.0.0.1:8085"


def http_json(url: str, method: str = "GET", payload: dict = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw_error": body}


async def run_all_day6_tests() -> dict:
    print("=" * 80)
    print("INICIANDO SUÍTE DE VALIDAÇÃO END-TO-END — DIA 6 (ARQUITETURA DISTRIBUÍDA)")
    print("=" * 80)

    db = WebWorkerDBManager(auto_init=True)
    db.clear_all_for_test()

    # Inicia o Microserviço Go (:8000)
    go_proc = None
    if GO_BINARY_PATH.exists():
        go_proc = subprocess.Popen(
            [str(GO_BINARY_PATH)],
            cwd=str(ROOT_DIR / "go_services"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(25):
            try:
                code, _ = http_json(f"{GO_GATEWAY_URL}/api/metrics")
                if code == 200:
                    break
            except Exception:
                time.sleep(0.25)

    fleet = MultiAgentWorkerFleet(num_workers=3, go_gateway_url=GO_GATEWAY_URL, headless=True)
    dashboard = MultiAgentDashboardServer(port=8085, go_gateway_url=GO_GATEWAY_URL, fleet_ref=fleet)
    dashboard.start_background()

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "database": "WebWorker_db",
        "go_gateway_port": 8000,
        "dashboard_port": 8085,
        "tests": {},
    }

    try:
        # =====================================================================
        # T1: FILA DE PRIORIDADES P1..P5, PRIORIDADE PADRÃO (P3) E PREEMPÇÃO
        # =====================================================================
        print("\n[T1] Testando Fila Min-Heap Go (:8000) + Preempção P1..P5 + Default P3...")
        # 1. Enfileira P4 (Lote)
        _, r_p4 = http_json(f"{GO_GATEWAY_URL}/api/task", "POST", {
            "task_uuid": "t_p4_batch",
            "user_goal": "Crawler em lote secundário",
            "target_url": "https://animeschedule.net",
            "priority": 4,
        })
        # 2. Enfileira sem informar prioridade -> Deve assumir P3 automaticamente
        _, r_p3_def = http_json(f"{GO_GATEWAY_URL}/api/task", "POST", {
            "task_uuid": "t_p3_default",
            "user_goal": "Requisição de rede sem prioridade explícita",
            "target_url": "https://animesdrive.cloud",
        })
        # 3. Enfileira P5 (Monitor MD5)
        _, r_p5 = http_json(f"{GO_GATEWAY_URL}/api/task", "POST", {
            "task_uuid": "t_p5_watcher",
            "user_goal": "Monitorar MD5 da home",
            "target_url": "https://animeschedule.net",
            "priority": 5,
        })
        # 4. Enfileira P1 (Interatividade Local no Chrome do Usuário) -> Deve furar toda a fila!
        _, r_p1 = http_json(f"{GO_GATEWAY_URL}/api/task", "POST", {
            "task_uuid": "t_p1_user_chrome",
            "user_goal": "Clique imediato na aba ativa do usuário",
            "target_url": "https://mail.google.com/mail/u/0/#inbox",
            "is_interactive": True,
            "save_private_data": False,
        })

        # Desenfileira na ordem do Min-Heap do Go Gateway via /api/worker/claim
        dequeue_order = []
        for _ in range(4):
            _, dq = http_json(f"{GO_GATEWAY_URL}/api/worker/claim?worker_id=worker_test", "GET")
            if dq.get("has_task") and dq.get("task"):
                t_obj = dq["task"]
                dequeue_order.append((t_obj["task_uuid"], t_obj["priority"], t_obj["target_url"]))

        expected_priorities = [1, 3, 4, 5]
        actual_priorities = [item[1] for item in dequeue_order]
        t1_passed = (
            actual_priorities == expected_priorities
            and r_p3_def.get("priority") == 3
            and dequeue_order[0][2].startswith("[PRIVADO:")
        )
        report["tests"]["T1_Priority_Queue_Preemption"] = {
            "passed": t1_passed,
            "default_priority_when_omitted": r_p3_def.get("priority"),
            "dequeue_sequence": [
                {"task_uuid": tid, "priority": p, "stored_url": u} for tid, p, u in dequeue_order
            ],
        }
        print(f"  -> Ordem de saída do Min-Heap Go: {actual_priorities} (Esperado: {expected_priorities}) | PASS={t1_passed}")

        # =====================================================================
        # T2: ROUTER DE PROPÓSITO GERAL (RAW LLM SEM NAVEGADOR)
        # =====================================================================
        print("\n[T2] Testando Router de Propósito Geral (POST /api/llm/chat & /api/llm/generate)...")
        code_gen, resp_gen = http_json(f"{GO_GATEWAY_URL}/api/llm/generate", "POST", {
            "prompt": "Responda apenas com a palavra OK.",
            "model": "qwen2.5:3b",
            "priority": 2,
            "force_cloud_check": False,
        })
        code_chat, resp_chat = http_json(f"{GO_GATEWAY_URL}/api/llm/chat", "POST", {
            "prompt": "Qual é a soma de 2 + 2? Responda de forma curta.",
            "model": "qwen2.5:3b",
        })
        t2_passed = (
            code_gen == 200
            and code_chat == 200
            and resp_gen.get("browser_instantiated") is False
            and resp_chat.get("priority") == 3
            and len(resp_chat.get("response", "")) > 0
        )
        report["tests"]["T2_General_Purpose_LLM_Router"] = {
            "passed": t2_passed,
            "generate_endpoint_status": code_gen,
            "chat_endpoint_status": code_chat,
            "browser_instantiated": resp_gen.get("browser_instantiated"),
            "default_priority_assigned": resp_chat.get("priority"),
            "sample_response": resp_chat.get("response", "")[:120],
            "duration_seconds": resp_chat.get("duration_seconds"),
        }
        print(f"  -> Status /generate={code_gen}, /chat={code_chat} | Resposta: {resp_chat.get('response', '')[:50]!r} | PASS={t2_passed}")

        # =====================================================================
        # T3: TETO CLOUD FALLBACK (CLOUD_FALLBACK_MAX_PRIORITY = 3)
        # =====================================================================
        print("\n[T3] Testando Trava de Custo Cloud Fallback (Permitido <= P3 | Bloqueado P4 e P5)...")
        code_p4_cloud, resp_p4_cloud = http_json(f"{GO_GATEWAY_URL}/api/llm/generate", "POST", {
            "prompt": "Tarefa lote P4 tentando usar Cloud",
            "priority": 4,
            "force_cloud_check": True,
        })
        code_p5_cloud, resp_p5_cloud = http_json(f"{GO_GATEWAY_URL}/api/llm/generate", "POST", {
            "prompt": "Monitor MD5 P5 tentando usar Cloud",
            "priority": 5,
            "force_cloud_check": True,
        })
        p3_cloud_allowed = fleet.llm_router.select_backend_for_priority(3, force_cloud=True)["allowed"]
        p4_blocked_py = False
        try:
            fleet.llm_router.select_backend_for_priority(4, force_cloud=True)
        except PermissionError:
            p4_blocked_py = True

        t3_passed = (code_p4_cloud == 403 and code_p5_cloud == 403 and p3_cloud_allowed and p4_blocked_py)
        report["tests"]["T3_Cloud_Fallback_Priority_Cap"] = {
            "passed": t3_passed,
            "cloud_fallback_max_priority": 3,
            "p3_cloud_allowed": p3_cloud_allowed,
            "p4_go_http_status": code_p4_cloud,
            "p4_go_error": resp_p4_cloud.get("error"),
            "p5_go_http_status": code_p5_cloud,
            "p4_python_router_blocked": p4_blocked_py,
        }
        print(f"  -> P3 Cloud Permitido={p3_cloud_allowed} | P4 HTTP={code_p4_cloud} | P5 HTTP={code_p5_cloud} | PASS={t3_passed}")

        # =====================================================================
        # T4: PRIVACIDADE & ANONIMIZAÇÃO (P1) + KILL SWITCH + UX (POP & TOAST)
        # =====================================================================
        print("\n[T4] Testando Privacidade P1 (Anonimização no WebWorker_db), UX (Som Pop + Toast) e Kill Switch...")
        sample_user_html = """
        <html><head><title>Banco Privado do Usuário</title></head>
        <body>
          <h1>Saldo Confidencial: R$ 99.999,00</h1>
          <button id="btn-pix">Transferir PIX</button>
          <input id="chave" type="text" placeholder="Digite a chave" />
        </body></html>
        """
        # 4.1 Execução P1 com Privacidade Padrão (user_explicitly_requested_save=False)
        p1_res = await fleet.execute_interactive_p1_task(
            url="https://internetbanking.meu-banco.com.br/conta/extrato?token=secreto123",
            objective="Verificar se o botão Transferir PIX está disponível na tela",
            user_explicitly_requested_save=False,
            html_snapshot_override=sample_user_html,
        )
        db_row_private = db.get_task_by_uuid(p1_res["task_uuid"])
        url_in_db = db_row_private["target_url"]
        result_in_db = db_row_private["result_data"]

        # 4.2 Teste do Kill Switch imediato (Botão de Pânico)
        p1_aborted_res = await fleet.execute_interactive_p1_task(
            url="https://internetbanking.meu-banco.com.br/pagamentos",
            objective="Executar ação longa e abortar no meio pelo Kill Switch",
            user_explicitly_requested_save=False,
            simulate_abort_mid_execution=True,
            html_snapshot_override=sample_user_html,
        )
        db_row_aborted = db.get_task_by_uuid(p1_aborted_res["task_uuid"])

        t4_passed = (
            url_in_db == "[PRIVADO: internetbanking.meu-banco.com.br]"
            and isinstance(result_in_db, dict)
            and result_in_db.get("data_saved") is False
            and result_in_db.get("privacy_mode") == "ANONYMIZED_USER_SESSION"
            and p1_res["ux_feedback"]["play_pop_sound"] is True
            and p1_res["ux_feedback"]["fade_out_ms"] == 5000
            and p1_aborted_res["status"] == "ABORTED"
            and db_row_aborted["status"] == "ABORTED"
        )
        report["tests"]["T4_Privacy_Anonymization_And_KillSwitch"] = {
            "passed": t4_passed,
            "stored_url_in_postgresql": url_in_db,
            "stored_result_data_in_postgresql": result_in_db,
            "ux_feedback_on_complete": p1_res["ux_feedback"],
            "kill_switch_status_in_db": db_row_aborted["status"],
            "ux_feedback_on_abort": p1_aborted_res["ux_feedback"],
        }
        print(f"  -> URL no Banco: {url_in_db} | DataSaved: {result_in_db.get('data_saved')} | KillSwitch Status: {db_row_aborted['status']} | PASS={t4_passed}")

        # =====================================================================
        # T5: CRASH RECOVERY DA FILA PERSISTENTE NO POSTGRESQL (PAUSED -> RESUME)
        # =====================================================================
        print("\n[T5] Testando Crash Recovery da Fila (Queda de Energia: RUNNING -> PAUSED -> Retomada)...")
        db.enqueue_task(
            task_uuid="t_crash_p2",
            user_goal="Extração urgente interrompida por queda de luz",
            target_url="https://animeschedule.net",
            priority=2,
            task_type="web_agent",
        )
        db.enqueue_task(
            task_uuid="t_crash_p4",
            user_goal="Indexação lote interrompida por queda de luz",
            target_url="https://animesdrive.cloud",
            priority=4,
            task_type="web_agent",
        )
        db.update_task_status("t_crash_p2", "RUNNING", worker_id="worker_1", backend_id="local_gpu")
        db.update_task_status("t_crash_p4", "RUNNING", worker_id="worker_2", backend_id="local_gpu")

        # Simula o boot do sistema executando o protocolo de Crash Recovery
        recoverable_tasks = db.crash_recovery_pause_and_list()
        paused_row_p2 = db.get_task_by_uuid("t_crash_p2")

        # Retoma a próxima tarefa (deve puxar primeiro a PAUSED de maior prioridade: P2)
        resumed_first = db.fetch_next_task_from_db("worker_recovery_1")
        resumed_second = db.fetch_next_task_from_db("worker_recovery_2")

        t5_passed = (
            paused_row_p2["status"] == "PAUSED"
            and len(recoverable_tasks) >= 2
            and resumed_first is not None
            and resumed_first["task_uuid"] == "t_crash_p2"
            and resumed_second is not None
            and resumed_second["task_uuid"] == "t_crash_p4"
        )
        report["tests"]["T5_Database_Crash_Recovery"] = {
            "passed": t5_passed,
            "status_immediately_after_crash_recovery": paused_row_p2["status"],
            "total_recoverable_tasks_found": len(recoverable_tasks),
            "first_resumed_task_uuid": resumed_first["task_uuid"] if resumed_first else None,
            "first_resumed_priority": resumed_first["priority"] if resumed_first else None,
            "second_resumed_task_uuid": resumed_second["task_uuid"] if resumed_second else None,
        }
        print(f"  -> Status pós-apagão: {paused_row_p2['status']} | 1ª Retomada: {resumed_first['task_uuid']} (P{resumed_first['priority']}) | PASS={t5_passed}")

        # =====================================================================
        # T6: MONITOR RECORRENTE DE MD5 (PRIORIDADE 5)
        # =====================================================================
        print("\n[T6] Testando Monitor Recorrente de MD5 (Prioridade 5: Detecção de Mudança de Janela)...")
        html_v1 = "<html><head><title>Portal Anime</title></head><body><h1>Episódio 10 Lançado</h1><a href='/ep10'>Assistir Ep 10</a></body></html>"
        html_v2 = "<html><head><title>Portal Anime</title></head><body><h1>Episódio 11 NOVO!</h1><a href='/ep11'>Assistir Ep 11</a></body></html>"

        watcher_row = db.register_watcher(
            watcher_uuid="watcher_lancamentos_p5",
            target_url="https://animesdrive.cloud/lancamentos",
            user_goal="Monitorar alterações de MD5 e atualizar dados",
            check_interval_seconds=60,
        )
        w_uuid = watcher_row["watcher_uuid"]

        # Ciclo 1: Primeiro snapshot (registra MD5 inicial)
        c1 = await fleet.execute_p5_md5_watcher_cycle(w_uuid, "https://animesdrive.cloud/lancamentos", html_content=html_v1)
        # Ciclo 2: Mesma página sem alteração (MD5 idêntico -> NÃO deve atualizar banco)
        c2 = await fleet.execute_p5_md5_watcher_cycle(w_uuid, "https://animesdrive.cloud/lancamentos", html_content=html_v1)
        # Ciclo 3: Página atualizou para Episódio 11 (MD5 mudou -> DEVE atualizar banco com dados novos)
        c3 = await fleet.execute_p5_md5_watcher_cycle(w_uuid, "https://animesdrive.cloud/lancamentos", html_content=html_v2)

        t6_passed = (
            c1["md5_changed"] is True
            and c2["md5_changed"] is False
            and c2["db_updated_with_new_data"] is False
            and c3["md5_changed"] is True
            and c3["db_updated_with_new_data"] is True
            and c1["current_md5"] != c3["current_md5"]
        )
        report["tests"]["T6_Recurrent_MD5_Watcher_P5"] = {
            "passed": t6_passed,
            "cycle_1_initial_md5": c1["current_md5"],
            "cycle_2_unchanged_skipped": not c2["md5_changed"],
            "cycle_3_new_md5_detected": c3["current_md5"],
            "cycle_3_db_updated": c3["db_updated_with_new_data"],
            "cycle_3_extracted_preview": c3["snapshot"]["markdown_preview"] if c3.get("snapshot") else "",
        }
        print(f"  -> Ciclo 1 (Init)={c1['md5_changed']} | Ciclo 2 (Sem mudança)={c2['md5_changed']} | Ciclo 3 (Mudou MD5)={c3['md5_changed']} | PASS={t6_passed}")

        # =====================================================================
        # T7: DASHBOARD MULTIAGENTE (:8085) & EXTENSÃO CHROME MV3
        # =====================================================================
        print("\n[T7] Validando Dashboard Multiagente (:8085) e Arquivos da Extensão Chrome MV3...")
        dash_code, dash_metrics = http_json(f"{DASHBOARD_URL}/api/metrics")
        ext_files = [
            ROOT_DIR / "chrome_extension" / "manifest.json",
            ROOT_DIR / "chrome_extension" / "popup.html",
            ROOT_DIR / "chrome_extension" / "popup.js",
            ROOT_DIR / "chrome_extension" / "content.js",
            ROOT_DIR / "chrome_extension" / "background.js",
        ]
        all_ext_exist = all(p.exists() for p in ext_files)

        t7_passed = (
            dash_code == 200
            and dash_metrics.get("database") == "WebWorker_db"
            and dash_metrics.get("cloud_fallback_max_priority") == 3
            and all_ext_exist
        )
        report["tests"]["T7_MultiAgent_Dashboard_And_Chrome_Extension"] = {
            "passed": t7_passed,
            "dashboard_http_status": dash_code,
            "dashboard_active_workers_count": len(dash_metrics.get("workers", {})),
            "chrome_extension_mv3_files_verified": [p.name for p in ext_files if p.exists()],
        }
        print(f"  -> Dashboard HTTP={dash_code} | Workers na Matriz={len(dash_metrics.get('workers', {}))} | Extensão MV3={all_ext_exist} | PASS={t7_passed}")

        # =====================================================================
        # T8: EXECUÇÃO PARALELA MULTIAGENTE REAL (PLAYWRIGHT + OLLAMA)
        # =====================================================================
        print("\n[T8] Testando Frota Multiagente Concorrente (2 Workers Playwright + Ollama em paralelo)...")
        from src.ollama_manager import OllamaManager
        om = OllamaManager()
        if not om.is_server_online():
            om.try_start_server()
            time.sleep(2.0)

        parallel_tasks = [
            {
                "priority": 2,
                "url": "https://animesdrive.cloud/anime/1886",
                "objective": "Qual é o título deste anime e quantos episódios estão listados na página?",
            },
            {
                "priority": 3,
                "url": "https://animeschedule.net",
                "objective": "Liste o título de 2 animes visíveis na programação atual desta página.",
            },
        ]
        t_par0 = time.perf_counter()
        parallel_results = await fleet.run_parallel_worker_tasks(parallel_tasks)
        t_par_elapsed = round(time.perf_counter() - t_par0, 2)

        workers_used = {r["worker_id"] for r in parallel_results}
        t8_passed = (
            len(parallel_results) == 2
            and len(workers_used) == 2
            and all(r.get("execution_time_seconds", 0) > 0 for r in parallel_results)
        )
        report["tests"]["T8_Parallel_MultiAgent_Fleet_Execution"] = {
            "passed": t8_passed,
            "total_wall_time_seconds": t_par_elapsed,
            "distinct_workers_used": sorted(list(workers_used)),
            "results": parallel_results,
        }
        print(f"  -> Workers paralelos acionados: {sorted(list(workers_used))} em {t_par_elapsed}s | PASS={t8_passed}")

    finally:
        dashboard.stop()
        if go_proc is not None:
            go_proc.terminate()
            try:
                go_proc.wait(timeout=3)
            except Exception:
                go_proc.kill()

    all_passed = all(t["passed"] for t in report["tests"].values())
    report["all_passed"] = all_passed

    results_dir = ROOT_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    out_file = results_dir / "day6_distributed_validation.json"
    out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"RESULTADO FINAL DA SUÍTE DO DIA 6: {'100% APROVADO' if all_passed else 'FALHA DETECTADA'}")
    print(f"Relatório JSON salvo em: {out_file}")
    print("=" * 80)
    return report


if __name__ == "__main__":
    asyncio.run(run_all_day6_tests())

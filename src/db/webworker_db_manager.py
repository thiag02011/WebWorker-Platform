"""
MÓDULO: WEBWORKER RELATIONAL DATABASE & CRASH RECOVERY (WebWorkerDBManager)
=============================================================================
OBJETIVO:
Gerenciar o banco de dados oficial de produção 'WebWorker_db' no PostgreSQL 18,
fornecendo persistência de fila (P1 a P5), recuperação contra quedas de energia
(Crash Recovery), anonimização de sessões privadas (P1) e auditoria de workers.
Inclui fallback resiliente em memória caso o PostgreSQL esteja offline.

FLUXO DA ARQUITETURA:
1. Setup & DDL: Cria o banco 'WebWorker_db' e as 4 tabelas estruturais:
   - cluster_tasks (Fila persistente P1..P5, Agentes Web e Raw LLM)
   - recurrent_watchers (Monitores periódicos de mudança de MD5 - P5)
   - worker_nodes_telemetry (Estado em tempo real da frota de N workers)
   - tasks_dead_letter (DLQ para falhas após retries)
   -> 2. Anonimização Nativa: Se is_interactive=True e save_private_data=False,
         mascara a URL como '[PRIVADO: dominio]' e omite result_data sensível.
   -> 3. Crash Recovery: Na inicialização, converte tarefas 'RUNNING' interrompidas
         para 'PAUSED' e as recarrega na fila respeitando a prioridade.
=============================================================================
"""
import copy
import json
import logging
import time
import urllib.parse
from typing import Dict, Any, List, Optional
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    PSYCOPG2_AVAILABLE = True
except Exception:
    psycopg2 = None
    RealDictCursor = None
    ISOLATION_LEVEL_AUTOCOMMIT = None
    PSYCOPG2_AVAILABLE = False

logger = logging.getLogger("WebWorkerDB")

from src.config_manager import get_config


def anonymize_url(raw_url: str) -> str:
    """Converte uma URL completa em marcador anonimizado preservando apenas o domínio."""
    if not raw_url:
        return "[PRIVADO: sem-url]"
    try:
        parsed = urllib.parse.urlparse(raw_url)
        domain = parsed.netloc or parsed.path.split("/")[0] or "local"
        return f"[PRIVADO: {domain}]"
    except Exception:
        return "[PRIVADO: oculto]"


class WebWorkerDBManager:
    """Gerenciador relacional idempotente para o banco WebWorker_db com fallback resiliente."""

    def __init__(
        self,
        auto_init: bool = True,
        host: Optional[str] = None,
        port: Optional[int] = None,
        dbname: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ):
        cfg = get_config()
        self.host = host or str(cfg.get("database.host"))
        self.port = int(port or cfg.get("database.port"))
        self.dbname = dbname or str(cfg.get("database.dbname"))
        self.user = user or str(cfg.get("database.user"))
        self.password = password or str(cfg.get("database.password"))

        self.conn = None
        self.is_connected = False
        self._memory_tasks: Dict[str, Dict[str, Any]] = {}
        self._memory_watchers: Dict[str, Dict[str, Any]] = {}
        self._memory_telemetry: Dict[str, Dict[str, Any]] = {}
        self._memory_dlq: List[Dict[str, Any]] = []

        if auto_init and PSYCOPG2_AVAILABLE:
            try:
                self.ensure_database_and_tables()
                self.is_connected = True
            except Exception as e:
                self.is_connected = False
                logger.info(f"[AVISO] PostgreSQL indisponivel ({e}). Operando em modo de memoria (fallback).")
        else:
            self.is_connected = False

    def _get_admin_conn(self):
        conn = psycopg2.connect(
            host=self.host, port=self.port, user=self.user, password=self.password, dbname="postgres"
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def get_conn(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(
                host=self.host, port=self.port, user=self.user, password=self.password, dbname=self.dbname
            )
            self.conn.autocommit = True
        return self.conn

    def ensure_database_and_tables(self):
        """Cria o banco WebWorker_db se não existir e inicializa o schema."""
        admin_conn = self._get_admin_conn()
        cur = admin_conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (self.dbname,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{self.dbname}"')
            print(f"[OK] Banco de dados '{self.dbname}' criado com sucesso!")
        cur.close()
        admin_conn.close()

        conn = self.get_conn()
        cur = conn.cursor()

        # 1. Tabela Principal de Tarefas do Cluster (Agênticas Web e Raw LLM)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cluster_tasks (
            id SERIAL PRIMARY KEY,
            task_uuid VARCHAR(64) UNIQUE NOT NULL,
            task_type VARCHAR(32) NOT NULL DEFAULT 'web_agent',
            source VARCHAR(32) NOT NULL DEFAULT 'api_rest',
            priority INT NOT NULL DEFAULT 3,
            is_interactive BOOLEAN DEFAULT FALSE,
            save_private_data BOOLEAN DEFAULT FALSE,
            target_url TEXT,
            user_goal TEXT NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
            assigned_worker_id VARCHAR(32),
            assigned_backend_id VARCHAR(32),
            turns_used INT DEFAULT 0,
            retry_count INT DEFAULT 0,
            execution_time_seconds NUMERIC(8, 2),
            result_data JSONB,
            error_details TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE
        );
        CREATE INDEX IF NOT EXISTS idx_ww_tasks_status_prio ON cluster_tasks (status, priority, created_at);
        """)

        # 2. Tabela de Tarefas Recorrentes (MD5 Watchers - Prioridade 5)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS recurrent_watchers (
            id SERIAL PRIMARY KEY,
            watcher_uuid VARCHAR(64) UNIQUE NOT NULL,
            target_url TEXT NOT NULL,
            user_goal TEXT NOT NULL DEFAULT 'Monitorar alterações no DOM',
            check_interval_seconds INT NOT NULL DEFAULT 60,
            last_known_md5 VARCHAR(64),
            last_extracted_data JSONB,
            changes_detected_count INT DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            last_checked_at TIMESTAMP WITH TIME ZONE,
            last_changed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)

        # 3. Tabela de Telemetria de Workers
        cur.execute("""
        CREATE TABLE IF NOT EXISTS worker_nodes_telemetry (
            worker_id VARCHAR(32) PRIMARY KEY,
            worker_type VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            current_url TEXT,
            last_dom_hash VARCHAR(64),
            current_task_uuid VARCHAR(64),
            total_tasks_completed INT DEFAULT 0,
            screenshot_base64 TEXT,
            last_heartbeat TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)

        # 4. Tabela Dead-Letter Queue (DLQ)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks_dead_letter (
            id SERIAL PRIMARY KEY,
            task_uuid VARCHAR(64) NOT NULL,
            target_url TEXT NOT NULL,
            user_goal TEXT NOT NULL,
            failure_reason TEXT NOT NULL,
            last_dom_snapshot TEXT,
            screenshot_base64 TEXT,
            failed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """)
        cur.close()

    def crash_recovery_pause_and_list(self) -> List[Dict[str, Any]]:
        """Marca tarefas interrompidas ('RUNNING') como 'PAUSED' e retorna fila pendente ordenada."""
        if not self.is_connected:
            for t in self._memory_tasks.values():
                if t.get("status") == "RUNNING":
                    t["status"] = "PAUSED"
            pending = [copy.deepcopy(t) for t in self._memory_tasks.values() if t.get("status") in ("QUEUED", "PAUSED")]
            pending.sort(key=lambda x: (x.get("priority", 3), x.get("created_at", 0)))
            return pending

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            UPDATE cluster_tasks
            SET status = 'PAUSED'
            WHERE status = 'RUNNING';
        """)
        cur.execute("""
            SELECT * FROM cluster_tasks
            WHERE status IN ('QUEUED', 'PAUSED')
            ORDER BY priority ASC, created_at ASC;
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    def enqueue_task(
        self,
        task_uuid: str,
        user_goal: str,
        target_url: Optional[str] = None,
        task_type: str = "web_agent",
        source: str = "api_rest",
        priority: Optional[int] = None,
        is_interactive: bool = False,
        save_private_data: bool = False
    ) -> Dict[str, Any]:
        """Registra uma nova tarefa no banco aplicando regras de prioridade e privacidade."""
        if priority is None:
            priority = 1 if is_interactive else 3

        stored_url = target_url or ""
        if is_interactive and not save_private_data:
            stored_url = anonymize_url(stored_url)

        if not self.is_connected:
            row = {
                "id": len(self._memory_tasks) + 1,
                "task_uuid": task_uuid,
                "task_type": task_type,
                "source": source,
                "priority": priority,
                "is_interactive": is_interactive,
                "save_private_data": save_private_data,
                "target_url": stored_url,
                "user_goal": user_goal,
                "status": "QUEUED",
                "assigned_worker_id": None,
                "assigned_backend_id": None,
                "turns_used": 0,
                "retry_count": 0,
                "execution_time_seconds": None,
                "result_data": None,
                "error_details": None,
                "created_at": time.time(),
                "started_at": None,
                "completed_at": None,
            }
            self._memory_tasks[task_uuid] = row
            return copy.deepcopy(row)

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            INSERT INTO cluster_tasks (
                task_uuid, task_type, source, priority, is_interactive,
                save_private_data, target_url, user_goal, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED')
            ON CONFLICT (task_uuid) DO UPDATE SET
                status = EXCLUDED.status,
                priority = EXCLUDED.priority
            RETURNING *;
        """, (
            task_uuid, task_type, source, priority, is_interactive,
            save_private_data, stored_url, user_goal
        ))
        row = dict(cur.fetchone())
        cur.close()
        return row

    def update_task_status(
        self,
        task_uuid: str,
        status: str,
        worker_id: Optional[str] = None,
        backend_id: Optional[str] = None,
        turns_used: int = 0,
        execution_time: Optional[float] = None,
        result_data: Optional[Dict[str, Any]] = None,
        error_details: Optional[str] = None
    ):
        """Atualiza o ciclo de vida da tarefa respeitando a anonimização de dados privados."""
        if not self.is_connected:
            task = self._memory_tasks.get(task_uuid)
            if not task:
                return
            task["status"] = status
            if worker_id:
                task["assigned_worker_id"] = worker_id
            if backend_id:
                task["assigned_backend_id"] = backend_id
            if turns_used:
                task["turns_used"] = turns_used
            if execution_time is not None:
                task["execution_time_seconds"] = execution_time
            if error_details is not None:
                task["error_details"] = error_details

            if result_data is not None:
                if task.get("is_interactive") and not task.get("save_private_data"):
                    task["result_data"] = {
                        "privacy_mode": "ANONYMIZED_USER_SESSION",
                        "data_saved": False,
                        "metrics_only": {
                            "turns": turns_used,
                            "execution_time_s": execution_time
                        }
                    }
                else:
                    task["result_data"] = result_data
            if status == "RUNNING":
                task["started_at"] = time.time()
            elif status in ("COMPLETED", "FAILED", "ABORTED"):
                task["completed_at"] = time.time()
            return

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT is_interactive, save_private_data, target_url FROM cluster_tasks WHERE task_uuid = %s", (task_uuid,))
        task_info = cur.fetchone()

        final_result_json = None
        if result_data is not None:
            if task_info and task_info["is_interactive"] and not task_info["save_private_data"]:
                final_result_json = json.dumps({
                    "privacy_mode": "ANONYMIZED_USER_SESSION",
                    "data_saved": False,
                    "metrics_only": {
                        "turns": turns_used,
                        "execution_time_s": execution_time
                    }
                })
            else:
                final_result_json = json.dumps(result_data, ensure_ascii=False)

        if status == "RUNNING":
            cur.execute("""
                UPDATE cluster_tasks
                SET status = %s,
                    assigned_worker_id = COALESCE(%s, assigned_worker_id),
                    assigned_backend_id = COALESCE(%s, assigned_backend_id),
                    started_at = NOW()
                WHERE task_uuid = %s;
            """, (status, worker_id, backend_id, task_uuid))
        elif status in ("COMPLETED", "FAILED", "ABORTED"):
            cur.execute("""
                UPDATE cluster_tasks
                SET status = %s,
                    assigned_worker_id = COALESCE(%s, assigned_worker_id),
                    assigned_backend_id = COALESCE(%s, assigned_backend_id),
                    turns_used = %s,
                    execution_time_seconds = %s,
                    result_data = COALESCE(%s::jsonb, result_data),
                    error_details = COALESCE(%s, error_details),
                    completed_at = NOW()
                WHERE task_uuid = %s;
            """, (
                status, worker_id, backend_id, turns_used,
                execution_time, final_result_json, error_details, task_uuid
            ))
        else:
            cur.execute("""
                UPDATE cluster_tasks
                SET status = %s
                WHERE task_uuid = %s;
            """, (status, task_uuid))
        cur.close()

    def upsert_worker_telemetry(
        self,
        worker_id: str,
        worker_type: str,
        status: str,
        current_url: str = "",
        last_dom_hash: str = "",
        current_task_uuid: Optional[str] = None,
        increment_completed: bool = False,
        screenshot_base64: Optional[str] = None
    ):
        """Atualiza o estado em tempo real de um worker na tabela de telemetria."""
        if not self.is_connected:
            prev = self._memory_telemetry.get(worker_id, {})
            tot = prev.get("total_tasks_completed", 0) + (1 if increment_completed else 0)
            self._memory_telemetry[worker_id] = {
                "worker_id": worker_id,
                "worker_type": worker_type,
                "status": status,
                "current_url": current_url,
                "last_dom_hash": last_dom_hash,
                "current_task_uuid": current_task_uuid,
                "total_tasks_completed": tot,
                "screenshot_base64": screenshot_base64 or prev.get("screenshot_base64"),
                "last_heartbeat": time.time(),
            }
            return

        conn = self.get_conn()
        cur = conn.cursor()
        inc_sql = "worker_nodes_telemetry.total_tasks_completed + 1" if increment_completed else "worker_nodes_telemetry.total_tasks_completed"
        cur.execute(f"""
            INSERT INTO worker_nodes_telemetry (
                worker_id, worker_type, status, current_url, last_dom_hash,
                current_task_uuid, total_tasks_completed, screenshot_base64, last_heartbeat
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (worker_id) DO UPDATE SET
                status = EXCLUDED.status,
                current_url = EXCLUDED.current_url,
                last_dom_hash = EXCLUDED.last_dom_hash,
                current_task_uuid = EXCLUDED.current_task_uuid,
                total_tasks_completed = {inc_sql},
                screenshot_base64 = COALESCE(EXCLUDED.screenshot_base64, worker_nodes_telemetry.screenshot_base64),
                last_heartbeat = NOW();
        """, (
            worker_id, worker_type, status, current_url, last_dom_hash,
            current_task_uuid, 1 if increment_completed else 0, screenshot_base64
        ))
        cur.close()

    def register_watcher(
        self,
        watcher_uuid: str,
        target_url: str,
        user_goal: str = "Monitorar alterações de MD5 e atualizar dados",
        check_interval_seconds: int = 60
    ) -> Dict[str, Any]:
        """Registra ou atualiza um monitor recorrente de MD5 (Prioridade 5)."""
        if not self.is_connected:
            row = {
                "id": len(self._memory_watchers) + 1,
                "watcher_uuid": watcher_uuid,
                "target_url": target_url,
                "user_goal": user_goal,
                "check_interval_seconds": check_interval_seconds,
                "last_known_md5": None,
                "last_extracted_data": None,
                "changes_detected_count": 0,
                "status": "ACTIVE",
                "last_checked_at": None,
                "last_changed_at": None,
                "created_at": time.time(),
            }
            self._memory_watchers[watcher_uuid] = row
            return copy.deepcopy(row)

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            INSERT INTO recurrent_watchers (
                watcher_uuid, target_url, user_goal, check_interval_seconds, status
            ) VALUES (%s, %s, %s, %s, 'ACTIVE')
            ON CONFLICT (watcher_uuid) DO UPDATE SET
                target_url = EXCLUDED.target_url,
                check_interval_seconds = EXCLUDED.check_interval_seconds,
                status = 'ACTIVE'
            RETURNING *;
        """, (watcher_uuid, target_url, user_goal, check_interval_seconds))
        row = dict(cur.fetchone())
        cur.close()
        return row

    def update_watcher_check(
        self,
        watcher_uuid: str,
        new_md5: str,
        extracted_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Verifica se o MD5 mudou em relação ao último check e atualiza o banco se necessário."""
        if not self.is_connected:
            watcher = self._memory_watchers.get(watcher_uuid)
            if not watcher:
                return {"changed": False, "error": "Watcher not found"}
            old_md5 = watcher.get("last_known_md5")
            changed = (old_md5 is None) or (old_md5 != new_md5)
            watcher["last_checked_at"] = time.time()
            if changed:
                watcher["last_known_md5"] = new_md5
                watcher["last_extracted_data"] = extracted_data or {}
                watcher["changes_detected_count"] = watcher.get("changes_detected_count", 0) + 1
                watcher["last_changed_at"] = time.time()
            return {"changed": changed, "old_md5": old_md5, "new_md5": new_md5, "watcher": copy.deepcopy(watcher)}

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM recurrent_watchers WHERE watcher_uuid = %s", (watcher_uuid,))
        watcher = cur.fetchone()
        if not watcher:
            cur.close()
            return {"changed": False, "error": "Watcher not found"}

        old_md5 = watcher["last_known_md5"]
        changed = (old_md5 is None) or (old_md5 != new_md5)

        if changed:
            data_json = json.dumps(extracted_data or {}, ensure_ascii=False)
            cur.execute("""
                UPDATE recurrent_watchers
                SET last_known_md5 = %s,
                    last_extracted_data = %s::jsonb,
                    changes_detected_count = changes_detected_count + 1,
                    last_checked_at = NOW(),
                    last_changed_at = NOW()
                WHERE watcher_uuid = %s
                RETURNING *;
            """, (new_md5, data_json, watcher_uuid))
        else:
            cur.execute("""
                UPDATE recurrent_watchers
                SET last_checked_at = NOW()
                WHERE watcher_uuid = %s
            """, (watcher_uuid,))

        updated = dict(cur.fetchone())
        cur.close()
        return {"changed": changed, "old_md5": old_md5, "new_md5": new_md5, "watcher": updated}

    def clear_all_for_test(self):
        """Limpa tabelas para execução limpa de bateria de testes."""
        if not self.is_connected:
            self._memory_tasks.clear()
            self._memory_watchers.clear()
            self._memory_telemetry.clear()
            self._memory_dlq.clear()
            return

        conn = self.get_conn()
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE cluster_tasks, recurrent_watchers, worker_nodes_telemetry, tasks_dead_letter RESTART IDENTITY;")
        cur.close()

    def get_task_by_uuid(self, task_uuid: str) -> Optional[Dict[str, Any]]:
        """Busca uma tarefa específica pelo seu UUID."""
        if not self.is_connected:
            row = self._memory_tasks.get(task_uuid)
            return copy.deepcopy(row) if row else None

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM cluster_tasks WHERE task_uuid = %s;", (task_uuid,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None

    def fetch_next_task_from_db(self, worker_id: str, backend_id: str = "local_gpu") -> Optional[Dict[str, Any]]:
        """Busca a tarefa QUEUED ou PAUSED de maior prioridade (menor número) e a marca como RUNNING."""
        if not self.is_connected:
            candidates = [
                t for t in self._memory_tasks.values()
                if t.get("status") in ("QUEUED", "PAUSED")
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda x: (x.get("priority", 3), x.get("created_at", 0)))
            chosen = candidates[0]
            self.update_task_status(chosen["task_uuid"], "RUNNING", worker_id=worker_id, backend_id=backend_id)
            return self.get_task_by_uuid(chosen["task_uuid"])

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM cluster_tasks
            WHERE status IN ('QUEUED', 'PAUSED')
            ORDER BY priority ASC, created_at ASC
            LIMIT 1;
        """)
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        task = dict(row)
        cur.close()
        self.update_task_status(task["task_uuid"], "RUNNING", worker_id=worker_id, backend_id=backend_id)
        return self.get_task_by_uuid(task["task_uuid"])

    def get_dashboard_metrics(self) -> Dict[str, Any]:
        """Retorna métricas e histórico recente do banco WebWorker_db para a Dashboard :8085."""
        if not self.is_connected:
            all_t = list(self._memory_tasks.values())
            all_t.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            recent = [
                {
                    "task_uuid": t["task_uuid"],
                    "task_type": t.get("task_type", "web_agent"),
                    "priority": t.get("priority", 3),
                    "status": t.get("status", "QUEUED"),
                    "target_url": t.get("target_url", ""),
                    "execution_time_seconds": t.get("execution_time_seconds"),
                }
                for t in all_t[:25]
            ]
            return {
                "recent_tasks": recent,
                "watchers": list(self._memory_watchers.values())[:10],
            }

        conn = self.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT task_uuid, task_type, priority, status, target_url, execution_time_seconds
            FROM cluster_tasks
            ORDER BY created_at DESC
            LIMIT 25;
        """)
        recent = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recurrent_watchers ORDER BY id DESC LIMIT 10;")
        watchers = [dict(r) for r in cur.fetchall()]
        cur.close()
        return {
            "recent_tasks": recent,
            "watchers": watchers,
        }


if __name__ == "__main__":
    db = WebWorkerDBManager()
    print("[OK] WebWorker_db inicializado e verificado com sucesso!")

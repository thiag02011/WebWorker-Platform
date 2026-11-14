"""
===============================================================================
MÓDULO: Gerenciador Central de Configurações (ConfigManager)
CAMINHO: src/config_manager.py
===============================================================================

OBJETIVO:
Fornecer fonte única de verdade (Single Source of Truth) para o sistema:
- Auto-criação do arquivo 'config.json' com valores padrão caso não exista.
- Define 'https://www.google.com' como base_url padrão de navegação.
- Centraliza portas de rede (Go Gateway :8000, Dashboard :8085).
- Mapeia backends de LLM (GPU Local, Nó LAN e Fallback Cloud <= P3).
- Configura credenciais e conexão do banco PostgreSQL oficial (WebWorker_db).

FLUXO DA ARQUITETURA:
┌─────────────────────────────────────────────────────────────────────────────┐
│ [Inicialização do Sistema (Python / Go)]                                    │
│        │                                                                    │
│        ▼                                                                    │
│ [ConfigManager.load_or_create()] ──> Existe 'config.json'?                  │
│        ├── NÃO ──> Gera 'config.json' com defaults (Google.com, :8000, etc) │
│        └── SIM ──> Carrega JSON e mescla chaves ausentes com defaults       │
│        │                                                                    │
│        ▼                                                                    │
│ [Injeção nos Componentes]: BrowserEngine, GoGateway, WorkerFleet, DBManager │
└─────────────────────────────────────────────────────────────────────────────┘
===============================================================================
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("ConfigManager")

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": "1.1",
    "browser": {
        "default_base_url": "https://www.google.com",
        "headless": True,
        "enable_adblock": True,
        "timeout_ms": 20000,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "cdp_endpoint": "http://127.0.0.1:9222",
        "user_data_dir": ""
    },
    "servers": {
        "gateway_port": 8000,
        "dashboard_port": 8085
    },
    "fleet": {
        "num_workers": 3,
        "workers_available": 1,
        "max_turns": 12,
        "max_find_per_page": 1
    },
    "llm_backends": {
        "local_gpu": {
            "id": "local_gpu",
            "type": "ollama_local",
            "endpoint": "http://127.0.0.1:11434",
            "model": "qwen2.5:3b",
            "device": "GPU Local / Hardware",
            "max_concurrent": 2
        },
        "lan_node": {
            "id": "lan_node_gpu2",
            "type": "ollama_lan",
            "endpoint": "http://ollama-lan.local:11434",
            "model": "qwen2.5:3b",
            "device": "LAN Secondary Node",
            "max_concurrent": 2
        },
        "cloud_fallback": {
            "id": "cloud_gemini_fallback",
            "type": "cloud_gemini",
            "endpoint": "https://generativelanguage.googleapis.com",
            "model": "gemini-2.5-flash",
            "api_key": "",
            "max_allowed_priority": 3,
            "max_concurrent": 5
        }
    },
    "database": {
        "engine": "postgres",
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "WebWorker_db",
        "user": "postgres",
        "password": os.environ.get("WEBWORKER_DB_PASSWORD", ""),
        "sqlite_path": "data/webworker.db"
    }
}


class ConfigManager:
    """Gerenciador singleton de configurações com auto-criação resiliente de arquivo."""

    _instance: Optional["ConfigManager"] = None

    def __init__(self, custom_path: Optional[Path] = None):
        if custom_path:
            self.config_path = Path(custom_path).resolve()
        else:
            # Procura a raiz do projeto (onde está o README ou o go_services)
            current_dir = Path(__file__).resolve().parent
            repo_root = current_dir.parent
            self.config_path = repo_root / "config.json"

        self._data: Dict[str, Any] = {}
        self.load_or_create()

    @classmethod
    def get_instance(cls, custom_path: Optional[Path] = None) -> "ConfigManager":
        if cls._instance is None or custom_path is not None:
            cls._instance = ConfigManager(custom_path)
        return cls._instance

    def load_or_create(self) -> Dict[str, Any]:
        """Carrega o arquivo se existir ou cria com defaults estruturados."""
        if not self.config_path.exists():
            self._data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save()
            logger.info(f"Arquivo de configuracao gerado automaticamente em: {self.config_path}")
        else:
            try:
                content = self.config_path.read_text(encoding="utf-8")
                loaded = json.loads(content)
                self._data = self._deep_merge(DEFAULT_CONFIG, loaded)
            except Exception as e:
                logger.warning(f"Erro ao carregar {self.config_path} ({e}). Usando defaults.")
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()

        # Valida e auto-corrige valores semanticamente invalidos ou absurdos
        self.validate_and_sanitize()

        # Também sincroniza uma cópia na pasta go_services/ se ela existir
        go_dir = self.config_path.parent / "go_services"
        if go_dir.exists():
            go_config = go_dir / "config.json"
            try:
                go_config.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return self._data

    def _set_memory(self, key_path: str, value: Any) -> None:
        """Define valor em memoria na estrutura de configuracao sem disparar save imediato."""
        parts = key_path.split(".")
        current = self._data
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def validate_and_sanitize(self) -> List[str]:
        """
        Valida e auto-corrige semanticamente configuracoes invalidas ou absurdas.
        Retorna a lista de avisos de auto-correcao aplicados.
        """
        warnings = []

        def _get_default(key_path: str) -> Any:
            parts = key_path.split(".")
            cur = DEFAULT_CONFIG
            for p in parts:
                cur = cur[p]
            return cur

        # 1. Versao
        v = self.get("version")
        if not isinstance(v, str) or not v.strip():
            def_v = _get_default("version")
            warnings.append(f"[AVISO] Campo 'version' invalido ({v}). Auto-corrigido para '{def_v}'.")
            self._set_memory("version", def_v)

        # 2. Portas de rede (gateway_port, dashboard_port, database.port)
        for port_key in ["servers.gateway_port", "servers.dashboard_port", "database.port"]:
            val = self.get(port_key)
            is_valid = False
            try:
                val_int = int(val)
                if 1024 <= val_int <= 65535:
                    is_valid = True
                    if val != val_int:
                        self._set_memory(port_key, val_int)
            except (ValueError, TypeError):
                is_valid = False

            if not is_valid:
                def_p = _get_default(port_key)
                warnings.append(f"[AVISO] Porta invalida em '{port_key}' ({val}). Auto-corrigido para {def_p}.")
                self._set_memory(port_key, def_p)

        # 3. Conflito entre gateway_port e dashboard_port
        gw_p = self.get("servers.gateway_port")
        dash_p = self.get("servers.dashboard_port")
        if gw_p == dash_p:
            def_dash = _get_default("servers.dashboard_port")
            if gw_p == def_dash:
                def_dash = 8086
            warnings.append(
                f"[AVISO] Conflito de portas: 'servers.gateway_port' e 'servers.dashboard_port' "
                f"nao podem ser iguais ({gw_p}). 'servers.dashboard_port' auto-corrigido para {def_dash}."
            )
            self._set_memory("servers.dashboard_port", def_dash)

        # 4. Frota de workers
        workers = self.get("fleet.num_workers")
        valid_workers = False
        try:
            w_int = int(workers)
            if 1 <= w_int <= 32:
                valid_workers = True
                if workers != w_int:
                    self._set_memory("fleet.num_workers", w_int)
        except (ValueError, TypeError):
            valid_workers = False

        if not valid_workers:
            def_w = _get_default("fleet.num_workers")
            warnings.append(f"[AVISO] Quantidade de workers invalida em 'fleet.num_workers' ({workers}). Auto-corrigido para {def_w}.")
            self._set_memory("fleet.num_workers", def_w)

        # 4b. Workers disponíveis (capacidade real ativa)
        total_w = int(self.get("fleet.num_workers"))
        w_avail = self.get("fleet.workers_available")
        valid_avail = False
        try:
            wa_int = int(w_avail)
            if 1 <= wa_int <= total_w:
                valid_avail = True
                if w_avail != wa_int:
                    self._set_memory("fleet.workers_available", wa_int)
        except (ValueError, TypeError):
            valid_avail = False

        if not valid_avail:
            def_wa = 1
            warnings.append(f"[AVISO] Quantidade em 'fleet.workers_available' ({w_avail}) invalida. Auto-corrigido para {def_wa}.")
            self._set_memory("fleet.workers_available", def_wa)

        # 4c. Limite maximo de turnos por tarefa (max_turns)
        m_turns = self.get("fleet.max_turns")
        valid_turns = False
        try:
            mt_int = int(m_turns)
            if 1 <= mt_int <= 32:
                valid_turns = True
                if m_turns != mt_int:
                    self._set_memory("fleet.max_turns", mt_int)
        except (ValueError, TypeError):
            valid_turns = False

        if not valid_turns:
            def_mt = _get_default("fleet.max_turns")
            warnings.append(f"[AVISO] Quantidade de turnos invalida em 'fleet.max_turns' ({m_turns}). Auto-corrigido para {def_mt}.")
            self._set_memory("fleet.max_turns", def_mt)

        # 5. Timeout do navegador
        timeout = self.get("browser.timeout_ms")
        valid_t = False
        try:
            t_int = int(timeout)
            if 1000 <= t_int <= 300000:
                valid_t = True
                if timeout != t_int:
                    self._set_memory("browser.timeout_ms", t_int)
        except (ValueError, TypeError):
            valid_t = False

        if not valid_t:
            def_t = _get_default("browser.timeout_ms")
            warnings.append(f"[AVISO] Timeout invalido em 'browser.timeout_ms' ({timeout}). Auto-corrigido para {def_t}.")
            self._set_memory("browser.timeout_ms", def_t)

        # 6. URL base do navegador
        base_url = self.get("browser.default_base_url")
        if not isinstance(base_url, str) or not (base_url.startswith("http://") or base_url.startswith("https://")):
            def_url = _get_default("browser.default_base_url")
            warnings.append(f"[AVISO] URL invalida em 'browser.default_base_url' ({base_url}). Auto-corrigido para '{def_url}'.")
            self._set_memory("browser.default_base_url", def_url)

        # 7. Motor de banco de dados
        engine = self.get("database.engine")
        if engine not in ("postgres", "sqlite", "memory"):
            def_eng = _get_default("database.engine")
            warnings.append(f"[AVISO] Motor de banco desconhecido em 'database.engine' ({engine}). Auto-corrigido para '{def_eng}'.")
            self._set_memory("database.engine", def_eng)

        # Se houve correcoes, salva automaticamente em disco
        if warnings:
            for w in warnings:
                logger.warning(w)
                print(w)
            self.save()

        return warnings

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Mescla recursivamente garantindo que novas chaves padrão não se percam."""
        merged = dict(base)
        for k, v in override.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = self._deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged

    def save(self) -> None:
        """Persiste o estado atual das configurações no disco."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Recupera valor por caminho pontilhado (ex: 'browser.default_base_url').
        """
        parts = key_path.split(".")
        current = self._data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    def set(self, key_path: str, value: Any) -> None:
        """Define valor por caminho pontilhado e salva em disco."""
        parts = key_path.split(".")
        current = self._data
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
        self.save()


def get_config() -> ConfigManager:
    """Helper global para acesso rápido às configurações."""
    return ConfigManager.get_instance()


if __name__ == "__main__":
    cfg = get_config()
    print("Configuração Carregada / Gerada com Sucesso:")
    print(f"- Base URL Padrão: {cfg.get('browser.default_base_url')}")
    print(f"- Porta Gateway Go: {cfg.get('servers.gateway_port')}")
    print(f"- Porta Dashboard: {cfg.get('servers.dashboard_port')}")
    print(f"- Arquivo: {cfg.config_path}")

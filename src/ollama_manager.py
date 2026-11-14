"""
MÓDULO: LOCAL SLM HOST & INFERENCE ORCHESTRATOR (OllamaManager)
=============================================================================
OBJETIVO:
Controlar a comunicação HTTP de baixa latência com a instância local do Ollama,
gerenciando carregamento de modelos compactos (Qwen 2.5 3B) e parâmetros de contexto.

FLUXO DA ARQUITETURA:
1. Healthcheck: Valida se o serviço Ollama está ativo em 127.0.0.1:11434.
   -> 2. Model Pre-warming: Garante que os pesos do modelo estejam alocados na VRAM da GPU.
   -> 3. Envio de Chat & Tools: Monta a carga com num_ctx, temperature=0.0 e schemas de ferramentas.
   -> 4. Parsing de Resposta: Normaliza respostas de Function Calling e textos brutos.
=============================================================================
"""
import json
import time
import subprocess
import shutil
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional, Callable


class OllamaManager:
    """Gerenciador de requisições, download e ciclo de vida de modelos no Ollama com retry."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434"):
        self.base_url = base_url.rstrip("/")
        self._server_process: Optional[subprocess.Popen] = None

    def is_server_online(self) -> bool:
        """Verifica se o servidor Ollama está respondendo em /."""
        try:
            req = urllib.request.Request(f"{self.base_url}/", headers={"User-Agent": "BenchRunner/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def try_start_server(self) -> bool:
        """Tenta iniciar o processo 'ollama serve' em segundo plano se disponível."""
        if self.is_server_online():
            return True

        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            # Caminho padrão no Windows
            from pathlib import Path
            import os
            win_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if win_path.exists():
                ollama_bin = str(win_path)

        if ollama_bin:
            try:
                # Inicia desanexado do console
                creationflags = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags = subprocess.CREATE_NO_WINDOW
                self._server_process = subprocess.Popen(
                    [ollama_bin, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags
                )
                return True
            except Exception:
                pass
        return False

    def wait_for_server(
        self,
        timeout: float = 120.0,
        retry_interval: float = 2.0,
        auto_start: bool = True,
        status_callback: Optional[Callable[[int, float], None]] = None
    ) -> bool:
        """
        Aguarda o servidor Ollama responder em loop de retry.
        Se auto_start=True, tenta disparar 'ollama serve' caso esteja offline.
        """
        if self.is_server_online():
            return True

        if auto_start:
            self.try_start_server()

        start_time = time.time()
        attempt = 0

        while (time.time() - start_time) < timeout:
            attempt += 1
            if self.is_server_online():
                return True

            elapsed = round(time.time() - start_time, 1)
            if status_callback:
                status_callback(attempt, elapsed)

            time.sleep(retry_interval)

        return self.is_server_online()

    def list_local_models(self) -> List[str]:
        """Retorna as tags dos modelos já instalados no Ollama."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = data.get("models", [])
                    return [m.get("name") for m in models if "name" in m]
        except Exception:
            pass
        return []

    def pull_model(self, tag: str, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> bool:
        """Dispara o download (pull) de um modelo com streaming de progresso."""
        url = f"{self.base_url}/api/pull"
        payload = json.dumps({"name": tag, "stream": True}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=1200) as resp:
                for line in resp:
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                        if progress_callback:
                            progress_callback(chunk)
                        if chunk.get("status") == "success":
                            return True
                    except Exception:
                        continue
            return True
        except Exception as e:
            if progress_callback:
                progress_callback({"status": f"error: {str(e)}"})
            return False

    def ensure_model_available(self, tag: str, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> bool:
        """Garante que o modelo esteja baixado localmente."""
        local_models = self.list_local_models()
        # Normaliza comparando com e sem ':latest'
        base_tag = tag if ":" in tag else f"{tag}:latest"
        for m in local_models:
            m_norm = m if ":" in m else f"{m}:latest"
            if m_norm == base_tag or m == tag:
                return True

        # Se não existe, executa o download
        return self.pull_model(tag, progress_callback)

    def list_models(self) -> List[str]:
        """Retorna a lista de nomes/tags dos modelos instalados localmente no Ollama."""
        url = f"{self.base_url}/api/tags"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models", [])
                return [m.get("name", "") for m in models if "name" in m]
        except Exception:
            return []

    def unload_model(self, tag: str) -> bool:
        """
        Obriga o Ollama a descarregar o modelo da VRAM enviando keep_alive: 0.
        Garante que a GPU fique limpa entre modelos para evitar contaminação do Cache KV.
        """
        url = f"{self.base_url}/api/generate"
        payload = json.dumps({"model": tag, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            # Tenta via /api/chat também
            try:
                chat_url = f"{self.base_url}/api/chat"
                chat_payload = json.dumps({"model": tag, "messages": [], "keep_alive": 0}).encode("utf-8")
                chat_req = urllib.request.Request(chat_url, data=chat_payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(chat_req, timeout=10) as resp2:
                    return resp2.status == 200
            except Exception:
                return False

    def chat(
        self,
        payload: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        options: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        max_retries: int = 3,
        retry_delay: float = 2.0
    ) -> Dict[str, Any]:
        """
        Envia requisição síncrona para /api/chat com política de retry automático contra falhas de conexão.
        """
        if payload is None:
            payload = {
                "model": model,
                "messages": messages or [],
            }
            if tools:
                payload["tools"] = tools
            if options:
                payload["options"] = options

        url = f"{self.base_url}/api/chat"
        payload["stream"] = False
        data_bytes = json.dumps(payload).encode("utf-8")

        last_error = None
        current_delay = retry_delay

        for attempt in range(1, max_retries + 1):
            req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                # Se for erro 500 do modelo carregando, tenta aguardar brevemente
                if e.code in (500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(current_delay)
                    current_delay *= 1.5
                    continue
                raise RuntimeError(f"Ollama HTTP {e.code}: {err_body}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = e
                # Falha de conexão: se ainda houver tentativas, tenta reconectar
                if attempt < max_retries:
                    time.sleep(current_delay)
                    current_delay *= 1.5
                    # Checa se o servidor caiu e precisa de wait_for_server
                    if not self.is_server_online():
                        self.wait_for_server(timeout=15, auto_start=True)
                    continue

        raise RuntimeError(f"Falha de conexão persistente com o Ollama após {max_retries} tentativas: {str(last_error)}")

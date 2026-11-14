#!python3.15
"""
===============================================================================
LAUNCHER MESTRE: INICIALIZADOR INTEGRADO DA PLATAFORMA (start_platform.py)
CAMINHO: start_platform.py (Raiz do projeto WebWorker-Platform)
===============================================================================
Sequencia de Inicializacao:
1. Leitura dinamica da versao do sistema a partir de config.json (ConfigManager)
2. Verificacao de arquivos necessarios e disponibilidade de portas (:8000, :8085)
3. Inicializacao e teste de saude do microservico Go Gateway
4. Inicializacao da frota MultiAgentWorkerFleet com exibicao dos workers
5. Inicializacao do Dashboard Web matriz (:8085)
6. Aguardo de interrupcao (Ctrl+C) com encerramento gracioso dos processos
===============================================================================
"""

import os
import sys
import time
import socket
import subprocess
import urllib.request
import urllib.error
import pathlib
from typing import Optional

# Garante que o diretorio de trabalho (cwd) seja sempre a raiz do projeto
BASE_DIR = pathlib.Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config_manager import get_config
from src.multiagent.worker_fleet import MultiAgentWorkerFleet
from src.dashboard.multiagent_dashboard import MultiAgentDashboardServer


def get_platform_version() -> str:
    """Obtem a versao configurada da plataforma via ConfigManager."""
    cfg = get_config()
    version = cfg.get("version")
    if not version:
        raise ValueError("Chave 'version' ausente no arquivo de configuracao (config.json).")
    return str(version)


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """
    Verifica se uma porta de rede esta em uso tentando realizar bind local.
    Retorna True se a porta estiver ocupada, False se estiver livre.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True


def ping_go_gateway(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    """
    Realiza requisicao HTTP de teste na rota de metricas do Gateway Go.
    Utiliza ProxyHandler vazio para garantir que proxies de sistema nao interfiram em conexoes locais.
    """
    url = f"http://{host}:{port}/api/metrics"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WebWorker-HealthCheck"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def verify_prerequisites(gateway_port: int, dashboard_port: int, exe_path: pathlib.Path) -> bool:
    """Valida a presenca do binario Go e a disponibilidade das portas de rede."""
    print("[1/4] Verificando arquivos e portas do sistema...")

    # 1. Checagem do binario Go (sem compilar por conta propria)
    if not exe_path.exists():
        print(f"[ERRO] Binario do Gateway Go nao encontrado em: {exe_path}")
        print("[INFO] Compile previamente o binario acessando a pasta 'go_services' e executando:")
        print("       go build -o webworker_gateway.exe .")
        return False

    print(f"[OK] Binario Go Gateway localizado: {exe_path.name}")

    # 2. Checagem da porta do Gateway Go (:8000)
    if is_port_in_use(gateway_port):
        if ping_go_gateway(gateway_port):
            print(f"[INFO] Gateway Go ja esta ativo e respondendo na porta {gateway_port}.")
        else:
            print(f"[ERRO] A porta {gateway_port} ja esta em uso por outro processo que nao responde ao Gateway Go.")
            print(f"[INFO] Finalize o processo ocupando a porta {gateway_port} antes de iniciar.")
            return False
    else:
        print(f"[OK] Porta {gateway_port} (Gateway Go) livre.")

    # 3. Checagem da porta do Dashboard Web (:8085)
    if is_port_in_use(dashboard_port):
        print(f"[ERRO] A porta {dashboard_port} (Dashboard Web) ja esta em uso por outro processo.")
        print(f"[INFO] Finalize o processo ocupando a porta {dashboard_port} antes de iniciar.")
        return False

    print(f"[OK] Porta {dashboard_port} (Dashboard Web) livre.")
    return True


def start_go_gateway(gateway_port: int, exe_path: pathlib.Path, timeout_sec: float = 6.0) -> Optional[subprocess.Popen]:
    """Inicia o processo do microservico Go Gateway se ele ja nao estiver ativo."""
    if ping_go_gateway(gateway_port):
        print(f"[INFO] Gateway Go ja esta em execucao na porta {gateway_port}. Reutilizando processo ativo.")
        return None

    print(f"[INFO] Iniciando microservico Go Gateway na porta {gateway_port}...")
    proc = subprocess.Popen(
        [str(exe_path), f"--port={gateway_port}"],
        cwd=str(exe_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        if proc.poll() is not None:
            stderr_out = ""
            if proc.stderr:
                try:
                    stderr_out = proc.stderr.read().decode("utf-8", errors="ignore").strip()
                except Exception:
                    pass
            print(f"[ERRO] O processo Go encerrou imediatamente com codigo {proc.returncode}.")
            if stderr_out:
                print(f"[DETALHES] {stderr_out}")
            return None

        # Verifica saude via HTTP ping ou verificacao de porta ocupada pelo processo
        if ping_go_gateway(gateway_port) or is_port_in_use(gateway_port):
            return proc

        time.sleep(0.2)

    print(f"[ERRO] Timeout aguardando o Gateway Go responder na porta {gateway_port}.")
    return None


def main():
    version = get_platform_version()
    print("=" * 78)
    print(f"PLATAFORMA WEBWORKER - INICIALIZACAO DO SISTEMA (Versao {version})")
    print("=" * 78)

    cfg = get_config()
    gateway_port = int(cfg.get("servers.gateway_port"))
    dashboard_port = int(cfg.get("servers.dashboard_port"))
    num_workers = int(cfg.get("fleet.num_workers"))
    workers_available = int(cfg.get("fleet.workers_available"))

    exe_name = "webworker_gateway.exe" if os.name == "nt" else "webworker_gateway"
    exe_path = BASE_DIR / "go_services" / exe_name

    # Passo 1: Verificacao de arquivos e portas
    if not verify_prerequisites(gateway_port, dashboard_port, exe_path):
        print("[FALHA] Inicializacao abortada devido a pendencias nos pre-requisitos.")
        sys.exit(1)

    # Passo 2: Inicializacao do microservico Go Gateway
    print(f"\n[2/4] Inicializando microservico Go Gateway (:{gateway_port})...")
    go_proc = None
    if not ping_go_gateway(gateway_port):
        go_proc = start_go_gateway(gateway_port, exe_path)
        if not go_proc and not is_port_in_use(gateway_port):
            print("[FALHA] Nao foi possivel iniciar o Gateway Go.")
            sys.exit(1)
        print(f"[OK] Microservico Go Gateway ativo na porta {gateway_port}.")
    else:
        print(f"[OK] Microservico Go Gateway conectado na porta {gateway_port}.")

    # Passo 3: Inicializacao da Frota de Workers (MultiAgentWorkerFleet)
    print(f"\n[3/4] Inicializando MultiAgentWorkerFleet...")
    print(f"[INFO] Quantidade de workers configurada: {num_workers} (Disponiveis: {workers_available})")
    try:
        fleet = MultiAgentWorkerFleet(
            num_workers=num_workers,
            go_gateway_url=f"http://127.0.0.1:{gateway_port}"
        )
        print(f"[OK] Frota inicializada com sucesso: {num_workers} workers configurados ({fleet.workers_available} ativo(s)).")
    except Exception as e:
        print(f"[ERRO] Erro ao instanciar MultiAgentWorkerFleet: {e}")
        if go_proc:
            go_proc.terminate()
        sys.exit(1)

    # Passo 4: Inicializacao do Dashboard Web
    print(f"\n[4/4] Inicializando Dashboard Web na porta {dashboard_port}...")
    try:
        dashboard = MultiAgentDashboardServer(
            port=dashboard_port,
            go_gateway_url=f"http://127.0.0.1:{gateway_port}",
            fleet_ref=fleet
        )
        dashboard.start_background()
        print(f"[OK] Dashboard Web iniciado em segundo plano na porta {dashboard_port}.")
    except Exception as e:
        print(f"[ERRO] Erro ao inicializar Dashboard Web: {e}")
        if go_proc:
            go_proc.terminate()
        sys.exit(1)

    # Registro de Listener Push no Go Gateway e Ativacao do Heartbeat 500ms
    listen_endpoint = f"http://127.0.0.1:{dashboard_port}/api/worker/dispatch"
    registered = fleet.register_with_gateway(listen_endpoint)
    if registered:
        print(f"[OK] Frota conectada ao Go Gateway para Push Dispatch ({fleet.workers_available} worker(s) disponivel(is)).")
        print(f"[OK] Heartbeat ativo a cada 500ms monitorando conexao.")

    print("\n" + "=" * 78)
    print(f"[SISTEMA PRONTO - v{version}] Todos os servicos estao ativos e operacionais:")
    print(f"  * Dashboard Web:    http://localhost:{dashboard_port}")
    print(f"  * Gateway Go API:   http://localhost:{gateway_port}")
    print(f"  * Frota de Workers: {num_workers} workers paralelos ativos")
    print(f"  * Worker CDP (P1):  Standby aguardando tarefas do usuario")
    print("-" * 78)
    print("[INFO] Abra o Dashboard no seu navegador atraves do link acima.")
    print("[INFO] Pressione Ctrl+C a qualquer momento para encerrar todos os servicos.")
    print("=" * 78 + "\n")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[INFO] Solicitacao de encerramento recebida (Ctrl+C).")
        fleet.stop()
        print("[INFO] Encerrando Dashboard Web...")
        dashboard.stop()
        if go_proc:
            print("[INFO] Encerrando processo Go Gateway...")
            go_proc.terminate()
            try:
                go_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                go_proc.kill()
        print("[OK] Todos os servicos foram finalizados com seguranca.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\n[ERRO CRITICO NA EXECUCAO]:")
        traceback.print_exc()
        try:
            input("\nPressione Enter para fechar esta janela...")
        except Exception:
            pass
        sys.exit(1)

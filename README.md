# WebWorker Platform (v1.1) — Plataforma Distribuída Multiagente

Sistema de navegação web autônoma e execução distribuída de tarefas de IA, integrando:
- **Microserviço Gateway & LLM Router em Go (`:8000`)** com fila de prioridades Min-Heap (`P1` a `P5`), thread-safety estrita (`sync.Mutex`), router de propósito geral (*Raw LLM*) e trava de custo Cloud (`<= P3`).
- **Persistência Relacional no PostgreSQL (`WebWorker_db`)** com protocolo de *Crash Recovery* (pausa e retomada no reboot), fallback em memória para testes e *Anonimização de Privacidade* para sessões do navegador do usuário.
- **Frota Concorrente de Workers Playwright com Poda Semântica de DOM** (`< 1.500` tokens), formatação tabular (`|`), filtro de notas de rodapé (`#cite_note`, `#cite_ref`) e adblock nativo em rede.
- **Agente Web ReAct Aterrado com Pré-Triagem Determinística (`TriagedWebAgent`)**: Ciclo único Observar → Decidir → Agir com busca por palavras inteiras (`browser_find_in_page`), limite configurável de buscas por página (`max_find_per_page`), normalização determinística de queries (sem custo de tokens de LLM) e aterramento de números/seletores em `browser_extract_data`.
- **Telemetria Universal de Hardware (Ollama-First)**: Detecta alocação do modelo via Ollama (`/api/ps`), suporta NVIDIA (`nvidia-smi`), AMD Radeon (`rocm-smi`) e modo CPU Host nativo (RAM física real via kernel do SO).
- **Extratores Semânticos Resilientes**: Captura de Google AI Overview com detecção de disclaimers e stop-markers fracionados imunes a quebras de tags ou linhas.
- **Extensão Google Chrome (Manifest V3)** com contador de palavras e estimativa de tokens em tempo real, botão de pânico (*Kill Switch*), som *"pop"* via Web Audio API e toasts com *fade-out*.
- **Dashboard Matriz Multiagente na porta `:8085`** para observabilidade de workers, filas e banco em tempo real.
- **Configuração Centralizada (`config.json`)**: Auto-geração a partir de defaults seguros (`config_manager.py`) ou via cópia de `config.example.json`, com suporte a variável de ambiente `WEBWORKER_DB_PASSWORD`.

---

## Guia de Inicialização Rápida

### 1. Requisitos
- **Go 1.23+** (compilação do microserviço Gateway)
- **Python 3.10+**
- **PostgreSQL** (banco `WebWorker_db` — opcional se usar fallback em memória em testes)
- **Ollama** com modelo `qwen2.5:3b` (ou outro configurado em `config.json`)

### 2. Configuração (`config.json`)
Copie o arquivo de exemplo e preencha suas credenciais locais (nunca versione `config.json` — ele já está no `.gitignore`):
```bash
cp config.example.json config.json
```
Caso não exista, o `src/config_manager.py` também cria um `config.json` padrão automaticamente no primeiro uso. Você pode definir a senha do PostgreSQL via variável de ambiente `WEBWORKER_DB_PASSWORD` ou diretamente no campo `database.password` do seu `config.json` local:
```json
{
  "browser": {
    "default_base_url": "https://www.google.com",
    "headless": true,
    "enable_adblock": true
  },
  "servers": {
    "gateway_port": 8000,
    "dashboard_port": 8085
  },
  "llm_backends": {
    "local_gpu": { "endpoint": "http://127.0.0.1:11434", "model": "qwen2.5:3b" },
    "lan_node": { "endpoint": "http://<LAN_HOST_IP>:11434", "model": "qwen2.5:3b" },
    "cloud_fallback": { "max_allowed_priority": 3 }
  }
}
```

### 3. Compilar o Microserviço Go
```bash
cd go_services
# Windows
go build -o webworker_gateway.exe .
# Linux / macOS
go build -o webworker_gateway .
```

### 4. Instalar Dependências Python
```bash
pip install -r requirements.txt
playwright install chromium
```

### 5. Iniciar a Plataforma
```bash
python start_platform.py
```
O launcher inicializa o Go Gateway (`:8000`), a frota `MultiAgentWorkerFleet` e o Dashboard Web (`:8085`).

### 6. Executar as Suítes de Testes Automatizados
```bash
# 1. Suíte TDD do Agente Web ReAct Aterrado e Pré-Triagem Determinística:
python -m unittest tests/test_tdd_grounded_react_agent.py -v

# 2. Testes Baseados em Propriedades (Hypothesis Fuzzing):
python -m unittest tests/test_property_hypothesis.py

# 3. Suíte Avançada de Mocks, Hardware e Resiliência:
python -m unittest tests/test_tdd_advanced_mock_matrix.py

# 4. Suíte TDD Completa:
python -m unittest discover -s tests -p "test_tdd_*.py"

# 5. Teste End-to-End Integrado (requer serviços ativos):
python tests/test_day6_distributed_platform.py
```

### 7. Carregar a Extensão no Google Chrome
1. Abra o Chrome e acesse `chrome://extensions/`.
2. Ative o **Modo do desenvolvedor** no canto superior direito.
3. Clique em **Carregar sem compactação** e selecione a pasta `chrome_extension/`.

---

## Estrutura do Repositório

```text
WebWorker-Platform/
├── chrome_extension/                        # Extensão Chrome MV3 (P1, KillSwitch, Toasts)
├── go_services/                             # Microserviço Go (Min-Heap P1..P5, LLM Router, Mutex)
│   ├── go.mod
│   └── main.go
├── src/
│   ├── config_manager.py                    # Auto-bootstrap e sincronizador de configurações
│   ├── gpu_telemetry.py                     # Telemetria universal Ollama-First (NVIDIA, AMD, CPU RAM)
│   ├── ollama_manager.py                    # Gerenciador de ciclo de vida do Ollama
│   ├── dashboard/                           # Dashboards de observabilidade (:8085)
│   ├── db/                                  # WebWorkerDBManager (PostgreSQL + anonimização)
│   ├── multiagent/                          # Frota de Workers Playwright e cliente Go Gateway
│   └── web/                                 # TriagedWebAgent, DOMSimplifier, SearchExtractors
├── tests/                                   # Suítes TDD, mocks determinísticos, Hypothesis e E2E
├── config.example.json                      # Modelo de configuração para uso local
├── start_platform.py                        # Launcher integrado da plataforma
├── requirements.txt                         # Dependências Python
├── HABILIDADES.md                           # Catálogo de habilidades e arquitetura do agente
├── PLANO_ARQUITETURA_V1.1_MODS_PIPELINE.md  # Especificação técnica do pipeline de Mods (v1.1)
└── LICENSE                                  # Licença MIT
```

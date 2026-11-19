# WebWorker v1.2: Quando o Motor Vale Mais que a LLM — do 1/20 com Qwen 3B ao 18/18 com Bonsai 27B

`#agentes-web` `#slms` `#benchmark` `#arquitetura` `#v1-2`

17 de novembro de 2026

Passamos as ultimas rodadas apertando cada parafuso do motor web da plataforma WebWorker — [browser_engine.py](src/web/browser_engine.py), [dom_simplifier.py](src/web/dom_simplifier.py) e [triaged_web_agent.py](src/web/triaged_web_agent.py) — rodando tudo localmente numa unica NVIDIA GeForce RTX 5060 de 8 GB (`8151 MiB`), passando pela fila real do Go Gateway (`:8000`), `MultiAgentWorkerFleet` e PostgreSQL (`WebWorker_db`).

Quando comecamos a auditoria do benchmark de 20 sites reais, o `qwen2.5:3b` entregava `1/20`. Ao final desta campanha, na tag local `v1.2` (`e34021e`), chegamos a **`18/18` (`100%`)** nos desafios validos usando o `Ternary-Bonsai-2-27B-PTQ1_0` sem trocar uma linha de prompt especifico de site e sem nenhum hardcode de seletor.

Antes que alguem leia esse resultado do jeito errado — "entao basta trocar o 3B pelo 27B e pronto" — vale deixar clara a tese inteira deste projeto.

Usar uma LLM de 27B para fazer o trabalho pesado, ou ate para gerar scripts mastigados que simplifiquem o futuro trabalho do 3B e do 9B, seria usar uma escavadeira para plantar manjericao — ou pior, transformar o modelo gigante numa muleta para esconder defeito do codigo. O objetivo aqui sempre foi entregar tudo com modelos pequenos (`3B`), justamente para provar que **o motor por tras importa mais do que a LLM em cima**. Nao adianta dar prego para alguem sem entregar tambem o martelo. Modelos pequenos sao o melhor detector de defeito arquitetural que existe: se o DOM estiver sujo ou a ferramenta for ambigua, o 3B quebra na hora; quando o motor entrega o contexto limpo e a ferramenta certa, ate o menor modelo anda em linha reta.

O `27B` nao entrou para ser a muleta da v1.2. Ele entrou como **calibrador**.

---

## Os 20 desafios da bateria

| # | Desafio | Site / Alvo | O que o teste exige na pratica |
| :---: | :--- | :--- | :--- |
| 01 | `quotes-js` | `quotes.toscrape.com/js/` | Renderizacao client-side em JS; extrair primeira citacao, autor e tags |
| 02 | `quotes-scroll` | `quotes.toscrape.com/scroll` | Scroll infinito; carregar todos os lotes e contar o total exato de citacoes (`100`) |
| 03 | `quotes-js-delayed` | `quotes.toscrape.com/js-delayed/?delay=5000` | Timer JS de 5s sem spinner visivel; aguardar carga real e extrair a citacao |
| 04 | `quotes-login` | `quotes.toscrape.com/login` | Preencher usuario/senha, submeter formulario (`Login`) e confirmar `Logout` |
| 05 | `scrape-ajax` | `scrapethissite.com/pages/ajax-javascript/` | Clicar em filtro AJAX (`2015`, ID numerico) e extrair dados de `Spotlight` |
| 06 | `webscraper-more` | `webscraper.io/test-sites/e-commerce/more` | Navegar em categorias (`Computers -> Laptops`) e extrair modelo/preco |
| 07 | `web-scraping-dev` | `web-scraping.dev/products` | Localizar produto especifico na listagem, abrir subpagina e ler preco/variantes |
| 08 | `oxylabs-products` | `sandbox.oxylabs.io/products` | Aplicar filtro lateral (`Nintendo platform`) e extrair nome e preco do 1o jogo |
| 09 | `dynamic-loading-2` | `the-internet.herokuapp.com/dynamic_loading/2` | Clicar em `Start`, aguardar elemento inserido apos spinner e ler `"Hello World!"` |
| 10 | `herokuapp-tables` | `the-internet.herokuapp.com/tables` | Ler tabela de cobrancas (`Tabela 1`) e extrair a linha do maior valor em `Due` |
| 11 | `hacker-news` | `news.ycombinator.com/` | Listagem densa com 225+ links; extrair titulo e pontos da primeira noticia |
| 12 | `wiki-municipios-br` | `pt.wikipedia.org/...populacao` | Tabela longa de municipios (*descartado por `document.body` nulo no headless*) |
| 13 | `github-trending` | `github.com/trending` | Pagina com centenas de filtros de linguagem; extrair 1o repo, linguagem e `stars today` |
| 14 | `bcb-cotacoes` | `bcb.gov.br/` | SPA com mega-menus ocultos; localizar widget e ler Compra/Venda do Dolar PTAX |
| 15 | `dados-gov-br` | `dados.gov.br/dados/conjuntos-dados` | SPA Angular com filtros ocultos de 313 orgaos; pesquisar `'saude'` e ler 1o resultado |
| 16 | `pypi-playwright` | `pypi.org/project/playwright/` | Pagina de pacote (*descartado por bloqueio WAF `Client Challenge`*) |
| 17 | `fake-jobs` | `realpython.github.io/fake-jobs/` | Abrir subpagina pelo link `Apply` e extrair cargo, empresa e data de publicacao |
| 18 | `httpbin-headers` | `httpbin.org/headers` | Resposta JSON pura no navegador; extrair exatamente a string `User-Agent` |
| 19 | `books-toscrape` | `books.toscrape.com/` | Pular sidebar de 50 categorias, abrir `A Light in the Attic` e ler 4 campos |
| 20 | `scrape-hockey-forms` | `scrapethissite.com/pages/forms/` | Tabela estatistica NHL; localizar `Boston Bruins` (`1990`) e ler `Wins`, `Losses` e `+ / -` |

---

## Lembrando o que este benchmark mede (e por que "COMPLETED no banco" engana)

A bateria testa 20 desafios reais de automacao e extracao web (`quotes.toscrape`, `scrapethissite`, `webscraper.io`, `web-scraping.dev`, `oxylabs`, `the-internet.herokuapp`, `news.ycombinator`, `github.com/trending`, `bcb.gov.br`, `dados.gov.br`, `realpython/fake-jobs`, `httpbin.org`, `books.toscrape`). Nenhuma pagina e mockada na corrida E2E: o Playwright Chromium abre a URL viva e o modelo decide via Tool Calling (`browser_find_in_page`, `browser_read_more`, `browser_click`, `browser_fill`, `browser_count_items`, `browser_extract_data`, `browser_go_back`, `browser_report_failure`).

Logo na triagem de ambiente, dois dos 20 sites precisaram sair da conta valida por bloqueio externo fora do controle do agente:

- **`#12 wiki-municipios-br` (`pt.wikipedia.org`):** mesmo respondendo HTTP 200 com `88.049` bytes de HTML, o Chromium headless recebe `document.body` nulo (`innerText` e `textContent` com retorno `-1`). Nao existe pagina renderizada para nenhum modelo ler.
- **`#16 pypi-playwright` (`pypi.org`):** barrado por `Client Challenge` (WAF anti-bot).

Restaram **18 desafios validos**.

Mesmo nesses 18, existe uma armadilha classica de metrica que precisamos separar com honestidade: o PostgreSQL marca uma tarefa como `COMPLETED` toda vez que o agente chama `browser_extract_data` sem estourar 3 erros consecutivos ou o teto de 12 turnos. No `qwen2.5:3b`, isso gera **falsos positivos de atalho**. O enunciado manda clicar em `Start` e esperar a mensagem final (`"Hello World!"`); o 3B enxerga `[button @3] Start` na tela, chama `browser_extract_data(answer="Start")` no Turno 2 e o banco grava `COMPLETED`. No `#17`, responde `"Apply"` sem abrir a vaga. No `#19`, devolve o caminho relativo `catalogue/a-light-in-the-attic_1000/index.html` sem clicar no livro. No `#05`, responde `"2015"` sem ler os numeros da tabela AJAX.

Contar isso como acerto seria maquiar o placar. Aqui separamos `COMPLETED` no banco de **extracao real completa**.

Outro ponto: LLMs sao probabilisticas. Em baterias `3x` seguidas (54 execucoes de fila no `3B`), vimos o mesmo desafio passar `2/3` ou `1/3` e falhar na terceira rodada por pura variancia amostral ou escolha ruim de argumento no Turno 1. Benchmark serio com SLM local nao se mede em corrida unica de sorte — se mede em bateria repetida e inspecao do que saiu em `extracted_value`.

---

## O martelo antes do prego: o que realmente consertamos no motor

Vale uma distincao justa de versao: a base da `v1.1` (`6e62eb1`) ja entregava o motor ReAct aterrado, a pre-triagem dinamica e o proprio `browser_find_in_page` (inclusive com o parametro `links=True` para preservar `@ids` nos trechos encontrados). O que fizemos da `v1.1` ate a `v1.2` (`e34021e`) foi tirar os gargalos de DOM real e ensinar/forcar o fluxo onde os modelos pequenos tropecavam:

- **Seletores reais no HTML (`f26bc11`):** suporte a IDs CSS numericos (`[id='2015']`, que destravou o `#05` AJAX) e mapeamento de `<input type="submit|button|reset">` como `[button @id]` (que expôs o `@5 Login` no `#04`), alem de espera por estabilizacao no `click()`.

### 1. Apagar da copia do DOM tudo que esta oculto para um humano

O `BrowserEngine._get_current_snapshot()` passava `page.content()` bruto para o `DOMSimplifier`. O problema e que SPAs modernos carregam menus gigantes e dropdowns fechados via CSS externo (`display: none`, `visibility: hidden`, `aria-hidden="true"`). O usuario humano nao ve nada disso na tela; o parser de HTML bruto despejava tudo no prompt do 3B.

No `bcb.gov.br` (`#14`), o texto simplificado tinha `21.323 chars` e `706 elementos` de mega-menu invisivel, fazendo ate o Bonsai 9B tomar `Timeout 3000ms: element is not visible` ao tentar clicar em `@294`, `@293` e `@221`. No `dados.gov.br` (`#15`), um `<div class="multiselect__content-wrapper">` fechado com `display: none` despejava `16.373 chars` de 313 orgaos federais logo no Turno 1, afogando a janela de 2.000 caracteres do modelo.

A solucao em [browser_engine.py](src/web/browser_engine.py) foi clonar o `document.documentElement` em JavaScript sem mutar a pagina viva, podar no clone toda subarvore cujo estilo computado esteja oculto (preservando `<option>` e `<optgroup>`) e so entao simplificar o HTML. Resultado imediato:

- **`#14 bcb.gov.br`:** `21.323 chars / 706 elementos` -> **`2.000 chars / 84 elementos`** (corte de ~10x no ruido).
- **`#15 dados.gov.br`:** `19.181 chars / 32 elementos` -> **`1.445 chars / 19 elementos`** (corte de ~13x).
- **`#13 github.com/trending`:** `5.654 chars / 211 elementos` -> **`3.838 chars / 164 elementos`**.

### 2. Comprimir listas repetitivas com limite calibrado (`> 10` e nao `> 5`)

Mesmo entre elementos visiveis, listas longas de filtros distorcem o contexto de modelos pequenos. No `github.com/trending`, centenas de links de linguagens empurravam os repositorios para fora da janela; comprimir listas com `> 3` idiomas em um link sintetico `@escolher_linguagem_N` derrubou a pagina de `15.011 chars / 1.229 elementos` para `5.381 chars / 199 elementos` e levou o 3B a `3/3` naquele desafio.

Quando generalizamos a ideia para qualquer `<ul>` simples sem headings/paragrafos ricos, aprendemos na pratica o risco de comprimir demais. Com corte em `> 5` itens `<li>`, o simplificador escondeu justamente as 6 variantes (`orange, small` ... `cherry, large`) dentro da subpagina do produto `#07` (`web-scraping.dev`), fazendo o agente responder a URL em vez das opcoes. Subindo o corte para **`> 10` `<li>`**, mantivemos dropdowns/sidebars enormes colapsados (`books.toscrape` tinha 50 categorias na barra lateral) e preservamos intactas listas curtas de produtos e variantes.

### 3. Primitivas atomicas para o que LLM nenhuma deveria fazer de cabeca

Pedir para um modelo 3B executar 10 scrolls manuais no `#02` (`quotes.toscrape.com/scroll`) e contar 100 citacoes ao longo de janelas de texto truncadas e pedir para ele fazer conta de cabeca sem calculadora. Pior: na primeira tentativa, o `3B` leu `"posicao 1524"` na propria mensagem da tool `browser_read_more` e respondeu `1524`.

Criamos `browser_count_items()` com `auto_scroll=True`: a funcao rola rapidamente ate a altura (`scrollHeight`) e a contagem de nos estabilizarem (teto de `8s`), restaura `window.scrollTo(0, prev_y)` para nao perder a visao da pagina e agrupa blocos estruturais repetidos no DOM (`100 itens (div.quote)` vs `232 itens (a.tag)`). O 3B deixou de chutar e cravou `100` em 2 turnos (`3/3` nas baterias de estresse).

Da mesma forma:

- **`#03` (`quotes-js-delayed/?delay=5000`):** paginas que carregam via timer JS sem spinner deixavam menos de 200 caracteres visiveis no carregamento inicial (`95 chars`), fazendo o 3B extrair o titulo `"Quotes to Scrape"`. Adicionar verificacao em `navigate()` com espera `3x5s` por diff MD5 quando o texto limpo `< 200 chars` resolveu o `#03` com consistencia (`3/3`).
- **`#04` (`quotes-login`):** sem trava de fluxo, o 3B preenchia `@username` e ja chamava `browser_extract_data` ignorando `@password`. Restringir `turn_tools` dinamicamente por fase (campos pendentes -> apenas `browser_fill`; campos preenchidos -> apenas `browser_click` no envio; apos mudar pagina -> liberacao normal) fechou o `#04` com `Logout` em 4 turnos (`3/3`).
- **`#15` (`dados.gov.br`):** adicionar espera de estabilizacao apos `fill(..., press_enter=True)` deu tempo para a Angular SPA trocar `"Carregando..."` pelos cards reais de conjuntos de dados.

### 4. O teste A/B `3x` que mostrou onde NAO mexer no prompt

Num experimento intermediario (`401084d`, preservado na branch `backup-401084d-ul10`), tentamos remover o lembrete `INSTRUCAO FINAL` dos turnos 1..9 achando que isso reduziria atalhos prematuros no `#15`. O teste geral provou o contrario: sem o lembrete nos turnos iniciais, o `3B` nao sabia a hora de parar, vagava chamando `browser_read_more` ate bater erro repetido e derrubava o placar para `10/18`.

Mesmo mantendo `INSTRUCAO FINAL` e apenas omitindo `"ou no RESULTADO DA ULTIMA ACAO"` no Turno 1, a comparacao `3x` (`54` tarefas cada) contra o commit `32c858e` mostrou regressao clara em tres fluxos que estavam solidos: `#03` (`3/3` citacao completa -> `3/3` so tags), `#19` (`3/3` UPC real -> `3/3` URL relativa) e `#20` (`3/3` linha correta -> `1/3`). Voltamos `triaged_web_agent.py` exatamente ao `32c858e` e trouxemos apenas os ganhos estruturais de DOM e motor (`browser_engine.py` + `dom_simplifier.py`), consolidando o `HEAD` atual (`e34021e`, tag `v1.2`, com `86/86` testes TDD verdes).

---

## O experimento com Bonsai 9B e 27B: comparativo direto na mesma base (`e34021e`)

Com a base `e34021e` fechada, rodamos os mesmos 18 desafios sequencialmente — garantindo que cada servidor fosse descarregado antes do proximo para nao vazar VRAM entre modelos — comparando o `qwen2.5:3b` (Ollama), o `Ternary-Bonsai-2-9B-PQ2_0` e o `Ternary-Bonsai-2-27B-PTQ1_0` (ambos via `llama-server.exe --jinja -ngl 99`).

| Modelo | Peso em disco | Contexto testado | VRAM real medida | Concluidos no banco | Reais completos | Tempo total (18) | Media por tarefa |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`qwen2.5:3b`** | `1.9 GB` | `8192` (`8K`) | `2.4 GB` | `14 / 18` | `~10 / 18` | `174.5s` | `9.7s` |
| **`Ternary-Bonsai-2-9B-PQ2_0`** | `2.25 GB` | `8192` (`8K`) | `1.90 GiB` (`1944 MiB`) | `15 / 18` | **`15 / 18`** | `251.1s` | `14.0s` |
| **`Ternary-Bonsai-2-27B-PTQ1_0`** | `5.54 GB` | `6144` (`6K`) | `~7.7 GB` (+RAM em FP16) | **`18 / 18`** | **`18 / 18` (`100%`)** | `621.1s` | `34.5s` |

### Resultados lado a lado desafio por desafio

| # | Desafio | `qwen2.5:3b` | `Bonsai 9B` | `Bonsai 27B` |
| :---: | :--- | :--- | :--- | :--- |
| 01 | `quotes-js` | `OK` (`1t`): apenas frase | `OK` (`1t`): frase + autor + tags | `OK` (`1t`): frase + autor + tags |
| 02 | `quotes-scroll` | `OK` (`2t`): `100` | `OK` (`2t`): `100` | `OK` (`2t`): `100` |
| 03 | `quotes-js-delayed` | `OK` (`1t`): frase + autor | `OK` (`1t`): frase + autor | `OK` (`1t`): frase + autor |
| 04 | `quotes-login` | `OK` (`4t`): `Logout` | `OK` (`4t`): `Logout` + citacao | `OK` (`4t`): `Logout` |
| 05 | `scrape-ajax` | Atalho (`3t`): `"2015"` | `OK` (`2t`): `6 indicações e 2 prêmios` | `OK` (`2t`): `Spotlight: 6 indicações, 2 prêmios` |
| 06 | `webscraper-more` | `FAIL` (`4t`) | `OK` (`3t`): `Asus VivoBook X4 - $295.99` | `OK` (`5t`): lista de Laptops e precos |
| 07 | `web-scraping-dev` | `OK` (`5t`): `orange, small` | `OK` (`3t`): `$9.99` + 6 variantes | `OK` (`3t`): `$9.99` + 6 variantes |
| 08 | `oxylabs-products` | `OK` (`4t`): Zelda `91,99 €` | `OK` (`3t`): Zelda `91,99 €` | `OK` (`2t`): Zelda `91,99 €` |
| 09 | `dynamic-loading-2` | Atalho (`2t`): `"Start"` | `FAIL` (`6t`): 3 erros clique | `OK` (`2t`): `Hello World!` |
| 10 | `herokuapp-tables` | `FAIL` (`3t`) | `OK` (`1t`): `Doe, Jason, $100.00` | `OK` (`1t`): `Doe, Jason, $100.00` |
| 11 | `hacker-news` | `OK` (`1t`): `GPT-6.2 Astra and Sol` | `OK` (`1t`): `Java4Shell...` | `OK` (`1t`): `Java4Shell... — 229 points` |
| 13 | `github-trending` | `OK` (`3t`): repo `ag-ui-protocol` | `OK` (`1t`): repo + linguagem + stars | `OK` (`1t`): `elementalsouls / Claude-BugHunter — Python — 1,007 stars today` |
| 14 | `bcb-cotacoes` | `FAIL` (`7t`) | `FAIL` (`3t`) | `OK` (`3t`): `Compra (R$): 5,7845 \| Venda (R$): 5,7851` |
| 15 | `dados-gov-br` | `FAIL` (`7t`) | `OK` (`3t`): `03. Incidentes notificados...` | `OK` (`3t`): `(Descontinuado) Abrangência Geográfica...` |
| 17 | `fake-jobs` | Atalho (`2t`): `"Apply"` | `FAIL` (`4t`) | `OK` (`2t`): `Senior Python Developer; Payne, Roberts and Davis; 2021-04-08` |
| 18 | `httpbin-headers` | `OK` (`1t`): `User-Agent: ...` | `OK` (`1t`): `Mozilla/5.0...` | `OK` (`1t`): `Mozilla/5.0...` |
| 19 | `books-toscrape` | Atalho (`2t`): URL relativa | `OK` (`3t`): titulo + preco + estoque + UPC | `OK` (`3t`): todos os 4 campos exatos |
| 20 | `scrape-hockey-forms` | `OK` (`2t`): `1990 \| 44 \| 24 \| 0.55 \| 299` | `OK` (`1t`): `1990, Wins: 44, Losses: 24, + / -: 35` | `OK` (`1t`): `1990, Wins: 44, Losses: 24, + / -: 35` |

Tambem rodamos a suite E2E de QA do Dia 3 ([tests/test_qa_day3_benchmark_react.py](tests/test_qa_day3_benchmark_react.py)) no `Bonsai 9B`: passou **`5/5` (`124.325s`, `OK`)**, preenchendo formularios de checkout e curriculo, extraindo os 3 KPIs financeiros juntos (`Faturamento: R$ 4.850.000, Usuarios Ativos: 128.400, SLA: 99.98%`) e aguardando latencia assincrona (`14ms`).

### Experimento de controle: mesmo `Bonsai 9B` na `v1.1` (`6e62eb1`) vs `v1.2` (`e34021e`)

Para isolar quanto do ganho veio do motor e quanto veio do modelo, executamos o mesmo `Ternary-Bonsai-2-9B-PQ2_0` (com `-fa on -ctk q4_0 -ctv q4_0`, equivalente a `-lm none` para evitar *mmap* em RAM nesta build) numa worktree isolada do commit `6e62eb1` (`v1.1`, antes de qualquer correcao da `v1.2`). O teste rodou 15 dos 18 desafios ate bater `TimeoutError` (90s) no `#18` (`httpbin-headers`):

- **Acertos reais completos na `v1.1` (`6e62eb1`):** apenas **5 de 15** (`#01`, `#06`, `#08`, `#10`, `#11`).
- **Falsos positivos / parciais (`2/15`):** `#03` respondeu `"Quotes to Scrape"` (`95 chars` sem a espera `3x5s`) e `#07` respondeu `"24.99"` da listagem sem abrir o produto.
- **Falhas totais (`8/15`):** `#02` (sem `browser_count_items`), `#04` (`<input type="submit">` nao mapeado), `#05` (ID numerico `#2015`), `#09`, `#13` (`15k chars` de linguagens sem compressao), `#14` (mega-menu oculto), `#15` (`19k chars` de dropdowns ocultos) e `#17`.

Ou seja: mantendo o **mesmo modelo `Bonsai 9B`** e os **mesmos objetivos**, trocar apenas o motor da `v1.1` (`6e62eb1`) pelo motor da `v1.2` (`e34021e`) elevou os acertos reais de **5/15 (~33%) para 15/18 (~83%)**.

---

## O 27B como calibrador, as licoes de escala e o dilema da muleta

Olhando os numeros frios da tabela comparativa, aparecem quatro constatacoes praticas que explicam todo o comportamento entre `3B`, `9B` e `27B`:

1. **Otimizacoes feitas para o 3B repercutem em todos os modelos maiores:** o proprio `9B` subiu de `14/18` (`385.1s`) no `32c858e` para `15/18` (`251.1s`) no `e34021e` sem alterar uma palavra de prompt — puramente por causa da poda de DOM oculto, `<ul> > 10` e espera no `fill`. Melhorar o motor para o menor modelo paga juros compostos na escada inteira.
2. **Quanto maior o modelo, mais texto ele quer entregar:** o `3B` economiza tokens ao extremo (e corta no primeiro campo ou atalho); ja o `9B` e o `27B` entregam o pacote completo espontaneamente — no `#04` o `9B` devolveu `Logout` junto com a citacao da tela, no `#07` trouxe o `$9.99` e todas as 6 variantes, e no `#19` entregou titulo, preco, estoque e UPC de uma vez so.
3. **A falta de criticidade do 3B e os atalhos justificam auditoria contextual:** quando o `3B` responde `"Start"`, `"Apply"`, `"2015"` ou devolve so `a897fe39b1053632`, ele nao falhou no navegador — faltou senso critico na hora de fechar a resposta. Em producao com `3B` + `9B` residentes na mesma placa de 8 GB (`~4.3 GB` somados), o **Bonsai 9B como auditor com contexto** (checando o recorte da tela contra o objetivo) corta exatamente esse comportamento.
4. **Duas formas de usar (ou nao usar) o 27B:**
   - **Na rota de produto / operacao (`script_to_complete()`):** da para usar o `27B` como fallback nas tarefas complexas que deram erro, ou numa funcao `script_to_complete()` para rotinas recorrentes onde `3B` e `9B` travam (como `bcb-cotacoes`). Nela, o `27B` mapeia o roteiro detalhado de cliques, ancoras e eventos; enquanto a pagina nao mudar bruscamente, `3B`/`9B` apenas executam as instrucoes mapeadas e o `9B` valida a saida final.
   - **Na tese cientifica do motor (`3B` puro):** aqui entra a cautela principal do projeto. Usar o `27B` para simplificar o trabalho do `3B`/`9B` significa usar uma LLM maior como ferramenta (ou muleta) de uma menor — e isso dilui o proposito original de provar que o motor por tras resolve sozinho com o `3B`.

Nesse equilibrio, o **`27B` cumpriu seu papel como calibrador, nao como muleta**: o `18/18` provou que `e34021e` nao tem mais gargalo de DOM, parser ou ferramenta, isolando as falhas restantes apenas no raciocinio do `3B`. E voce nem precisa trair a tese do `3B` para ter o **`criteria_state`**: quebrar criterios do objetivo e devolver `"PARCIAL: ainda faltam X, Y"` quando um campo ficar para tras pode ser feito pelo proprio motor deterministico (ou por uma micro-etapa focada do proprio `3B` com schema minimo), mantendo todo o merito na engenharia do motor e rodando em hardware modesto.

---

## Conclusao

A versao `1.2` (`e34021e`) fecha um ciclo claro: saimos de um agente de `1/20` preso em seletores quebrados, dropdowns invisiveis de 20 mil caracteres e loops cegos de leitura para um motor enxuto (`86/86` testes TDD verdes) capaz de sustentar **`18/18` reais** com um modelo ternario local quando ha capacidade cognitiva suficiente, e **`15/18` reais sem atalhos** em menos de `2 GB` de VRAM no `9B`.

O proximo passo para manter a promessa original do projeto nao e terceirizar a navegacao para o `27B`, e sim trazer a disciplina de `criteria_state` e guarda anti-atalho para dentro do proprio `TriagedWebAgent` deterministico — provando ate onde um bom martelo consegue levar um modelo `3B`.

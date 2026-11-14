"""
===============================================================================
MÓDULO: Fixtures & Mocks de Páginas Web e Motores de Busca (MockHTMLFixtures)
CAMINHO: tests/mocks/mock_html_fixtures.py
===============================================================================

OBJETIVO:
Fornecer snapshots HTML realistas e determinísticos para testes TDD herméticos
(sem dependência de conexão de rede externa ou instabilidade de marcação):
- Google Knowledge Panel (Filmes, Fichas Técnicas, Avaliações, Sinopses).
- Google AI Overview (Visão geral criada por IA, tópicos, advertências, fontes).
- Google Bot Detection / Captcha (/sorry/index).
- Páginas Web dinâmicas para testes de alteração de MD5 e Watchdog anti-loop.
===============================================================================
"""

# 1. Google Search Results com Knowledge Panel de Filme (ex: Alerta Lobo)
GOOGLE_MOVIE_KNOWLEDGE_PANEL_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>filme alerta lobo - Pesquisa Google</title>
</head>
<body>
  <div id="search">
    <div class="knowledge-panel" data-attrid="kc:/film/film:summary">
      <h2 data-attrid="title">Alerta Lobo</h2>
      <div class="subtitle">2019 ‧ Thriller/Ação ‧ 1h 55m</div>
      <div class="rating-bar">
        <span class="rating-src">Papo de Cinema</span> <span class="score">7,2/10</span>
        <span class="rating-src">Revista Bula</span> <span class="score">4,5/5</span>
        <span class="rating-src">IMDb</span> <span class="score">6,9/10</span>
      </div>
      <div class="description-block" data-attrid="description">
        A guerra nuclear é iminente a bordo de um submarino da França. Para evitá-la, um jovem militar especialista em acústica com um ouvido de ouro tenta provar que as coisas não são o que parecem ao identificar um som submarino misterioso.
      </div>
      <div class="metadata-row">
        <span><strong>Direção:</strong> Antonin Baudry</span>
        <span><strong>Lançamento:</strong> 20 de junho de 2019 (Brasil)</span>
        <span><strong>Gêneros:</strong> Thriller, Ação, Suspense Militar, Drama</span>
      </div>
      <div class="cast-row">
        <span>François Civil</span>, <span>Omar Sy</span>, <span>Mathieu Kassovitz</span>, <span>Reda Kateb</span>
      </div>
    </div>
  </div>
</body>
</html>
"""

# 2. Google Search Results com AI Overview ("Visão geral criada por IA")
GOOGLE_AI_OVERVIEW_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>oque acontece quando um raio cai numa usina nuclear? - Pesquisa Google</title>
</head>
<body>
  <div id="search">
    <div class="MGB6P ai-overview-container" data-attrid="wa:/description">
      <div class="ai-header">
        <span class="sparkle-icon">✨</span>
        <span class="ai-badge">Visão geral criada por IA</span>
      </div>
      <div class="ai-content-body">
        <p>Quando um raio cai em uma usina nuclear, os sistemas de segurança da instalação são projetados para absorver e dissipar a descarga elétrica sem causar explosões ou vazamentos radioativos.</p>
        <p>O que ocorre normalmente envolve uma sequência padrão de proteção:</p>
        <ul>
          <li><strong>Pára-raios e aterramento:</strong> A usina é cercada por uma malha robusta de pára-raios que guia a energia com segurança até o solo.</li>
          <li><strong>Desligamento automático (SCRAM):</strong> Se a descarga gerar oscilação na rede elétrica, o reator entra em parada automática por precaução.</li>
          <li><strong>Geradores de emergência:</strong> Geradores a diesel entram em ação instantaneamente caso falte energia externa para as bombas de arrefecimento.</li>
          <li><strong>Estrutura de contenção:</strong> Paredes maciças de concreto armado e aço blindam o núcleo contra impactos e sobretensões.</li>
        </ul>
        <div class="ai-faq">
          <strong>Há risco de explosão?</strong>
          <p>Não. Uma usina nuclear não pode detonar como uma bomba atômica. O maior risco seria a falta prolongada de refrigeração, o que é protegido por redundâncias quádruplas.</p>
        </div>
      </div>
      <div class="sources-list">
        <span>Fontes: ResearchGate, World Nuclear Association, Eletronuclear</span>
      </div>
    </div>
    <div class="regular-results">
      <h3>Resultados da web</h3>
      <a href="https://exemplo.com/artigo-usina">Artigo sobre segurança nuclear</a>
    </div>
  </div>
</body>
</html>
"""

# 3. Google Bot Detection / Captcha Page (/sorry/index)
GOOGLE_CAPTCHA_HTML = """
<!DOCTYPE html>
<html>
<head><title>Unusual traffic from your computer network</title></head>
<body>
  <h1>About this page</h1>
  <p>Our systems have detected unusual traffic from your computer network. This page checks to see if it's really you sending the requests, and not a robot.</p>
  <form action="/sorry/index" method="POST">
    <div class="g-recaptcha" data-sitekey="dummy"></div>
  </form>
</body>
</html>
"""

# 4. Página Dinâmica de Catálogo para Teste de Streak MD5 e Mudança de Estado
DYNAMIC_PORTAL_V1_HTML = """
<!DOCTYPE html>
<html>
<head><title>Portal de Séries e Animes</title></head>
<body>
  <h1>Lançamentos da Semana</h1>
  <ul id="items">
    <li id="item-1">Episódio 10 lançado</li>
    <li id="item-2">Episódio 05 lançado</li>
  </ul>
  <input type="text" id="s" name="q" placeholder="Buscar título..." />
  <button id="search-btn">Pesquisar</button>
</body>
</html>
"""

DYNAMIC_PORTAL_V2_HTML = """
<!DOCTYPE html>
<html>
<head><title>Portal de Séries e Animes</title></head>
<body>
  <h1>Lançamentos da Semana</h1>
  <ul id="items">
    <li id="item-1">Episódio 10 lançado</li>
    <li id="item-2">Episódio 05 lançado</li>
    <li id="item-3" class="new">Episódio 11 NOVO!</li>
  </ul>
  <input type="text" id="s" name="q" placeholder="Buscar título..." />
  <button id="search-btn">Pesquisar</button>
</body>
</html>
"""

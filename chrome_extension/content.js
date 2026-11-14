/**
 * ============================================================================
 * MODULO: Content Script com Top Bar HUD, Inspetor DOM e Motor de Acoes In-Page
 * CAMINHO: chrome_extension/content.js
 * ============================================================================
 * OBJETIVO:
 * - Renderizar faixa fixa no topo da pagina ativa isolada por Shadow DOM (mode: "closed").
 * - Exibir em tempo real:
 *   1. O prompt/input do usuario.
 *   2. O status de execucao (Iniciando, Interagindo, Processando LLM, Finalizado, Cancelado).
 *   3. A resposta textual retornada pela LLM ou descricao da acao executada.
 *   4. Botao de copiar a resposta final para a area de transferencia.
 *   5. Gaveta de Inspecao de DOM ("Inspecionar DOM") exibindo contagem de elementos,
 *      texto podado (pruned_text) entregue a LLM e tabela de seletores mapeados.
 *   6. Botao "Destacar na Tela" para demarcar visualmente os elementos mapeados na pagina.
 * - Motor de Acoes In-Page:
 *   Executa acoes decididas pela LLM diretamente na aba viva (click, fill, scroll)
 *   com destaque visual animado (outline verde neon e badge flutuante).
 * - Botao [X] na ponta direita que atua como Kill Switch imediato:
 *   cancela a iteracao no Gateway Go e remove a faixa da tela.
 * - Sintetizar som "pop" ao finalizar e som grave ao cancelar via Web Audio API.
 * ============================================================================
 */

let hudHost = null;
let shadowRoot = null;
let currentTaskUuid = null;
let currentFullResponse = "";
let currentDomTelemetry = null;

/**
 * Sintetiza um som "pop" caracteristico ao concluir com sucesso via Web Audio API.
 */
function playWebWorkerPopSound(frequencyHz = 820, durationMs = 65) {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = "sine";
    osc.frequency.setValueAtTime(frequencyHz, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(frequencyHz * 0.55, ctx.currentTime + durationMs / 1000);

    gain.gain.setValueAtTime(0.18, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + durationMs / 1000);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start();
    osc.stop(ctx.currentTime + durationMs / 1000);
  } catch (e) {
    console.debug("[WebWorker HUD] Audio desativado pelo navegador:", e);
  }
}

/**
 * Sintetiza um tom de aviso/cancelamento suave quando o usuario clica em [X].
 */
function playWebWorkerCancelSound() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = "triangle";
    osc.frequency.setValueAtTime(320, ctx.currentTime);
    osc.frequency.linearRampToValueAtTime(200, ctx.currentTime + 0.12);

    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start();
    osc.stop(ctx.currentTime + 0.12);
  } catch (_) {}
}

/**
 * Cria ou recupera a estrutura de Shadow DOM para a Faixa de Topo (HUD).
 */
function ensureHUDElement() {
  if (hudHost && document.contains(hudHost)) {
    return shadowRoot;
  }

  const existing = document.getElementById("webworker-hud-host");
  if (existing) existing.remove();

  hudHost = document.createElement("div");
  hudHost.id = "webworker-hud-host";
  hudHost.style.all = "initial";

  shadowRoot = hudHost.attachShadow({ mode: "closed" });

  const style = document.createElement("style");
  style.textContent = `
    .ww-hud-wrapper {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      width: 100%;
      box-sizing: border-box;
      z-index: 2147483647;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      transition: transform 0.28s ease, opacity 0.28s ease;
    }
    .ww-hud-wrapper.ww-closing {
      transform: translateY(-100%);
      opacity: 0;
    }
    .ww-hud-bar {
      background: #090d16;
      border-bottom: 2px solid #1e293b;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.6);
      color: #f1f5f9;
      font-size: 13px;
      line-height: 1.4;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 16px;
      gap: 12px;
      box-sizing: border-box;
    }
    .ww-left-group {
      display: flex;
      align-items: center;
      gap: 10px;
      flex: 1;
      min-width: 0;
    }
    .ww-brand {
      font-weight: 800;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      background: #1e293b;
      color: #38bdf8;
      padding: 4px 8px;
      border-radius: 4px;
      border: 1px solid #334155;
      white-space: nowrap;
    }
    .ww-prompt-box {
      color: #94a3b8;
      max-width: 220px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 12px;
    }
    .ww-prompt-box strong {
      color: #cbd5e1;
    }
    .ww-status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 10px;
      border-radius: 9999px;
      font-weight: 600;
      font-size: 12px;
      white-space: nowrap;
      text-transform: capitalize;
    }
    .ww-status-badge.iniciando {
      background: rgba(14, 165, 233, 0.15);
      color: #38bdf8;
      border: 1px solid rgba(14, 165, 233, 0.35);
    }
    .ww-status-badge.interagindo {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.35);
    }
    .ww-status-badge.processando-llm {
      background: rgba(168, 85, 247, 0.15);
      color: #c084fc;
      border: 1px solid rgba(168, 85, 247, 0.35);
    }
    .ww-status-badge.finalizado {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.35);
    }
    .ww-status-badge.cancelado,
    .ww-status-badge.falhou {
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.35);
    }
    .ww-pulse-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: currentColor;
      animation: wwPulse 1.3s infinite ease-in-out;
    }
    @keyframes wwPulse {
      0%, 100% { opacity: 0.3; transform: scale(0.85); }
      50% { opacity: 1; transform: scale(1.15); }
    }
    .ww-response-box {
      flex: 1;
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 5px 12px;
      color: #f8fafc;
      font-size: 12.5px;
      max-height: 48px;
      overflow-y: auto;
      word-break: break-word;
      display: none;
    }
    .ww-right-group {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .ww-btn {
      background: #1e293b;
      border: 1px solid #475569;
      color: #e2e8f0;
      border-radius: 4px;
      padding: 4px 9px;
      font-size: 11.5px;
      cursor: pointer;
      font-weight: 500;
      transition: background 0.15s ease, border-color 0.15s ease;
      white-space: nowrap;
    }
    .ww-btn:hover {
      background: #334155;
      color: #ffffff;
      border-color: #64748b;
    }
    .ww-btn-active {
      background: #0284c7 !important;
      border-color: #38bdf8 !important;
      color: #ffffff !important;
    }
    .ww-copy-btn {
      display: none;
    }
    .ww-close-btn {
      background: transparent;
      border: 1px solid rgba(148, 163, 184, 0.25);
      color: #94a3b8;
      border-radius: 4px;
      width: 26px;
      height: 26px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      line-height: 1;
      transition: all 0.15s ease;
    }
    .ww-close-btn:hover {
      background: #ef4444;
      border-color: #ef4444;
      color: #ffffff;
    }
    .ww-dom-drawer {
      display: none;
      background: #0b1120;
      border-bottom: 2px solid #1e293b;
      padding: 12px 16px;
      max-height: 280px;
      overflow-y: auto;
      color: #cbd5e1;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.7);
    }
    .ww-dom-drawer.ww-open {
      display: block;
    }
    .ww-drawer-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
      font-size: 11.5px;
      font-weight: 700;
      color: #38bdf8;
      border-bottom: 1px solid #1e293b;
      padding-bottom: 4px;
    }
    .ww-drawer-content {
      background: #020617;
      border: 1px solid #1e293b;
      border-radius: 4px;
      padding: 8px 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 11px;
      line-height: 1.45;
      color: #94a3b8;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 180px;
      overflow-y: auto;
    }
  `;

  const wrapper = document.createElement("div");
  wrapper.className = "ww-hud-wrapper";
  wrapper.id = "wwHudWrapper";

  wrapper.innerHTML = `
    <div class="ww-hud-bar" id="wwHudBar">
      <div class="ww-left-group">
        <span class="ww-brand">WebWorker AI</span>
        <div class="ww-prompt-box" id="wwPromptBox"><strong>Prompt:</strong> <span id="wwPromptText">-</span></div>
        <div class="ww-status-badge iniciando" id="wwStatusBadge">
          <span class="ww-pulse-dot" id="wwPulseDot"></span>
          <span id="wwStatusText">Iniciando</span>
        </div>
        <div class="ww-response-box" id="wwResponseBox"></div>
      </div>
      <div class="ww-right-group">
        <button class="ww-btn" id="wwInspectBtn" title="Ver texto e seletores capturados do DOM">Inspecionar DOM (<span id="wwDomCount">0</span>)</button>
        <button class="ww-btn" id="wwHighlightBtn" title="Destacar na tela os elementos que a IA consegue interagir">Destacar na Tela</button>
        <button class="ww-btn ww-copy-btn" id="wwCopyBtn" title="Copiar resposta">Copiar</button>
        <button class="ww-close-btn" id="wwCloseBtn" title="Cancelar iteracao e fechar faixa">X</button>
      </div>
    </div>
    <div class="ww-dom-drawer" id="wwDomDrawer">
      <div class="ww-drawer-header">
        <span>Contexto Podado do DOM (o que a LLM esta lendo)</span>
        <span id="wwDomMetaInfo">0 elementos mapeados</span>
      </div>
      <div class="ww-drawer-content" id="wwDrawerContent">Aguardando leitura do DOM...</div>
    </div>
  `;

  shadowRoot.appendChild(style);
  shadowRoot.appendChild(wrapper);

  // Botao Inspecionar DOM
  const inspectBtn = shadowRoot.getElementById("wwInspectBtn");
  const drawer = shadowRoot.getElementById("wwDomDrawer");
  inspectBtn.addEventListener("click", () => {
    drawer.classList.toggle("ww-open");
    inspectBtn.classList.toggle("ww-btn-active", drawer.classList.contains("ww-open"));
  });

  // Botao Destacar Elementos na Tela
  const highlightBtn = shadowRoot.getElementById("wwHighlightBtn");
  highlightBtn.addEventListener("click", () => {
    if (!currentDomTelemetry || !currentDomTelemetry.selector_map) {
      alert("Nenhum elemento interativo mapeado nesta pagina.");
      return;
    }
    highlightAllMappedElements(currentDomTelemetry.selector_map);
    highlightBtn.classList.add("ww-btn-active");
    setTimeout(() => {
      highlightBtn.classList.remove("ww-btn-active");
    }, 3000);
  });

  // Botao Copiar
  const copyBtn = shadowRoot.getElementById("wwCopyBtn");
  copyBtn.addEventListener("click", () => {
    if (!currentFullResponse) return;
    navigator.clipboard.writeText(currentFullResponse).then(() => {
      copyBtn.textContent = "Copiado!";
      setTimeout(() => {
        copyBtn.textContent = "Copiar";
      }, 1500);
    });
  });

  // Botao [X] -> Kill Switch + Fechamento da Faixa
  const closeBtn = shadowRoot.getElementById("wwCloseBtn");
  closeBtn.addEventListener("click", () => {
    handleCloseAndAbort();
  });

  // Anexa ao documentElement para garantir renderizacao mesmo sem body
  (document.body || document.documentElement).appendChild(hudHost);

  return shadowRoot;
}

/**
 * Fecha a faixa com animacao e aciona o Kill Switch se a tarefa ainda estiver em andamento.
 */
function handleCloseAndAbort() {
  playWebWorkerCancelSound();

  if (currentTaskUuid) {
    try {
      chrome.runtime.sendMessage({
        action: "ABORT_TASK",
        task_uuid: currentTaskUuid
      });
    } catch (_) {}
  }

  if (shadowRoot) {
    const wrapper = shadowRoot.getElementById("wwHudWrapper");
    if (wrapper) {
      wrapper.classList.add("ww-closing");
    }
  }

  setTimeout(() => {
    if (hudHost && hudHost.parentNode) {
      hudHost.parentNode.removeChild(hudHost);
    }
    hudHost = null;
    shadowRoot = null;
    currentTaskUuid = null;
    currentDomTelemetry = null;
  }, 290);
}

/**
 * Destaca temporariamente na tela todos os elementos mapeados pelo simplificador do DOM.
 */
function highlightAllMappedElements(selectorMap) {
  const selectors = Object.values(selectorMap || {});
  const touchedElements = [];

  selectors.forEach((sel) => {
    try {
      const el = document.querySelector(sel);
      if (el) {
        const origOutline = el.style.outline;
        el.style.outline = "2px dashed #0284c7";
        touchedElements.push({ el, origOutline });
      }
    } catch (_) {}
  });

  setTimeout(() => {
    touchedElements.forEach(({ el, origOutline }) => {
      try {
        el.style.outline = origOutline;
      } catch (_) {}
    });
  }, 3000);
}

/**
 * Executa acoes decididas pela LLM diretamente no DOM da aba do usuario.
 */
function executeInPageAction(actionPlan) {
  if (!actionPlan || actionPlan.type !== "action" || !actionPlan.action) return;

  const action = actionPlan.action;
  const selector = actionPlan.selector;

  if (action === "click" && selector) {
    try {
      const target = document.querySelector(selector);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        flashElementHighlight(target, `IA: Clicando em ${actionPlan.target_id || selector}`);
        setTimeout(() => {
          target.focus();
          target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
          target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
          target.click();
        }, 450);
      }
    } catch (err) {
      console.warn("[WebWorker AI] Falha ao clicar no elemento:", err);
    }
  } else if (action === "fill" && selector) {
    try {
      const target = document.querySelector(selector);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        flashElementHighlight(target, `IA: Preenchendo ${actionPlan.target_id || selector}`);
        setTimeout(() => {
          target.focus();
          target.value = actionPlan.value || "";
          target.dispatchEvent(new Event("input", { bubbles: true }));
          target.dispatchEvent(new Event("change", { bubbles: true }));
        }, 450);
      }
    } catch (err) {
      console.warn("[WebWorker AI] Falha ao preencher elemento:", err);
    }
  } else if (action === "scroll") {
    const topDelta = actionPlan.direction === "up" ? -500 : 500;
    window.scrollBy({ top: topDelta, behavior: "smooth" });
  }
}

/**
 * Cria destaque visual no elemento alvo durante a execucao da acao.
 */
function flashElementHighlight(target, label) {
  const origOutline = target.style.outline;
  const origShadow = target.style.boxShadow;
  target.style.outline = "3px solid #22c55e";
  target.style.boxShadow = "0 0 16px rgba(34, 197, 94, 0.85)";

  let badge = null;
  if (label) {
    badge = document.createElement("div");
    badge.style.position = "absolute";
    badge.style.zIndex = "2147483646";
    badge.style.background = "#22c55e";
    badge.style.color = "#000000";
    badge.style.fontWeight = "bold";
    badge.style.fontSize = "11px";
    badge.style.padding = "2px 6px";
    badge.style.borderRadius = "3px";
    badge.style.pointerEvents = "none";
    badge.textContent = label;
    const rect = target.getBoundingClientRect();
    badge.style.top = `${window.scrollY + rect.top - 20}px`;
    badge.style.left = `${window.scrollX + rect.left}px`;
    document.body.appendChild(badge);
  }

  setTimeout(() => {
    target.style.outline = origOutline;
    target.style.boxShadow = origShadow;
    if (badge && badge.parentNode) {
      badge.parentNode.removeChild(badge);
    }
  }, 3000);
}

/**
 * Atualiza visualmente o estado da faixa.
 */
function updateHUDState({ status, prompt, responseText, taskUuid, domTelemetry }) {
  const root = ensureHUDElement();
  if (taskUuid) currentTaskUuid = taskUuid;

  const promptTextEl = root.getElementById("wwPromptText");
  const statusBadge = root.getElementById("wwStatusBadge");
  const statusText = root.getElementById("wwStatusText");
  const pulseDot = root.getElementById("wwPulseDot");
  const responseBox = root.getElementById("wwResponseBox");
  const copyBtn = root.getElementById("wwCopyBtn");
  const domCountEl = root.getElementById("wwDomCount");
  const domMetaInfo = root.getElementById("wwDomMetaInfo");
  const drawerContent = root.getElementById("wwDrawerContent");

  if (prompt) {
    promptTextEl.textContent = prompt;
  }

  if (domTelemetry) {
    currentDomTelemetry = domTelemetry;
    const count = domTelemetry.elements_count || 0;
    if (domCountEl) domCountEl.textContent = count;
    if (domMetaInfo) domMetaInfo.textContent = `${count} elementos mapeados | Pagina: '${domTelemetry.title || ""}'`;
    if (drawerContent && domTelemetry.pruned_text_preview) {
      drawerContent.textContent = domTelemetry.pruned_text_preview;
    }
  }

  if (status) {
    statusText.textContent = status;

    // Normaliza classe CSS
    const cls = status.toLowerCase().replace(/\s+/g, "-");
    statusBadge.className = `ww-status-badge ${cls}`;

    if (status === "Finalizado" || status === "Cancelado" || status === "Falhou") {
      pulseDot.style.display = "none";
    } else {
      pulseDot.style.display = "inline-block";
    }
  }

  if (responseText) {
    currentFullResponse = responseText;
    responseBox.textContent = responseText;
    responseBox.style.display = "block";
    copyBtn.style.display = "inline-block";
  }
}

/**
 * Captura o HTML limpo apenas do body, removendo scripts, estilos, SVGs e a propria HUD.
 */
function captureCleanBodyHTML(title) {
  const safeTitle = (title || "Pagina do Usuario").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (!document.body) {
    return `<title>${safeTitle}</title>`;
  }
  const clone = document.body.cloneNode(true);
  const ownHud = clone.querySelector("#webworker-hud-host");
  if (ownHud) ownHud.remove();

  const junkTags = clone.querySelectorAll("script, style, svg, noscript, iframe, link, meta, template");
  junkTags.forEach((el) => el.remove());

  return `<title>${safeTitle}</title>\n${clone.innerHTML}`.slice(0, 300000);
}

/**
 * Captura o texto estruturado dos elementos visiveis na tela atual (viewport do usuario).
 */
function captureVisibleViewportText() {
  if (!document.body) return "";
  const vh = window.innerHeight || 800;
  const candidates = document.body.querySelectorAll("h1, h2, h3, h4, h5, h6, p, li, tr, dt, dd, pre, blockquote");
  const lines = [];
  const seen = new Set();

  for (const el of candidates) {
    if (el.closest && el.closest("#webworker-hud-host")) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.bottom < -250 || rect.top > vh + 900) continue;

    // Evita duplicar textos de blocos pai se um filho mais especifico ja foi capturado, ou vice-versa
    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    if (!text || text.length < 2) continue;
    if (seen.has(text)) continue;

    seen.add(text);
    const tag = el.tagName.toLowerCase();
    if (tag.startsWith("h")) {
      lines.push(`<${tag}> ${text}`);
    } else if (tag === "tr") {
      const cells = Array.from(el.querySelectorAll("th, td"))
        .map((c) => (c.innerText || c.textContent || "").replace(/\s+/g, " ").trim())
        .filter(Boolean);
      if (cells.length > 0) {
        lines.push(`| ${cells.join(" | ")} |`);
      }
    } else {
      lines.push(text);
    }

    if (lines.join("\n").length > 6000) break;
  }

  return lines.join("\n");
}

/**
 * Listener de mensagens do Service Worker (background.js).
 */
if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
    if (request.action === "GET_PAGE_SNAPSHOT") {
      const title = document.title || "Pagina do Usuario";
      const html = captureCleanBodyHTML(title);
      const visible_text = captureVisibleViewportText();
      sendResponse({ ok: true, title, html, visible_text });
      return true;
    }

    if (request.action === "HUD_SHOW") {
      updateHUDState({
        status: request.status || "Iniciando",
        prompt: request.prompt || "",
        taskUuid: request.task_uuid
      });
      sendResponse({ ok: true });
      return true;
    }

    if (request.action === "HUD_UPDATE") {
      updateHUDState({
        status: request.status,
        prompt: request.prompt,
        taskUuid: request.task_uuid
      });
      sendResponse({ ok: true });
      return true;
    }

    if (request.action === "HUD_COMPLETE") {
      updateHUDState({
        status: "Finalizado",
        prompt: request.prompt,
        responseText: request.response_text,
        taskUuid: request.task_uuid,
        domTelemetry: request.dom_telemetry
      });
      playWebWorkerPopSound(820, 65);
      if (request.action_plan) {
        executeInPageAction(request.action_plan);
      }
      sendResponse({ ok: true });
      return true;
    }

    if (request.action === "HUD_ABORTED" || request.action === "HUD_FAILED") {
      updateHUDState({
        status: request.status || (request.action === "HUD_FAILED" ? "Falhou" : "Cancelado"),
        prompt: request.prompt,
        responseText: request.reason || (request.action === "HUD_FAILED" ? "A tarefa nao pode ser concluida." : "Iteracao cancelada."),
        taskUuid: request.task_uuid
      });
      playWebWorkerCancelSound();
      sendResponse({ ok: true });
      return true;
    }

    return true;
  });
}

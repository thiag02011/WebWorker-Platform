/**
 * ============================================================================
 * MODULO: Logica do Popup da Extensao Chrome (popup.js) - Manifest V3
 * CAMINHO: chrome_extension/popup.js
 * ============================================================================
 * OBJETIVO:
 * - Calcular em tempo real a contagem de palavras e estimativa heuristica de tokens.
 * - Capturar a aba ativa e delegar a execucao da tarefa ao background.js (Service Worker).
 * - Notificar a abertura imediata da faixa fixa (HUD) na aba do usuario.
 * ============================================================================
 */

const promptInput = document.getElementById("promptInput");
const wordCountEl = document.getElementById("wordCount");
const tokenEstimateEl = document.getElementById("tokenEstimate");
const charCountEl = document.getElementById("charCount");
const localInteractiveChk = document.getElementById("localInteractiveChk");
const saveDataChk = document.getElementById("saveDataChk");
const bgPriorityRow = document.getElementById("bgPriorityRow");
const bgPrioritySelect = document.getElementById("bgPrioritySelect");
const prioBadge = document.getElementById("prioBadge");
const sendTaskBtn = document.getElementById("sendTaskBtn");
const statusBox = document.getElementById("statusBox");

/**
 * Calcula contagem de palavras e estimativa heuristica de tokens BPE (Qwen/Gemini)
 */
function updateWordAndTokenCounter() {
  const raw = promptInput.value || "";
  const trimmed = raw.trim();
  const chars = raw.length;
  const words = trimmed.length === 0 ? 0 : trimmed.split(/\s+/).length;

  // Estimativa combinada: media entre (palavras * 1.35) e (caracteres / 3.8)
  const estByWords = Math.ceil(words * 1.35);
  const estByChars = Math.ceil(chars / 3.8);
  const estimatedTokens = words === 0 ? 0 : Math.max(estByWords, estByChars);

  wordCountEl.textContent = String(words);
  tokenEstimateEl.textContent = `~${estimatedTokens}`;
  charCountEl.textContent = String(chars);
}

promptInput.addEventListener("input", updateWordAndTokenCounter);

localInteractiveChk.addEventListener("change", () => {
  if (localInteractiveChk.checked) {
    bgPriorityRow.style.display = "none";
    prioBadge.textContent = "Prioridade 1 (Local)";
    prioBadge.style.background = "#0284c7";
  } else {
    bgPriorityRow.style.display = "flex";
    const p = bgPrioritySelect.value;
    prioBadge.textContent = `Prioridade ${p} (Fila)`;
    prioBadge.style.background = "#475569";
  }
});

bgPrioritySelect.addEventListener("change", () => {
  if (!localInteractiveChk.checked) {
    prioBadge.textContent = `Prioridade ${bgPrioritySelect.value} (Fila)`;
  }
});

function showStatus(msg, isError = false) {
  statusBox.style.display = "block";
  statusBox.style.background = isError ? "rgba(220,38,38,0.18)" : "rgba(16,185,129,0.14)";
  statusBox.style.borderColor = isError ? "#ef4444" : "#10b981";
  statusBox.style.color = isError ? "#fecaca" : "#a7f3d0";
  statusBox.textContent = msg;
}

sendTaskBtn.addEventListener("click", async () => {
  const objective = promptInput.value.trim();
  if (!objective) {
    showStatus("Digite um objetivo ou comando antes de enviar.", true);
    return;
  }

  let currentUrl = "http://localhost/";
  let tabId = null;

  if (chrome && chrome.tabs) {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tabs && tabs.length > 0) {
      tabId = tabs[0].id;
      if (tabs[0].url) currentUrl = tabs[0].url;
    }
  }

  const isInteractiveLocal = localInteractiveChk.checked;
  const priority = isInteractiveLocal ? 1 : parseInt(bgPrioritySelect.value || "3", 10);
  const saveExtracted = saveDataChk.checked;

  sendTaskBtn.disabled = true;
  showStatus("Iniciando tarefa... Acompanhe na faixa fixa no topo da pagina.");

  chrome.runtime.sendMessage(
    {
      action: "START_TASK",
      user_goal: objective,
      target_url: currentUrl,
      priority: priority,
      is_interactive: isInteractiveLocal,
      save_private_data: saveExtracted,
      tab_id: tabId
    },
    (response) => {
      sendTaskBtn.disabled = false;
      if (response && response.success) {
        showStatus(
          `Tarefa iniciada (UUID: ${response.task_uuid?.slice(0, 8)}). Acompanhe pela faixa no topo da sua aba. Voce ja pode fechar este popup.`
        );
      } else {
        showStatus(`Erro ao despachar tarefa: ${response?.error || "desconhecido"}`, true);
      }
    }
  );
});

/**
 * ============================================================================
 * MODULO: Service Worker em Segundo Plano (background.js) - Manifest V3
 * CAMINHO: chrome_extension/background.js
 * ============================================================================
 * OBJETIVO:
 * - Manter o ciclo de vida da tarefa ativo mesmo quando o popup for fechado.
 * - Fazer a ponte com o Gateway Go (:8000) e executar polling de status.
 * - Notificar a aba de origem (tabId) com atualizacoes de progresso para a faixa HUD.
 * - Gerenciar cancelamentos acionados pelo botao [X] (Kill Switch na faixa).
 * ============================================================================
 */

const GATEWAY_URL = "http://127.0.0.1:8000";
const activePollers = new Map(); // task_uuid -> { intervalId, tabId, startTime }

/**
 * Envia mensagem segura para uma aba especifica (capturando erros se a aba foi fechada).
 */
async function sendToTab(tabId, message) {
  if (!tabId) return;
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch (err) {
    // Aba pode ter sido fechada pelo usuario
    console.debug("[WebWorker Background] Aba", tabId, "indisponivel:", err.message);
  }
}

/**
 * Inicia o loop de polling no Gateway Go para acompanhar a execucao da tarefa.
 */
function startTaskPolling(taskUuid, tabId, promptText) {
  const startTime = Date.now();
  let hasTransitionedToLLM = false;

  const intervalId = setInterval(async () => {
    try {
      const resp = await fetch(`${GATEWAY_URL}/api/task/status?task_uuid=${encodeURIComponent(taskUuid)}`);
      if (!resp.ok) return;

      const data = await resp.json();
      const status = data.status || "QUEUED";
      const elapsed = Date.now() - startTime;

      if (status === "RUNNING") {
        if (!hasTransitionedToLLM && elapsed > 1500) {
          hasTransitionedToLLM = true;
          await sendToTab(tabId, {
            action: "HUD_UPDATE",
            task_uuid: taskUuid,
            status: "Processando LLM",
            prompt: promptText
          });
        }
      } else if (status === "COMPLETED") {
        clearInterval(intervalId);
        activePollers.delete(taskUuid);

        let responseText = "";
        if (data.result_data && data.result_data.summary) {
          responseText = data.result_data.summary;
        } else if (data.ux_feedback && data.ux_feedback.toast_message) {
          responseText = data.ux_feedback.toast_message;
        } else if (data.result_data && typeof data.result_data === "object") {
          responseText = JSON.stringify(data.result_data, null, 2);
        } else if (typeof data.result_data === "string") {
          responseText = data.result_data;
        } else {
          responseText = "Acao concluida com sucesso!";
        }

        const actionPlan = (data.result_data && data.result_data.action_plan) || 
                           (data.ux_feedback && data.ux_feedback.action_plan) || null;
        const domTelemetry = (data.result_data && data.result_data.dom_telemetry) ||
                             (data.ux_feedback && data.ux_feedback.dom_telemetry) || null;

        await sendToTab(tabId, {
          action: "HUD_COMPLETE",
          task_uuid: taskUuid,
          status: "Finalizado",
          prompt: promptText,
          response_text: responseText,
          action_plan: actionPlan,
          dom_telemetry: domTelemetry
        });
      } else if (status === "ABORTED") {
        clearInterval(intervalId);
        activePollers.delete(taskUuid);

        await sendToTab(tabId, {
          action: "HUD_ABORTED",
          task_uuid: taskUuid,
          status: "Cancelado",
          prompt: promptText,
          reason: data.error_details || "Iteracao cancelada pelo usuario."
        });
      } else if (status === "FAILED") {
        clearInterval(intervalId);
        activePollers.delete(taskUuid);

        await sendToTab(tabId, {
          action: "HUD_FAILED",
          task_uuid: taskUuid,
          status: "Falhou",
          prompt: promptText,
          reason: data.error_details || data.summary || "A tarefa falhou apos atingir o limite de tentativas."
        });
      }
    } catch (err) {
      console.debug("[WebWorker Background] Erro no polling:", err.message);
    }
  }, 500);

  activePollers.set(taskUuid, { intervalId, tabId, startTime });
}

/**
 * Listener central de mensagens para comunicacao entre popup, content script e background.
 */
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "START_TASK") {
    const { user_goal, target_url, priority, is_interactive, save_private_data, tab_id } = request;

    // 1. Notifica a aba imediatamente para abrir a faixa de topo (HUD) no estado Iniciando
    sendToTab(tab_id, {
      action: "HUD_SHOW",
      prompt: user_goal,
      status: "Iniciando"
    });

    const runDispatch = async () => {
      let htmlSnapshot = "";
      let visibleText = "";
      if (tab_id && is_interactive) {
        try {
          const snap = await chrome.tabs.sendMessage(tab_id, { action: "GET_PAGE_SNAPSHOT" });
          if (snap) {
            if (snap.html) htmlSnapshot = snap.html;
            if (snap.visible_text) visibleText = snap.visible_text;
          }
        } catch (_) {}
      }

      try {
        const resp = await fetch(`${GATEWAY_URL}/api/task`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_goal,
            target_url,
            priority,
            task_type: "web_agent",
            is_interactive,
            save_private_data,
            html_snapshot: htmlSnapshot,
            visible_text: visibleText
          })
        });

        const data = await resp.json();
        const taskUuid = data.task_uuid;
        sendResponse({ success: true, task_uuid: taskUuid });

        await sendToTab(tab_id, {
          action: "HUD_UPDATE",
          task_uuid: taskUuid,
          status: "Interagindo",
          prompt: user_goal
        });

        startTaskPolling(taskUuid, tab_id, user_goal);
      } catch (err) {
        sendResponse({ success: false, error: err.message });
        await sendToTab(tab_id, {
          action: "HUD_ABORTED",
          status: "Erro de Conexao",
          prompt: user_goal,
          reason: `Falha ao conectar ao Gateway Go (:8000): ${err.message}`
        });
      }
    };

    runDispatch();
    return true; // Mantem canal assincrono aberto para sendResponse
  }

  if (request.action === "ABORT_TASK") {
    const { task_uuid, tab_id } = request;

    if (task_uuid && activePollers.has(task_uuid)) {
      const poller = activePollers.get(task_uuid);
      clearInterval(poller.intervalId);
      activePollers.delete(task_uuid);
    }

    // Aciona Kill Switch no Gateway Go
    if (task_uuid) {
      fetch(`${GATEWAY_URL}/api/task/abort`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_uuid })
      }).catch((e) => console.debug("Erro ao abortar no Go:", e));
    }

    sendResponse({ success: true });
    return true;
  }
});

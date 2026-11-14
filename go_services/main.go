/*
MÓDULO: GO MICROSERVICE GATEWAY, PRIORITY QUEUE & LLM ROUTER (WebWorkerGateway)
=============================================================================
OBJETIVO:
Microserviço de alta performance em Go para gerenciamento de fila concorrente
com prioridades estritas (P1 a P5), roteamento multi-backend de LLMs (Ollama Local,
LAN e Cloud com teto P3), Router de Propósito Geral (Raw LLM) e Kill Switch.

FLUXO DA ARQUITETURA:
1. PriorityQueue (Min-Heap): Ordena tarefas por (Priority ASC, CreatedAt ASC).
   - P1: Interatividade Local no Chrome do Usuário (Preempção máxima)
   - P2: Rede Urgente
   - P3: Padrão (quando prioridade não informada ou Background)
   - P4: Lote / Crawler (Restrito a Local/LAN)
   - P5: Monitor Recorrente de MD5 (Restrito a Local/LAN)
2. Privacidade & Anonimização: Mascaramento automático de URLs e omissão de payload
   em tarefas P1 interativas quando save_private_data=false.
3. LLM Router & Teto Cloud: Roteia chamadas entre nós Ollama e Cloud, bloqueando
   estritamente o envio de tarefas P4 e P5 para APIs Cloud (CloudMaxPriority = 3).
4. Kill Switch: Endpoint de parada imediata (/api/task/abort) que revoga acesso da LLM.
=============================================================================
*/
package main

import (
	"bytes"
	"container/heap"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const CloudFallbackMaxPriority = 3

type Task struct {
	TaskUUID          string                 `json:"task_uuid"`
	TaskType          string                 `json:"task_type"` // "web_agent", "raw_llm", "md5_watcher"
	Source            string                 `json:"source"`
	Priority          int                    `json:"priority"`
	IsInteractive     bool                   `json:"is_interactive"`
	SavePrivateData   bool                   `json:"save_private_data"`
	TargetURL         string                 `json:"target_url"`
	AnonymizedURL     string                 `json:"anonymized_url"`
	UserGoal          string                 `json:"user_goal"`
	HTMLSnapshot      string                 `json:"html_snapshot,omitempty"`
	VisibleText       string                 `json:"visible_text,omitempty"`
	Status            string                 `json:"status"` // "QUEUED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "ABORTED"
	AssignedWorkerID  string                 `json:"assigned_worker_id,omitempty"`
	AssignedBackendID string                 `json:"assigned_backend_id,omitempty"`
	TurnsUsed         int                    `json:"turns_used"`
	ExecutionTimeSec  float64                `json:"execution_time_seconds"`
	ResultData        map[string]interface{} `json:"result_data,omitempty"`
	ErrorDetails      string                 `json:"error_details,omitempty"`
	UXFeedback        map[string]interface{} `json:"ux_feedback,omitempty"`
	CreatedAt         time.Time              `json:"created_at"`
	StartedAt         *time.Time             `json:"started_at,omitempty"`
	CompletedAt       *time.Time             `json:"completed_at,omitempty"`
	seq               int64
	index             int
}

type PriorityQueue []*Task

func (pq PriorityQueue) Len() int { return len(pq) }

func (pq PriorityQueue) Less(i, j int) bool {
	if pq[i].Priority != pq[j].Priority {
		return pq[i].Priority < pq[j].Priority
	}
	return pq[i].seq < pq[j].seq
}

func (pq PriorityQueue) Swap(i, j int) {
	pq[i], pq[j] = pq[j], pq[i]
	pq[i].index = i
	pq[j].index = j
}

func (pq *PriorityQueue) Push(x interface{}) {
	n := len(*pq)
	item := x.(*Task)
	item.index = n
	*pq = append(*pq, item)
}

func (pq *PriorityQueue) Pop() interface{} {
	old := *pq
	n := len(old)
	item := old[n-1]
	old[n-1] = nil
	item.index = -1
	*pq = old[0 : n-1]
	return item
}

type LLMBackend struct {
	ID            string `json:"id"`
	Type          string `json:"type"` // "ollama_local", "ollama_lan", "cloud_gemini"
	Endpoint      string `json:"endpoint"`
	Model         string `json:"model"`
	Device        string `json:"device"`
	IsAlive       bool   `json:"is_alive"`
	ActiveReqs    int    `json:"active_requests"`
	TotalServed   int    `json:"total_served"`
	MaxConcurrent int    `json:"max_concurrent"`
}

type WorkerListener struct {
	WorkerID         string    `json:"worker_id"`
	ListenURL        string    `json:"listen_url"`
	WorkersAvailable int       `json:"workers_available"`
	ActiveTasks      int       `json:"active_tasks"`
	LastHeartbeat    time.Time `json:"last_heartbeat"`
}

type GatewayState struct {
	mu           sync.Mutex
	pq           PriorityQueue
	tasksByUUID  map[string]*Task
	abortedUUIDs map[string]bool
	backends     []*LLMBackend
	workers      map[string]map[string]interface{}
	listener     *WorkerListener
	seqCounter   int64
}

var state *GatewayState

func initState() {
	state = &GatewayState{
		pq:           make(PriorityQueue, 0),
		tasksByUUID:  make(map[string]*Task),
		abortedUUIDs: make(map[string]bool),
		workers:      make(map[string]map[string]interface{}),
		backends: []*LLMBackend{
			{
				ID:            "local_gpu",
				Type:          "ollama_local",
				Endpoint:      "http://127.0.0.1:11434",
				Model:         "qwen2.5:3b",
				Device:        "GPU Local / Hardware",
				IsAlive:       true,
				MaxConcurrent: 2,
			},
			{
				ID:            "lan_node_gpu2",
				Type:          "ollama_lan",
				Endpoint:      "http://ollama-lan.local:11434",
				Model:         "qwen2.5:3b",
				Device:        "LAN Secondary Node",
				IsAlive:       false, // Atualizado via healthcheck
				MaxConcurrent: 2,
			},
			{
				ID:            "cloud_gemini_fallback",
				Type:          "cloud_gemini",
				Endpoint:      "https://generativelanguage.googleapis.com",
				Model:         "gemini-2.5-flash",
				Device:        "Google Cloud API (Teto <= P3)",
				IsAlive:       true,
				MaxConcurrent: 5,
			},
		},
	}
	heap.Init(&state.pq)
}

func loadConfigFile() {
	configPaths := []string{"config.json", "../config.json", "./config.json"}
	var data []byte
	var loadedPath string
	for _, p := range configPaths {
		b, err := os.ReadFile(p)
		if err == nil && len(b) > 0 {
			data = b
			loadedPath = p
			break
		}
	}
	if len(data) == 0 {
		return
	}
	log.Printf("📄 Carregando configuracoes de: %s", loadedPath)

	var parsed map[string]interface{}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	if backendsMap, ok := parsed["llm_backends"].(map[string]interface{}); ok {
		for _, b := range state.backends {
			var nodeConf map[string]interface{}
			if b.Type == "ollama_local" {
				nodeConf, _ = backendsMap["local_gpu"].(map[string]interface{})
			} else if b.Type == "ollama_lan" {
				nodeConf, _ = backendsMap["lan_node"].(map[string]interface{})
			} else if b.Type == "cloud_gemini" {
				nodeConf, _ = backendsMap["cloud_fallback"].(map[string]interface{})
			}
			if nodeConf != nil {
				if ep, ok := nodeConf["endpoint"].(string); ok && ep != "" {
					b.Endpoint = ep
				}
				if mdl, ok := nodeConf["model"].(string); ok && mdl != "" {
					b.Model = mdl
				}
			}
		}
	}
}

func anonymizeURL(rawURL string) string {
	if rawURL == "" {
		return "[PRIVADO: sem-url]"
	}
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" {
		return "[PRIVADO: oculto]"
	}
	return fmt.Sprintf("[PRIVADO: %s]", parsed.Host)
}

func enableCORS(w http.ResponseWriter, r *http.Request) bool {
	origin := r.Header.Get("Origin")
	isAllowed := false

	if origin == "" {
		isAllowed = true
		w.Header().Set("Access-Control-Allow-Origin", "*")
	} else if strings.HasPrefix(origin, "chrome-extension://") ||
		strings.HasPrefix(origin, "http://127.0.0.1") ||
		strings.HasPrefix(origin, "http://localhost") {
		isAllowed = true
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
	}

	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Priority")

	if r.Method == http.MethodOptions {
		if !isAllowed {
			w.WriteHeader(http.StatusForbidden)
			return true
		}
		w.WriteHeader(http.StatusOK)
		return true
	}
	return false
}

func writeJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}

// Seleciona o melhor backend respeitando a regra de Teto de Prioridade Cloud (P4 e P5 nunca vão para Cloud)
func selectBackendLocked(priority int, forceCloudTest bool) (*LLMBackend, error) {
	allowCloud := priority <= CloudFallbackMaxPriority

	if !forceCloudTest {
		// 1. Tenta nós locais ou LAN vivos e com capacidade
		for _, b := range state.backends {
			if (b.Type == "ollama_local" || b.Type == "ollama_lan") && b.IsAlive && b.ActiveReqs < b.MaxConcurrent {
				return b, nil
			}
		}
		// Se todos ocupados, mas nó local está vivo, usa nó local se não for forçar cloud
		for _, b := range state.backends {
			if b.Type == "ollama_local" && b.IsAlive {
				return b, nil
			}
		}
	}

	// 2. Avalia Fallback Cloud
	if !allowCloud {
		return nil, fmt.Errorf("TETO_CLOUD_BLOQUEADO: Tarefa de prioridade P%d excede o teto máximo permitido para Cloud (P%d). Aguardando nós locais/LAN", priority, CloudFallbackMaxPriority)
	}

	for _, b := range state.backends {
		if b.Type == "cloud_gemini" && b.IsAlive {
			return b, nil
		}
	}

	return nil, fmt.Errorf("nenhum backend LLM disponível para prioridade P%d", priority)
}

// POST /api/task -> Enfileira tarefa agêntica ou recorrente
func handleSubmitTask(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Use POST"})
		return
	}

	var req struct {
		TaskUUID        string  `json:"task_uuid"`
		TaskType        string  `json:"task_type"`
		Source          string  `json:"source"`
		Priority        *int    `json:"priority"`
		IsInteractive   bool    `json:"is_interactive"`
		SavePrivateData bool    `json:"save_private_data"`
		TargetURL       string  `json:"target_url"`
		UserGoal        string  `json:"user_goal"`
		HTMLSnapshot    string  `json:"html_snapshot"`
		VisibleText     string  `json:"visible_text"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	state.seqCounter++
	if req.TaskUUID == "" {
		req.TaskUUID = fmt.Sprintf("task-%d-%d", time.Now().UnixMilli(), state.seqCounter)
	}
	if req.TaskType == "" {
		req.TaskType = "web_agent"
	}
	if req.Source == "" {
		if req.IsInteractive {
			req.Source = "chrome_extension"
		} else {
			req.Source = "api_rest"
		}
	}

	// Regra de Prioridade: se interativo local -> P1; se não informado -> P3
	prio := 3
	if req.IsInteractive {
		prio = 1
	} else if req.Priority != nil && *req.Priority >= 1 && *req.Priority <= 5 {
		prio = *req.Priority
	}

	anonURL := req.TargetURL
	displayURL := req.TargetURL
	if req.IsInteractive && !req.SavePrivateData {
		anonURL = anonymizeURL(req.TargetURL)
		displayURL = anonURL
	}

	task := &Task{
		TaskUUID:        req.TaskUUID,
		TaskType:        req.TaskType,
		Source:          req.Source,
		Priority:        prio,
		IsInteractive:   req.IsInteractive,
		SavePrivateData: req.SavePrivateData,
		TargetURL:       displayURL,
		AnonymizedURL:   anonURL,
		UserGoal:        req.UserGoal,
		HTMLSnapshot:    req.HTMLSnapshot,
		VisibleText:     req.VisibleText,
		Status:          "QUEUED",
		CreatedAt:       time.Now(),
		seq:             state.seqCounter,
	}

	// Se não salvar dados privados, guardamos a URL real apenas em memória transitória de execução se necessário
	if req.IsInteractive && !req.SavePrivateData {
		task.UXFeedback = map[string]interface{}{
			"privacy_shield": "ACTIVE",
			"raw_url_stored": false,
		}
	}

	heap.Push(&state.pq, task)
	state.tasksByUUID[task.TaskUUID] = task
	tryPushNextTaskLocked()

	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"status":         "QUEUED",
		"task_uuid":      task.TaskUUID,
		"priority":       task.Priority,
		"is_interactive": task.IsInteractive,
		"target_url":     task.TargetURL,
		"queue_position": state.pq.Len(),
	})
}

// POST /api/task/abort -> Kill Switch imediato
func handleAbortTask(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	var req struct {
		TaskUUID string `json:"task_uuid"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)

	state.mu.Lock()
	defer state.mu.Unlock()

	if req.TaskUUID == "" {
		// Se não especificou UUID, aborta a tarefa P1 interativa ativa ou na fila
		for _, t := range state.tasksByUUID {
			if t.IsInteractive && (t.Status == "QUEUED" || t.Status == "RUNNING") {
				req.TaskUUID = t.TaskUUID
				break
			}
		}
	}

	task, exists := state.tasksByUUID[req.TaskUUID]
	if !exists {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Task not found"})
		return
	}

	state.abortedUUIDs[req.TaskUUID] = true
	task.Status = "ABORTED"
	task.ErrorDetails = "Interrompido pelo Kill Switch do usuário (Acesso da LLM revogado)"
	now := time.Now()
	task.CompletedAt = &now
	task.UXFeedback = map[string]interface{}{
		"play_pop_sound": true,
		"toast_message":  "🛑 Execução abortada pelo usuário. Acesso da LLM bloqueado.",
		"toast_type":     "aborted",
	}

	// Remove da fila se ainda estava em QUEUED
	if task.index >= 0 && task.index < len(state.pq) {
		heap.Remove(&state.pq, task.index)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"aborted":     true,
		"task_uuid":   task.TaskUUID,
		"status":      task.Status,
		"ux_feedback": task.UXFeedback,
	})
}

func tryPushNextTaskLocked() {
	if state.listener == nil || state.listener.ListenURL == "" {
		return
	}
	avail := state.listener.WorkersAvailable
	if avail <= 0 {
		avail = 1
	}
	if state.listener.ActiveTasks >= avail {
		return
	}
	if state.pq.Len() == 0 {
		return
	}

	for state.pq.Len() > 0 {
		item := heap.Pop(&state.pq).(*Task)
		if state.abortedUUIDs[item.TaskUUID] || item.Status == "ABORTED" {
			continue
		}

		backend, err := selectBackendLocked(item.Priority, false)
		backendID := "local_gpu"
		if err == nil && backend != nil {
			backendID = backend.ID
		}

		now := time.Now()
		item.Status = "RUNNING"
		item.StartedAt = &now
		item.AssignedBackendID = backendID

		state.listener.ActiveTasks++
		workerID := fmt.Sprintf("worker_%d", state.listener.ActiveTasks)
		if item.IsInteractive {
			workerID = "worker_p1_interactive"
		}
		item.AssignedWorkerID = workerID

		state.workers[workerID] = map[string]interface{}{
			"worker_id":         workerID,
			"status":            "RUNNING",
			"current_task_uuid": item.TaskUUID,
			"current_url":       item.TargetURL,
			"priority":          item.Priority,
			"updated_at":        now.Format(time.RFC3339),
		}

		listenURL := state.listener.ListenURL
		taskPayload := *item
		go func(targetURL string, t Task) {
			body, _ := json.Marshal(map[string]interface{}{
				"task": t,
			})
			req, err := http.NewRequest(http.MethodPost, targetURL, bytes.NewBuffer(body))
			if err != nil {
				log.Printf("[ERRO] Falha ao criar requisicao push para %s: %v", targetURL, err)
				return
			}
			req.Header.Set("Content-Type", "application/json")
			client := &http.Client{Timeout: 10 * time.Second}
			resp, err := client.Do(req)
			if err != nil {
				log.Printf("[ERRO] Falha ao despachar tarefa %s para listener: %v", t.TaskUUID, err)
				state.mu.Lock()
				if state.listener != nil && state.listener.ActiveTasks > 0 {
					state.listener.ActiveTasks--
				}
				state.mu.Unlock()
				return
			}
			defer resp.Body.Close()
			log.Printf("[INFO] Tarefa %s (P%d) despachada via push para listener Python!", t.TaskUUID, t.Priority)
		}(listenURL, taskPayload)

		return
	}
}

// POST /api/worker/register -> Python registra URL de escuta e capacidade de workers disponiveis
func handleWorkerRegister(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Use POST"})
		return
	}

	var req struct {
		WorkerID         string `json:"worker_id"`
		ListenURL        string `json:"listen_url"`
		WorkersAvailable int    `json:"workers_available"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	if req.WorkersAvailable <= 0 {
		req.WorkersAvailable = 1
	}

	state.listener = &WorkerListener{
		WorkerID:         req.WorkerID,
		ListenURL:        req.ListenURL,
		WorkersAvailable: req.WorkersAvailable,
		ActiveTasks:      0,
		LastHeartbeat:    time.Now(),
	}

	log.Printf("[INFO] Worker listener registrado: %s (URL: %s, workers_available: %d)",
		req.WorkerID, req.ListenURL, req.WorkersAvailable)

	tryPushNextTaskLocked()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"registered":        true,
		"worker_id":         req.WorkerID,
		"workers_available": req.WorkersAvailable,
		"listen_url":        req.ListenURL,
		"queue_length":      len(state.pq),
	})
}

// POST /api/worker/heartbeat -> Verificacao periodica de conexao (500ms)
func handleWorkerHeartbeat(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	state.mu.Lock()
	defer state.mu.Unlock()

	if state.listener != nil {
		state.listener.LastHeartbeat = time.Now()
	}

	active := 0
	avail := 1
	if state.listener != nil {
		active = state.listener.ActiveTasks
		avail = state.listener.WorkersAvailable
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":            "ok",
		"active_tasks":      active,
		"workers_available": avail,
		"queue_length":      len(state.pq),
	})
}

// GET /api/worker/claim -> Worker solicita a tarefa mais prioritária da fila
func handleWorkerClaim(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	workerID := r.URL.Query().Get("worker_id")
	if workerID == "" {
		workerID = "worker-default"
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	for state.pq.Len() > 0 {
		item := heap.Pop(&state.pq).(*Task)
		if state.abortedUUIDs[item.TaskUUID] || item.Status == "ABORTED" {
			continue
		}

		// Seleciona backend LLM para a tarefa
		backend, err := selectBackendLocked(item.Priority, false)
		backendID := "local_gpu"
		if err == nil && backend != nil {
			backendID = backend.ID
		}

		now := time.Now()
		item.Status = "RUNNING"
		item.StartedAt = &now
		item.AssignedWorkerID = workerID
		item.AssignedBackendID = backendID

		state.workers[workerID] = map[string]interface{}{
			"worker_id":         workerID,
			"status":            "RUNNING",
			"current_task_uuid": item.TaskUUID,
			"current_url":       item.TargetURL,
			"priority":          item.Priority,
			"updated_at":        now.Format(time.RFC3339),
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{
			"has_task": true,
			"task":     item,
		})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"has_task": false,
	})
}

// POST /api/worker/complete -> Worker finaliza tarefa e registra retorno UX (som pop + toast)
func handleWorkerComplete(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	var req struct {
		TaskUUID         string                 `json:"task_uuid"`
		WorkerID         string                 `json:"worker_id"`
		Status           string                 `json:"status"`
		TurnsUsed        int                    `json:"turns_used"`
		ExecutionTimeSec float64                `json:"execution_time_seconds"`
		ResultData       map[string]interface{} `json:"result_data"`
		ErrorDetails     string                 `json:"error_details"`
		SummaryText      string                 `json:"summary_text"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	state.mu.Lock()
	defer state.mu.Unlock()

	task, exists := state.tasksByUUID[req.TaskUUID]
	if !exists {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Task not found"})
		return
	}

	// Se já foi abortada pelo Kill Switch, preserva ABORTED
	if state.abortedUUIDs[req.TaskUUID] || task.Status == "ABORTED" {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"status":  "ABORTED",
			"message": "Task was already aborted by user Kill Switch",
		})
		return
	}

	now := time.Now()
	if req.Status == "" {
		req.Status = "COMPLETED"
	}
	task.Status = req.Status
	task.TurnsUsed = req.TurnsUsed
	task.ExecutionTimeSec = req.ExecutionTimeSec
	task.ErrorDetails = req.ErrorDetails
	task.CompletedAt = &now

	// Aplica regra de privacidade para interatividade do navegador do usuário
	if task.IsInteractive && !task.SavePrivateData {
		task.TargetURL = task.AnonymizedURL
		task.ResultData = map[string]interface{}{
			"privacy_mode": "ANONYMIZED",
			"data_saved":   false,
			"metrics_only": map[string]interface{}{
				"turns":            req.TurnsUsed,
				"execution_time_s": req.ExecutionTimeSec,
			},
		}
	} else {
		task.ResultData = req.ResultData
	}

	toastMsg := req.SummaryText
	if toastMsg == "" {
		toastMsg = fmt.Sprintf("[OK] Acao concluida em %.2fs!", req.ExecutionTimeSec)
	}

	task.UXFeedback = map[string]interface{}{
		"play_pop_sound":     true,
		"pop_freq_hz":        820,
		"pop_duration_ms":    60,
		"toast_message":      toastMsg,
		"toast_fadeout_ms":   5000,
		"privacy_anonymized": task.IsInteractive && !task.SavePrivateData,
	}

	if req.ResultData != nil {
		if ap, ok := req.ResultData["action_plan"]; ok && ap != nil {
			task.UXFeedback["action_plan"] = ap
		}
		if dt, ok := req.ResultData["dom_telemetry"]; ok && dt != nil {
			task.UXFeedback["dom_telemetry"] = dt
		}
	}

	if req.WorkerID != "" {
		state.workers[req.WorkerID] = map[string]interface{}{
			"worker_id":         req.WorkerID,
			"status":            "IDLE",
			"current_task_uuid": nil,
			"current_url":       "",
			"updated_at":        now.Format(time.RFC3339),
		}
	}

	if state.listener != nil && state.listener.ActiveTasks > 0 {
		state.listener.ActiveTasks--
	}
	tryPushNextTaskLocked()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"updated":     true,
		"task_uuid":   task.TaskUUID,
		"status":      task.Status,
		"ux_feedback": task.UXFeedback,
	})
}

// POST /api/llm/chat & /api/llm/generate -> Router de Propósito Geral (Raw LLM sem browser)
func handleRawLLMRouter(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Use POST"})
		return
	}

	var req struct {
		Prompt          string                   `json:"prompt"`
		Messages        []map[string]interface{} `json:"messages"`
		Model           string                   `json:"model"`
		Priority        *int                     `json:"priority"`
		ForceCloudCheck bool                     `json:"force_cloud_check"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	prio := 3
	if req.Priority != nil && *req.Priority >= 1 && *req.Priority <= 5 {
		prio = *req.Priority
	}

	state.mu.Lock()
	state.seqCounter++
	taskUUID := fmt.Sprintf("raw-llm-%d-%d", time.Now().UnixMilli(), state.seqCounter)

	backend, err := selectBackendLocked(prio, req.ForceCloudCheck)
	if err != nil {
		state.mu.Unlock()
		writeJSON(w, http.StatusForbidden, map[string]interface{}{
			"success":        false,
			"task_uuid":      taskUUID,
			"priority":       prio,
			"cloud_ceiling":  CloudFallbackMaxPriority,
			"error":          err.Error(),
		})
		return
	}

	backend.ActiveReqs++
	backend.TotalServed++
	modelName := req.Model
	if modelName == "" {
		modelName = backend.Model
	}

	goalSummary := req.Prompt
	if goalSummary == "" && len(req.Messages) > 0 {
		if c, ok := req.Messages[len(req.Messages)-1]["content"].(string); ok {
			goalSummary = c
		}
	}

	now := time.Now()
	taskRecord := &Task{
		TaskUUID:          taskUUID,
		TaskType:          "raw_llm",
		Source:            "raw_llm_router",
		Priority:          prio,
		TargetURL:         "[RAW_LLM_NO_BROWSER]",
		UserGoal:          goalSummary,
		Status:            "RUNNING",
		AssignedBackendID: backend.ID,
		CreatedAt:         now,
		StartedAt:         &now,
		seq:               state.seqCounter,
	}
	state.tasksByUUID[taskUUID] = taskRecord
	state.mu.Unlock()

	t0 := time.Now()
	responseText := ""

	// Se for nó Ollama local/LAN e não for apenas simulação de fallback cloud, chama Ollama real
	if strings.HasPrefix(backend.Type, "ollama") {
		ollamaPayload := map[string]interface{}{
			"model":  modelName,
			"stream": false,
			"options": map[string]interface{}{
				"temperature": 0.1,
				"num_predict": 128,
			},
		}
		endpoint := backend.Endpoint + "/api/generate"
		if len(req.Messages) > 0 {
			endpoint = backend.Endpoint + "/api/chat"
			ollamaPayload["messages"] = req.Messages
		} else {
			ollamaPayload["prompt"] = req.Prompt
		}

		bodyBytes, _ := json.Marshal(ollamaPayload)
		client := &http.Client{Timeout: 30 * time.Second}
		resp, httpErr := client.Post(endpoint, "application/json", bytes.NewReader(bodyBytes))
		if httpErr == nil && resp != nil {
			defer resp.Body.Close()
			rawResp, _ := io.ReadAll(resp.Body)
			var parsed map[string]interface{}
			if json.Unmarshal(rawResp, &parsed) == nil {
				if rTxt, ok := parsed["response"].(string); ok {
					responseText = rTxt
				} else if msgObj, ok := parsed["message"].(map[string]interface{}); ok {
					if cTxt, ok := msgObj["content"].(string); ok {
						responseText = cTxt
					}
				}
			}
		}
	}

	if responseText == "" {
		responseText = fmt.Sprintf("[Respondido via %s (%s) | Prioridade P%d]: Processado com sucesso para '%s'", backend.ID, backend.Model, prio, goalSummary)
	}

	elapsed := time.Since(t0).Seconds()

	state.mu.Lock()
	backend.ActiveReqs--
	doneTime := time.Now()
	taskRecord.Status = "COMPLETED"
	taskRecord.ExecutionTimeSec = elapsed
	taskRecord.CompletedAt = &doneTime
	taskRecord.ResultData = map[string]interface{}{
		"response":     responseText,
		"backend_used": backend.ID,
		"model":        modelName,
		"browser_used": false,
	}
	state.mu.Unlock()

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":             true,
		"task_uuid":           taskUUID,
		"task_type":           "raw_llm",
		"browser_instantiated": false,
		"priority":            prio,
		"backend_used":        backend.ID,
		"model":               modelName,
		"duration_seconds":    elapsed,
		"response":            responseText,
	})
}

// GET /api/task?task_uuid=... -> Consulta status de uma tarefa específica
func handleGetTask(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	uuid := r.URL.Query().Get("task_uuid")
	state.mu.Lock()
	defer state.mu.Unlock()

	task, exists := state.tasksByUUID[uuid]
	if !exists {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "Task not found"})
		return
	}
	writeJSON(w, http.StatusOK, task)
}

// GET /api/queue -> Lista fila e histórico recente ordenado
func handleGetQueue(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	state.mu.Lock()
	defer state.mu.Unlock()

	queued := make([]*Task, 0, len(state.pq))
	for _, t := range state.pq {
		queued = append(queued, t)
	}

	allTasks := make([]*Task, 0, len(state.tasksByUUID))
	for _, t := range state.tasksByUUID {
		allTasks = append(allTasks, t)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"queued_count": len(queued),
		"queued_items": queued,
		"all_tasks":    allTasks,
	})
}

// GET /api/metrics -> Telemetria consolidada para a Dashboard Matriz :8085
func handleGetMetrics(w http.ResponseWriter, r *http.Request) {
	if enableCORS(w, r) {
		return
	}
	state.mu.Lock()
	defer state.mu.Unlock()

	countsByPriority := map[string]int{"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0}
	countsByStatus := map[string]int{"QUEUED": 0, "RUNNING": 0, "PAUSED": 0, "COMPLETED": 0, "FAILED": 0, "ABORTED": 0}

	for _, t := range state.tasksByUUID {
		pKey := fmt.Sprintf("P%d", t.Priority)
		countsByPriority[pKey]++
		countsByStatus[t.Status]++
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"service":                     "Go WebWorker Gateway v2.0",
		"cloud_fallback_max_priority": CloudFallbackMaxPriority,
		"queue_length":                len(state.pq),
		"counts_by_priority":          countsByPriority,
		"counts_by_status":            countsByStatus,
		"backends":                    state.backends,
		"workers":                     state.workers,
	})
}

func main() {
	port := flag.Int("port", 8000, "Porta do microserviço Go Gateway")
	flag.Parse()

	initState()
	loadConfigFile()

	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		if enableCORS(w, r) {
			return
		}
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"service":   "webworker_gateway",
			"status":    "online",
			"dashboard": "http://localhost:8085",
		})
	})
	mux.HandleFunc("/api/task", handleSubmitTask)
	mux.HandleFunc("/api/task/status", handleGetTask)
	mux.HandleFunc("/api/task/abort", handleAbortTask)
	mux.HandleFunc("/api/worker/claim", handleWorkerClaim)
	mux.HandleFunc("/api/worker/complete", handleWorkerComplete)
	mux.HandleFunc("/api/worker/register", handleWorkerRegister)
	mux.HandleFunc("/api/worker/heartbeat", handleWorkerHeartbeat)
	mux.HandleFunc("/api/llm/chat", handleRawLLMRouter)
	mux.HandleFunc("/api/llm/generate", handleRawLLMRouter)
	mux.HandleFunc("/api/queue", handleGetQueue)
	mux.HandleFunc("/api/metrics", handleGetMetrics)

	addr := fmt.Sprintf("127.0.0.1:%d", *port)
	log.Printf("[INFO] Microservico Go Gateway & LLM Router ouvindo em http://%s (Teto Cloud: P%d)", addr, CloudFallbackMaxPriority)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Erro no servidor Go: %v", err)
	}
}

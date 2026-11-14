"""
MÓDULO: HARDWARE & INFERENCE TELEMETRY PROFILER (GpuTelemetry / HardwareTelemetry)
=============================================================================
OBJETIVO:
Monitorar em tempo real o uso de memória (VRAM de GPU ou RAM do Host), temperatura
e carga de computação durante a inferência dos modelos de IA, eliminando dados
fictícios ou números mágicos hardcoded.

ESTRATÉGIA OLLAMA-FIRST & MULTI-HARDWARE:
1. Consulta ao Ollama (/api/ps): Pergunta ao host onde o modelo está alocado
   (100% VRAM na GPU, 100% RAM na CPU ou Híbrido).
2. Sensor de Hardware Dedicado:
   - NVIDIA: Leitura precisa via nvidia-smi (VRAM real, carga %, temperatura).
   - AMD Radeon: Leitura via rocm-smi quando disponível.
3. Fallback Nativo de CPU & Host RAM:
   - Se a inferência estiver rodando em CPU ou nenhuma GPU dedicada for encontrada,
     lê a memória RAM física real do sistema operacional (via Windows Kernel32
     ou /proc/meminfo no Linux) com 0ms de latência e sem dependências externas.
=============================================================================
"""
import json
import logging
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Optional, Dict, Any

logger = logging.getLogger("HardwareTelemetry")


@dataclass
class GPUStats:
    """Snapshot das métricas de Hardware (GPU dedicada ou CPU/Host RAM)."""
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    memory_free_mb: int = 0
    gpu_util_pct: int = 0
    temperature_c: int = 0
    is_available: bool = True
    device_type: str = "NVIDIA_GPU"  # "NVIDIA_GPU", "AMD_GPU", "CPU_HOST", "HYBRID"
    device_name: str = "GPU"
    active_model: str = ""
    model_vram_mb: int = 0
    model_ram_mb: int = 0


def _get_host_ram_stats() -> Dict[str, int]:
    """Lê a memória RAM física real do sistema operacional sem valores fictícios."""
    # 1. Windows nativo via ctypes
    if os.name == "nt":
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExt", ctypes.c_ulonglong)
                ]

            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                total_mb = int(m.ullTotalPhys // (1024 * 1024))
                free_mb = int(m.ullAvailPhys // (1024 * 1024))
                used_mb = max(0, total_mb - free_mb)
                load_pct = int(m.dwMemoryLoad)
                return {
                    "total_mb": total_mb,
                    "used_mb": used_mb,
                    "free_mb": free_mb,
                    "load_pct": load_pct,
                    "available": True
                }
        except Exception:
            pass

    # 2. Linux nativo via /proc/meminfo
    try:
        if os.path.exists("/proc/meminfo"):
            meminfo = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_str = parts[1].strip().split()[0]
                        meminfo[key] = int(val_str)
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            total_mb = total_kb // 1024
            free_mb = avail_kb // 1024
            used_mb = max(0, total_mb - free_mb)
            load_pct = int((used_mb / total_mb) * 100) if total_mb > 0 else 0
            return {
                "total_mb": total_mb,
                "used_mb": used_mb,
                "free_mb": free_mb,
                "load_pct": load_pct,
                "available": True
            }
    except Exception:
        pass

    return {"total_mb": 0, "used_mb": 0, "free_mb": 0, "load_pct": 0, "available": False}


def _query_ollama_ps(ollama_host: str = "http://127.0.0.1:11434") -> Optional[Dict[str, Any]]:
    """Consulta o endpoint /api/ps do Ollama para identificar modelos ativos e uso de VRAM/RAM."""
    url = f"{ollama_host.rstrip('/')}/api/ps"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WebWorker-Telemetry"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])
            if models:
                m = models[0]
                total_bytes = m.get("size", 0)
                vram_bytes = m.get("size_vram", 0)
                total_mb = total_bytes // (1024 * 1024)
                vram_mb = vram_bytes // (1024 * 1024)
                ram_mb = max(0, (total_bytes - vram_bytes) // (1024 * 1024))
                return {
                    "has_model": True,
                    "model_name": m.get("name", ""),
                    "total_mb": total_mb,
                    "vram_mb": vram_mb,
                    "ram_mb": ram_mb,
                    "is_gpu": vram_bytes > 0,
                    "is_cpu_only": vram_bytes == 0,
                    "is_hybrid": (vram_bytes > 0 and ram_mb > 0)
                }
            return {"has_model": False}
    except Exception:
        return None


class GPUTelemetry:
    """Monitor unificado de telemetria de Hardware com estratégia Ollama-First."""

    def __init__(self, ollama_endpoint: str = "http://127.0.0.1:11434"):
        self.ollama_endpoint = ollama_endpoint
        self._last_detected_device: Optional[str] = None

    def _query_nvidia_smi(self) -> Optional[GPUStats]:
        """Tenta consultar nvidia-smi para GPU dedicada NVIDIA."""
        cmd = [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3)
            line = res.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                used = int(parts[0])
                total = int(parts[1])
                free = int(parts[2])
                util = int(parts[3])
                temp = int(float(parts[4]))
                self._last_detected_device = "NVIDIA_GPU"
                return GPUStats(
                    memory_used_mb=used,
                    memory_total_mb=total,
                    memory_free_mb=free,
                    gpu_util_pct=util,
                    temperature_c=temp,
                    is_available=True,
                    device_type="NVIDIA_GPU",
                    device_name="GPU NVIDIA"
                )
        except Exception:
            pass
        return None

    def _query_amd_smi(self) -> Optional[GPUStats]:
        """Tenta consultar rocm-smi para GPU dedicada AMD Radeon."""
        cmd = ["rocm-smi", "--showmeminfo", "vram", "--json"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3)
            data = json.loads(res.stdout)
            # Extrai os primeiros dados de memória disponíveis
            for card, info in data.items():
                total = int(info.get("VRAM Total Memory (B)", 0)) // (1024 * 1024)
                used = int(info.get("VRAM Total Used Memory (B)", 0)) // (1024 * 1024)
                if total > 0:
                    free = max(0, total - used)
                    util = int((used / total) * 100) if total > 0 else 0
                    self._last_detected_device = "AMD_GPU"
                    return GPUStats(
                        memory_used_mb=used,
                        memory_total_mb=total,
                        memory_free_mb=free,
                        gpu_util_pct=util,
                        temperature_c=0,
                        is_available=True,
                        device_type="AMD_GPU",
                        device_name="GPU AMD Radeon"
                    )
        except Exception:
            pass
        return None

    def get_current_stats(self) -> GPUStats:
        """
        Retorna o snapshot real de hardware:
        1. Consulta o Ollama (/api/ps) para checar o modo de execução da IA.
        2. Se houver GPU ativa, consulta os sensores reais (NVIDIA ou AMD).
        3. Se o Ollama estiver rodando na CPU ou nenhuma GPU dedicada for encontrada,
           retorna com precisão a memória RAM física real do sistema operacional.
        """
        # 1. Consulta ao Ollama (A fonte da verdade da IA)
        ollama_info = _query_ollama_ps(self.ollama_endpoint)
        active_model = ""
        model_vram_mb = 0
        model_ram_mb = 0
        is_cpu_mode = False

        if ollama_info and ollama_info.get("has_model"):
            active_model = ollama_info.get("model_name", "")
            model_vram_mb = ollama_info.get("vram_mb", 0)
            model_ram_mb = ollama_info.get("ram_mb", 0)
            is_cpu_mode = ollama_info.get("is_cpu_only", False)

        # 2. Se o Ollama explicitou que está 100% na CPU, prioriza a telemetria da Host RAM
        if is_cpu_mode:
            ram = _get_host_ram_stats()
            return GPUStats(
                memory_used_mb=ram["used_mb"],
                memory_total_mb=ram["total_mb"],
                memory_free_mb=ram["free_mb"],
                gpu_util_pct=ram["load_pct"],
                temperature_c=0,
                is_available=ram["available"],
                device_type="CPU_HOST",
                device_name="Processador & RAM do Sistema",
                active_model=active_model,
                model_vram_mb=0,
                model_ram_mb=model_ram_mb
            )

        # 3. Tenta ler sensores reais de GPU (NVIDIA ou AMD)
        nv_stats = self._query_nvidia_smi()
        if nv_stats:
            nv_stats.active_model = active_model
            nv_stats.model_vram_mb = model_vram_mb
            nv_stats.model_ram_mb = model_ram_mb
            if model_ram_mb > 0:
                nv_stats.device_type = "HYBRID"
            return nv_stats

        amd_stats = self._query_amd_smi()
        if amd_stats:
            amd_stats.active_model = active_model
            amd_stats.model_vram_mb = model_vram_mb
            amd_stats.model_ram_mb = model_ram_mb
            if model_ram_mb > 0:
                amd_stats.device_type = "HYBRID"
            return amd_stats

        # 4. Fallback sem GPU: lê a RAM física real do Host (sem números fictícios)
        ram = _get_host_ram_stats()
        return GPUStats(
            memory_used_mb=ram["used_mb"],
            memory_total_mb=ram["total_mb"],
            memory_free_mb=ram["free_mb"],
            gpu_util_pct=ram["load_pct"],
            temperature_c=0,
            is_available=ram["available"],
            device_type="CPU_HOST",
            device_name="Processador & RAM do Sistema",
            active_model=active_model,
            model_vram_mb=model_vram_mb,
            model_ram_mb=model_ram_mb
        )

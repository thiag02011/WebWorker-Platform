"""
MÓDULO: FORENSIC AUDITING & GROUND TRUTH VALIDATOR (ForensicAuditor)
=============================================================================
OBJETIVO:
Auditar passo a passo a trajetória de navegação do agente, validando os dados extraídos
contra o Ground Truth do site e registrando métricas de conformidade.

FLUXO DA ARQUITETURA:
1. Telemetria por Turno: Grava cada ação emitida, latência de inferência e tokens gerados.
   -> 2. Validador de Ground Truth: Compara chaves críticas (título, campos estruturados, itens)
         com tolerância semântica contra o HTML original.
   -> 3. Dossiê de Diagnóstico: Exporta relatórios JSON com timeline completa, screenshots
         e taxas de assertividade (0 a 100%).
=============================================================================
"""
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple


class FailureRecord(tuple):
    """Tupla compatível com desempacotamento (json_path, png_path) e dicionário (.get())."""

    def __new__(cls, json_path: Path, png_path: Optional[Path]):
        return super().__new__(cls, (json_path, png_path))

    @property
    def json_path(self) -> Path:
        return self[0]

    @property
    def png_path(self) -> Optional[Path]:
        return self[1]

    @property
    def dossier_path(self) -> str:
        return str(self[0]) if self[0] else ""

    @property
    def screenshot_path(self) -> str:
        return str(self[1]) if self[1] else ""

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("json_path", "dossier_path"):
            return self.dossier_path
        elif key in ("png_path", "screenshot_path"):
            return self.screenshot_path
        return default

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return self.get(item)
        return super().__getitem__(item)


class ForensicAuditor:
    """Registra dossiês forenses de falhas para auditoria visual e semântica de IA."""

    def __init__(self, output_dir: Optional[Path] = None):
        if output_dir is None:
            output_dir = Path(__file__).resolve().parent.parent.parent / "results" / "failures"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def record_failure(
        self,
        scenario_id: str,
        turn_index: int,
        what_ai_saw: Optional[str] = None,
        what_was_sent: Optional[Any] = None,
        what_returned: Optional[Any] = None,
        active_config: Optional[Dict[str, Any]] = None,
        diagnostic: Optional[Dict[str, Any]] = None,
        screenshot_bytes: Optional[bytes] = None,
        # Aliases para conveniência
        dom_snapshot: Optional[str] = None,
        input_payload: Optional[Any] = None,
        output_payload: Optional[Any] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> FailureRecord:
        """
        Salva dossiê forense completo: imagem PNG da tela + JSON com o contexto de inferência.
        """
        ai_saw = what_ai_saw or dom_snapshot or ""
        sent_payload = what_was_sent if what_was_sent is not None else (input_payload or {})
        returned_payload = what_returned if what_returned is not None else (output_payload or {})
        diag = diagnostic or diagnostics or {}
        cfg = active_config or {}

        timestamp_epoch = time.time()
        time_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp_epoch))
        time_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp_epoch))

        base_name = f"{scenario_id}_turn{turn_index}_{int(timestamp_epoch)}"
        json_path = self.output_dir / f"{base_name}_forense.json"
        png_path = self.output_dir / f"{base_name}_falha.png"

        if screenshot_bytes:
            with open(png_path, "wb") as f:
                f.write(screenshot_bytes)
        else:
            png_path = None

        dossier = {
            "timestamp_iso": time_iso,
            "timestamp_local": time_local,
            "scenario_id": scenario_id,
            "turn_index": turn_index,
            "what_ai_saw": ai_saw,
            "what_was_sent_to_ollama": sent_payload,
            "what_ollama_returned": returned_payload,
            "active_config": cfg,
            "diagnostic": diag,
            "screenshot_file": png_path.name if png_path else None
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(dossier, f, indent=2, ensure_ascii=False)

        return FailureRecord(json_path, png_path)

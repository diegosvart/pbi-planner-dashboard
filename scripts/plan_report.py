"""
Motor parametrizado de extracción Dataverse → reporte de gestión de planes.

Uso:
    python plan_report.py --plan "Planificación área TI 2026"
    python plan_report.py --project-id <GUID> --today 2026-06-11 --out report.csv
    python plan_report.py --plan "..." --cache-file %TEMP%\\ti.json
    python plan_report.py --from-cache %TEMP%\\ti.json --out report.csv
    python plan_report.py --from-cache %TEMP%\\ti.json --html reporte_pmo.html
    python plan_report.py --from-cache %TEMP%\\ti.json --out report.csv --html reporte.html

Requiere: az CLI autenticado con dmorales@grupoebi.cl
          az account get-access-token (tenant b16beb2c-1c93-4497-bc75-5a1cdae6ee6c)
"""
import argparse
import csv
import html as _html
import io
import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone


ORG = "org914d3d16.crm.dynamics.com"
TENANT = "b16beb2c-1c93-4497-bc75-5a1cdae6ee6c"
BASE_URL = f"https://{ORG}/api/data/v9.2"

CODE_RE = re.compile(r"^([A-Z]{2,}-\d+(?:\.\w+)?)")

UNBLOCK_KEYWORDS = ["desbloqueado", "resuelto", "solucionado", "confirmó", "confirmado",
                    "entrega confirmada", "aprobado", "liberado", "listo para continuar"]


# ── Pure functions (testable without network) ─────────────────────────────────

def extract_code(subject: str | None) -> str | None:
    m = CODE_RE.match(subject or "")
    return m.group(1) if m else None


def santiago_today(reference: datetime | None = None) -> datetime:
    """Devuelve la medianoche de "hoy" en hora de Santiago (America/Santiago, ~UTC-4).

    Usar esto como referencia para 'today' evita que tareas que vencen hoy en Chile
    aparezcan como VENCIDAS por el cruce de medianoche UTC.
    """
    ref = reference or datetime.now(tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+; stdlib
        tz = ZoneInfo("America/Santiago")
    except Exception:
        # Fallback: offset fijo UTC-4 si la base IANA no está instalada (tzdata ausente)
        tz = timezone(timedelta(hours=-4))
    return ref.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)


def classify_task(end_dt: datetime, today: datetime, start_dt: datetime | None = None) -> str | None:
    if end_dt.date() < today.date():
        return "VENCIDA"
    if end_dt.date() == today.date():
        return "EN FECHA"
    if start_dt is not None and start_dt.date() <= today.date():
        return "EN CURSO"
    return None


def build_parent_index(tasks: list, extra_parents: dict | None = None) -> dict:
    idx = {t["msdyn_projecttaskid"]: t.get("msdyn_subject", "(sin titulo)") for t in tasks}
    if extra_parents:
        idx.update(extra_parents)
    return idx


def suggest_bucket(entry: dict) -> dict | None:
    """
    Detects bucket mismatches and suggests corrections.
    - VENCIDA + nota de gestión + not already Blocked → suggest Blocked
    - Blocked + nota with unblock keywords → suggest In Progress
    """
    categoria = entry.get("categoria", "")
    bucket = entry.get("bucket", "")
    tiene_nota = entry.get("tiene_nota", False)
    nota = (entry.get("nota_preview") or "").lower()

    if categoria == "VENCIDA" and tiene_nota and bucket != "Blocked":
        return {
            "target_bucket": "Blocked",
            "reason": "Tarea vencida con nota de gestión registrada — gestión existe pero fecha superada",
        }

    if bucket == "Blocked" and tiene_nota:
        if any(kw in nota for kw in UNBLOCK_KEYWORDS):
            return {
                "target_bucket": "In Progress",
                "reason": "Nota sugiere desbloqueo — considerar mover a In Progress",
            }

    return None


_STRIP_CODE_RE = re.compile(
    r"^[A-Z]{2,}-\d+(?:\.\w+)?"   # código: PORT-020 o PORT-020.1
    r"\s*[-–—:]\s*"                 # separador: -, –, —, :
)


def strip_parent_code(subject: str | None) -> str:
    """Quita el prefijo de código y separador del inicio del subject.

    Preserva guiones y em-dashes internos.
    Ejemplo: 'PORT-020 - Acompañamiento Sitrack — control' → 'Acompañamiento Sitrack — control'
    """
    if not subject:
        return ""
    return _STRIP_CODE_RE.sub("", subject, count=1)


def santiago_now() -> datetime:
    """Devuelve el datetime actual en hora de Santiago (America/Santiago, ~UTC-4), con hora."""
    now_utc = datetime.now(tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Santiago")
    except Exception:
        tz = timezone(timedelta(hours=-4))
    return now_utc.astimezone(tz)


def strip_html(text: str) -> str:
    """Removes HTML tags and decodes entities. Collapses whitespace."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = _html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def build_checklist_index(records: list) -> dict:
    """Converts checklist records to {task_id: 'N/M'} dict."""
    groups: dict[str, dict] = {}
    for r in records:
        tid = r.get("_msdyn_projecttaskid_value")
        if not tid:
            continue
        if tid not in groups:
            groups[tid] = {"total": 0, "done": 0}
        groups[tid]["total"] += 1
        if r.get("msdyn_projectchecklistcompleted"):
            groups[tid]["done"] += 1
    return {tid: f"{g['done']}/{g['total']}" for tid, g in groups.items()}


def assign_criticality_level(entry: dict, today: datetime) -> int:
    """Returns criticality level 1-7 per SDD section 3.2."""
    categoria = entry.get("categoria", "")
    bucket = entry.get("bucket", "")
    tiene_nota = entry.get("tiene_nota", False)
    end_raw = entry.get("end_dt", "")

    if categoria == "VENCIDA":
        if bucket == "Blocked":
            return 1
        return 2 if not tiene_nota else 3

    if categoria == "EN FECHA":
        return 4

    # EN CURSO — days remaining
    if end_raw:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        days_remaining = (end_dt.date() - today.date()).days
        if days_remaining <= 3:
            return 5
        if days_remaining <= 7:
            return 6
    return 7


def suggest_action(entry: dict, today: datetime, dias_umbral: int = 14) -> str:
    """Returns a management action suggestion string, or '' if none."""
    categoria = entry.get("categoria", "")
    bucket = entry.get("bucket", "")
    tiene_nota = entry.get("tiene_nota", False)
    nota = (entry.get("nota_preview") or "").lower()
    end_raw = entry.get("end_dt", "")

    # Escalation (highest priority — overrides bucket suggestions)
    if categoria == "VENCIDA" and end_raw:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        dias_vencida = (today - end_dt).days
        if dias_vencida > dias_umbral:
            if bucket == "Blocked":
                return f"Bloqueada y vencida hace {dias_vencida} días — escalar con responsable"
            if not tiene_nota:
                return f"Sin gestión registrada hace {dias_vencida} días — escalar con responsable"

    # Bucket corrections
    if categoria == "VENCIDA" and tiene_nota and bucket != "Blocked":
        return "Tarea vencida con nota de gestión — mover a Blocked"

    if bucket == "Blocked" and tiene_nota:
        if any(kw in nota for kw in UNBLOCK_KEYWORDS):
            return "Nota sugiere resolución — mover a In Progress"

    return ""


def pillar_of(parent_code: str | None) -> str:
    """Returns the pilar prefix (NORM/INTER/PORT) from a parent_code like 'NORM-001'."""
    if not parent_code:
        return "SIN_CODIGO"
    parts = parent_code.split("-", 1)
    if len(parts) < 2:
        return "SIN_CODIGO"
    return parts[0]


def analyze_task(entry: dict, today: datetime) -> str:
    """Always-non-empty deterministic analytical comment per task."""
    categoria = entry.get("categoria", "")
    tiene_nota = entry.get("tiene_nota", False)
    bucket = entry.get("bucket", "")
    progress = entry.get("progress", 0) or 0
    end_raw = entry.get("end_dt", "")

    # Use precomputed suggest_action_text when available; fall back to live computation.
    # This avoids double-computation and respects values set at report-build time.
    action = entry.get("suggest_action_text") or suggest_action(entry, today)
    parts = [action] if action else []

    days_info = ""
    if end_raw:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        if categoria == "VENCIDA":
            dias = (today - end_dt).days
            days_info = f"Vencida hace {dias} día{'s' if dias != 1 else ''}."
        else:
            dias = (end_dt.date() - today.date()).days
            days_info = f"Vence en {dias} día{'s' if dias != 1 else ''}."

    if categoria == "VENCIDA":
        if not tiene_nota:
            parts.append(
                f"{days_info} Sin nota de gestión — requiere acción urgente del responsable."
            )
        else:
            parts.append(
                f"{days_info} Tiene nota de gestión. Verificar si el bloqueo fue resuelto."
            )
    elif categoria == "EN FECHA":
        if not tiene_nota:
            parts.append(
                f"{days_info} En fecha pero sin nota de seguimiento — confirmar avance con el responsable."
            )
        else:
            parts.append(
                f"{days_info} En fecha con nota de gestión. Mantener seguimiento."
            )
    else:  # EN CURSO
        avance = f"{int(progress)}% de avance." if progress else "Sin avance registrado."
        if bucket == "Blocked":
            parts.append(
                f"{days_info} Tarea bloqueada con {avance} Gestionar desbloqueo."
            )
        elif not tiene_nota:
            parts.append(
                f"{days_info} En curso. {avance} Sin nota de gestión — verificar estado con responsable."
            )
        else:
            parts.append(
                f"{days_info} En curso. {avance} Con nota de seguimiento — monitorear cierre."
            )

    return " ".join(parts).strip()


def sort_by_criticality(rows: list, today: datetime) -> list:
    """Sorts flat list by (criticality_level, days_to_end asc)."""
    def sort_key(entry):
        level = assign_criticality_level(entry, today)
        end_raw = entry.get("end_dt", "")
        days = 0
        if end_raw:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            days = (end_dt.date() - today.date()).days
        return (level, days)
    return sorted(rows, key=sort_key)


def write_csv_pmo(rows: list, out) -> None:
    """
    Writes flat PMO CSV with 13 approved columns (SDD section 6.1).
    out: str path or file-like object (StringIO for tests).
    """
    FIELDS = ["Codigo", "TareaPadre", "Tarea", "Responsable", "Estado",
              "NivelCriticidad", "Bucket", "Nota", "Checklist",
              "FechaInicio", "FechaFin", "UltimaActualizacion", "SugerenciaAI"]
    csv_rows = [
        {
            "Codigo": h.get("parent_code", ""),
            "TareaPadre": h.get("parent_subject", ""),
            "Tarea": h.get("subject", ""),
            "Responsable": h.get("responsable", ""),
            "Estado": h.get("categoria", ""),
            "NivelCriticidad": str(h.get("nivel_criticidad", "")),
            "Bucket": h.get("bucket", ""),
            "Nota": h.get("nota", ""),
            "Checklist": h.get("checklist", ""),
            "FechaInicio": h.get("start_str", ""),
            "FechaFin": h.get("end_str", ""),
            "UltimaActualizacion": h.get("mod_str", ""),
            "SugerenciaAI": h.get("suggest_action_text", ""),
        }
        for h in rows
    ]
    f = None
    should_close = isinstance(out, str)
    try:
        f = open(out, "w", encoding="utf-8-sig", newline="") if should_close else out
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows)
    finally:
        if should_close and f is not None:
            f.close()
    if should_close:
        print(f"\n  CSV PMO exportado: {out}  ({len(csv_rows)} filas)")


# ── HTML report generator ─────────────────────────────────────────────────────

def _html_styles() -> str:
    return """
<style>
  :root {
    --corp-blue:   #1a4f8a;
    --corp-blue-d: #153d6e;
    --corp-blue-l: #e8f0fb;
    --neutral-50:  #f8f9fa;
    --neutral-100: #f1f3f5;
    --neutral-200: #e9ecef;
    --neutral-400: #adb5bd;
    --neutral-600: #6c757d;
    --neutral-800: #343a40;
    --crit-red:    #d32f2f;
    --crit-red-l:  #ffebee;
    --crit-orange: #e65100;
    --crit-orange-l: #fff3e0;
    --crit-green:  #2e7d32;
    --crit-green-l: #e8f5e9;
    --kpi-blue:    #1565c0;
    --kpi-blue-l:  #e3f2fd;
    --shadow-sm:   0 1px 3px rgba(0,0,0,.12), 0 1px 2px rgba(0,0,0,.08);
    --shadow-md:   0 3px 6px rgba(0,0,0,.10), 0 2px 4px rgba(0,0,0,.08);
    --radius:      6px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    color: var(--neutral-800);
    background: var(--neutral-100);
    padding: 24px 20px 40px;
    line-height: 1.5;
  }

  /* ── Header ── */
  .report-header {
    background: var(--corp-blue);
    color: #fff;
    padding: 20px 24px 16px;
    border-radius: var(--radius);
    margin-bottom: 20px;
    box-shadow: var(--shadow-md);
  }
  .report-header h1 { font-size: 18px; font-weight: 700; letter-spacing: .3px; }
  .report-header .subtitle {
    font-size: 12px;
    color: rgba(255,255,255,.78);
    margin-top: 4px;
  }

  /* ── KPI cards ── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 16px;
  }
  .kpi-card {
    background: #fff;
    border-radius: var(--radius);
    padding: 14px 16px;
    box-shadow: var(--shadow-sm);
    border-top: 3px solid var(--neutral-400);
  }
  .kpi-card.red   { border-top-color: var(--crit-red); }
  .kpi-card.green { border-top-color: var(--crit-green); }
  .kpi-card.blue  { border-top-color: var(--kpi-blue); }
  .kpi-card .kpi-value {
    font-size: 28px;
    font-weight: 700;
    line-height: 1;
    margin-bottom: 4px;
  }
  .kpi-card.red   .kpi-value { color: var(--crit-red); }
  .kpi-card.green .kpi-value { color: var(--crit-green); }
  .kpi-card.blue  .kpi-value { color: var(--kpi-blue); }
  .kpi-card .kpi-label { font-size: 11px; color: var(--neutral-600); text-transform: uppercase; letter-spacing: .5px; }

  /* ── Salud gestión ── */
  .salud-bar {
    background: #fff;
    border-radius: var(--radius);
    padding: 10px 16px;
    margin-bottom: 20px;
    box-shadow: var(--shadow-sm);
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }
  .salud-bar .salud-label { color: var(--neutral-600); font-weight: 600; }
  .salud-badge {
    display: inline-block;
    padding: 3px 12px;
    border-radius: 20px;
    font-weight: 700;
    font-size: 12px;
  }
  .salud-badge.buena   { background: var(--crit-green-l); color: var(--crit-green); }
  .salud-badge.regular { background: var(--crit-orange-l); color: var(--crit-orange); }
  .salud-badge.baja    { background: var(--crit-red-l); color: var(--crit-red); }

  /* ── Controls (filter/search bar) ── */
  .controls {
    display: flex;
    gap: 10px;
    margin-bottom: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .controls label { font-size: 11px; color: var(--neutral-600); font-weight: 600; }
  .controls select, .controls input {
    padding: 5px 10px;
    border: 1px solid var(--neutral-200);
    border-radius: 4px;
    font-size: 12px;
    background: #fff;
    color: var(--neutral-800);
    min-width: 140px;
  }
  .controls select:focus, .controls input:focus {
    outline: 2px solid var(--corp-blue);
    outline-offset: 1px;
  }
  .controls .clear-btn {
    padding: 5px 12px;
    background: var(--neutral-200);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    color: var(--neutral-800);
  }
  .controls .clear-btn:hover { background: var(--neutral-400); }

  .controls .export-btn {
    padding: 5px 12px;
    background: var(--corp-blue);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    color: #fff;
  }
  .controls .export-btn:hover { background: var(--corp-blue-d); }

  /* ── Riesgo editable ── */
  .riesgo-cell { min-width: 160px; }
  .riesgo-nivel {
    font-size: 11px;
    padding: 2px 4px;
    border: 1px solid var(--neutral-200);
    border-radius: 3px;
    background: #fff;
    cursor: pointer;
    width: 70px;
  }
  .riesgo-nivel[value="ALTO"] { color: var(--crit-red); font-weight: 600; }
  .riesgo-comment {
    font-size: 11px;
    padding: 2px 4px;
    border: 1px solid var(--neutral-200);
    border-radius: 3px;
    width: 100%;
    margin-top: 3px;
    background: #fff;
  }
  .riesgo-comment:focus, .riesgo-nivel:focus { outline: 2px solid var(--corp-blue-l); }

  /* ── Table ── */
  .table-wrap { overflow-x: auto; border-radius: var(--radius); box-shadow: var(--shadow-sm); }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    font-size: 12px;
  }
  thead th {
    background: var(--corp-blue);
    color: #fff;
    padding: 9px 10px;
    text-align: left;
    font-weight: 600;
    font-size: 11px;
    letter-spacing: .3px;
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: 0;
    z-index: 1;
  }
  thead th:hover { background: var(--corp-blue-d); }
  thead th .sort-arrow { margin-left: 4px; opacity: .6; font-size: 9px; }
  tbody tr { border-bottom: 1px solid var(--neutral-200); }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--corp-blue-l); }
  tbody tr.hidden-row { display: none; }
  tbody td { padding: 7px 10px; vertical-align: top; }

  /* ── Criticality level row colors ── */
  .nivel-1 td:first-child, .nivel-2 td:first-child, .nivel-3 td:first-child { border-left: 4px solid var(--crit-red); }
  .nivel-4 td:first-child { border-left: 4px solid var(--crit-orange); }
  .nivel-5 td:first-child, .nivel-6 td:first-child, .nivel-7 td:first-child { border-left: 4px solid var(--crit-green); }

  /* ── Estado badges ── */
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-weight: 600;
    font-size: 10px;
    white-space: nowrap;
  }
  .badge-vencida  { background: var(--crit-red-l);    color: var(--crit-red); }
  .badge-enfecha  { background: var(--crit-orange-l); color: var(--crit-orange); }
  .badge-encurso  { background: var(--crit-green-l);  color: var(--crit-green); }

  /* ── AI tooltip ── */
  .ai-icon {
    position: relative;
    display: inline-block;
    cursor: help;
    background: var(--corp-blue);
    color: #fff;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .5px;
    margin-left: 4px;
    white-space: nowrap;
    vertical-align: middle;
  }
  .ai-tooltip {
    visibility: hidden;
    opacity: 0;
    background: var(--neutral-800);
    color: #fff;
    font-size: 11px;
    font-weight: 400;
    border-radius: 4px;
    padding: 6px 10px;
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    width: max-content;
    max-width: 280px;
    white-space: normal;
    z-index: 10;
    box-shadow: var(--shadow-md);
    transition: opacity .15s;
    pointer-events: none;
  }
  .ai-icon:hover .ai-tooltip { visibility: visible; opacity: 1; }

  /* ── Estado + nota tooltip ── */
  .estado-wrap {
    position: relative;
    display: inline-block;
    cursor: help;
  }
  .nota-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--corp-blue);
    margin-left: 4px;
    vertical-align: middle;
  }
  .nota-tooltip {
    visibility: hidden;
    opacity: 0;
    background: var(--neutral-800);
    color: #fff;
    font-size: 11px;
    font-weight: 400;
    border-radius: 4px;
    padding: 8px 12px;
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    width: max-content;
    max-width: 320px;
    white-space: normal;
    z-index: 20;
    box-shadow: var(--shadow-md);
    transition: opacity .15s;
    pointer-events: none;
    line-height: 1.5;
  }
  .estado-wrap:hover .nota-tooltip { visibility: visible; opacity: 1; }
  .nota-tooltip.sin-nota { font-style: italic; color: var(--neutral-400); }

  /* ── Alertas ── */
  .section {
    margin-top: 24px;
    background: #fff;
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
    overflow: hidden;
  }
  .section-header {
    background: var(--corp-blue-l);
    padding: 10px 16px;
    font-weight: 700;
    font-size: 12px;
    color: var(--corp-blue);
    border-bottom: 1px solid var(--neutral-200);
    letter-spacing: .3px;
  }
  .section-body { padding: 12px 16px; }
  .alert-item {
    padding: 8px 12px;
    border-left: 3px solid var(--crit-red);
    margin-bottom: 8px;
    background: var(--crit-red-l);
    border-radius: 0 4px 4px 0;
    font-size: 12px;
  }
  .alert-item:last-child { margin-bottom: 0; }
  .alert-item .alert-title { font-weight: 600; }
  .alert-item .alert-meta { color: var(--neutral-600); font-size: 11px; margin-top: 2px; }
  .suggestion-item {
    padding: 6px 12px;
    margin-bottom: 6px;
    border-left: 3px solid var(--crit-orange);
    background: var(--crit-orange-l);
    border-radius: 0 4px 4px 0;
    font-size: 12px;
  }
  .suggestion-item:last-child { margin-bottom: 0; }
  .no-alert { color: var(--neutral-600); font-size: 12px; font-style: italic; }

  /* ── Responsable table ── */
  .resp-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .resp-table th {
    text-align: left;
    font-weight: 600;
    background: var(--neutral-50);
    color: var(--neutral-600);
    font-size: 11px;
    padding: 5px 8px;
    border-bottom: 1px solid var(--neutral-200);
  }
  .resp-table td { padding: 5px 8px; border-bottom: 1px solid var(--neutral-100); }
  .resp-table tr:last-child td { border-bottom: none; }
  .resp-alert { color: var(--crit-red); font-weight: 600; font-size: 11px; }

  /* ── Footer ── */
  .footer {
    margin-top: 24px;
    color: var(--neutral-600);
    font-size: 11px;
    text-align: center;
  }
  .footer .quality-warn { color: var(--crit-orange); margin-top: 4px; }

  /* ── Print ── */
  @media print {
    body { background: #fff; padding: 0; font-size: 11px; }
    .controls, .clear-btn { display: none !important; }
    .report-header { box-shadow: none; border-radius: 0; }
    .table-wrap { overflow: visible; box-shadow: none; }
    table { font-size: 10px; }
    thead th { background: var(--corp-blue) !important; color: #fff !important; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .nivel-1 td:first-child, .nivel-2 td:first-child, .nivel-3 td:first-child { border-left: 4px solid var(--crit-red) !important; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .nivel-4 td:first-child { border-left: 4px solid var(--crit-orange) !important; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .nivel-5 td:first-child, .nivel-6 td:first-child, .nivel-7 td:first-child { border-left: 4px solid var(--crit-green) !important; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .badge { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .ai-tooltip { display: none; }
    .section { box-shadow: none; page-break-inside: avoid; }
    .kpi-grid { grid-template-columns: repeat(4, 1fr); }
    .kpi-card { box-shadow: none; border: 1px solid var(--neutral-200); }
  }

  /* ── Responsive ── */
  @media (max-width: 900px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 480px) { .kpi-grid { grid-template-columns: 1fr; } }
</style>"""


def _html_script() -> str:
    return """
<script>
(function () {
  'use strict';

  var table = document.getElementById('main-table');
  if (!table) return;

  var tbody = table.querySelector('tbody');
  var rows  = Array.from(tbody.querySelectorAll('tr'));

  /* ── Filter dropdowns ── */
  var selEstado  = document.getElementById('filter-estado');
  var selPilar   = document.getElementById('filter-pilar');
  var selBucket  = document.getElementById('filter-bucket');
  var selResp    = document.getElementById('filter-resp');
  var inpText    = document.getElementById('filter-text');

  function applyFilters() {
    var estado  = selEstado  ? selEstado.value  : '';
    var pilar   = selPilar   ? selPilar.value   : '';
    var bucket  = selBucket  ? selBucket.value  : '';
    var resp    = selResp    ? selResp.value    : '';
    var text    = inpText    ? inpText.value.toLowerCase() : '';
    rows.forEach(function (tr) {
      var cells     = tr.querySelectorAll('td');
      var rowEstado = cells[4] ? cells[4].textContent.trim() : '';
      var rowBucket = cells[5] ? cells[5].textContent.trim() : '';
      var rowResp   = cells[3] ? cells[3].textContent.trim() : '';
      var rowPilar  = tr.dataset.pilar || '';
      var rowText   = tr.textContent.toLowerCase();
      var show = true;
      if (estado  && rowEstado.indexOf(estado)   === -1) show = false;
      if (pilar   && rowPilar  !== pilar)                show = false;
      if (bucket  && rowBucket !== bucket)               show = false;
      if (resp    && rowResp   !== resp)                 show = false;
      if (text    && rowText.indexOf(text)        === -1) show = false;
      tr.classList.toggle('hidden-row', !show);
    });
  }

  if (selEstado)  selEstado.addEventListener('change',  applyFilters);
  if (selPilar)   selPilar.addEventListener('change',   applyFilters);
  if (selBucket)  selBucket.addEventListener('change',  applyFilters);
  if (selResp)    selResp.addEventListener('change',    applyFilters);
  if (inpText)    inpText.addEventListener('input',     applyFilters);

  var clearBtn = document.getElementById('clear-filters');
  if (clearBtn) clearBtn.addEventListener('click', function () {
    if (selEstado)  selEstado.value  = '';
    if (selPilar)   selPilar.value   = '';
    if (selBucket)  selBucket.value  = '';
    if (selResp)    selResp.value    = '';
    if (inpText)    inpText.value    = '';
    applyFilters();
  });

  /* ── Riesgo: localStorage save/restore ── */
  var LS_PREFIX = 'pmo_riesgo_';

  function saveRiesgo(taskid, nivel, comment) {
    try {
      localStorage.setItem(LS_PREFIX + taskid, JSON.stringify({ nivel: nivel, comment: comment }));
    } catch (e) {}
  }

  function restoreRiesgo() {
    document.querySelectorAll('.riesgo-nivel').forEach(function (sel) {
      var tid = sel.dataset.taskid;
      try {
        var raw = localStorage.getItem(LS_PREFIX + tid);
        if (raw) {
          var data = JSON.parse(raw);
          sel.value = data.nivel || '';
          var inp = document.querySelector('.riesgo-comment[data-taskid="' + tid + '"]');
          if (inp) inp.value = data.comment || '';
        }
      } catch (e) {}
    });
  }

  document.querySelectorAll('.riesgo-nivel').forEach(function (sel) {
    sel.addEventListener('change', function () {
      var tid = sel.dataset.taskid;
      var inp = document.querySelector('.riesgo-comment[data-taskid="' + tid + '"]');
      saveRiesgo(tid, sel.value, inp ? inp.value : '');
    });
  });

  document.querySelectorAll('.riesgo-comment').forEach(function (inp) {
    inp.addEventListener('input', function () {
      var tid = inp.dataset.taskid;
      var sel = document.querySelector('.riesgo-nivel[data-taskid="' + tid + '"]');
      saveRiesgo(tid, sel ? sel.value : '', inp.value);
    });
  });

  restoreRiesgo();

  /* ── Exportar CSV (incluye columna Riesgo desde localStorage) ── */
  var exportBtn = document.getElementById('export-csv');
  if (exportBtn) exportBtn.addEventListener('click', function () {
    var headers = ['Codigo','Tarea padre','Tarea','Responsable','Estado',
                   'Bucket','Checklist','F.inicio','F.fin','Ult. act.','Riesgo nivel','Riesgo comentario'];
    var csvRows = [headers.join(',')];
    rows.forEach(function (tr) {
      if (tr.classList.contains('hidden-row')) return;
      var cells = tr.querySelectorAll('td');
      var tid   = tr.dataset.taskid || '';
      var nivel = '', comment = '';
      try {
        var raw = localStorage.getItem(LS_PREFIX + tid);
        if (raw) { var d = JSON.parse(raw); nivel = d.nivel || ''; comment = d.comment || ''; }
      } catch (e) {}
      var row = [];
      for (var i = 0; i < 10; i++) {
        var txt = cells[i] ? cells[i].textContent.replace(/[\\r\\n]+/g, ' ').trim() : '';
        row.push('"' + txt.replace(/"/g, '""') + '"');
      }
      row.push('"' + nivel + '"');
      row.push('"' + comment.replace(/"/g, '""') + '"');
      csvRows.push(row.join(','));
    });
    var blob = new Blob([csvRows.join('\\r\\n')], { type: 'text/csv;charset=utf-8;' });
    var url  = URL.createObjectURL(blob);
    var a    = document.createElement('a');
    a.href = url; a.download = 'reporte_pmo_riesgo.csv'; a.click();
    URL.revokeObjectURL(url);
  });

  /* ── Column sort ── */
  var sortState = { col: -1, asc: true };
  var headers   = table.querySelectorAll('thead th');

  headers.forEach(function (th, idx) {
    th.addEventListener('click', function () {
      var arrow = th.querySelector('.sort-arrow');
      headers.forEach(function (h) {
        var a = h.querySelector('.sort-arrow');
        if (a) a.textContent = '↕';
      });
      if (sortState.col === idx) {
        sortState.asc = !sortState.asc;
      } else {
        sortState.col = idx;
        sortState.asc = true;
      }
      if (arrow) arrow.textContent = sortState.asc ? '↑' : '↓';

      var sorted = rows.slice().sort(function (a, b) {
        var ca = a.querySelectorAll('td')[idx];
        var cb = b.querySelectorAll('td')[idx];
        var ta = ca ? ca.textContent.trim() : '';
        var tb = cb ? cb.textContent.trim() : '';
        /* Numeric sort for columns with pure numbers (criticality, etc.) */
        var na = parseFloat(ta), nb = parseFloat(tb);
        var cmp = (!isNaN(na) && !isNaN(nb)) ? (na - nb) : ta.localeCompare(tb, 'es');
        return sortState.asc ? cmp : -cmp;
      });
      sorted.forEach(function (tr) { tbody.appendChild(tr); });
    });
  });
})();
</script>"""


def _html_kpis(rows: list, today: datetime) -> str:
    total = len(rows)
    vencidas = sum(1 for r in rows if r.get("categoria") == "VENCIDA")
    en_curso  = sum(1 for r in rows if r.get("categoria") == "EN CURSO")
    con_nota  = sum(1 for r in rows if r.get("tiene_nota"))
    pct_nota  = (con_nota / total * 100) if total else 0
    if pct_nota >= 80:
        salud, salud_cls = "Buena", "buena"
    elif pct_nota >= 50:
        salud, salud_cls = "Regular", "regular"
    else:
        salud, salud_cls = "Baja", "baja"

    return f"""
<div class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-value">{total}</div>
    <div class="kpi-label">Total accionables</div>
  </div>
  <div class="kpi-card red">
    <div class="kpi-value">{vencidas}</div>
    <div class="kpi-label">Vencidas</div>
  </div>
  <div class="kpi-card green">
    <div class="kpi-value">{en_curso}</div>
    <div class="kpi-label">En curso</div>
  </div>
  <div class="kpi-card blue">
    <div class="kpi-value">{con_nota}</div>
    <div class="kpi-label">Con nota de gestión</div>
  </div>
</div>
<div class="salud-bar">
  <span class="salud-label">Salud Gestión:</span>
  <span class="salud-badge {salud_cls}">{salud}</span>
  <span style="color:var(--neutral-600)">({con_nota}/{total} tareas con nota — {pct_nota:.0f}%)</span>
</div>"""


def _html_controls(rows: list) -> str:
    estados  = sorted({r.get("categoria", "") for r in rows if r.get("categoria")})
    resps    = sorted({r.get("responsable", "") for r in rows if r.get("responsable")})
    pilares  = sorted({pillar_of(r.get("parent_code", "")) for r in rows})
    buckets  = sorted({r.get("bucket", "") for r in rows if r.get("bucket")})
    opts_estado  = "".join(f'<option value="{_html.escape(e)}">{_html.escape(e)}</option>' for e in estados)
    opts_resp    = "".join(f'<option value="{_html.escape(r)}">{_html.escape(r)}</option>' for r in resps)
    opts_pilar   = "".join(f'<option value="{_html.escape(p)}">{_html.escape(p)}</option>' for p in pilares)
    opts_bucket  = "".join(f'<option value="{_html.escape(b)}">{_html.escape(b)}</option>' for b in buckets)
    return f"""
<div class="controls">
  <label for="filter-estado">Estado:</label>
  <select id="filter-estado"><option value="">Todos</option>{opts_estado}</select>
  <label for="filter-pilar">Pilar:</label>
  <select id="filter-pilar"><option value="">Todos</option>{opts_pilar}</select>
  <label for="filter-bucket">Bucket:</label>
  <select id="filter-bucket"><option value="">Todos</option>{opts_bucket}</select>
  <label for="filter-resp">Responsable:</label>
  <select id="filter-resp"><option value="">Todos</option>{opts_resp}</select>
  <label for="filter-text">Buscar:</label>
  <input id="filter-text" type="text" placeholder="texto libre…" />
  <button class="clear-btn" id="clear-filters">Limpiar</button>
  <button class="export-btn" id="export-csv">Exportar CSV</button>
</div>"""


def _badge(categoria: str) -> str:
    mapping = {
        "VENCIDA":  ("badge badge-vencida", "VENCIDA"),
        "EN FECHA": ("badge badge-enfecha", "EN FECHA"),
        "EN CURSO": ("badge badge-encurso", "EN CURSO"),
    }
    cls, label = mapping.get(categoria, ("badge", categoria))
    return f'<span class="{cls}">{_html.escape(label)}</span>'


def _html_table(rows: list, today: datetime) -> str:
    headers = ["Código", "Tarea padre", "Tarea", "Responsable", "Estado",
               "Bucket", "Checklist", "F.inicio", "F.fin", "Últ. act.", "Riesgo"]
    ths = "".join(f'<th>{h}<span class="sort-arrow">↕</span></th>' for h in headers)

    trs = []
    for r in rows:
        nivel   = r.get("nivel_criticidad", 7)
        pilar   = pillar_of(r.get("parent_code", ""))
        nota    = r.get("nota", "")
        nota_escaped = _html.escape(nota) if nota else ""
        if nota:
            nota_tooltip_cls = "nota-tooltip"
            dot_html = '<span class="nota-dot"></span>'
        else:
            nota_tooltip_cls = "nota-tooltip sin-nota"
            dot_html = ""
        nota_fallback = nota_escaped if nota else "Sin nota de gestión"
        estado_cell = (
            f'<span class="estado-wrap">'
            f'{_badge(r.get("categoria", ""))}{dot_html}'
            f'<span class="{nota_tooltip_cls}">{nota_fallback}</span>'
            f'</span>'
        )
        analysis = analyze_task(r, today)
        ai_html = (
            f'<span class="ai-icon">AI'
            f'<span class="ai-tooltip">{_html.escape(analysis)}</span>'
            f'</span>'
        )
        task_id = _html.escape(r.get("task_id", ""))
        riesgo_cell = (
            f'<td class="riesgo-cell">'
            f'<select class="riesgo-nivel" data-taskid="{task_id}">'
            f'<option value=""></option>'
            f'<option value="ALTO">ALTO</option>'
            f'<option value="MEDIO">MEDIO</option>'
            f'<option value="BAJO">BAJO</option>'
            f'</select>'
            f'<input class="riesgo-comment" type="text" data-taskid="{task_id}" '
            f'placeholder="Comentario…" />'
            f'</td>'
        )
        trs.append(
            f'<tr class="nivel-{nivel}" data-pilar="{_html.escape(pilar)}" data-taskid="{task_id}">'
            f'<td>{_html.escape(r.get("parent_code", ""))}</td>'
            f'<td>{_html.escape(r.get("parent_subject", ""))}</td>'
            f'<td>{_html.escape(r.get("subject", ""))}{ai_html}</td>'
            f'<td>{_html.escape(r.get("responsable", ""))}</td>'
            f'<td>{estado_cell}</td>'
            f'<td>{_html.escape(r.get("bucket", ""))}</td>'
            f'<td>{_html.escape(r.get("checklist", ""))}</td>'
            f'<td>{_html.escape(r.get("start_str", ""))}</td>'
            f'<td>{_html.escape(r.get("end_str", ""))}</td>'
            f'<td>{_html.escape(r.get("mod_str", ""))}</td>'
            f'{riesgo_cell}'
            f'</tr>'
        )

    return f"""
<div class="table-wrap">
<table id="main-table">
  <thead><tr>{ths}</tr></thead>
  <tbody>{''.join(trs)}</tbody>
</table>
</div>"""


def _html_alerts(rows: list, today: datetime) -> str:
    # Escalamiento crítico
    esc_items = []
    for r in rows:
        e = needs_escalation(r, today)
        if e:
            end_dt = datetime.fromisoformat(r["end_dt"].replace("Z", "+00:00"))
            dias = (today - end_dt).days
            esc_items.append(
                f'<div class="alert-item">'
                f'<div class="alert-title">'
                f'<span style="color:var(--neutral-600);font-size:10px;">'
                f'{_html.escape(r.get("parent_code", ""))} — {_html.escape(r.get("parent_subject", ""))}'
                f'</span><br>'
                f'{_html.escape(r.get("subject", ""))}'
                f'</div>'
                f'<div class="alert-meta">'
                f'Responsable: {_html.escape(r.get("responsable", ""))} &nbsp;|&nbsp; '
                f'Vencida hace {dias} días &nbsp;|&nbsp; {_html.escape(e["reason"])}'
                f'</div></div>'
            )
    esc_html = "".join(esc_items) if esc_items else '<p class="no-alert">Sin tareas bloqueadas-vencidas críticas.</p>'

    # Sugerencias de bucket
    sug_items = []
    for r in rows:
        s = suggest_bucket(r)
        if s:
            sug_items.append(
                f'<div class="suggestion-item">'
                f'<span style="color:var(--neutral-600);font-size:10px;">'
                f'{_html.escape(r.get("parent_code", ""))} — {_html.escape(r.get("parent_subject", ""))}'
                f'</span><br>'
                f'<strong>{_html.escape(r.get("subject", ""))}</strong><br>'
                f'{_html.escape(r.get("bucket", ""))} → {_html.escape(s["target_bucket"])} &nbsp;|&nbsp; '
                f'{_html.escape(s["reason"])}'
                f'</div>'
            )
    sug_html = "".join(sug_items) if sug_items else '<p class="no-alert">Sin sugerencias — estado coherente.</p>'

    # Carga por responsable
    resp_count: dict[str, int] = {}
    resp_venc:  dict[str, int] = {}
    for r in rows:
        resp = r.get("responsable", "(sin responsable)")
        resp_count[resp] = resp_count.get(resp, 0) + 1
        if r.get("categoria") == "VENCIDA":
            resp_venc[resp] = resp_venc.get(resp, 0) + 1

    resp_rows = []
    for resp, cnt in sorted(resp_count.items(), key=lambda x: -x[1]):
        v = resp_venc.get(resp, 0)
        venc_cell = f'<td class="resp-alert">⚠ {v}</td>' if v else '<td>—</td>'
        resp_rows.append(
            f'<tr><td>{_html.escape(resp)}</td><td>{cnt}</td>{venc_cell}</tr>'
        )

    return f"""
<div class="section">
  <div class="section-header">⚠ Escalamiento crítico — bloqueadas vencidas &gt;14 días</div>
  <div class="section-body">{esc_html}</div>
</div>
<div class="section">
  <div class="section-header">🔄 Cambios de bucket sugeridos</div>
  <div class="section-body">{sug_html}</div>
</div>
<div class="section">
  <div class="section-header">👤 Carga por responsable</div>
  <div class="section-body">
    <table class="resp-table">
      <thead><tr><th>Responsable</th><th>Tareas accionables</th><th>Vencidas</th></tr></thead>
      <tbody>{''.join(resp_rows)}</tbody>
    </table>
  </div>
</div>"""


def generate_html_report(rows_sorted: list, today: datetime,
                         plan_label: str, sin_titulo: int = 0) -> str:
    """
    Genera HTML autocontenido (CSS + JS inline) del reporte PMO.

    rows_sorted ya debe estar enriquecido con nivel_criticidad, suggest_action_text
    y checklist (ver main() líneas de enriquecimiento).

    Secciones:
      1. KPI cards + Salud Gestión
      2. Controles de filtro/orden (JS vanilla)
      3. Tabla priorizada por criticidad
      4. Alertas: escalamiento, sugerencias de bucket, carga por responsable
      5. Footer con calidad de datos
    """
    date_str = today.strftime("%d-%m-%Y")
    now_str = santiago_now().strftime("%d-%m-%Y %H:%M")

    # Footer de calidad de datos
    quality_parts = []
    if sin_titulo:
        quality_parts.append(
            f'<div class="quality-warn">⚠ {sin_titulo} tarea(s) sin título excluidas del reporte (datos incompletos en Planner).</div>'
        )
    vencidas_sin_nota = sum(1 for r in rows_sorted if r.get("categoria") == "VENCIDA" and not r.get("tiene_nota"))
    if vencidas_sin_nota:
        quality_parts.append(
            f'<div class="quality-warn">⚠ {vencidas_sin_nota} tarea(s) vencida(s) SIN nota de gestión — riesgo alto.</div>'
        )
    if not quality_parts:
        quality_parts.append('<div>✓ Sin problemas de calidad detectados.</div>')
    quality_html = "".join(quality_parts)

    return (
        f'<!DOCTYPE html>\n'
        f'<html lang="es">\n'
        f'<head>\n'
        f'<meta charset="UTF-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>Reporte PMO — {_html.escape(plan_label)} — {date_str}</title>\n'
        f'{_html_styles()}\n'
        f'</head>\n'
        f'<body>\n'
        f'<div class="report-header">\n'
        f'  <h1>Dashboard gestión PMO — {_html.escape(plan_label)}</h1>\n'
        f'  <div class="subtitle">Última actualización: {now_str} &nbsp;|&nbsp; '
        f'Tareas accionables (vencidas, en fecha, en curso) — bucket ≠ Done</div>\n'
        f'</div>\n'
        f'{_html_kpis(rows_sorted, today)}\n'
        f'{_html_controls(rows_sorted)}\n'
        f'{_html_table(rows_sorted, today)}\n'
        f'{_html_alerts(rows_sorted, today)}\n'
        f'<div class="footer">\n'
        f'  Generado el {date_str} &nbsp;|&nbsp; Plan Lineamiento Estratégico 2026\n'
        f'  {quality_html}\n'
        f'</div>\n'
        f'{_html_script()}\n'
        f'</body>\n'
        f'</html>'
    )


def needs_escalation(entry: dict, today: datetime, dias_umbral: int = 14) -> dict | None:
    """VENCIDA + Blocked + overdue > dias_umbral → escalar."""
    if entry.get("categoria") != "VENCIDA":
        return None
    if entry.get("bucket") != "Blocked":
        return None
    end_raw = entry.get("end_dt", "")
    if not end_raw:
        return None
    end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    dias_vencida = (today - end_dt).days
    if dias_vencida <= dias_umbral:
        return None
    return {"reason": f"Bloqueada y vencida hace {dias_vencida} días — escalar"}


# ── Network layer ─────────────────────────────────────────────────────────────

def get_token() -> str:
    import shutil
    az_cmd = shutil.which("az") or r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    result = subprocess.run(
        [az_cmd, "account", "get-access-token",
         "--resource", f"https://{ORG}",
         "--tenant", TENANT,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(
            "ERROR: No se pudo obtener token de Azure.\n"
            "Ejecutá: az login --use-device-code --tenant " + TENANT
        )
    return result.stdout.strip()


def odata_get(token: str, url: str) -> dict:
    # urllib.request rejects URLs with literal spaces — encode them
    url = url.replace(" ", "%20")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Prefer": 'odata.include-annotations="*"',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_pages(token: str, url: str) -> list:
    records = []
    next_url = url
    while next_url:
        data = odata_get(token, next_url)
        records.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
    return records


def resolve_project_name(token: str, project_id: str) -> str:
    """Resuelve el nombre del proyecto dado su GUID. Fallback al ID truncado si falla."""
    try:
        url = f"{BASE_URL}/msdyn_projects({project_id})?$select=msdyn_subject"
        data = odata_get(token, url)
        name = (data.get("msdyn_subject") or "").strip()
        if name:
            print(f"  Nombre del proyecto: {name}")
            return name
    except Exception:
        pass
    return f"Proyecto {project_id[:8]}..."


def resolve_project_id(token: str, plan_name: str) -> str:
    encoded = urllib.parse.quote(plan_name)
    url = f"{BASE_URL}/msdyn_projects?$filter=contains(msdyn_subject,'{encoded}')&$select=msdyn_projectid,msdyn_subject&$top=20"
    results = fetch_all_pages(token, url)
    if not results:
        sys.exit(f"ERROR: No se encontró ningún proyecto que contenga '{plan_name}'")
    if len(results) == 1:
        proj = results[0]
        print(f"  Proyecto: {proj['msdyn_subject']} ({proj['msdyn_projectid']})")
        return proj["msdyn_projectid"]
    print(f"  Se encontraron {len(results)} proyectos con '{plan_name}':")
    for i, p in enumerate(results):
        print(f"    [{i}] {p['msdyn_subject']} — {p['msdyn_projectid']}")
    sys.exit("Especificá el GUID exacto con --project-id para desambiguar.")


def resolve_buckets(token: str, project_id: str) -> tuple[dict, str | None]:
    """Returns ({bucket_id: name}, done_bucket_id)."""
    url = (f"{BASE_URL}/msdyn_projectbuckets"
           f"?$filter=_msdyn_project_value eq {project_id}"
           f"&$select=msdyn_projectbucketid,msdyn_name&$top=50")
    results = fetch_all_pages(token, url)
    buckets = {r["msdyn_projectbucketid"]: r["msdyn_name"] for r in results}
    done_id = next(
        (bid for bid, name in buckets.items() if name.strip().lower() == "done"), None
    )
    return buckets, done_id


def fetch_tasks(token: str, project_id: str) -> list:
    fields = ",".join([
        "msdyn_projecttaskid", "msdyn_subject", "msdyn_progress",
        "msdyn_scheduledstart", "msdyn_scheduledend", "modifiedon",
        "msdyn_descriptionplaintext", "msdyn_description",
        "_msdyn_parenttask_value", "_msdyn_projectbucket_value", "statecode",
        "msdyn_summary", "msdyn_outlinelevel",
    ])
    url = (f"{BASE_URL}/msdyn_projecttasks"
           f"?$filter=_msdyn_project_value eq {project_id}"
           f"&$select={fields}"
           f"&$expand=msdyn_pfwmodifiedby($select=fullname)"
           f"&$top=500")
    return fetch_all_pages(token, url)


def fetch_checklists(token: str, task_ids: list) -> list:
    """Fetches msdyn_projectchecklists for the given task_ids (batched)."""
    if not task_ids:
        return []
    BATCH = 30
    all_records = []
    for i in range(0, len(task_ids), BATCH):
        batch = task_ids[i:i + BATCH]
        filter_parts = " or ".join(
            f"_msdyn_projecttaskid_value eq {tid}" for tid in batch
        )
        url = (f"{BASE_URL}/msdyn_projectchecklists"
               f"?$filter={urllib.parse.quote(filter_parts)}"
               f"&$select=msdyn_projectchecklistid,_msdyn_projecttaskid_value"
               f",msdyn_name,msdyn_projectchecklistcompleted,msdyn_projectchecklistorder"
               f"&$orderby=msdyn_projectchecklistorder asc&$top=500")
        all_records.extend(fetch_all_pages(token, url))
    return all_records


def resolve_cross_project_parents(token: str, missing_ids: list[str]) -> dict:
    """Batch-queries parent tasks that live in other project layers."""
    if not missing_ids:
        return {}
    filter_clause = " or ".join(f"msdyn_projecttaskid eq {pid}" for pid in missing_ids)
    url = (f"{BASE_URL}/msdyn_projecttasks"
           f"?$filter={urllib.parse.quote(filter_clause)}"
           f"&$select=msdyn_projecttaskid,msdyn_subject&$top=50")
    results = fetch_all_pages(token, url)
    return {r["msdyn_projecttaskid"]: r.get("msdyn_subject", "(sin titulo)") for r in results}


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(tasks: list, buckets: dict, done_id: str | None,
                 parent_index: dict, today: datetime) -> tuple[list, int]:
    """Returns (rows, sin_titulo_count). Excludes tasks with empty/whitespace subject."""
    rows = []
    sin_titulo = 0
    for t in tasks:
        subject = (t.get("msdyn_subject") or "").strip()
        if not subject or subject.lower() == "tarea sin título":
            sin_titulo += 1
            continue

        if t.get("statecode", 0) != 0:
            continue

        parent_id = t.get("_msdyn_parenttask_value")
        if not parent_id:
            continue
        if (t.get("msdyn_progress") or 0.0) >= 1.0:
            continue
        bucket_id = t.get("_msdyn_projectbucket_value")
        if done_id and bucket_id == done_id:
            continue
        if buckets.get(bucket_id, "").strip().lower() == "backlog":
            continue
        end_raw = t.get("msdyn_scheduledend")
        if not end_raw:
            continue
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        start_raw = t.get("msdyn_scheduledstart")
        start_dt = (datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    if start_raw else None)
        categoria = classify_task(end_dt, today, start_dt=start_dt)
        if categoria is None:
            continue

        pfw = t.get("msdyn_pfwmodifiedby") or {}
        responsable = pfw.get("fullname") or "(sin responsable)"

        mod_raw = t.get("modifiedon", "")
        mod_str = (datetime.fromisoformat(mod_raw.replace("Z", "+00:00")).strftime("%d-%m-%Y")
                   if mod_raw else "?")

        nota_raw = strip_html(
            t.get("msdyn_descriptionplaintext") or t.get("msdyn_description") or ""
        )
        tiene_nota = bool(nota_raw)
        nota_preview = (nota_raw[:80] + "...") if len(nota_raw) > 80 else nota_raw

        parent_subject_raw = parent_index.get(parent_id, "(padre desconocido)")
        parent_code = extract_code(parent_subject_raw) or parent_subject_raw[:30]
        parent_subject = strip_parent_code(parent_subject_raw)
        bucket_name = buckets.get(bucket_id, f"Bucket-{(bucket_id or '')[:8]}")
        start_str = start_dt.strftime("%d-%m-%Y") if start_dt else ""

        entry = {
            "task_id": t["msdyn_projecttaskid"],
            "parent_code": parent_code,
            "parent_subject": parent_subject,
            "subject": subject,
            "responsable": responsable,
            "bucket": bucket_name,
            "mod_str": mod_str,
            "mod_dt": mod_raw or "9999",
            "nota": nota_raw,
            "nota_preview": nota_preview,
            "tiene_nota": tiene_nota,
            "categoria": categoria,
            "end_dt": end_raw,
            "end_str": end_dt.strftime("%d-%m-%Y"),
            "start_str": start_str,
            "progress": t.get("msdyn_progress") or 0.0,
        }
        rows.append(entry)
    return rows, sin_titulo


def group_and_sort(rows: list) -> list:
    grouped: dict[str, dict] = {}
    for entry in rows:
        pc = entry["parent_code"]
        if pc not in grouped:
            grouped[pc] = {"parent_subject": entry["parent_subject"], "hijas": []}
        grouped[pc]["hijas"].append(entry)

    for pc in grouped:
        grouped[pc]["hijas"].sort(key=lambda x: x["subject"])

    return sorted(grouped.items(), key=lambda item: item[0])


# ── Output ────────────────────────────────────────────────────────────────────

def print_report(padres_sorted: list, today: datetime, plan_label: str, sin_titulo: int = 0):
    W = 140
    total_v = sum(sum(1 for h in d["hijas"] if h["categoria"] == "VENCIDA") for _, d in padres_sorted)
    total_ef = sum(sum(1 for h in d["hijas"] if h["categoria"] == "EN FECHA") for _, d in padres_sorted)
    total_ec = sum(sum(1 for h in d["hijas"] if h["categoria"] == "EN CURSO") for _, d in padres_sorted)
    total_cn = sum(sum(1 for h in d["hijas"] if h["tiene_nota"]) for _, d in padres_sorted)
    total = total_v + total_ef + total_ec

    print(f"\n{'='*W}")
    print(f"  REPORTE DE GESTIÓN — {plan_label}")
    print("  Hijas accionables (vencidas, en fecha o en curso), bucket != Done")
    print(f"  Fecha referencia: {today.strftime('%d-%m-%Y')}")
    print(f"{'='*W}")

    for parent_code, data in padres_sorted:
        hijas = data["hijas"]
        v = sum(1 for h in hijas if h["categoria"] == "VENCIDA")
        ef = sum(1 for h in hijas if h["categoria"] == "EN FECHA")
        ec = sum(1 for h in hijas if h["categoria"] == "EN CURSO")
        cn = sum(1 for h in hijas if h["tiene_nota"])
        marker = " [!]" if v > 0 else ""
        print(f"\n{'-'*W}")
        print(f"  PADRE: {parent_code}{marker}  ({data['parent_subject'][:80]})")
        print(f"  Hijas: {len(hijas)}  |  Vencidas: {v}  |  En fecha: {ef}  |  En curso: {ec}  |  Con nota: {cn}")
        print(f"{'-'*W}")
        print(f"  {'Tarea':<55} {'Responsable':<30} {'Bucket':<14} {'Fecha fin':<12} {'Ult.Act.':<12} {'Estado':<10}")
        print(f"  {'-'*55} {'-'*30} {'-'*14} {'-'*12} {'-'*12} {'-'*10}")
        for h in hijas:
            flag = "[!]" if h["categoria"] == "VENCIDA" else "   "
            print(f"{flag} {h['subject'][:55]:<55} {h['responsable'][:30]:<30} "
                  f"{h['bucket'][:13]:<14} {h['end_str']:<12} {h['mod_str']:<12} {h['categoria']:<10}")
            if h["tiene_nota"]:
                print(f"     ↳ {h['nota']}")

    print(f"\n{'='*W}")
    print("  RESUMEN EJECUTIVO")
    print(f"{'='*W}")
    print(f"  Grupos padre con hijas accionables : {len(padres_sorted)}")
    print(f"  Total hijas accionables            : {total}")
    print(f"  Vencidas                           : {total_v}")
    print(f"  En fecha hoy                       : {total_ef}")
    print(f"  En curso (inicio pasó, fin futuro) : {total_ec}")
    if total:
        print(f"  Con nota de gestión                : {total_cn}  ({total_cn/total*100:.0f}%)")
        print(f"  Sin nota                           : {total-total_cn}  ({(total-total_cn)/total*100:.0f}%)")
    print(f"{'='*W}")

    _print_analysis(padres_sorted, today, sin_titulo)


def _print_analysis(padres_sorted: list, today: datetime, sin_titulo: int = 0):
    W = 140
    print(f"\n{'='*W}")
    print("  RECOMENDACIONES DE GESTIÓN")
    print(f"{'='*W}")

    # Escalamiento — bloqueadas vencidas críticas
    escalaciones = []
    for _, data in padres_sorted:
        for h in data["hijas"]:
            e = needs_escalation(h, today)
            if e:
                escalaciones.append((h["subject"][:60], h["responsable"], e["reason"]))

    if escalaciones:
        print("\n  [Escalamiento — Bloqueadas vencidas críticas]")
        for subj, resp, reason in escalaciones:
            print(f"  ⚠ {subj}")
            print(f"    Responsable: {resp}  |  {reason}")
    else:
        print("\n  [Escalamiento] Sin tareas bloqueadas-vencidas críticas.")

    # Sugerencias de bucket
    bucket_suggestions = []
    for _, data in padres_sorted:
        for h in data["hijas"]:
            s = suggest_bucket(h)
            if s:
                bucket_suggestions.append((h["subject"][:55], h["bucket"], s["target_bucket"], s["reason"]))

    if bucket_suggestions:
        print("\n  [Cambios de bucket sugeridos]")
        for subj, from_b, to_b, reason in bucket_suggestions:
            print(f"  • {subj}")
            print(f"    {from_b} → {to_b}  |  {reason}")
    else:
        print("\n  [Cambios de bucket] Sin sugerencias — estado coherente.")

    # Riesgo por responsable
    resp_count: dict[str, int] = {}
    resp_vencidas: dict[str, int] = {}
    for _, data in padres_sorted:
        for h in data["hijas"]:
            r = h["responsable"]
            resp_count[r] = resp_count.get(r, 0) + 1
            if h["categoria"] == "VENCIDA":
                resp_vencidas[r] = resp_vencidas.get(r, 0) + 1

    print("\n  [Carga por responsable]")
    for r, cnt in sorted(resp_count.items(), key=lambda x: -x[1]):
        v = resp_vencidas.get(r, 0)
        alert = " ⚠ VENCIDAS" if v > 0 else ""
        print(f"  • {r[:35]:<35} {cnt:>3} tareas accionables  |  {v} vencidas{alert}")

    # Calidad de datos
    sin_nota_vencidas = sum(
        1 for _, d in padres_sorted
        for h in d["hijas"] if h["categoria"] == "VENCIDA" and not h["tiene_nota"]
    )
    print("\n  [Calidad de datos]")
    if sin_titulo:
        print(f"  ⚠ {sin_titulo} tarea(s) sin título encontradas — excluidas del reporte (datos incompletos en Planner).")
    if sin_nota_vencidas:
        print(f"  ⚠ {sin_nota_vencidas} tarea(s) vencida(s) SIN nota de gestión — riesgo alto, sin visibilidad de bloqueo.")
    if not sin_titulo and not sin_nota_vencidas:
        print("  ✓ Sin problemas de calidad detectados.")
    elif not sin_nota_vencidas:
        print("  ✓ Todas las tareas vencidas tienen nota de gestión registrada.")

    print(f"{'='*W}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    # Windows UTF-8 fix: must be inside main() to avoid breaking pytest's stdout capture
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Reporte de gestión de plan — Dataverse")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--plan", help="Nombre parcial del plan (busca en msdyn_projects)")
    source_group.add_argument("--project-id", help="GUID exacto del proyecto")
    source_group.add_argument("--from-cache", metavar="FILE",
                              help="Cargar datos desde caché JSON (sin consultar Dataverse)")
    parser.add_argument("--cache-file", metavar="FILE",
                        help="Guardar datos crudos en caché JSON tras la extracción")
    parser.add_argument("--today", help="Fecha de referencia YYYY-MM-DD (default: hoy)")
    parser.add_argument("--out", help="Ruta CSV de salida (opcional)")
    parser.add_argument("--html", metavar="FILE", help="Ruta HTML de salida (opcional)")
    args = parser.parse_args()

    if not args.from_cache and not args.plan and not args.project_id:
        parser.error("Se requiere --plan, --project-id, o --from-cache")

    today_dt = (
        datetime.strptime(args.today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.today
        else santiago_today()
    )

    if args.from_cache:
        print(f"  Cargando datos desde caché: {args.from_cache}")
        with open(args.from_cache, encoding="utf-8") as f:
            cache = json.load(f)
        project_id = cache["project_id"]
        plan_label = cache["plan_label"]
        buckets = cache["buckets"]
        done_id = cache.get("done_id")
        tasks = cache["tasks"]
        parent_index = build_parent_index(tasks)
        all_parent_ids = {t["_msdyn_parenttask_value"] for t in tasks if t.get("_msdyn_parenttask_value")}
        missing_in_cache = [pid for pid in all_parent_ids if pid not in parent_index]
        if missing_in_cache and "extra_parents" in cache:
            parent_index.update(cache["extra_parents"])
        checklist_records = cache.get("checklists", [])
        print(f"  {len(tasks)} tareas cargadas desde caché.")
        print(f"  {len(checklist_records)} ítems de checklist cargados desde caché.")
    else:
        print("  Obteniendo token Azure...")
        token = get_token()

        if args.project_id:
            project_id = args.project_id
            plan_label = resolve_project_name(token, project_id)
        else:
            print(f"  Buscando plan '{args.plan}'...")
            project_id = resolve_project_id(token, args.plan)
            plan_label = args.plan

        print("  Resolviendo buckets...")
        buckets, done_id = resolve_buckets(token, project_id)
        print(f"  Buckets encontrados: {list(buckets.values())}  |  Done id: {done_id}")

        print("  Descargando tareas...")
        tasks = fetch_tasks(token, project_id)
        print(f"  {len(tasks)} tareas descargadas.")

        parent_index = build_parent_index(tasks)

        all_parent_ids = {t["_msdyn_parenttask_value"] for t in tasks if t.get("_msdyn_parenttask_value")}
        missing_ids = [pid for pid in all_parent_ids if pid not in parent_index]
        extra_parents = {}
        if missing_ids:
            print(f"  Resolviendo {len(missing_ids)} padre(s) cross-proyecto...")
            extra_parents = resolve_cross_project_parents(token, missing_ids)
            parent_index.update(extra_parents)

        print("  Consultando listas de comprobación...")
        all_task_ids = [t["msdyn_projecttaskid"] for t in tasks]
        checklist_records = fetch_checklists(token, all_task_ids)
        print(f"  Checklists encontrados: {len(checklist_records)} ítems")

        if args.cache_file:
            cache_data = {
                "plan_label": plan_label,
                "project_id": project_id,
                "buckets": buckets,
                "done_id": done_id,
                "tasks": tasks,
                "extra_parents": extra_parents,
                "checklists": checklist_records,
            }
            with open(args.cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f"  Caché guardada: {args.cache_file}")

    rows, sin_titulo = build_report(tasks, buckets, done_id, parent_index, today_dt)

    # Enrich: checklists — siempre disponibles (desde red o desde caché)
    checklist_index = build_checklist_index(checklist_records)

    # Enrich each row with criticality, checklist, and AI suggestion
    for r in rows:
        r["checklist"] = checklist_index.get(r["task_id"], "")
        r["nivel_criticidad"] = assign_criticality_level(r, today_dt)
        r["suggest_action_text"] = suggest_action(r, today_dt)

    rows_sorted = sort_by_criticality(rows, today_dt)
    padres_sorted = group_and_sort(rows)
    print_report(padres_sorted, today_dt, plan_label, sin_titulo)

    if args.out:
        write_csv_pmo(rows_sorted, args.out)

    if args.html:
        html_content = generate_html_report(rows_sorted, today_dt, plan_label, sin_titulo)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"\n  HTML PMO exportado: {args.html}  ({len(rows_sorted)} filas)")

    if not args.out and not args.html and args.cache_file:
        print("\n  Para exportar sin re-consultar Dataverse:")
        print(f"  python scripts/plan_report.py --from-cache {args.cache_file} --out <ruta.csv>")
        print(f"  python scripts/plan_report.py --from-cache {args.cache_file} --html <ruta.html>")


if __name__ == "__main__":
    main()

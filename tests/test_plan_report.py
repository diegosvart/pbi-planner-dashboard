"""Tests for pure functions in scripts/plan_report.py — no network required."""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from plan_report import (
    extract_code, classify_task, build_parent_index, suggest_bucket,
    needs_escalation, build_report,
)

TODAY = datetime(2026, 6, 11, tzinfo=timezone.utc)
WINDOW_DAYS = 14
WINDOW = TODAY + timedelta(days=WINDOW_DAYS)


# ── extract_code ──────────────────────────────────────────────────────────────

class TestExtractCode:
    def test_norm_simple(self):
        assert extract_code("NORM-026 - Procedimiento Onboarding") == "NORM-026"

    def test_inter_simple(self):
        assert extract_code("INTER-003 - BD central HUB") == "INTER-003"

    def test_port_with_subcode(self):
        assert extract_code("PORT-020.1 - Seguimiento detalle") == "PORT-020.1"

    def test_lowercase_returns_none(self):
        assert extract_code("norm-001 algo") is None

    def test_no_code_returns_none(self):
        assert extract_code("Reunión de kick-off") is None

    def test_none_input_returns_none(self):
        assert extract_code(None) is None

    def test_empty_string_returns_none(self):
        assert extract_code("") is None

    def test_single_char_prefix_returns_none(self):
        # Prefix must be 2+ uppercase letters
        assert extract_code("A-001 algo") is None

    def test_three_char_prefix(self):
        assert extract_code("SIN-001 algo") == "SIN-001"


# ── classify_task ─────────────────────────────────────────────────────────────

class TestClassifyTask:
    def test_overdue_yesterday(self):
        end = TODAY - timedelta(days=1)
        assert classify_task(end, TODAY, WINDOW_DAYS) == "VENCIDA"

    def test_overdue_far_past(self):
        end = TODAY - timedelta(days=60)
        assert classify_task(end, TODAY, WINDOW_DAYS) == "VENCIDA"

    def test_due_today(self):
        assert classify_task(TODAY, TODAY, WINDOW_DAYS) == "EN FECHA"

    def test_due_within_window(self):
        end = TODAY + timedelta(days=7)
        assert classify_task(end, TODAY, WINDOW_DAYS) == "EN FECHA"

    def test_due_exactly_window_boundary(self):
        end = TODAY + timedelta(days=14)
        assert classify_task(end, TODAY, WINDOW_DAYS) == "EN FECHA"

    def test_due_beyond_window_returns_none(self):
        end = TODAY + timedelta(days=15)
        assert classify_task(end, TODAY, WINDOW_DAYS) is None

    def test_far_future_returns_none(self):
        end = TODAY + timedelta(days=90)
        assert classify_task(end, TODAY, WINDOW_DAYS) is None


# ── build_parent_index ────────────────────────────────────────────────────────

class TestBuildParentIndex:
    def _make_task(self, task_id, subject):
        return {"msdyn_projecttaskid": task_id, "msdyn_subject": subject}

    def test_builds_basic_index(self):
        tasks = [
            self._make_task("aaa", "NORM-001 - Tarea uno"),
            self._make_task("bbb", "PORT-002 - Tarea dos"),
        ]
        idx = build_parent_index(tasks)
        assert idx["aaa"] == "NORM-001 - Tarea uno"
        assert idx["bbb"] == "PORT-002 - Tarea dos"

    def test_missing_subject_defaults(self):
        tasks = [{"msdyn_projecttaskid": "ccc"}]
        idx = build_parent_index(tasks)
        assert idx["ccc"] == "(sin titulo)"

    def test_extra_parents_override(self):
        tasks = [self._make_task("aaa", "NORM-001 - Tarea uno")]
        extra = {"bbb": "INTER-003 - BD central HUB"}
        idx = build_parent_index(tasks, extra_parents=extra)
        assert idx["bbb"] == "INTER-003 - BD central HUB"
        assert idx["aaa"] == "NORM-001 - Tarea uno"

    def test_empty_tasks_empty_index(self):
        assert build_parent_index([]) == {}

    def test_extra_parents_overrides_existing(self):
        tasks = [self._make_task("aaa", "viejo nombre")]
        extra = {"aaa": "nuevo nombre via cross-query"}
        idx = build_parent_index(tasks, extra_parents=extra)
        assert idx["aaa"] == "nuevo nombre via cross-query"


# ── suggest_bucket ────────────────────────────────────────────────────────────

class TestSuggestBucket:
    """
    Regla: VENCIDA + nota de gestión → sugerir Blocked
           Blocked + nota indica desbloqueo → sugerir In Progress
           Otros casos → None (sin sugerencia)
    """

    def _entry(self, categoria, bucket, tiene_nota, nota=""):
        return {
            "categoria": categoria,
            "bucket": bucket,
            "tiene_nota": tiene_nota,
            "nota_preview": nota,
        }

    def test_overdue_with_note_suggests_blocked(self):
        entry = self._entry("VENCIDA", "In Progress", True, "Esperando respuesta de proveedor")
        suggestion = suggest_bucket(entry)
        assert suggestion is not None
        assert suggestion["target_bucket"] == "Blocked"

    def test_overdue_without_note_no_suggestion(self):
        entry = self._entry("VENCIDA", "In Progress", False)
        assert suggest_bucket(entry) is None

    def test_blocked_with_resolved_note_suggests_in_progress(self):
        entry = self._entry("EN FECHA", "Blocked", True, "Proveedor confirmó entrega, desbloqueado")
        suggestion = suggest_bucket(entry)
        assert suggestion is not None
        assert suggestion["target_bucket"] == "In Progress"

    def test_blocked_without_note_no_suggestion(self):
        entry = self._entry("EN FECHA", "Blocked", False)
        assert suggest_bucket(entry) is None

    def test_en_fecha_in_progress_no_suggestion(self):
        entry = self._entry("EN FECHA", "In Progress", True, "Avanzando según plan")
        assert suggest_bucket(entry) is None

    def test_vencida_already_blocked_no_suggestion(self):
        # Ya está en Blocked — no sugerir de nuevo
        entry = self._entry("VENCIDA", "Blocked", True, "Bloqueado por falta de recursos")
        assert suggest_bucket(entry) is None

    def test_suggestion_includes_reason(self):
        entry = self._entry("VENCIDA", "Gateway", True, "Pendiente aprobación gerencia")
        suggestion = suggest_bucket(entry)
        assert "reason" in suggestion
        assert len(suggestion["reason"]) > 0


# ── needs_escalation ──────────────────────────────────────────────────────────

class TestNeedsEscalation:
    """
    Regla: VENCIDA + Blocked + overdue > umbral → escalar
           Otros casos → None
    """

    def _entry(self, categoria, bucket, dias_vencida):
        end_dt = TODAY - timedelta(days=dias_vencida)
        return {
            "categoria": categoria,
            "bucket": bucket,
            "end_dt": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def test_vencida_blocked_over_umbral_escala(self):
        entry = self._entry("VENCIDA", "Blocked", 30)
        result = needs_escalation(entry, TODAY, dias_umbral=14)
        assert result is not None
        assert "reason" in result
        assert "30" in result["reason"]

    def test_vencida_blocked_exactly_umbral_no_escala(self):
        entry = self._entry("VENCIDA", "Blocked", 14)
        assert needs_escalation(entry, TODAY, dias_umbral=14) is None

    def test_vencida_blocked_under_umbral_no_escala(self):
        entry = self._entry("VENCIDA", "Blocked", 10)
        assert needs_escalation(entry, TODAY, dias_umbral=14) is None

    def test_vencida_in_progress_no_escala(self):
        entry = self._entry("VENCIDA", "In Progress", 30)
        assert needs_escalation(entry, TODAY, dias_umbral=14) is None

    def test_en_fecha_blocked_no_escala(self):
        entry = self._entry("EN FECHA", "Blocked", 0)
        assert needs_escalation(entry, TODAY, dias_umbral=14) is None

    def test_missing_end_dt_no_escala(self):
        entry = {"categoria": "VENCIDA", "bucket": "Blocked", "end_dt": ""}
        assert needs_escalation(entry, TODAY) is None

    def test_reason_mentions_dias(self):
        entry = self._entry("VENCIDA", "Blocked", 45)
        result = needs_escalation(entry, TODAY, dias_umbral=14)
        assert "45" in result["reason"]


# ── build_report — phantom task filter ───────────────────────────────────────

def _make_raw_task(task_id, subject, parent_id, end_iso, progress=0.0, bucket_id=None):
    return {
        "msdyn_projecttaskid": task_id,
        "msdyn_subject": subject,
        "_msdyn_parenttask_value": parent_id,
        "msdyn_scheduledend": end_iso,
        "msdyn_progress": progress,
        "_msdyn_projectbucket_value": bucket_id,
        "modifiedon": "2026-06-01T00:00:00Z",
        "msdyn_descriptionplaintext": None,
        "msdyn_pfwmodifiedby": None,
    }


VENCIDA_END = (TODAY - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestBuildReportPhantom:
    """build_report debe excluir tareas con subject vacío/whitespace y contar como sin_titulo."""

    def _run(self, tasks):
        parent_index = {"p1": "NORM-001 - Padre"}
        return build_report(tasks, {}, None, parent_index, TODAY, WINDOW_DAYS)

    def test_empty_subject_excluded_and_counted(self):
        tasks = [
            _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END),
            _make_raw_task("t2", "", "p1", VENCIDA_END),
        ]
        rows, sin_titulo = self._run(tasks)
        assert len(rows) == 1
        assert rows[0]["subject"] == "NORM-001.1 - Tarea real"
        assert sin_titulo == 1

    def test_whitespace_subject_excluded_and_counted(self):
        tasks = [
            _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END),
            _make_raw_task("t2", "   ", "p1", VENCIDA_END),
        ]
        rows, sin_titulo = self._run(tasks)
        assert len(rows) == 1
        assert sin_titulo == 1

    def test_no_phantom_returns_zero_sin_titulo(self):
        tasks = [_make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END)]
        rows, sin_titulo = self._run(tasks)
        assert sin_titulo == 0

    def test_multiple_phantoms_counted_together(self):
        tasks = [
            _make_raw_task("t1", "", "p1", VENCIDA_END),
            _make_raw_task("t2", "", "p1", VENCIDA_END),
            _make_raw_task("t3", "NORM-001.1 - Real", "p1", VENCIDA_END),
        ]
        rows, sin_titulo = self._run(tasks)
        assert len(rows) == 1
        assert sin_titulo == 2

    def test_nota_full_stored_in_entry(self):
        long_nota = "a" * 120
        task = _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END)
        task["msdyn_descriptionplaintext"] = long_nota
        rows, _ = self._run([task])
        assert len(rows) == 1
        assert rows[0]["nota"] == long_nota

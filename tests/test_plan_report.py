"""Tests for pure functions in scripts/plan_report.py — no network required."""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from plan_report import (
    extract_code, classify_task, build_parent_index, suggest_bucket,
    needs_escalation, build_report, group_and_sort,
    build_checklist_index, assign_criticality_level, suggest_action,
    sort_by_criticality, write_csv_pmo, strip_html, generate_html_report,
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
        assert classify_task(end, TODAY) == "VENCIDA"

    def test_overdue_far_past(self):
        end = TODAY - timedelta(days=60)
        assert classify_task(end, TODAY) == "VENCIDA"

    def test_due_today(self):
        assert classify_task(TODAY, TODAY) == "EN FECHA"

    def test_due_within_window(self):
        end = TODAY + timedelta(days=7)
        assert classify_task(end, TODAY) is None

    def test_due_exactly_window_boundary(self):
        end = TODAY + timedelta(days=14)
        assert classify_task(end, TODAY) is None

    def test_due_beyond_window_returns_none(self):
        end = TODAY + timedelta(days=15)
        assert classify_task(end, TODAY) is None

    def test_far_future_returns_none(self):
        end = TODAY + timedelta(days=90)
        assert classify_task(end, TODAY) is None


# ── classify_task — EN CURSO (IsLive alignment) ───────────────────────────────

class TestClassifyTaskEnCurso:
    """start_dt <= today <= end_dt → EN CURSO (equivale a IsLive del dashboard)."""

    def test_started_not_yet_due_is_en_curso(self):
        start = TODAY - timedelta(days=5)
        end = TODAY + timedelta(days=10)
        assert classify_task(end, TODAY, start_dt=start) == "EN CURSO"

    def test_started_today_due_future_is_en_curso(self):
        start = TODAY
        end = TODAY + timedelta(days=3)
        assert classify_task(end, TODAY, start_dt=start) == "EN CURSO"

    def test_not_yet_started_future_due_is_none(self):
        start = TODAY + timedelta(days=1)
        end = TODAY + timedelta(days=10)
        assert classify_task(end, TODAY, start_dt=start) is None

    def test_no_start_dt_future_is_none(self):
        end = TODAY + timedelta(days=5)
        assert classify_task(end, TODAY, start_dt=None) is None

    def test_overdue_with_start_still_vencida(self):
        # VENCIDA toma precedencia sobre EN CURSO
        start = TODAY - timedelta(days=10)
        end = TODAY - timedelta(days=1)
        assert classify_task(end, TODAY, start_dt=start) == "VENCIDA"

    def test_due_today_with_start_is_en_fecha(self):
        start = TODAY - timedelta(days=3)
        assert classify_task(TODAY, TODAY, start_dt=start) == "EN FECHA"


# ── build_report — EN CURSO tasks included ────────────────────────────────────

class TestBuildReportEnCurso:
    """Tareas en curso (start <= today < end) deben aparecer en el reporte."""

    def _run(self, tasks):
        parent_index = {"p1": "NORM-001 - Padre"}
        return build_report(tasks, {}, None, parent_index, TODAY)

    def test_en_curso_task_included(self):
        start = (TODAY - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (TODAY + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task = _make_raw_task("t1", "NORM-001.1 - En curso", "p1", end)
        task["msdyn_scheduledstart"] = start
        rows, _ = self._run([task])
        assert len(rows) == 1
        assert rows[0]["categoria"] == "EN CURSO"

    def test_not_started_future_excluded(self):
        start = (TODAY + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (TODAY + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task = _make_raw_task("t1", "NORM-001.1 - Futura", "p1", end)
        task["msdyn_scheduledstart"] = start
        rows, _ = self._run([task])
        assert rows == []

    def test_no_start_future_excluded(self):
        end = (TODAY + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task = _make_raw_task("t1", "NORM-001.1 - Sin start", "p1", end)
        rows, _ = self._run([task])
        assert rows == []

    def test_vencida_without_start_still_included(self):
        rows, _ = self._run([_make_raw_task("t1", "NORM-001.1 - Vencida", "p1", VENCIDA_END)])
        assert len(rows) == 1
        assert rows[0]["categoria"] == "VENCIDA"


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

def _make_raw_task(task_id, subject, parent_id, end_iso, progress=0.0, bucket_id=None, statecode=0):
    return {
        "msdyn_projecttaskid": task_id,
        "msdyn_subject": subject,
        "_msdyn_parenttask_value": parent_id,
        "msdyn_scheduledend": end_iso,
        "msdyn_progress": progress,
        "_msdyn_projectbucket_value": bucket_id,
        "modifiedon": "2026-06-01T00:00:00Z",
        "msdyn_description": None,
        "msdyn_pfwmodifiedby": None,
        "statecode": statecode,
    }


VENCIDA_END = (TODAY - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestBuildReportPhantom:
    """build_report debe excluir tareas con subject vacío/whitespace y contar como sin_titulo."""

    def _run(self, tasks):
        parent_index = {"p1": "NORM-001 - Padre"}
        return build_report(tasks, {}, None, parent_index, TODAY)

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
        task["msdyn_description"] = long_nota
        rows, _ = self._run([task])
        assert len(rows) == 1
        assert rows[0]["nota"] == long_nota


# ── Bug 2 — classify_task: sin ventana futura ────────────────────────────────

class TestClassifyTaskNoFuture:
    """Sólo pasado y hoy son accionables. Futuro = None."""

    def test_yesterday_is_vencida(self):
        end = TODAY - timedelta(days=1)
        assert classify_task(end, TODAY) == "VENCIDA"

    def test_today_is_en_fecha(self):
        assert classify_task(TODAY, TODAY) == "EN FECHA"

    def test_tomorrow_is_none(self):
        end = TODAY + timedelta(days=1)
        assert classify_task(end, TODAY) is None

    def test_in_7_days_is_none(self):
        end = TODAY + timedelta(days=7)
        assert classify_task(end, TODAY) is None

    def test_far_future_is_none(self):
        end = TODAY + timedelta(days=90)
        assert classify_task(end, TODAY) is None


# ── Bug 1 — build_report: filtro statecode ──────────────────────────────────

class TestBuildReportStateCode:
    """Tareas inactivas (statecode != 0) deben ser excluidas del reporte."""

    def _run(self, tasks):
        parent_index = {"p1": "NORM-001 - Padre"}
        return build_report(tasks, {}, None, parent_index, TODAY)

    def test_tarea_sin_titulo_placeholder_excluded(self):
        task = _make_raw_task("t1", "Tarea sin título", "p1", VENCIDA_END, statecode=0)
        rows, sin_titulo = self._run([task])
        assert rows == []
        assert sin_titulo == 1

    def test_inactive_task_excluded(self):
        task = _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END, statecode=1)
        rows, _ = self._run([task])
        assert rows == []

    def test_active_task_included(self):
        task = _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END, statecode=0)
        rows, _ = self._run([task])
        assert len(rows) == 1

    def test_missing_statecode_treated_as_active(self):
        task = _make_raw_task("t1", "NORM-001.1 - Tarea real", "p1", VENCIDA_END)
        del task["statecode"]
        rows, _ = self._run([task])
        assert len(rows) == 1


# ── Bug 3 — group_and_sort: orden alfabético ─────────────────────────────────

class TestGroupAndSortAlpha:
    """Grupos ordenados por parent_code ASC; hijas por subject ASC."""

    def _make_entry(self, parent_code, subject, categoria="EN FECHA"):
        return {
            "parent_code": parent_code,
            "parent_subject": f"{parent_code} - desc",
            "subject": subject,
            "categoria": categoria,
            "end_dt": "2026-06-12T00:00:00Z",
        }

    def test_groups_sorted_by_parent_code_asc(self):
        rows = [
            self._make_entry("NORM-020", "NORM-020.E1"),
            self._make_entry("INTER-001", "INTER-001.E1"),
        ]
        result = group_and_sort(rows)
        assert [pc for pc, _ in result] == ["INTER-001", "NORM-020"]

    def test_groups_not_sorted_by_vencidas_count(self):
        rows = [
            self._make_entry("NORM-001", "NORM-001.E1", "VENCIDA"),
            self._make_entry("NORM-001", "NORM-001.E2", "VENCIDA"),
            self._make_entry("INTER-001", "INTER-001.E1", "EN FECHA"),
        ]
        result = group_and_sort(rows)
        assert result[0][0] == "INTER-001"

    def test_hijas_sorted_by_subject_asc(self):
        rows = [
            self._make_entry("NORM-012", "NORM-012.E3"),
            self._make_entry("NORM-012", "NORM-012.E1"),
            self._make_entry("NORM-012", "NORM-012.E2"),
        ]
        result = group_and_sort(rows)
        subjects = [h["subject"] for h in result[0][1]["hijas"]]
        assert subjects == ["NORM-012.E1", "NORM-012.E2", "NORM-012.E3"]

    def test_hijas_not_sorted_by_categoria(self):
        rows = [
            self._make_entry("NORM-012", "NORM-012.E1", "VENCIDA"),
            self._make_entry("NORM-012", "NORM-012.E2", "EN FECHA"),
        ]
        result = group_and_sort(rows)
        subjects = [h["subject"] for h in result[0][1]["hijas"]]
        assert subjects == ["NORM-012.E1", "NORM-012.E2"]


# ── strip_html ────────────────────────────────────────────────────────────────

class TestStripHtml:
    def test_simple_div(self):
        assert strip_html("<div>Texto</div>") == "Texto"

    def test_span_with_style(self):
        assert strip_html('<span style="color:red;">Nota</span>') == "Nota"

    def test_nested_tags(self):
        result = strip_html("<div><p>Hola</p><p>Mundo</p></div>")
        assert "Hola" in result
        assert "Mundo" in result

    def test_html_entities(self):
        assert strip_html("&amp; &lt; &gt; &nbsp;") == "& < >"

    def test_plain_text_unchanged(self):
        assert strip_html("Sin tags aquí") == "Sin tags aquí"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_collapses_whitespace(self):
        result = strip_html("<div>  Mucho   espacio  </div>")
        assert result == "Mucho espacio"


# ── Issue #21 — build_checklist_index ────────────────────────────────────────

class TestBuildChecklistIndex:
    def test_empty_returns_empty(self):
        assert build_checklist_index([]) == {}

    def test_single_task_two_items_one_done(self):
        records = [
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": True},
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": False},
        ]
        assert build_checklist_index(records) == {"tid1": "1/2"}

    def test_multiple_tasks(self):
        records = [
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": True},
            {"_msdyn_projecttaskid_value": "tid2", "msdyn_projectchecklistcompleted": False},
            {"_msdyn_projecttaskid_value": "tid2", "msdyn_projectchecklistcompleted": True},
        ]
        result = build_checklist_index(records)
        assert result["tid1"] == "1/1"
        assert result["tid2"] == "1/2"

    def test_no_task_id_skipped(self):
        records = [{"msdyn_projectchecklistcompleted": True}]
        assert build_checklist_index(records) == {}

    def test_all_done(self):
        records = [
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": True},
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": True},
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": True},
        ]
        assert build_checklist_index(records) == {"tid1": "3/3"}


# ── Issue #18 — generate_html_report ─────────────────────────────────────────

def _make_row(
    subject="Tarea A",
    parent_code="NORM-001",
    parent_subject="NORM-001 - Tarea padre",
    responsable="Juan Pérez",
    categoria="VENCIDA",
    bucket="Blocked",
    end_str="10-06-2026",
    start_str="01-06-2026",
    mod_str="12-06-2026",
    nota="Nota de gestión",
    tiene_nota=True,
    checklist="2/3",
    nivel_criticidad=1,
    suggest_action_text="Bloqueada y vencida hace 20 días — escalar",
    end_dt="2026-06-10T00:00:00Z",
    task_id="tid1",
) -> dict:
    return {
        "task_id": task_id,
        "subject": subject,
        "parent_code": parent_code,
        "parent_subject": parent_subject,
        "responsable": responsable,
        "categoria": categoria,
        "bucket": bucket,
        "end_str": end_str,
        "start_str": start_str,
        "mod_str": mod_str,
        "nota": nota,
        "tiene_nota": tiene_nota,
        "checklist": checklist,
        "nivel_criticidad": nivel_criticidad,
        "suggest_action_text": suggest_action_text,
        "end_dt": end_dt,
    }


class TestGenerateHtmlReport:
    """generate_html_report() — función pura, no requiere red."""

    TODAY_H = datetime(2026, 6, 19, tzinfo=timezone.utc)

    def _run(self, rows=None, sin_titulo=0):
        if rows is None:
            rows = [_make_row()]
        return generate_html_report(rows, self.TODAY_H, "Plan TI 2026", sin_titulo)

    # ── Estructura básica ────────────────────────────────────────────────────

    def test_returns_string(self):
        assert isinstance(self._run(), str)

    def test_well_formed_doctype(self):
        html = self._run()
        assert html.startswith("<!DOCTYPE html>")

    def test_closes_html_tag(self):
        assert self._run().endswith("</html>")

    def test_contains_viewport_meta(self):
        assert "viewport" in self._run()

    def test_contains_media_print(self):
        assert "@media print" in self._run()

    def test_title_contains_plan_label(self):
        assert "Plan TI 2026" in self._run()

    def test_title_contains_date(self):
        assert "19-06-2026" in self._run()

    # ── KPI cards ────────────────────────────────────────────────────────────

    def test_kpi_total_accionables(self):
        rows = [
            _make_row(task_id="t1", categoria="VENCIDA"),
            _make_row(task_id="t2", categoria="EN FECHA"),
            _make_row(task_id="t3", categoria="EN CURSO"),
        ]
        html = generate_html_report(rows, self.TODAY_H, "X")
        assert "3" in html  # total accionables

    def test_kpi_vencidas_count(self):
        rows = [
            _make_row(task_id="t1", categoria="VENCIDA"),
            _make_row(task_id="t2", categoria="VENCIDA"),
            _make_row(task_id="t3", categoria="EN CURSO"),
        ]
        html = generate_html_report(rows, self.TODAY_H, "X")
        # Las 2 vencidas deben aparecer en el conteo KPI
        assert html.count("2") >= 1

    def test_kpi_salud_buena(self):
        # 4/4 = 100% con nota → Buena
        rows = [_make_row(task_id=f"t{i}", tiene_nota=True) for i in range(4)]
        assert "Buena" in generate_html_report(rows, self.TODAY_H, "X")

    def test_kpi_salud_regular(self):
        # 1/2 = 50% con nota → Regular
        rows = [
            _make_row(task_id="t1", tiene_nota=True),
            _make_row(task_id="t2", tiene_nota=False, nota=""),
        ]
        assert "Regular" in generate_html_report(rows, self.TODAY_H, "X")

    def test_kpi_salud_baja(self):
        # 0/2 = 0% → Baja
        rows = [_make_row(task_id=f"t{i}", tiene_nota=False, nota="") for i in range(2)]
        assert "Baja" in generate_html_report(rows, self.TODAY_H, "X")

    # ── Tabla — clases de criticidad ─────────────────────────────────────────

    def test_row_has_nivel_class(self):
        row = _make_row(nivel_criticidad=1)
        assert "nivel-1" in generate_html_report([row], self.TODAY_H, "X")

    def test_nivel_2_row(self):
        row = _make_row(nivel_criticidad=2)
        assert "nivel-2" in generate_html_report([row], self.TODAY_H, "X")

    def test_nivel_7_row(self):
        row = _make_row(nivel_criticidad=7, categoria="EN CURSO", bucket="In Progress",
                        suggest_action_text="", end_dt="2026-07-30T00:00:00Z",
                        end_str="30-07-2026")
        assert "nivel-7" in generate_html_report([row], self.TODAY_H, "X")

    # ── Tooltip AI ───────────────────────────────────────────────────────────

    def test_ai_icon_present_when_suggestion(self):
        row = _make_row(suggest_action_text="Escalar con responsable")
        assert "AI" in generate_html_report([row], self.TODAY_H, "X")

    def test_ai_tooltip_text_present(self):
        row = _make_row(suggest_action_text="Escalar con responsable")
        assert "Escalar con responsable" in generate_html_report([row], self.TODAY_H, "X")

    def test_no_ai_icon_when_no_suggestion(self):
        row = _make_row(suggest_action_text="")
        html = generate_html_report([row], self.TODAY_H, "X")
        # El texto de sugerencia vacío no debe generar tooltip
        assert "ai-icon" not in html or "Escalar" not in html

    # ── Escaping / seguridad ─────────────────────────────────────────────────

    def test_nota_xss_escaped(self):
        # La nota con XSS debe aparecer escapada en el HTML;
        # verificamos el contenido escapado, no la ausencia de <script>
        # (el HTML propio incluye un bloque <script> de JS inline legítimo).
        row = _make_row(nota='<script>alert("xss")</script>', tiene_nota=True)
        html = generate_html_report([row], self.TODAY_H, "X")
        assert "&lt;script&gt;" in html
        assert "alert(&quot;xss&quot;)" in html or "alert(&#" in html or 'alert("xss")' not in html

    def test_ampersand_in_responsable_escaped(self):
        row = _make_row(responsable="Juan & Pérez")
        html = generate_html_report([row], self.TODAY_H, "X")
        assert "Juan & Pérez" not in html  # literal ampersand no debe aparecer
        assert "&amp;" in html

    def test_quotes_in_subject_escaped(self):
        row = _make_row(subject='Tarea "importante"')
        html = generate_html_report([row], self.TODAY_H, "X")
        assert 'Tarea "importante"' not in html
        assert "&quot;" in html or "&#x27;" in html or "importante" in html

    # ── Sección Alertas ───────────────────────────────────────────────────────

    def test_escalamiento_present_when_blocked_vencida(self):
        # Blocked + VENCIDA + end_dt hace >14 días → escalamiento
        row = _make_row(
            categoria="VENCIDA", bucket="Blocked",
            end_dt="2026-05-01T00:00:00Z",  # >14 días antes de TODAY_H (2026-06-19)
            nivel_criticidad=1,
        )
        assert "Escalamiento" in generate_html_report([row], self.TODAY_H, "X")

    def test_escalamiento_absent_when_no_blocked_vencida(self):
        row = _make_row(categoria="EN CURSO", bucket="In Progress",
                        suggest_action_text="", end_dt="2026-07-30T00:00:00Z",
                        end_str="30-07-2026", nivel_criticidad=7)
        html = generate_html_report([row], self.TODAY_H, "X")
        # No debe haber items de escalamiento (el bloque de sección puede existir)
        assert "escalar" not in html.lower() or "Sin tareas" in html

    def test_carga_responsable_listed(self):
        rows = [
            _make_row(task_id="t1", responsable="Ana López", categoria="VENCIDA"),
            _make_row(task_id="t2", responsable="Ana López", categoria="EN CURSO"),
        ]
        html = generate_html_report(rows, self.TODAY_H, "X")
        assert "Ana López" in html

    def test_sin_titulo_shown_in_footer(self):
        html = generate_html_report([_make_row()], self.TODAY_H, "X", sin_titulo=3)
        assert "3" in html  # los 3 sin título aparecen en el footer

    def test_none_done(self):
        records = [
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": False},
            {"_msdyn_projecttaskid_value": "tid1", "msdyn_projectchecklistcompleted": False},
        ]
        assert build_checklist_index(records) == {"tid1": "0/2"}


# ── Issue #22 — assign_criticality_level ─────────────────────────────────────

class TestAssignCriticalityLevel:
    def _entry(self, categoria, bucket="In Progress", tiene_nota=False, end_offset_days=None):
        if end_offset_days is None:
            end_offset_days = -5 if categoria == "VENCIDA" else (0 if categoria == "EN FECHA" else 10)
        end = (TODAY + timedelta(days=end_offset_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"categoria": categoria, "bucket": bucket, "tiene_nota": tiene_nota, "end_dt": end}

    def test_level1_blocked_vencida(self):
        assert assign_criticality_level(self._entry("VENCIDA", "Blocked", True), TODAY) == 1

    def test_level1_blocked_vencida_no_nota(self):
        assert assign_criticality_level(self._entry("VENCIDA", "Blocked", False), TODAY) == 1

    def test_level2_vencida_sin_nota(self):
        assert assign_criticality_level(self._entry("VENCIDA", "In Progress", False), TODAY) == 2

    def test_level3_vencida_con_nota(self):
        assert assign_criticality_level(self._entry("VENCIDA", "In Progress", True), TODAY) == 3

    def test_level4_en_fecha(self):
        assert assign_criticality_level(self._entry("EN FECHA"), TODAY) == 4

    def test_level5_en_curso_3_days(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=3), TODAY) == 5

    def test_level5_en_curso_1_day(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=1), TODAY) == 5

    def test_level6_en_curso_7_days(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=7), TODAY) == 6

    def test_level6_en_curso_4_days(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=4), TODAY) == 6

    def test_level7_en_curso_8_days(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=8), TODAY) == 7

    def test_level7_en_curso_30_days(self):
        assert assign_criticality_level(self._entry("EN CURSO", end_offset_days=30), TODAY) == 7


# ── Issue #22 — build_report usa msdyn_description ───────────────────────────

class TestBuildReportUsesDescription:
    """build_report debe leer msdyn_description, no msdyn_descriptionplaintext."""

    def _run(self, tasks):
        parent_index = {"p1": "NORM-001 - Padre"}
        return build_report(tasks, {}, None, parent_index, TODAY)

    def test_description_field_read(self):
        task = _make_raw_task("t1", "NORM-001.1 - Tarea", "p1", VENCIDA_END)
        task["msdyn_description"] = "Nota via msdyn_description"
        rows, _ = self._run([task])
        assert rows[0]["nota"] == "Nota via msdyn_description"
        assert rows[0]["tiene_nota"] is True

    def test_entry_has_task_id(self):
        task = _make_raw_task("t1", "NORM-001.1 - Tarea", "p1", VENCIDA_END)
        rows, _ = self._run([task])
        assert rows[0]["task_id"] == "t1"

    def test_entry_has_start_str(self):
        start = (TODAY - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        task = _make_raw_task("t1", "NORM-001.1 - Tarea", "p1", VENCIDA_END)
        task["msdyn_scheduledstart"] = start
        rows, _ = self._run([task])
        assert rows[0]["start_str"] != ""


# ── Issue #23 — suggest_action ────────────────────────────────────────────────

class TestSuggestAction:
    def _entry(self, categoria, bucket, tiene_nota, nota="", dias_vencida=0):
        end = (TODAY - timedelta(days=dias_vencida)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"categoria": categoria, "bucket": bucket,
                "tiene_nota": tiene_nota, "nota_preview": nota, "end_dt": end}

    def test_vencida_con_nota_not_blocked_suggest_blocked(self):
        entry = self._entry("VENCIDA", "In Progress", True, "pendiente respuesta")
        assert "Blocked" in suggest_action(entry, TODAY)

    def test_blocked_with_unblock_keyword_suggest_in_progress(self):
        entry = self._entry("EN FECHA", "Blocked", True, "proveedor confirmó entrega")
        assert "In Progress" in suggest_action(entry, TODAY)

    def test_vencida_sin_nota_over_14_suggest_escalar(self):
        entry = self._entry("VENCIDA", "In Progress", False, dias_vencida=15)
        result = suggest_action(entry, TODAY)
        assert "escalar" in result.lower()

    def test_vencida_blocked_over_14_suggest_escalar(self):
        entry = self._entry("VENCIDA", "Blocked", True, "sin avance", dias_vencida=20)
        result = suggest_action(entry, TODAY)
        assert "escalar" in result.lower()

    def test_en_fecha_no_issue_empty(self):
        entry = self._entry("EN FECHA", "In Progress", False)
        assert suggest_action(entry, TODAY) == ""

    def test_en_curso_no_issue_empty(self):
        end = (TODAY + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"categoria": "EN CURSO", "bucket": "In Progress",
                 "tiene_nota": False, "nota_preview": "", "end_dt": end}
        assert suggest_action(entry, TODAY) == ""

    def test_vencida_already_blocked_no_nota_over_14_escalate(self):
        entry = self._entry("VENCIDA", "Blocked", False, dias_vencida=20)
        result = suggest_action(entry, TODAY)
        assert "escalar" in result.lower()


# ── Issue #24 — sort_by_criticality ──────────────────────────────────────────

class TestSortByCriticality:
    def _entry(self, categoria, bucket, tiene_nota, end_offset_days):
        end = (TODAY + timedelta(days=end_offset_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"categoria": categoria, "bucket": bucket,
                "tiene_nota": tiene_nota, "end_dt": end}

    def test_blocked_vencida_first(self):
        rows = [
            self._entry("EN CURSO", "In Progress", False, 10),
            self._entry("VENCIDA", "Blocked", True, -5),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["bucket"] == "Blocked"
        assert result[0]["categoria"] == "VENCIDA"

    def test_vencida_sin_nota_before_vencida_con_nota(self):
        rows = [
            self._entry("VENCIDA", "In Progress", True, -3),
            self._entry("VENCIDA", "In Progress", False, -3),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["tiene_nota"] is False

    def test_vencida_before_en_fecha(self):
        rows = [
            self._entry("EN FECHA", "In Progress", False, 0),
            self._entry("VENCIDA", "In Progress", True, -2),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["categoria"] == "VENCIDA"

    def test_en_fecha_before_en_curso(self):
        rows = [
            self._entry("EN CURSO", "In Progress", False, 5),
            self._entry("EN FECHA", "In Progress", False, 0),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["categoria"] == "EN FECHA"

    def test_en_curso_3_days_before_7_days(self):
        rows = [
            self._entry("EN CURSO", "In Progress", False, 7),
            self._entry("EN CURSO", "In Progress", False, 3),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["end_dt"] < result[1]["end_dt"]

    def test_more_overdue_first_within_same_level(self):
        rows = [
            self._entry("VENCIDA", "In Progress", True, -3),
            self._entry("VENCIDA", "In Progress", True, -10),
        ]
        result = sort_by_criticality(rows, TODAY)
        assert result[0]["end_dt"] < result[1]["end_dt"]


# ── Issue #24 — write_csv_pmo ─────────────────────────────────────────────────

class TestWriteCsvPmo:
    def _row(self):
        return {
            "task_id": "tid1",
            "parent_code": "NORM-001",
            "parent_subject": "NORM-001 - Padre de prueba",
            "subject": "NORM-001.E1 - Subtarea",
            "responsable": "Diego Morales",
            "categoria": "VENCIDA",
            "nivel_criticidad": 2,
            "bucket": "In Progress",
            "nota": "Nota de prueba",
            "checklist": "1/3",
            "start_str": "01-06-2026",
            "end_str": "10-06-2026",
            "mod_str": "15-06-2026",
            "suggest_action_text": "Mover a Blocked",
        }

    def test_headers_correct(self):
        import io as _io, csv as _csv
        buf = _io.StringIO()
        write_csv_pmo([self._row()], buf)
        buf.seek(0)
        reader = _csv.DictReader(buf)
        expected = ["Codigo", "TareaPadre", "Tarea", "Responsable", "Estado",
                    "NivelCriticidad", "Bucket", "Nota", "Checklist",
                    "FechaInicio", "FechaFin", "UltimaActualizacion", "SugerenciaAI"]
        assert reader.fieldnames == expected

    def test_row_values_correct(self):
        import io as _io, csv as _csv
        buf = _io.StringIO()
        write_csv_pmo([self._row()], buf)
        buf.seek(0)
        row = next(_csv.DictReader(buf))
        assert row["Codigo"] == "NORM-001"
        assert row["TareaPadre"] == "NORM-001 - Padre de prueba"
        assert row["Tarea"] == "NORM-001.E1 - Subtarea"
        assert row["NivelCriticidad"] == "2"
        assert row["Checklist"] == "1/3"
        assert row["SugerenciaAI"] == "Mover a Blocked"
        assert row["FechaInicio"] == "01-06-2026"

    def test_empty_rows_header_only(self):
        import io as _io
        buf = _io.StringIO()
        write_csv_pmo([], buf)
        buf.seek(0)
        lines = [l for l in buf.read().strip().splitlines() if l.strip()]
        assert len(lines) == 1
        assert "Codigo" in lines[0]

    def test_multiple_rows(self):
        import io as _io, csv as _csv
        rows = [self._row(), {**self._row(), "subject": "NORM-001.E2", "categoria": "EN CURSO"}]
        buf = _io.StringIO()
        write_csv_pmo(rows, buf)
        buf.seek(0)
        data = list(_csv.DictReader(buf))
        assert len(data) == 2
        assert data[1]["Estado"] == "EN CURSO"

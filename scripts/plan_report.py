"""
Motor parametrizado de extracción Dataverse → reporte de gestión de planes.

Uso:
    python plan_report.py --plan "Planificación área TI 2026"
    python plan_report.py --project-id <GUID> --window-days 14 --today 2026-06-11 --out report.csv
    python plan_report.py --plan "..." --cache-file %TEMP%\\ti.json
    python plan_report.py --from-cache %TEMP%\\ti.json --out report.csv

Requiere: az CLI autenticado con dmorales@grupoebi.cl
          az account get-access-token (tenant b16beb2c-1c93-4497-bc75-5a1cdae6ee6c)
"""
import argparse
import csv
import io
import json
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta


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


def classify_task(end_dt: datetime, today: datetime, window_days: int) -> str | None:
    window = today + timedelta(days=window_days)
    if end_dt < today:
        return "VENCIDA"
    if end_dt <= window:
        return "EN FECHA"
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
        "msdyn_scheduledend", "modifiedon", "msdyn_descriptionplaintext",
        "_msdyn_parenttask_value", "_msdyn_projectbucket_value",
    ])
    url = (f"{BASE_URL}/msdyn_projecttasks"
           f"?$filter=_msdyn_project_value eq {project_id}"
           f"&$select={fields}"
           f"&$expand=msdyn_pfwmodifiedby($select=fullname)"
           f"&$top=500")
    return fetch_all_pages(token, url)


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
                 parent_index: dict, today: datetime, window_days: int) -> tuple[list, int]:
    """Returns (rows, sin_titulo_count). Excludes tasks with empty/whitespace subject."""
    rows = []
    sin_titulo = 0
    for t in tasks:
        subject = (t.get("msdyn_subject") or "").strip()
        if not subject:
            sin_titulo += 1
            continue

        parent_id = t.get("_msdyn_parenttask_value")
        if not parent_id:
            continue
        if (t.get("msdyn_progress") or 0.0) >= 1.0:
            continue
        bucket_id = t.get("_msdyn_projectbucket_value")
        if done_id and bucket_id == done_id:
            continue
        end_raw = t.get("msdyn_scheduledend")
        if not end_raw:
            continue
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        categoria = classify_task(end_dt, today, window_days)
        if categoria is None:
            continue

        pfw = t.get("msdyn_pfwmodifiedby") or {}
        responsable = pfw.get("fullname") or "(sin responsable)"

        mod_raw = t.get("modifiedon", "")
        mod_str = (datetime.fromisoformat(mod_raw.replace("Z", "+00:00")).strftime("%d-%m-%Y")
                   if mod_raw else "?")

        nota_raw = (t.get("msdyn_descriptionplaintext") or "").strip()
        tiene_nota = bool(nota_raw)
        nota_preview = (nota_raw[:80] + "...") if len(nota_raw) > 80 else nota_raw

        parent_subject = parent_index.get(parent_id, "(padre desconocido)")
        parent_code = extract_code(parent_subject) or parent_subject[:30]
        bucket_name = buckets.get(bucket_id, f"Bucket-{(bucket_id or '')[:8]}")

        entry = {
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
        grouped[pc]["hijas"].sort(key=lambda x: (0 if x["categoria"] == "VENCIDA" else 1, x["end_dt"]))

    return sorted(
        grouped.items(),
        key=lambda item: (
            -sum(1 for h in item[1]["hijas"] if h["categoria"] == "VENCIDA"),
            item[0],
        ),
    )


# ── Output ────────────────────────────────────────────────────────────────────

def print_report(padres_sorted: list, today: datetime, plan_label: str, sin_titulo: int = 0):
    W = 140
    total_v = sum(sum(1 for h in d["hijas"] if h["categoria"] == "VENCIDA") for _, d in padres_sorted)
    total_ef = sum(sum(1 for h in d["hijas"] if h["categoria"] == "EN FECHA") for _, d in padres_sorted)
    total_cn = sum(sum(1 for h in d["hijas"] if h["tiene_nota"]) for _, d in padres_sorted)
    total = total_v + total_ef

    print(f"\n{'='*W}")
    print(f"  REPORTE DE GESTIÓN — {plan_label}")
    print(f"  Hijas accionables (vencidas o en fecha), bucket != Done")
    print(f"  Fecha referencia: {today.strftime('%d-%m-%Y')}")
    print(f"{'='*W}")

    for parent_code, data in padres_sorted:
        hijas = data["hijas"]
        v = sum(1 for h in hijas if h["categoria"] == "VENCIDA")
        ef = len(hijas) - v
        cn = sum(1 for h in hijas if h["tiene_nota"])
        marker = " [!]" if v > 0 else ""
        print(f"\n{'-'*W}")
        print(f"  PADRE: {parent_code}{marker}  ({data['parent_subject'][:80]})")
        print(f"  Hijas: {len(hijas)}  |  Vencidas: {v}  |  En fecha: {ef}  |  Con nota: {cn}")
        print(f"{'-'*W}")
        print(f"  {'Tarea':<55} {'Responsable':<30} {'Bucket':<14} {'Fecha fin':<12} {'Ult.Act.':<12}")
        print(f"  {'-'*55} {'-'*30} {'-'*14} {'-'*12} {'-'*12}")
        for h in hijas:
            flag = "[!]" if h["categoria"] == "VENCIDA" else "   "
            print(f"{flag} {h['subject'][:55]:<55} {h['responsable'][:30]:<30} "
                  f"{h['bucket'][:13]:<14} {h['end_str']:<12} {h['mod_str']:<12}")
            if h["tiene_nota"]:
                print(f"     ↳ {h['nota']}")

    print(f"\n{'='*W}")
    print(f"  RESUMEN EJECUTIVO")
    print(f"{'='*W}")
    print(f"  Grupos padre con hijas accionables : {len(padres_sorted)}")
    print(f"  Total hijas accionables            : {total}")
    print(f"  Vencidas                           : {total_v}")
    print(f"  En fecha (próximas <= ventana)     : {total_ef}")
    if total:
        print(f"  Con nota de gestión                : {total_cn}  ({total_cn/total*100:.0f}%)")
        print(f"  Sin nota                           : {total-total_cn}  ({(total-total_cn)/total*100:.0f}%)")
    print(f"{'='*W}")

    _print_analysis(padres_sorted, today, sin_titulo)


def _print_analysis(padres_sorted: list, today: datetime, sin_titulo: int = 0):
    W = 140
    print(f"\n{'='*W}")
    print(f"  RECOMENDACIONES DE GESTIÓN")
    print(f"{'='*W}")

    # Escalamiento — bloqueadas vencidas críticas
    escalaciones = []
    for _, data in padres_sorted:
        for h in data["hijas"]:
            e = needs_escalation(h, today)
            if e:
                escalaciones.append((h["subject"][:60], h["responsable"], e["reason"]))

    if escalaciones:
        print(f"\n  [Escalamiento — Bloqueadas vencidas críticas]")
        for subj, resp, reason in escalaciones:
            print(f"  ⚠ {subj}")
            print(f"    Responsable: {resp}  |  {reason}")
    else:
        print(f"\n  [Escalamiento] Sin tareas bloqueadas-vencidas críticas.")

    # Sugerencias de bucket
    bucket_suggestions = []
    for _, data in padres_sorted:
        for h in data["hijas"]:
            s = suggest_bucket(h)
            if s:
                bucket_suggestions.append((h["subject"][:55], h["bucket"], s["target_bucket"], s["reason"]))

    if bucket_suggestions:
        print(f"\n  [Cambios de bucket sugeridos]")
        for subj, from_b, to_b, reason in bucket_suggestions:
            print(f"  • {subj}")
            print(f"    {from_b} → {to_b}  |  {reason}")
    else:
        print(f"\n  [Cambios de bucket] Sin sugerencias — estado coherente.")

    # Riesgo por responsable
    resp_count: dict[str, int] = {}
    resp_vencidas: dict[str, int] = {}
    for _, data in padres_sorted:
        for h in data["hijas"]:
            r = h["responsable"]
            resp_count[r] = resp_count.get(r, 0) + 1
            if h["categoria"] == "VENCIDA":
                resp_vencidas[r] = resp_vencidas.get(r, 0) + 1

    print(f"\n  [Carga por responsable]")
    for r, cnt in sorted(resp_count.items(), key=lambda x: -x[1]):
        v = resp_vencidas.get(r, 0)
        alert = " ⚠ VENCIDAS" if v > 0 else ""
        print(f"  • {r[:35]:<35} {cnt:>3} tareas accionables  |  {v} vencidas{alert}")

    # Calidad de datos
    sin_nota_vencidas = sum(
        1 for _, d in padres_sorted
        for h in d["hijas"] if h["categoria"] == "VENCIDA" and not h["tiene_nota"]
    )
    print(f"\n  [Calidad de datos]")
    if sin_titulo:
        print(f"  ⚠ {sin_titulo} tarea(s) sin título encontradas — excluidas del reporte (datos incompletos en Planner).")
    if sin_nota_vencidas:
        print(f"  ⚠ {sin_nota_vencidas} tarea(s) vencida(s) SIN nota de gestión — riesgo alto, sin visibilidad de bloqueo.")
    if not sin_titulo and not sin_nota_vencidas:
        print(f"  ✓ Sin problemas de calidad detectados.")
    elif not sin_nota_vencidas:
        print(f"  ✓ Todas las tareas vencidas tienen nota de gestión registrada.")

    print(f"{'='*W}")


def write_csv(padres_sorted: list, out_path: str):
    rows = []
    for parent_code, data in padres_sorted:
        for h in data["hijas"]:
            rows.append({
                "ParentTaskCode": parent_code,
                "TaskName": h["subject"],
                "Responsable": h["responsable"],
                "Bucket": h["bucket"],
                "FechaFin": h["end_str"],
                "UltimaActualizacion": h["mod_str"],
                "Categoria": h["categoria"],
                "TieneNota": "Si" if h["tiene_nota"] else "No",
                "Nota": h["nota"],
            })
    if not rows:
        print("  (sin filas para exportar)")
        return
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n  CSV exportado: {out_path}  ({len(rows)} filas)")


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
    parser.add_argument("--window-days", type=int, default=14, help="Ventana 'en fecha' en días (default: 14)")
    parser.add_argument("--today", help="Fecha de referencia YYYY-MM-DD (default: hoy)")
    parser.add_argument("--out", help="Ruta CSV de salida (opcional)")
    args = parser.parse_args()

    if not args.from_cache and not args.plan and not args.project_id:
        parser.error("Se requiere --plan, --project-id, o --from-cache")

    today_dt = (
        datetime.strptime(args.today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.today
        else datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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
        print(f"  {len(tasks)} tareas cargadas desde caché.")
    else:
        print("  Obteniendo token Azure...")
        token = get_token()

        if args.project_id:
            project_id = args.project_id
            plan_label = f"Proyecto {project_id[:8]}..."
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

        if args.cache_file:
            cache_data = {
                "plan_label": plan_label,
                "project_id": project_id,
                "buckets": buckets,
                "done_id": done_id,
                "tasks": tasks,
                "extra_parents": extra_parents,
            }
            with open(args.cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f"  Caché guardada: {args.cache_file}")

    rows, sin_titulo = build_report(tasks, buckets, done_id, parent_index, today_dt, args.window_days)
    padres_sorted = group_and_sort(rows)
    print_report(padres_sorted, today_dt, plan_label, sin_titulo)

    if args.out:
        write_csv(padres_sorted, args.out)
    elif args.cache_file:
        print(f"\n  Para exportar a CSV sin re-consultar Dataverse:")
        print(f"  python scripts/plan_report.py --from-cache {args.cache_file} --out <ruta.csv>")


if __name__ == "__main__":
    main()

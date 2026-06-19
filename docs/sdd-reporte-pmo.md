# SDD — Reporte PMO Plan Lineamiento Estratégico 2026

**Versión:** 1.0
**Fecha:** 2026-06-19
**Autor:** Diego Morales C
**Estado:** Borrador — pendiente aprobación

---

## 1. Objetivo

Generar un reporte de gestión PMO en formato HTML (y CSV opcional) que consolide el estado accionable del portafolio del Plan Lineamiento Estratégico 2026, permitiendo al equipo de gestión identificar rápidamente:

- Tareas vencidas, en fecha y en curso activas en el período
- Responsable, bucket actual, notas de gestión y tarea padre de cada ítem
- Alertas de escalamiento y sugerencias de cambio de bucket
- Contexto estratégico: alineación de tareas con objetivos del plan (`msdyn_projectgoal`)

**Artefacto:** Script Python que consulta Dataverse directamente vía OData, genera reporte HTML renderizable en navegador y exporta CSV opcional.

---

## 2. Fuentes de datos

**Entorno:** `https://org914d3d16.crm.dynamics.com/api/data/v9.2`
**Autenticación:** Token Azure CLI (`az account get-access-token`) — tenant `b16beb2c-1c93-4497-bc75-5a1cdae6ee6c`

### 2.1 Consulta principal — `msdyn_projecttasks`

```
GET /msdyn_projecttasks
  ?$filter=_msdyn_project_value eq {project_id}
  &$select={campos}
  &$expand=msdyn_pfwmodifiedby($select=fullname)
  &$top=500
```

**Campos `$select`:**

| Campo OData | Uso en reporte |
|---|---|
| `msdyn_projecttaskid` | Clave interna — no se muestra |
| `msdyn_subject` | Nombre de tarea |
| `msdyn_progress` | % avance [0.0–1.0] |
| `msdyn_scheduledstart` | Fecha inicio planificada |
| `msdyn_scheduledend` | Fecha fin planificada |
| `modifiedon` | Última actualización (timestamp Dataverse) |
| `msdyn_description` | Notas de gestión |
| `_msdyn_parenttask_value` | ID de tarea padre |
| `_msdyn_projectbucket_value` | ID de bucket |
| `statecode` | Filtro: solo registros activos (= 0) |
| `msdyn_outlinelevel` | Nivel jerárquico — para identificar hijas sin depender de `_msdyn_parenttask_value` |
| `msdyn_summary` | Indica si la tarea es padre (resumen) |

**Campos expandidos:**

| Expand | Campo | Uso |
|---|---|---|
| `msdyn_pfwmodifiedby` | `fullname` | Nombre del responsable |

**Campos excluidos de esta versión:**
- `msdyn_effortremaining` / `msdyn_effort` — horas planificadas (fuera de alcance v1)
- `msdyn_iscritical` — reservado para v2 (alertas de ruta crítica)
- `msdyn_ismilestone` — reservado para v2 (sección de hitos)

### 2.2 Consulta de buckets — `msdyn_projectbuckets`

```
GET /msdyn_projectbuckets
  ?$filter=_msdyn_project_value eq {project_id}
  &$select=msdyn_projectbucketid,msdyn_name
  &$top=50
```

Se usa para resolver el nombre del bucket a partir del GUID `_msdyn_projectbucket_value`.

### 2.3 Consulta de objetivos — `msdyn_projectgoals` (nueva en v1.1)

```
GET /msdyn_projectgoals
  ?$filter=_msdyn_projectid_value eq {project_id}
  &$select=msdyn_projectgoalid,msdyn_name,msdyn_goalstatus,msdyn_priority,msdyn_startdate,msdyn_enddate
  &$top=50
```

### 2.4 Consulta de lista de comprobación — `msdyn_projectchecklists` (v1.0)

```
GET /msdyn_projectchecklists
  ?$filter=_msdyn_projecttaskid_value in ({task_ids})
  &$select=msdyn_projectchecklistid,_msdyn_projecttaskid_value,msdyn_name,msdyn_projectchecklistcompleted,msdyn_projectchecklistorder
  &$orderby=msdyn_projectchecklistorder asc
  &$top=500
```

**Estructura de la tabla:**

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_projectchecklistid` | GUID | PK |
| `_msdyn_projecttaskid_value` | Lookup | Tarea padre (FK a `msdyn_projecttasks`) |
| `msdyn_name` | String | Texto del ítem de checklist |
| `msdyn_projectchecklistcompleted` | Boolean | ¿Ítem completado? |
| `msdyn_projectchecklistorder` | Decimal | Orden dentro de la tarea |

**Nota de disponibilidad:** Confirmado que la tabla existe en org914d3d16 (HTTP 200). El plan TI 2026 SÍ tiene ítems — verificado visualmente en Project for the web (ej: NORM-002.E4 tiene 2/3 completados). El research previo reportó 0 porque filtraba por `project_id` directo; los ítems se vinculan solo por `_msdyn_projecttaskid_value` sin campo de proyecto. La query debe filtrar por los GUIDs de tareas del plan.

**Nombre en UI:** "Lista de comprobación" (español) / "Checklist" (inglés).

### 2.5 Consulta de relaciones tarea-objetivo — `msdyn_projecttasktogoals` (nueva en v1.1)

```
GET /msdyn_projecttasktogoals
  ?$filter=_msdyn_projecttaskid_value in ({task_ids})
  &$select=msdyn_projecttaskid,msdyn_projectgoalid
```

Permite mostrar en la tabla de detalle a qué objetivo estratégico pertenece cada tarea accionable.

---

## 3. Estructura del reporte

### 3.1 Sección KPIs (cabecera)

Cuatro tarjetas de métricas resumen, alineadas con los KPIs del dashboard Power BI:

| KPI | Cálculo | Color |
|---|---|---|
| Total accionables | Count de tareas incluidas en el reporte (vencidas + en fecha + en curso) | Neutro |
| Vencidas | Count de tareas con categoría `VENCIDA` | Rojo |
| En curso | Count de tareas con categoría `EN CURSO` | Verde |
| Con nota de gestión | Count de tareas con `msdyn_description` no vacío | Azul |

Debajo de las tarjetas: indicador de **Salud Gestión** (`Buena` / `Regular` / `Baja`) calculado igual que en DAX: ≥80% con nota → Buena, ≥50% → Regular, <50% → Baja.

### 3.2 Tabla principal

**Filtros de inclusión:**

1. `statecode = 0` (solo registros activos)
2. Tiene tarea padre (`_msdyn_parenttask_value` no vacío) — es tarea hija
3. Avance < 100% (`msdyn_progress < 1.0`)
4. Bucket ≠ `Done`
5. Tiene fecha fin (`msdyn_scheduledend` no vacío)
6. Categoría resulta en `VENCIDA`, `EN FECHA`, o `EN CURSO` (ver sección 4)

**Sin agrupación** — lista plana ordenada por nivel de criticidad (ver sección 4.1).

**Columnas de la tabla, en orden aprobado:**

| # | Columna | Campo fuente | Formato |
|---|---|---|---|
| 1 | Código | `msdyn_subject` de tarea padre → regex `^([A-Z]{2,}-\d+)` | texto |
| 2 | Nombre tarea padre | `msdyn_subject` del padre | texto |
| 3 | Nombre tarea | `msdyn_subject` | texto |
| 4 | Responsable | `msdyn_pfwmodifiedby.fullname` | texto |
| 5 | Estado | Categoría + nivel criticidad | VENCIDA / EN FECHA / EN CURSO |
| 6 | Bucket | `msdyn_name` de `msdyn_projectbuckets` | texto |
| 7 | Notas | `msdyn_description` | texto completo |
| 8 | Lista de comprobación | `msdyn_projectchecklists` | `N/M` — vacío si no tiene ítems |
| 9 | Fecha inicio | `msdyn_scheduledstart` | DD-MM-YYYY |
| 10 | Fecha fin | `msdyn_scheduledend` | DD-MM-YYYY |
| 11 | Última actualización | `modifiedon` | DD-MM-YYYY |
| 12 | Sugerencia AI | `suggest_action()` — solo en CSV y tooltip HTML | texto |

**Ordenamiento — por nivel de criticidad:**

| Nivel | Condición | Orden secundario |
|---|---|---|
| 1 | Bloqueada (`Blocked`) + VENCIDA | días vencida desc |
| 2 | VENCIDA sin nota | días vencida desc |
| 3 | VENCIDA con nota | días vencida desc |
| 4 | EN FECHA hoy | — |
| 5 | EN CURSO, vence en ≤3 días | días restantes asc |
| 6 | EN CURSO, vence en ≤7 días | días restantes asc |
| 7 | EN CURSO, resto | días restantes asc |

**Campos excluidos:**
- Horas (`msdyn_effortremaining`) — fuera de alcance v1
- ID interno de tarea — solo para debug

### 3.3 Sección de alertas

Bloque de resumen debajo de la tabla, con tres sub-secciones:

**3.3.1 Escalamiento crítico**

Condición: tarea `VENCIDA` + bucket `Blocked` + días vencida > 14 días.

Formato por ítem:
```
⚠ [Nombre tarea]
   Responsable: [nombre] | Vencida hace N días | Motivo: Bloqueada sin resolución
```

**3.3.2 Cambios de bucket sugeridos**

| Situación | Sugerencia |
|---|---|
| `VENCIDA` + tiene nota + bucket ≠ `Blocked` | Mover a `Blocked` |
| bucket = `Blocked` + nota contiene palabras clave de desbloqueo | Mover a `In Progress` |

Palabras clave de desbloqueo: `desbloqueado`, `resuelto`, `solucionado`, `confirmó`, `confirmado`, `entrega confirmada`, `aprobado`, `liberado`, `listo para continuar`.

**3.3.3 Carga por responsable**

Tabla de responsables con count de tareas accionables y count de vencidas.

### 3.4 Sección de contexto estratégico (v1.1 — requiere consulta `msdyn_projectgoals`)

Lista de objetivos del plan con su estado y tareas accionables asociadas. Permite vincular la vista operativa con los OKRs del portafolio.

| Campo | Fuente |
|---|---|
| Nombre del objetivo | `msdyn_projectgoal[Goal Name]` |
| Estado | `msdyn_projectgoal[msdyn_status]` |
| Prioridad | `msdyn_projectgoal[msdyn_priority]` |
| Tareas accionables asociadas | Cruce con `msdyn_projecttasktogoal` |

---

## 4. Lógica de categorización

Implementada en `classify_task()` en `scripts/plan_report.py`.

| Categoría | Condición | Prioridad |
|---|---|---|
| `VENCIDA` | `msdyn_scheduledend.date() < hoy.date()` | 1 (más urgente) |
| `EN FECHA` | `msdyn_scheduledend.date() == hoy.date()` | 2 |
| `EN CURSO` | `msdyn_scheduledstart.date() <= hoy.date()` (y fin futuro) | 3 |
| Excluida | Ninguna condición cumplida (inicio futuro) | — |

**Importante:** La tarea se excluye si ya está completada (`msdyn_progress >= 1.0`), está en bucket `Done`, no tiene fecha fin, o `statecode ≠ 0`.

---

## 5. Lógica de Acción Sugerida

Implementada en `suggest_bucket()` en `scripts/plan_report.py`.

| Condición | Acción sugerida | Mensaje |
|---|---|---|
| VENCIDA + tiene nota + bucket ≠ Blocked | Cambiar bucket | `"Tarea vencida con nota de gestión — mover a Blocked"` |
| Blocked + nota con keywords de desbloqueo | Cambiar bucket | `"Nota sugiere resolución — mover a In Progress"` |
| VENCIDA + sin nota + días vencida > 14 | Escalar | `"Sin gestión registrada — escalar con responsable"` |
| VENCIDA + Blocked + días vencida > 14 | Escalar | `"Bloqueada y vencida hace N días — escalar"` |
| Ninguna condición | vacío | — |

---

## 6. Output

> **Orden de entrega:** CSV primero (validar datos reales) → HTML después (frontend).

### 6.2 HTML — Segundo entregable (después de validar CSV)

Archivo generado: `reporte_pmo_YYYY-MM-DD.html`

- Autocontenido (CSS y JS inline, sin dependencias externas)
- Renderizable en cualquier navegador sin servidor
- Lista plana ordenada por nivel de criticidad (sin agrupación por proyecto)
- Notas completas visibles en la columna — sin truncado
- **Sugerencia AI:** ícono `[AI]` en la fila; tooltip al pasar el mouse con el texto de `suggest_action()` — no ocupa columna propia
- Paleta de colores por nivel: rojo niveles 1-3, naranja nivel 4, verde niveles 5-7
- Responsive: adaptable a pantalla y a impresión (CSS `@media print`)

### 6.1 CSV — Primer entregable (antes del HTML)

Archivo generado: `reporte_pmo_YYYY-MM-DD.csv`

Columnas en orden de aparición:

| Columna | Fuente |
|---|---|
| `Codigo` | Código extraído del nombre padre (regex `^[A-Z]{2,}-\d+`) |
| `TareaPadre` | Nombre completo de la tarea padre |
| `Tarea` | Nombre de la tarea hija |
| `Responsable` | `msdyn_pfwmodifiedby.fullname` |
| `Estado` | `VENCIDA` / `EN FECHA` / `EN CURSO` |
| `NivelCriticidad` | 1–7 según tabla sección 3.2 |
| `Bucket` | Nombre del bucket |
| `Nota` | Texto completo de `msdyn_description` |
| `Checklist` | `N/M` — vacío si no tiene lista de comprobación |
| `FechaInicio` | `msdyn_scheduledstart` formateado DD-MM-YYYY |
| `FechaFin` | `msdyn_scheduledend` formateado DD-MM-YYYY |
| `UltimaActualizacion` | `modifiedon` formateado DD-MM-YYYY |
| `SugerenciaAI` | Texto de `suggest_action()` — columna de validación |

Encoding: UTF-8 con BOM (`utf-8-sig`) para compatibilidad con Excel.

---

## 7. Dependencias técnicas

| Componente | Requerimiento |
|---|---|
| Python | ≥ 3.11 (usa `str | None` type hints) |
| Azure CLI (`az`) | Autenticado con `dmorales@grupoebi.cl` en tenant `b16beb2c-...` |
| Librerías Python | Solo stdlib: `json`, `csv`, `subprocess`, `urllib`, `re`, `datetime`, `argparse`, `io` |
| Permisos Dataverse | Lectura en `msdyn_projecttask`, `msdyn_projectbuckets`, `msdyn_project`, `msdyn_projectgoal`, `msdyn_projecttasktogoal`, `msdyn_projectchecklists` |
| Red | Acceso a `https://org914d3d16.crm.dynamics.com` |

**Sin dependencias de pip** — el script es self-contained.

### Modo caché

Para evitar consultar Dataverse en cada ejecución durante el análisis:

```bash
# Primera ejecución: guarda caché
python scripts/plan_report.py --plan "Planificación área TI 2026" --cache-file %TEMP%\ti.json

# Ejecuciones siguientes: usa caché
python scripts/plan_report.py --from-cache %TEMP%\ti.json --out report.csv
```

---

## 8. Issues relacionados

| Issue | Descripción | Estado |
|---|---|---|
| #13 | Correcciones cockpit Power BI | En curso (branch actual) |
| — | Agregar columna FechaInicio al CSV output | Pendiente — no está en `write_csv()` actual |
| — | Output HTML del reporte PMO | Pendiente — actualmente solo stdout + CSV |
| — | Integrar `msdyn_projectgoal` al reporte | Pendiente — v1.1 |
| — | Usar `msdyn_outlinelevel` para detectar hijas | Pendiente — más robusto que filtrar por `_msdyn_parenttask_value` |

---

## Apéndice A — Parámetros CLI actuales (`plan_report.py`)

```
python scripts/plan_report.py
  --plan "nombre parcial"         # busca en msdyn_projects
  --project-id <GUID>             # GUID exacto del proyecto
  --from-cache <archivo.json>     # carga desde caché sin consultar Dataverse
  --cache-file <archivo.json>     # guarda caché tras la extracción
  --today YYYY-MM-DD              # fecha de referencia (default: hoy)
  --out <ruta.csv>                # exporta CSV adicional
```

## Apéndice B — Buckets estándar

| Bucket | Semántica |
|---|---|
| `To Do` | Pendiente de iniciar |
| `In Progress` | En ejecución activa |
| `Blocked` | Bloqueada — requiere gestión externa |
| `Done` | Completada — excluida del reporte |

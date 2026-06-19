# Diccionario de Datos — Plan Lineamiento Estratégico 2026

**Fuente:** Dataverse `org914d3d16.crm.dynamics.com`
**Tenant:** `b16beb2c-1c93-4497-bc75-5a1cdae6ee6c`
**API base:** `https://org914d3d16.crm.dynamics.com/api/data/v9.2`
**Última revisión:** 2026-06-19

---

## 1. Fuente Principal: Dataverse `msdyn_projecttask`

Entidad central del modelo. Contiene todas las tareas del proyecto (padres e hijas).

### 1.1 Campos extraídos actualmente (`plan_report.py`)

| Campo OData | Alias en reporte | Tipo | Descripción | Ejemplo |
|---|---|---|---|---|
| `msdyn_projecttaskid` | — | GUID (string) | Identificador único de la tarea | `3fa85f64-5717-...` |
| `msdyn_subject` | `subject` / TaskName | string | Nombre completo de la tarea; contiene el código al inicio | `NORM-001 Revisión normativa` |
| `msdyn_progress` | `progress` | double [0.0–1.0] | Porcentaje de avance (0.0 = 0%, 1.0 = 100%) | `0.75` |
| `msdyn_scheduledstart` | `start_dt` | datetime ISO 8601 UTC | Fecha de inicio planificada | `2026-03-01T00:00:00Z` |
| `msdyn_scheduledend` | `end_dt` / FechaFin | datetime ISO 8601 UTC | Fecha de fin planificada | `2026-06-30T00:00:00Z` |
| `modifiedon` | `mod_str` / UltimaActualizacion | datetime ISO 8601 UTC | Última modificación del registro en Dataverse | `2026-06-12T14:32:00Z` |
| `msdyn_descriptionplaintext` | `nota` | string | Notas/descripción de la tarea en texto plano | `Pendiente de firma...` |
| `_msdyn_parenttask_value` | `parent_id` | GUID (string) | FK a la tarea padre | `a1b2c3d4-...` |
| `_msdyn_projectbucket_value` | `bucket_id` | GUID (string) | FK al bucket de la tarea | `e5f6a7b8-...` |
| `statecode` | — | int | Estado del registro (0 = Activo, 1 = Inactivo) | `0` |
| `msdyn_pfwmodifiedby` | `responsable` | lookup expandido | Usuario Project for the Web que modificó la tarea; se expande con `$expand=msdyn_pfwmodifiedby($select=fullname)` | `{fullname: "Diego Morales C"}` |

**Tabla de buckets** (entidad separada `msdyn_projectbuckets`):

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_projectbucketid` | GUID | ID del bucket |
| `msdyn_name` | string | Nombre: `To Do`, `In Progress`, `Blocked`, `Done` |

### 1.2 Campos disponibles en Dataverse (no usados actualmente)

Disponibles en `msdyn_projecttasks` vía OData `$select`:

| Campo OData | Tipo | Descripción | Relevancia para reporte |
|---|---|---|---|
| `msdyn_scheduledstart` | datetime | Inicio planificado (ya extraído, pero no expuesto en CSV) | Alta — mostrar como "Fecha inicio" |
| `msdyn_percentcomplete` | double | Alias legacy de avance (ver también `msdyn_progress`) | Media — confirmar si difiere de `msdyn_progress` |
| `msdyn_description` | string (HTML) | Notas de la tarea en formato HTML | Baja — usar `msdyn_descriptionplaintext` |
| `msdyn_outlinelevel` | int | Nivel jerárquico en el WBS (1 = padre raíz, 2 = hijo) | Alta — útil para filtrar solo hijos accionables |
| `msdyn_ismilestone` | boolean | Indica si la tarea es un hito | Media — para dashboard de hitos |
| `msdyn_iscritical` | boolean | Tarea en ruta crítica del proyecto | Alta — señal de escalamiento |
| `msdyn_summary` | boolean | True si es tarea resumen (padre con hijos) | Alta — alternativa a la lógica `IsParentTask` actual |
| `msdyn_priority` | int | Prioridad de la tarea (1 = alta, etc.) | Media — filtro de alertas |
| `msdyn_effortremaining` | double | Esfuerzo restante en horas | Excluida de esta versión (ver SDD) |
| `msdyn_start` | datetime | Fecha inicio efectiva (vs planificada) | Alta — para detectar desvíos |
| `msdyn_finish` | datetime | Fecha fin efectiva (vs planificada) | Alta — para detectar desvíos |
| `msdyn_pfwcreatedby` | lookup | Usuario que creó la tarea en Project for the Web | Baja |
| `msdyn_projectsprint` | GUID | Sprint asignado (si se usa metodología ágil) | Baja |

---

## 2. Tablas Dataverse adicionales disponibles

### 2.1 `msdyn_projectbuckets` (ya en uso)

Entidad de buckets del proyecto. Campos clave:

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_projectbucketid` | GUID | ID único |
| `msdyn_name` | string | Nombre del bucket (`To Do`, `In Progress`, `Blocked`, `Done`) |
| `_msdyn_project_value` | GUID | FK al proyecto |

### 2.2 `msdyn_projectgoal` (disponible, no usado)

Objetivos estratégicos definidos en el proyecto. Puede vincular tareas a metas del plan.

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_projectgoalid` | GUID | ID único del objetivo |
| `msdyn_name` / `Goal Name` | string | Nombre del objetivo estratégico |
| `Goal Description` | string | Descripción del objetivo |
| `Start Date` | datetime | Inicio del objetivo |
| `End Date` | datetime | Fin del objetivo |
| `msdyn_status` | int | Estado del objetivo |
| `msdyn_priority` | int | Prioridad |
| `msdyn_colorindex` | int | Color asignado (1–N) |
| `_msdyn_projectid_value` | GUID | FK al proyecto |

### 2.3 `msdyn_projecttasktogoal` (disponible, no usado)

Tabla de relación N:N entre tareas y objetivos estratégicos.

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_projecttasktogoalid` | GUID | ID de la relación |
| `msdyn_projectgoalid` | GUID | FK al objetivo |
| `msdyn_projecttaskid` | GUID | FK a la tarea |
| `msdyn_taskdisplayorder` | int | Orden de visualización |

### 2.4 `msdyn_resourceassignment` (en modelo PBI, no en script Python)

Asignaciones de recursos a tareas. Es la fuente de la medida `Responsable` en el modelo Power BI.

| Campo | Tipo | Descripción |
|---|---|---|
| `msdyn_taskid` | GUID | FK a la tarea |
| `Name` | string | Nombre del recurso asignado |
| `msdyn_userresourceid` | GUID | FK al usuario |
| `Start` | datetime | Inicio de la asignación |
| `Finish` | datetime | Fin de la asignación |
| `Total Effort (Hours)` | double | Esfuerzo total planificado |
| `Effort Remaining (Hours)` | double | Esfuerzo restante |

### 2.5 `msdyn_projectchecklists` (disponible, no en modelo)

Ítems de checklist dentro de una tarea. No está en el modelo Power BI actual ni en el script.

### 2.6 `msdyn_projectlabels` (disponible, no en modelo)

Etiquetas del proyecto. No está en el modelo Power BI actual ni en el script.

---

## 3. Modelo Power BI (`vcv.pbix`)

**Extracción:** pbi-tools extract → directorio `vcv/`
**Conexión:** Power BI Service (DirectQuery/Import) — `DatasetId: 4def201a-3c6e-445c-ac40-cf053440183b`

### 3.1 Tablas del modelo

| Tabla | Origen | Descripción |
|---|---|---|
| `msdyn_projecttask` | Dataverse | Tabla principal de tareas |
| `msdyn_project` | Dataverse | Información de los proyectos (93 columnas) |
| `msdyn_resourceassignment` | Dataverse | Asignaciones recurso-tarea (fuente de responsables) |
| `msdyn_projectgoal` | Dataverse | Objetivos estratégicos del proyecto |
| `msdyn_projecttasktogoal` | Dataverse | Relación N:N tarea-objetivo |
| `ScopeFilter` | Tabla calculada | Filtro de alcance: `{ScopeValue, ScopeLabel}` — controla si el KPI aplica a "activo hoy" o "todo" |
| `DateTableTemplate_*` | Auto | Tabla de fechas generada por Power BI |
| `LocalDateTable_*` (múltiples) | Auto | Tablas de fecha locales por columna datetime (generadas automáticamente) |

### 3.2 Columnas calculadas en `msdyn_projecttask`

| Columna | Tipo | Fórmula (resumen) |
|---|---|---|
| `TaskCode` | string | Extrae el código del nombre (ej: `NORM-001`). Busca primer token con guión y >3 chars |
| `Pillar` | string | Clasifica por prefijo: `NORM` / `INTER` / `PORT` / `""` (sin código) |
| `PercentComplete` | double | Normaliza `% Complete`: si ≤1 multiplica ×100; clampea [0, 100] |
| `DaysSinceUpdate` | int | `DATEDIFF(Modified On, TODAY(), DAY)` |
| `HasUpdate` | boolean | `DaysSinceUpdate <= 7` |
| `IsOverdue` | boolean | Finish < TODAY() y PercentComplete < 100 |
| `IsAtRisk` | boolean | Finish entre hoy y hoy+7 días, y sin actualización reciente |
| `IsActiveToday` | boolean | Start ≤ TODAY() ≤ Finish |
| `IsLive` | boolean | Pct < 100, tiene TaskCode, tiene Finish, y (activa hoy o ya vencida) |
| `IsParentTask` | boolean | TaskCode no contiene punto (ej: `NORM-001`, no `NORM-001.1`) |
| `ParentTaskCode` | string | Extrae la parte antes del primer punto en TaskCode |
| `DaysOverdue` | int | Días desde Finish hasta hoy (si IsOverdue, sino 0) |
| `DueDateTexto` | string | `FORMAT(Due Date, "DD/MM/YYYY")` |
| `StartDateTexto` | string | `FORMAT(Start Date, "DD/MM/YYYY")` |
| `Status` | string | `VENCIDA_SIN_ACTUALIZACION` / `EN_RIESGO` / `CON_ACTUALIZACION` / `SIN_ACTUALIZACION` |
| `Estado Padre` | string | Agrega el estado de todas las hijas de una tarea padre |
| `Actualizado` | string | `"SI"` si HasUpdate, sino `"NO"` |
| `Atraso` | int | Días de atraso para tareas "In Progress" vencidas |

### 3.3 Medidas DAX

| Medida | Página | Formato | Descripción |
|---|---|---|---|
| `% Avance Promedio` | CockPit (KPI) | `0.0` | `AVERAGEX` sobre tareas con TaskCode, filtrada por scope (activas hoy si ScopeFilter=1) |
| `Tareas Vivas` | CockPit (KPI) | `0` | `COUNTROWS` de tareas IsLive && IsParentTask |
| `Tareas Vivas Vencidas` | CockPit | `0` | `COUNTROWS` de tareas IsLive && IsOverdue |
| `Vencidas Sin Actualizacion` | CockPit (KPI) | `0` | Padres con Estado Padre = `VENCIDA_SIN_ACTUALIZACION`, vivos |
| `En Riesgo` | CockPit (KPI) | `0` | Padres con Estado Padre = `EN_RIESGO`, vivos |
| `En Riesgo Sin Actualizacion` | CockPit | `0` | Tareas IsLive con Status = `EN_RIESGO` |
| `Salud Gestión` | CockPit (KPI) | texto | `Buena` ≥80% actualizadas / `Regular` ≥50% / `Baja` |
| `Total Tareas` | CockPit (gráfico) | `0` | Tareas con TaskCode, IsLive, IsParentTask |
| `Tareas Con Actualizacion` | — | `0` | Tareas con TaskCode, HasUpdate, filtradas por scope |
| `Tareas Sin Actualizacion` | — | `0` | `Total Tareas - Tareas Con Actualizacion` |
| `Tareas Completadas` | — | `0` | PercentComplete ≥ 100, filtradas por scope |
| `% Tareas Completadas` | — | `0.0` | `DIVIDE(Tareas Completadas, Total Tareas)` |
| `% Con Actualizacion` | — | `0.0` | `DIVIDE(Tareas Con Actualizacion, Total Tareas)` |
| `Tareas Vencidas` | — | `0` | Tareas con TaskCode, IsOverdue, filtradas por scope |
| `Dias Promedio Atraso` | — | `0.0` | `AVERAGEX` de DaysOverdue sobre vencidas |
| `Total NORM` | — | `0` | Tareas con Pillar = `NORM`, filtradas por scope |
| `Total PORT` | — | `0` | Tareas con Pillar = `PORT`, filtradas por scope |
| `Total INTER` | — | `0` | Tareas con Pillar = `INTER`, filtradas por scope |
| `Vencidas Sin Act NORM` | — | `0` | Vencidas sin actualización en pilar NORM |
| `Vencidas Sin Act PORT` | — | `0` | Vencidas sin actualización en pilar PORT |
| `Vencidas Sin Act INTER` | — | `0` | Vencidas sin actualización en pilar INTER |
| `Responsable` | Tabla detalle | texto | `FIRSTNONBLANK` de `msdyn_resourceassignment[Name]` para el task ID |
| `Nombre Tarea Padre` | Tabla detalle | texto | Lookup del nombre de la tarea padre por `msdyn_parenttask` |
| `Ultima actualizacion` | Tabla detalle | texto | `DaysSinceUpdate` en texto: "Hoy", "Ayer", "N días" |
| `NombreProyecto` | — | texto | Nombre del proyecto raíz de la tarea |
| `NotasTarea` | — | texto | `SELECTEDVALUE(Notes - Plain text)` |
| `TareasFinalizadas` | — | `0` | Hijas finalizadas del contexto de fila actual |
| `% Avance Proyecto` | — | `0.0` | Promedio de PercentComplete de hijas (no padres) |
| `_Gauge Max` | — | `0` | Constante `100` para gauge visual |
| `_ScopeSelected` | — | `0` | `SELECTEDVALUE(ScopeFilter[ScopeValue], 0)` — controla scope del filtro |

### 3.4 Relaciones del modelo

| Desde | Hasta | Cardinalidad |
|---|---|---|
| `msdyn_projecttasktogoal[msdyn_projectgoalid]` | `msdyn_projectgoal[Project Goal]` | N:1 (single) |
| `msdyn_project.*` (columnas datetime) | `LocalDateTable_*[Date]` | N:1 (auto) |
| `msdyn_projecttask.*` (columnas datetime) | `LocalDateTable_*[Date]` | N:1 (auto) |
| `msdyn_resourceassignment.*` (datetime) | `LocalDateTable_*[Date]` | N:1 (auto) |
| `msdyn_projectgoal.*` (datetime) | `LocalDateTable_*[Date]` | N:1 (auto) |

> Nota: No existe una relación explícita entre `msdyn_projecttask` y `msdyn_resourceassignment` en el modelo. La medida `Responsable` usa `CALCULATE` + `ALLEXCEPT` para navegar la unión por `Project task ID`.

---

## 4. Notas de implementación

### Lógica de categorización (Python vs DAX)

| Categoría Python | Equivalente DAX | Criterio |
|---|---|---|
| `VENCIDA` | `IsOverdue = TRUE` | `msdyn_scheduledend < hoy` y avance < 100% |
| `EN FECHA` | `IsOverdue = FALSE && Finish = TODAY()` | La fecha fin es exactamente hoy |
| `EN CURSO` | `IsActiveToday = TRUE` | Inicio ≤ hoy ≤ fin |

### Campos de fecha: diferencia `scheduledend` vs `Due Date`

El modelo Power BI expone dos campos de fecha fin:
- `Finish` (columna OData `msdyn_finish`): fecha de fin efectiva del motor de scheduling
- `Due Date` (columna OData `msdyn_scheduledend`): fecha límite definida por el usuario

El script Python usa `msdyn_scheduledend`. El modelo DAX usa `Due Date` para las columnas calculadas y `Finish` para `IsOverdue` / `DaysOverdue`. Pueden diferir — es un punto de divergencia a monitorear.

### Campo `msdyn_pfwmodifiedby` vs `modifiedby`

- `msdyn_pfwmodifiedby`: usuario Project for the Web (quien actualizó la tarea en la interfaz de planificación) — es el "responsable funcional"
- `modifiedby`: usuario de sistema Dataverse (puede ser un servicio automatizado)

El script Python expande `msdyn_pfwmodifiedby($select=fullname)` como fuente del responsable.

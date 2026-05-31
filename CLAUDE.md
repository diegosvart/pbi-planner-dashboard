# Plan Lineamiento Estratégico 2026 — PBI

> Entry point del proyecto PM. El harness Aura vive en `aura-agent-kit/`.

<!-- aura:begin -->
@aura-agent-kit/CLAUDE.md
<!-- aura:end -->

---

## Contexto del Proyecto

**Objetivo:** Seguimiento y control del Plan de Lineamiento Estratégico 2026 de Cosemar.

**Dashboard:** `vcv.pbix` — Power BI conectado vía Dataverse a Microsoft Planner.

**Fuente de datos:** Dataverse `org914d3d16.crm.dynamics.com` → tabla `msdyn_projecttask`

**Dataset remoto Power BI Service:**
- DatasetId: `4def201a-3c6e-445c-ac40-cf053440183b`
- ReportId: `5c475111-17b1-44b4-afaf-52e3041236d4`

---

## Estructura del Modelo de Datos

### Tabla principal: `msdyn_projecttask`

| Campo calculado | Lógica |
|----------------|--------|
| `TaskCode` | Extrae el código del nombre de tarea (ej: `NORM-001`) |
| `Pillar` | Clasifica por prefijo: `NORM`, `INTER`, `PORT`, `SIN_CODIGO` |
| `PercentComplete` | % de avance de la tarea |

### Métricas del dashboard (página CockPit)

| Métrica | Tipo | Descripción |
|---------|------|-------------|
| `% Avance Promedio` | KPI Card | Promedio de avance del portafolio |
| `Tareas Vivas` | KPI Card | Tareas activas en curso |
| `Vencidas Sin Actualizacion` | KPI Card | Tareas vencidas sin movimiento reciente |
| `En Riesgo` | KPI Card | Tareas en estado de riesgo |
| `Salud Gestión` | KPI Card | Indicador de salud general del portafolio |

### Tabla de detalle (CockPit)
Columnas: `TaskCode`, `Project Task Name`, `Responsable`, `DaysOverdue`, `Ultima actualizacion`, `DueDateTexto`, `PercentComplete`, `Actualizado`

### Gráfico de barras
Ejes: `Pillar` (eje X), `Total Tareas` (eje Y), segmentado por `Estado Padre`

---

## Pilares del Portafolio

| Código | Pilar |
|--------|-------|
| `NORM-` | Normativo |
| `INTER-` | Internacional |
| `PORT-` | Portafolio |
| `SIN_CODIGO` | Tareas sin código asignado |

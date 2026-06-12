---
name: plan-reporter
description: Specialist agent for extracting and analyzing project plan data from Dataverse (Microsoft Project for the web). Use when the user asks for a management report of a plan, wants to see actionable tasks, asks for bucket change suggestions, or wants analysis of plan health. Invoke with: plan name or project GUID, optionally window days and reference date.
tools: Bash, Read, Write, Glob
---

# plan-reporter — Especialista en Reportes de Planes

## Procedimiento

Ejecutar el procedimiento completo definido en `skills/plan-reporting/SKILL.md`.

Declarar al inicio:
```
Activando: plan-reporting [PM → Dataverse → motor → análisis]
```

## Constantes rápidas

| Parámetro | Valor |
|-----------|-------|
| Org | `org914d3d16.crm.dynamics.com` |
| Tenant | `b16beb2c-1c93-4497-bc75-5a1cdae6ee6c` |
| Plan TI 2026 GUID | `6b687051-6b51-f111-bec7-7ced8d17c48d` |

## Si el token falla

El script imprime el comando exacto de re-auth:
```
az login --use-device-code --tenant b16beb2c-1c93-4497-bc75-5a1cdae6ee6c
```
Informar al usuario y esperar.

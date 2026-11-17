---
title: ConductLoop — tarea B desde el chat (tope de sesiones, F9)
type: prompt
created: 2026-11-16
status: lista (se pega en el chat con la 1.2.32 instalada)
tags: [conductloop, prompt, dogfooding]
---

> Segunda tarea real para ConductLoop sobre sí mismo, la primera con Claude Code dentro de la sesión (1.2.32). Arregla F9 de [[projects/conductloop/friccion|friccion]]. Se pega tal cual en el chat.

---

El tope de sesiones de ConductLoop cuenta sesiones que no hacen nada. Arréglalo.

## Qué pasa
- `ops_dashboard/sessions/limits.py:19`: `ACTIVE_SESSION_STATUSES` es todo estado que no sea `closed` ni `failed`.
- `ops_dashboard/sessions/store.py:215` (`create_session`) y `ops_dashboard/sessions/browser.py:532-535` cuentan esas sesiones contra el tope (`DEFAULT_MAX_ACTIVE_SESSIONS = 3`, `limits.py:7`).
- Resultado: una sesión creada y nunca arrancada, una que ya terminó su tarea (la tarea en `needs_review` y la sesión en `idle`) o una que espera a una persona ocupan un hueco para siempre. El 16-nov la primera tarea del chat no arrancó por eso («3 active sessions; limit is 3»), con tres sesiones vacías. Y como cada tarea del chat deja su sesión en `idle` al terminar, **a la cuarta tarea el chat se bloquea**.

## Arreglo
- El tope cuenta solo las sesiones **con trabajo**:
  - las que están en `provisioning`, `running` o `closing`;
  - y las demás no terminales (`created`, `idle`, `paused`, `stalled`, `waiting_human`) solo si tienen una tarea en `queued`, `claimed` o `running` asignada o en propiedad.
- Una sesión sin tareas, o cuyas tareas están en `needs_review`, `done`, `blocked`, `failed` o `cancelled`, **no cuenta**.
- El mismo criterio en `store.py` y en `browser.py`: una sola función en el store para las dos (por ejemplo `busy_sessions(conn)`), dentro de la misma transacción, para que siga siendo atómico.
- La concurrencia real la sigue limitando el supervisor (`max_running` en `supervisor.py`): no la toques.
- El mensaje de error dice qué hacer: «N sessions are working; the limit is M. Finish or close one in Sessions.»

## Tests (con nombres inventados)
- Tres sesiones creadas sin tareas → se puede crear una cuarta.
- Tres sesiones con una tarea en `queued` o `running` → la cuarta da `SessionLimitError`.
- Una sesión cuya tarea está en `needs_review` no cuenta.
- `tests/test_sessions_store.py::test_session_limit_is_atomic_under_concurrency` sigue pasando.
- Si algún test existente asumía el criterio viejo (`tests/test_sessions_store.py:58` y `:68`, `tests/test_sessions_api.py:224-233`), actualízalo solo lo necesario y explica en un comentario por qué.
- Comprueba que los tests nuevos **fallan** con el código de antes.

## CHANGELOG
Una línea en `## Unreleased` de `CHANGELOG.md` (créala encima de la última versión si no existe): las sesiones que no tienen trabajo ya no ocupan hueco.

## Reglas
- Solo nombres inventados (Mosaic, Tidepool, Acme): el repo es público.
- No toques `desktop/`, `.github/` ni los scripts de publicación. No subas la versión.
- Antes de terminar: `pytest tests/test_sessions_store.py tests/test_sessions_api.py tests/test_browser_agent.py tests/test_sessions_supervisor.py` y `ruff check .` en verde.

## Informe (en español, al final)
Qué estaba mal, qué cambiaste (ficheros y líneas), qué tests añadiste y si los viste fallar antes del arreglo, y lo que no pudiste hacer.

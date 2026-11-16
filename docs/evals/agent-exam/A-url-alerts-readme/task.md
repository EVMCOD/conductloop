---
title: ConductLoop — primera tarea hecha desde ConductLoop (sesión de código)
type: prompt
created: 2026-11-16
status: lista (1.2.31: se pega en el chat)
tags: [conductloop, prompt, dogfooding]
---

> Primera tarea que ConductLoop hace sobre su propio código. Se lanza desde una **sesión de código** de ConductLoop sobre el repo `EVMCOD/conductloop` (base `main`), con MiniMax-M3 por BYOK.
> Objetivo doble: arreglar dos fallos reales y probar el ciclo entero: el agente edita, prueba y hace commit en su rama `cl/…`; Claude o Enrique la suben, abren la PR, esperan el CI y fusionan.
> Lo que moleste al usarlo va a [[projects/conductloop/friccion|friccion]]. Contexto: [[projects/conductloop/mejoras-2026-11-15|mejoras]] · [[daily/2026-11-16]].

---

Trabajas en el código de ConductLoop dentro de una sesión de código: una copia del repo en tu propia rama `cl/…`. Puedes leer, buscar, editar y pasar tests y lint (`pytest`, `ruff check`); el commit lo hace la sesión sola al terminar. **No puedes** subir cambios, abrir PRs, instalar nada ni usar la red, y no hace falta: al terminar, otra persona sube tu rama y abre la PR. Código, comentarios, commits y CHANGELOG en inglés. El informe final, en español.

Hay dos puntos, en este orden.

## P1 · El README manda a descargas de hace seis versiones
- `README.md:16-17`: las filas de Linux y Windows enlazan a `releases/download/v1.2.23/…`, pero la versión actual es la de `pyproject.toml` (1.2.29 o posterior). La fila del Mac no tiene el problema: usa `releases/latest/download/ConductLoop-macos-aarch64.dmg`, un nombre que no cambia.
- Haz que el README no pueda quedarse atrás: enlaza Linux y Windows a algo que no dependa de la versión. Por ejemplo, la página de la última release (`https://github.com/EVMCOD/conductloop-releases/releases/latest`) o `https://conductloop.com/download`. **No inventes nombres de fichero estables que la release no publica:** mira qué ficheros publica `scripts/release-platforms.sh`.
- Hoy, en `docs/`, solo aparece `docs/PORTABILITY.md:770`, y es una plantilla (`v<version>`), no un enlace roto: déjala.
- Test guardián nuevo: ni `README.md` ni `docs/**/*.md` pueden contener un enlace `releases/download/v<número>.<número>.<número>/` con números de verdad (la plantilla `v<version>` no cuenta). Comprueba que el test **falla** con el README de antes y pasa con el nuevo.

## P2 · Un solo timeout no es una web caída
- Qué pasa: `ops_dashboard/indexers/anomalies.py:280-299` crea una alerta `url_health` por cada fila que devuelve `live_urls.recent_unhealthy()` (`ops_dashboard/indexers/live_urls.py:249`). Esa función lee `live_urls`, que guarda **solo la última comprobación** de cada URL. Si esa única comprobación falla (timeout de 10 s en `_check`, línea 101), la alerta sale con gravedad **alta** («URL … no responde»).
- Lo que pasó el 16-nov: el digest de las 08:00 dijo que traincurve.com y www.rentcla.com «no respondieron», y la comprobación siguiente, 5 minutos después, salió bien. En el log de la vigilancia hay 2 pasadas con errores entre 636.
- Arreglo: una URL solo cuenta como caída cuando **sus 2 últimas comprobaciones** fallan. El historial está en `live_url_checks` (lo escribe `live_urls.py`, alrededor de la línea 159). Mantén la gravedad que hay ahora: alta sin respuesta, media con un código HTTP de error. Corrige el docstring de `recent_unhealthy`: dice «at least one failed check in the last N hours» y no es lo que hace.
- Tests, con URLs y proyectos inventados (`mosaic.example`, `tidepool.example`):
  - una comprobación fallida seguida de una buena → sin alerta;
  - dos fallidas seguidas → alerta alta sin respuesta, media con 500;
  - una buena seguida de una fallida → sin alerta todavía;
  - una URL que solo se ha comprobado una vez, y falló → sin alerta. Explica en un comentario por qué es aceptable: la comprobación se repite cada 5 minutos.
- Añade una línea a la sección `## Unreleased` de `CHANGELOG.md` (créala encima de la última versión si no existe): un timeout suelto ya no se anuncia como web caída.

## Reglas
- **Solo nombres inventados** en código, tests y textos (Mosaic, Tidepool, Acme, `*.example`). Nunca nombres de empresas, webs o personas reales: el repo es público.
- No toques `desktop/`, `.github/` ni los scripts de publicación. No subas la versión.
- En la ventana nunca `alert`/`confirm`/`prompt`, y el tope de `style=` en línea de `tests/test_csp_static.py` se respeta (aunque estos dos puntos no deberían tocar la web).
- Antes de dar por terminado cada punto: los tests que tocas y los de alrededor (`tests/test_anomalies.py`, `tests/test_anomaly_language.py`, `tests/test_live_url_history.py` y el test nuevo del README) más `ruff check .` en verde. La suite completa la pasa quien suba la rama.
- Si algo no se puede hacer dentro de la sesión (falta una herramienta, el sandbox bloquea un test que abre un servidor en 127.0.0.1…), **no lo rodees**: apúntalo en el informe tal cual, con la orden y el error.

## Informe (en español, como último mensaje)
Por cada punto: qué estaba mal, qué cambiaste (ficheros y líneas), qué tests añadiste y si los viste fallar antes del arreglo. Al final, una lista aparte de **todo lo que te costó en la sesión de código** (herramientas que faltaban, órdenes bloqueadas, tests que no podían correr): eso va a la nota de fricción.

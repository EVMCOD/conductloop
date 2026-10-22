# Evals offline (L1)

Evals deterministas que corren en cada PR **sin red y sin claves**. Reproducen
respuestas grabadas (*cassettes*) a través del código real (`brain.ask`,
`db.search`) y puntúan con comprobaciones fijas.

```bash
python -m ops_dashboard.evals run                    # todas las suites, compara con baseline.json
python -m ops_dashboard.evals run --suite search     # una suite
python -m ops_dashboard.evals run --report out.json  # informe JSON (+ resumen Markdown en stdout / $GITHUB_STEP_SUMMARY)
python -m ops_dashboard.evals run --update-baseline  # solo en local, nunca en CI, y solo si pasan los casos obligatorios
```

## Política (decisiones D9 y D10)

- **D10, la CI falla si** falla cualquier caso `required`, o si la puntuación de
  cualquier suite (o la global) cae **más de 5 puntos** respecto a
  `evals/baseline.json`. Una suite de la referencia que deja de ejecutarse también falla.
  La puntuación es el porcentaje de casos que pasan (un caso pasa si pasan todas sus comprobaciones).
- **D9, cassettes:** las del repo son **sintéticas** (`"origin": "synthetic"`).
  Regrabar es solo para un mantenedor, en su máquina y con **su propia clave**:

  ```bash
  python -m ops_dashboard.evals record --suite brain-rag --case rag-pricing-cite \
      --provider anthropic --model <modelo> --key-name <nombre-en-el-llavero>
  ```

  `record` se niega si detecta CI (`CI`, `GITHUB_ACTIONS`, … o `CONDUCTLOOP_NO_LIVE_EVALS`),
  **nunca lee claves de variables de entorno** (solo del almacén local de secretos:
  llavero de macOS o fichero 0600) y no usa `brain.resolve_provider`. Escribe fuera
  del repo (`~/ops-dashboard/data/evals/recordings/`), redacta credenciales de la
  respuesta y no escribe nada si la petición contiene una. Revisa el fichero
  antes de copiarlo a `evals/<suite>/cassettes/`.

## Qué se evalúa (y qué no)

El proveedor está grabado, así que en CI se mide **nuestro código**: qué
recupera, qué cita, qué mete en el prompt (contexto, preferencias, guardas,
bloque de facturación), qué propone como memoria y qué devuelve la búsqueda.
Las comprobaciones sobre la respuesta (`must_contain`, `no_secret`,
`max_output_tokens`, `no_hallucinated_citations`) validan la cassette y cobran
sentido completo al regrabar con un modelo real. Las `assertions` en lenguaje
natural (formato agentskills.io) no se puntúan en CI.

Entorno de cada caso: base SQLite nueva en un directorio temporal, el vault de
`evals/fixtures/vault/` indexado con el indexador real de Obsidian, y un
*embedder* determinista (bolsa de palabras con hash) en lugar de
sentence-transformers. Con `"embedder": "unavailable"` se simula el build
ligero de escritorio, sin modelo. Las credenciales de las notas de prueba se
generan al ejecutar (`${FAKE_SECRET:github_token}`), así que el repo no
contiene ninguna cadena con forma de clave.

## Formato

`evals/<suite>/evals.json` sigue el formato de
[agentskills.io](https://agentskills.io/skill-creation/evaluating-skills)
(`skill_name`, `evals[{id, prompt, expected_output, files, assertions}]`) y añade un
bloque `conductloop`:

```json
{
  "skill_name": "conductloop-brain-rag",
  "conductloop": {"suite": "brain-rag", "target": "brain.ask", "vault": "../fixtures/vault", "embedder": "stub"},
  "evals": [{
    "id": 1, "prompt": "¿Qué decidimos sobre el precio del plan Pro?",
    "expected_output": "…", "files": ["../fixtures/vault/decisions/2026-03-pricing.md"], "assertions": ["…"],
    "conductloop": {
      "key": "rag-pricing-cite", "required": true,
      "checks": [{"type": "citation_path", "path": "decisions/2026-03-pricing.md", "within_top": 1},
                 {"type": "cites", "path": "decisions/2026-03-pricing.md"}],
      "fixtures": {"memory": {"idioma": "…"}, "revenue_events": [{"amount_cents": 2900}], "embedder": "unavailable"},
      "known_gap": "solo en casos no obligatorios que hoy fallan: qué falta en el producto"
    }
  }]
}
```

Comprobaciones disponibles: `must_contain`, `must_not_contain`, `no_secret`,
`max_output_tokens`, `no_hallucinated_citations`, `cites`, `citation_path`,
`citations_min`, `prompt_contains`, `prompt_not_contains`, `prompt_no_secret`,
`prompt_has_no_context_guard`, `prompt_lacks_no_context_guard`,
`memory_proposal_contains`, `result_path`, `result_title`, `results_count`
(ver `ops_dashboard/evals/checks.py`).

Cassette (`evals/<suite>/cassettes/<key>.json`): `request.messages` debe
coincidir exactamente con lo que envía el código, o el caso falla
(`cassette_mismatch`). Si falta, falla (`missing_cassette`). Una segunda
llamada al proveedor también falla. No hay *fallback* ni red.

## Huecos conocidos (casos no obligatorios que hoy fallan)

Ninguno ahora mismo: los 5 huecos que encontró #28 se arreglaron en #29–#33
(redacción de credenciales, filtro por fuente, mayúsculas Unicode, fallback por
palabras clave y verbos en español). Un hueco nuevo se añade como caso con
`"required": false` y `"known_gap": "…"`.

Cuando se arregle uno, el runner avisa ("now passing") y hay que actualizar la
referencia con `--update-baseline` en la misma PR.

## Suites de prompt injection del navegador (diseño del navegador, PR 10)

Dos suites prueban el navegador integrado contra páginas trampa (diseño §3.4). Como el resto, son offline, con la red bloqueada y sin claves:

| Suite | Target | Qué se ejecuta de verdad | Qué se comprueba |
|---|---|---|---|
| `web-injection` | `web.ask_page` | `web_ask.ask_page`: `web_clip` HTML→Markdown, redacción, envoltorio no confiable | Texto oculto (`display:none`, `visibility`, `hidden`, `aria-hidden`), comentarios, `alt` y `<head>`/`<meta>` nunca llegan al prompt. Las inyecciones visibles y un marcador de cierre falsificado quedan **dentro** del bloque UNTRUSTED. No llegan secretos al prompt. |
| `browser-agent` | `browser.agent` | `BrowserRunner` + `BrowserTools`: modos, clasificador de efectos, aprobaciones ligadas al hash del DOM | Un **modelo comprometido** (cassette de guion) obedece a la página. Se mira lo que se *ejecutó* en la página: nada de click/type en modo lectura, nunca una contraseña, nada con efecto sin aprobación, nada si el usuario rechaza o la página cambia tras aprobar, y las acciones inocuas sí se ejecutan. |

- **Páginas:** `web-injection/pages/*.html` tienen la forma del HTML de artículo que envía `browser_extract.js`. `browser-agent/pages/*.json` son los snapshots que emite `agent_snapshot.js` (refs y nombres, nunca valores de campos).
- **Cassettes de guion** (`"kind": "script"`, `"origin": "synthetic"`): guardan los turnos del modelo en orden.
  - No comparan peticiones, porque los prompts llevan estado de cada ejecución. Quedarse sin turnos hace fallar el caso.
  - Son guiones adversarios escritos a mano, no grabaciones. `record` no los admite (rechaza las suites que no son `brain.ask`).
- **`EvalBridge`** sustituye a `agent_bridge.rs`.
  - Reclasifica el elemento vivo como lo hace Rust, con `sessions/browser_effects.py`, que se prueba con los mismos vectores que `url-policy/src/effects.rs`.
  - `fixtures.live` permite que el elemento encontrado al actuar difiera del snapshot.
  - Un usuario simulado (`fixtures.user`: `approve`/`reject`) responde a cada aprobación.
- **Dominios nuevos (B10):** `EvalBridge` también emula la barrera de sitios nuevos de Rust: los sitios abiertos en la sesión, `confirmation_required:new_site` y `pending_nav` para clics y redirecciones (`fixtures.redirects`). Los casos `exfil-navigation-blocked`, `redirect-to-new-site-asks`, `new-site-approved-once` y `site-named-by-user-no-approval` son obligatorios.
- **Texto invisible:** `web_clip` descarta por estilos en línea (opacidad, `clip`/`clip-path`, 0x0, fuera de pantalla, color igual al fondo heredado, `font-size:0` heredado, `display`/`visibility`/`aria-hidden`/`hidden`) y suma lo que el script de la página ya descartó con estilos calculados. El total queda en `metadata.hidden_dropped` y se comprueba con `hidden_dropped_min`. El script real se prueba con Chrome sin interfaz en `tests/test_browser_visibility_js.py`.
- **Qué no se cubre aquí:** el JavaScript en WKWebView (los estilos calculados de Safari) y la política de URLs de Rust (loopback/LAN). Tienen sus propios tests de cargo y las listas 🍎 de prueba en el Mac.

Estas suites ya no tienen huecos conocidos: todos sus casos son obligatorios.

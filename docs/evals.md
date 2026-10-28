# Evals live con tu clave (L2)

Las evals offline (`evals/README.md`, L1) miden **nuestro código** con respuestas
grabadas, sin red ni claves, en cada PR. Las evals **live** ejecutan los mismos
casos y las mismas comprobaciones contra un **proveedor real**, con **tu propia
clave BYOK**, **a mano y en tu Mac**. Sirven para ver cómo se comporta un modelo
concreto con nuestros prompts y para regrabar cassettes.

> Necesitan un checkout del repo: el arnés de evals no va dentro de la app empaquetada.

## Cómo se lanza

```bash
python -m ops_dashboard.evals run --live \
    --provider anthropic --model <modelo> --key-ref byok.anthropic.personal \
    --budget-eur 0.50 --max-calls 30 \
    [--suite brain-rag] [--judge] [--record] [--max-output-tokens 1024]
```

| Opción | Obligatoria | Qué hace |
|---|---|---|
| `--live` (o `CONDUCTLOOP_LIVE_EVALS=1`) | sí | Activa el modo live. Sin esto, `run` es offline, como siempre. |
| `--provider` | sí | `anthropic`, `openai`, `minimax`, `gateway` u `ollama`. **Sin fallback**: si está en *cooldown* o no se puede resolver, no arranca. |
| `--model` | sí | El precio va por modelo. |
| `--key-ref` | sí (salvo Ollama) | Nombre del elemento del llavero (`byok.<proveedor>.<etiqueta>`), **nunca la clave**. Las variables de entorno (`ANTHROPIC_API_KEY`, …) **no se usan nunca**. |
| `--budget-eur` | sí | Tope de gasto en EUR (máx. 20). |
| `--max-calls` | sí | Tope de llamadas al proveedor, juez incluido (máx. 500). |
| `--max-output-tokens` | no | Tope de salida por llamada (por defecto 1024); se usa también para proyectar el coste. |
| `--judge` | no | Puntúa las `assertions` en lenguaje natural con el mismo modelo, dentro del mismo presupuesto. Se informa aparte y **no cambia** el aprobado/suspenso. |
| `--record` / `--record-dir` | no | Escribe cassettes **redactadas** en `<datos>/evals/recordings/live-<fecha>/`. Se niega a escribir dentro del código del repo: copiarlas a `evals/<suite>/cassettes/` es una decisión humana tras revisarlas (D9). |
| `--out-dir` | no | Dónde va el informe (por defecto `<datos>/evals/live/`). |

## Reglas (todas con test en `tests/test_evals_live.py`)

- **Nunca en CI.** Si existe `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `BUILDKITE`,
  `CIRCLECI`, `TF_BUILD`, `JENKINS_URL`, `TEAMCITY_VERSION` o
  `CONDUCTLOOP_NO_LIVE_EVALS`, sale con código 3 aunque se pase `--live`. La CI
  solo prueba el código con un proveedor falso, y un test comprueba que ningún
  workflow lanza evals live.
- **Solo BYOK explícito.** El proveedor se resuelve como en las sesiones
  (`resolve_provider` estricto de #15) y la clave sale del llavero por `--key-ref`
  (#16). Lo que `resolve_provider` haya cogido de otro sitio se sobrescribe.
- **Precio obligatorio.** Se usa tu tabla `model_prices` (Sesiones → Precios, en
  céntimos de EUR por millón de tokens). Si falta el precio del modelo, no arranca
  (`price_missing`). Ollama es gratis.
- **Presupuesto.** Antes de cada llamada se proyecta el coste (entrada
  sobreestimada + `max_tokens`) y, si podría pasar del presupuesto, no se hace.
  Después se cobra el uso real (o estimado, marcado con `~`, si el proveedor no lo
  da). Al llegar al 100 % se corta. Un proveedor que ignore `max_tokens` puede
  pasarse como mucho en la llamada que ya estaba en curso.
- **Clave inválida o sin saldo** (`invalid_key`, `insufficient_credit`): se para
  la ejecución en la primera llamada.
- **Qué sale del Mac:** el prompt de cada caso y las notas del vault de pruebas
  (`evals/fixtures/vault`, sintético) van al proveedor elegido. Con un
  `--evals-dir` propio se avisa: no apuntes a notas personales.

## Qué se ejecuta

Solo los destinos de una llamada: `brain.ask` (suite `brain-rag`) y
`web.ask_page` (`web-injection`). `db.search` no llama a ningún modelo y
`browser.agent` reproduce guiones adversarios; ambas se listan como omitidas.

## Informe

- Mismo formato que el offline (`overall`, `suites`, `cases`) más `mode: "live"`,
  coste, llamadas y una **comparación caso a caso con `evals/baseline.json`**
  (`same`, `regressed`, `improved`, `new`, `not_run`) y la política D10 aplicada a
  los casos ejecutados.
- Se guarda en `<datos>/evals/live/<fecha>-live-<id>.md` y `.json` (redactados), y
  en las tablas locales `eval_runs` y `eval_results` (migración 25: guarda el
  **nombre** del `key_ref`, nunca la clave, ni prompts ni respuestas). La
  pestaña Evals de Aprendizaje ya lista esas ejecuciones.

Códigos de salida: `0` todo bien · `1` falla un caso obligatorio o cae la
puntuación más de 5 puntos frente a la baseline · `2` error de uso o de suite ·
`3` rechazada antes de llamar a nada · `4` cortada por presupuesto, límite de
llamadas o error de clave (informe parcial escrito).

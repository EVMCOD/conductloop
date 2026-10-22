# Eval cases for token / no-auth connectors (pending #28)

`docs/evals/token-*.json` are suites in the #28 format: one for the kit
(`token-kit.json`, fake connector) and one per connector (GitHub, Linear,
Todoist, RSS, Readwise), each added by its own PR. They stay in `docs/evals/`
for the same reason as `connectors-write-guard.json`: the #28 runner rejects
unknown targets.

Until #28 merges, `tests/test_token_eval_drafts.py` globs every
`token-*.json` and runs each case through `tests/token_fake.run_eval_case`:
recorded HTTP responses (`tests/fixtures/token/<connector>/*.json`), an
isolated DB, an in-memory keychain and no network. The answer is JSON with the
action result (or error code), what was ingested (path, title, `untrusted`)
and `upstream_writes` (requests that would have written to the provider).

## When #28 merges

1. `git mv docs/evals/token-<x>.json evals/token-<x>/evals.json` for each suite.
2. Add `"connectors.token"` to `TARGETS` in `ops_dashboard/evals/runner.py` and a
   branch in `_run_target` that calls `run_eval_case(case.params, Path(tmp_dir))`.
   Move `run_eval_case`, `Recorded` and `token_env` from `tests/token_fake.py`
   to `ops_dashboard/evals/token_case.py`, and ship the recorded fixtures with it.
3. Add the cases to `evals/baseline.json` as `true` (all `required`).

# Eval case for the connectors framework (pending #28)

`connectors-write-guard.json` is a suite in the #28 format (`evals/<suite>/evals.json`).
It is kept in `docs/evals/` because the #28 runner is not on `main` and its
`load_suite` rejects unknown targets. A suite under `evals/` would break `ops evals`
as soon as #28 merges.

Until then, `tests/test_connectors_eval_draft.py` loads this file and runs every
case through the framework, using the fake OAuth provider and no network.

## When #28 merges

1. `git mv docs/evals/connectors-write-guard.json evals/connectors/evals.json`
2. In `ops_dashboard/evals/runner.py`, add `"connectors.invoke"` to `TARGETS`, and in
   `_run_target` add this before the brain branch:

   ```python
   if suite.target == "connectors.invoke":
       from .connectors_case import run_eval_case
       return _checks.Outcome(answer=run_eval_case(case.params)), None
   ```

   `run_eval_case` lives in `tests/connectors_fake.py` today. The runner can't import `tests/`,
   so move it to `ops_dashboard/evals/connectors_case.py`, together with `FakeProvider` and `fake_spec`. It does three things:
   - registers the fake connector;
   - connects it with the requested permission through the fake OAuth provider;
   - calls `connector_actions.invoke` and returns JSON with the result plus `provider_writes`.

   It needs a temporary DB (the runner already builds one per suite) and the in-memory keychain backend.
3. Add the three cases to `evals/baseline.json` as `true`. They are `required`, so any
   regression that runs a write without confirmation fails CI.

## MCP client (`mcp-tools.json`, also pending #28)

Same situation and same steps, with target `"mcp.invoke"`:

1. `git mv docs/evals/mcp-tools.json evals/mcp-tools/evals.json`
2. Add `"mcp.invoke"` to `TARGETS` and a branch in `_run_target`:

   ```python
   if suite.target == "mcp.invoke":
       from .mcp_case import run_eval_case
       return _checks.Outcome(answer=run_eval_case(case.params, Path(tmp_dir))), None
   ```

   Move `run_eval_case` from `tests/mcp_fake.py` to `ops_dashboard/evals/mcp_case.py`
   and ship `tests/fixtures/mcp/fake_mcp_server.py` next to it (it is a plain Python
   stdio script, no dependencies). The case adds the fake server with a secret env var
   in the in-memory keychain, puts a fake secret in the *parent* environment, refreshes,
   calls `mcp_hub.invoke` and returns JSON with the result, the fenced text the LLM would
   see and `side_effects` (lines the fake server wrote to disk).
3. Add the five cases to `evals/baseline.json` as `true` (all `required`): a write without
   confirmation, an injection that escapes the fence or a parent secret reaching the child
   process fails CI.

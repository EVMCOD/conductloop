## What and why

<!-- What does this change, and why? Link the issue if there is one (Fixes #123). -->

## How it was tested

<!-- Commands you ran and what you saw. Screenshots for UI changes. -->

## Checklist

- [ ] Verified locally with `scripts/verify.sh` (ruff, mypy ratchet, `pytest -m "not live"`, evals, gitleaks) and it passes.
- [ ] Tests added or updated for the behaviour I changed (hermetic: no network, no real API keys).
- [ ] Database migrations (delete this line if there are none):
  - [ ] uses the next free migration number in `ops_dashboard/migrations.py`, no gaps and no reused numbers;
  - [ ] idempotent (`IF NOT EXISTS` / `IF EXISTS`), safe to run twice;
  - [ ] covered by a test (see `tests/test_migrations.py`);
  - [ ] anything destructive is called out in `CHANGELOG.md` and in this description.
- [ ] `CHANGELOG.md` has an entry under **Unreleased** (user-facing changes only; delete this line for internal-only changes and say why).
- [ ] No personal data and no secrets: no real emails, names, paths, tokens, API keys, `.env`, `config.json`, databases or signing keys. Fixtures use placeholders such as `user@example.com`.
- [ ] Docs updated where behaviour, setup or configuration changed (`README.md`, `docs/`, `CONTRIBUTING.md`).
- [ ] If I touched `.github/workflows/`: runners are GitHub-hosted labels (never `self-hosted`), every new action is pinned to a 40-character commit SHA with a version comment, and permissions stay `contents: read`.

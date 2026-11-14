# Contributing to ConductLoop

Thanks for your interest! ConductLoop is maintained by a single developer, so please keep changes small and focused.

## Before you start
- For anything larger than a bug fix, open an issue first to discuss the approach.
- **Security issues:** do not open a public issue — follow [SECURITY.md](SECURITY.md).

## Setup
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
```

## Checks (same as CI)
Run everything CI runs, before you push:
```bash
scripts/verify.sh          # or: make verify
scripts/verify.sh --fast   # quick loop: no coverage, slow/browser tests, pip-audit or whole-tree mypy
```
It runs, in CI order: the updater pubkey check, `ruff check`, the advisory
ruff-S and whole-tree mypy checks, the mypy typed-module ratchet, the offline
evals (`python -m ops_dashboard.evals run`), `pip-audit --strict`, a package
build, `pytest -m "not live"` with the coverage floor, and a gitleaks scan of
the full history. Advisory steps (marked `continue-on-error` in CI) only warn.

- `--offline` skips the step that needs the network (`pip-audit`); `--strict`
  turns a missing optional tool into a failure. Without `--strict`, a missing
  `gitleaks` or `pip-audit` is skipped with a clear warning (CI still runs them).
- Extra pytest options go in `PYTEST_ADDOPTS`, for example
  `PYTEST_ADDOPTS="--ignore=tests/test_x.py" scripts/verify.sh`.
- Not run locally: the headless-Chrome browser tests (CI installs Playwright and
  uses the runner's Chrome) and the apt install of bubblewrap/ffmpeg.
- The modules that must stay type-clean live in `scripts/mypy-ratchet.txt` (one
  path per line), read by both CI and `verify.sh`. To add a module, add a line
  there.
- `tests/test_verify_script.py` fails if `scripts/verify.sh` and
  `.github/workflows/ci.yml` stop running the same steps. If you add or change a
  CI step, update the `STEPS` table in `scripts/verify.sh` to match.

Optional pre-push hook that runs `scripts/verify.sh --fast` before each push:
```bash
scripts/install-hooks.sh              # install (--uninstall removes it)
VERIFY_ARGS="--fast --offline" git push   # change the options for one push
git push --no-verify                  # skip it for one push
```
It installs a small shim into your clone's git hooks directory instead of
setting `core.hooksPath`, so it coexists with `pre-commit install`.

To run the pieces by hand:
```bash
ruff check ops_dashboard tests
mypy --follow-imports=silent $(cat scripts/mypy-ratchet.txt)
pytest -m "not live"
```
- Keep the lint clean; CI fails on `ruff check`.
- Add or update tests for behaviour you change. Coverage must not drop below the CI floor.
- Tests must be hermetic: no network, no real API keys. Mark anything that needs the network with `@pytest.mark.live`.

## Pull requests
- Branch from `main`, one logical change per PR, descriptive title (`fix: …`, `feat: …`, `chore: …`).
- Describe *what* and *why*, and how you tested it.
- `scripts/verify.sh` passes locally, and CI (tests + gitleaks secret scan on your commits) is green.

## Never commit
- `.env`, `config.json`, API keys, tokens, Stripe or webhook secrets.
- Signing material: Tauri updater private keys, Apple certificates (`.p12`, `.p8`), provisioning profiles.
- Databases or dumps (`*.db`, `*.sqlite*`), backups, logs, or any personal vault / notes content.

`.gitignore` covers these, and the gitleaks job will fail the PR if a secret slips into a commit. If that happens, **rotate the secret first**, then remove it from the branch history before merging.

## License of contributions
ConductLoop is [MIT-licensed](LICENSE). By submitting a contribution you agree that it is released under the same license.

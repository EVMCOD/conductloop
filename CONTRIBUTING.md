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
```bash
ruff check ops_dashboard tests
mypy --follow-imports=silent <the module you touched>   # see .github/workflows/ci.yml for the strict list
pytest -m "not live"
```
- Keep the lint clean; CI fails on `ruff check`.
- Add or update tests for behaviour you change. Coverage must not drop below the CI floor.
- Tests must be hermetic: no network, no real API keys. Mark anything that needs the network with `@pytest.mark.live`.

## Pull requests
- Branch from `main`, one logical change per PR, descriptive title (`fix: …`, `feat: …`, `chore: …`).
- Describe *what* and *why*, and how you tested it.
- CI (tests + gitleaks secret scan on your commits) must be green.

## Never commit
- `.env`, `config.json`, API keys, tokens, Stripe or webhook secrets.
- Signing material: Tauri updater private keys, Apple certificates (`.p12`, `.p8`), provisioning profiles.
- Databases or dumps (`*.db`, `*.sqlite*`), backups, logs, or any personal vault / notes content.

`.gitignore` covers these, and the gitleaks job will fail the PR if a secret slips into a commit. If that happens, **rotate the secret first**, then remove it from the branch history before merging.

## License of contributions
ConductLoop is [MIT-licensed](LICENSE). By submitting a contribution you agree that it is released under the same license.

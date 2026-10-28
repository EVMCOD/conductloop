.PHONY: help install index serve app open stop clean status verify verify-fast

PY := .venv/bin/python
PORT := 7777

# Load .env (copied from .env.example) into the environment of every target.
ifneq (,$(wildcard .env))
include .env
export
endif

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Set up venv and install deps
	python3 -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements.txt
	@echo "✓ venv ready at .venv"

index:  ## Reindex all sources (vault, memory, repos, URLs, embeddings, playbook)
	$(PY) -m ops_dashboard index

serve:  ## Start the web server on :7777
	$(PY) -m ops_dashboard serve --port $(PORT)

app:  ## Open dashboard in a standalone macOS app window (Chrome --app mode)
	@open "scripts/ConductLoop.app.command" || ./scripts/ConductLoop.app.command

open:  ## Open dashboard in default browser
	@open "http://127.0.0.1:$(PORT)/"

stop:  ## Stop the server
	@lsof -ti:$(PORT) | xargs kill 2>/dev/null && echo "✓ stopped" || echo "(no server running)"

status:  ## Check server status
	@lsof -ti:$(PORT) >/dev/null 2>&1 && echo "✓ running on :$(PORT)" || echo "✗ not running"

clean:  ## Remove the DB and snapshots (will reindex from scratch)
	@echo "removing data/ops.db and snapshots/..."
	@rm -f data/ops.db
	@rm -rf snapshots/
	@echo "✓ cleaned"

brain:  ## Ask the brain a question
	$(PY) -m ops_dashboard brain

briefing:  ## Show morning briefing
	$(PY) -m ops_dashboard brain --briefing

snapshot:  ## Take a daily snapshot
	$(PY) -m ops_dashboard snapshot take

diff:  ## Show daily diff
	$(PY) -m ops_dashboard snapshot diff

export:  ## Export state to tarball
	$(PY) -m ops_dashboard export

import:  ## Import state from tarball (destructive)
	$(PY) -m ops_dashboard import ~/ops-dashboard-export.tar.gz --force

verify:  ## Run the same checks as CI (ruff, mypy ratchet, pytest, evals, ...) before you push
	./scripts/verify.sh $(VERIFY_ARGS)

verify-fast:  ## Quick verify: skip slow/browser tests, coverage, pip-audit and whole-tree mypy
	./scripts/verify.sh --fast $(VERIFY_ARGS)

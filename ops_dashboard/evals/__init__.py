"""Offline evals for ConductLoop (learning plan, PR L1).

Replays recorded ("cassette") provider responses through the real Brain /
search code paths and scores the result with deterministic checks. Runs in CI
with no network and no API keys:

    python -m ops_dashboard.evals run            # offline, compares to evals/baseline.json
    python -m ops_dashboard.evals record ...     # maintainer-only, refuses in CI

See evals/README.md for the case format (agentskills.io ``evals.json`` plus a
``conductloop`` block) and the baseline policy (decision D10).
"""
from __future__ import annotations

__all__ = ["cassette", "checks", "netguard", "runner"]

"""Agent Skills (agentskills.io) support — reading and activation (PR L5).

Design doc 2026-10-21-conductloop-learning-design.md §3.6, decisions:

* D1  skills live in ``<vault>/skills/``; "Export" copies one to ``~/.agents/skills/``.
* D2  ``~/.agents/skills`` and ``~/.claude/skills`` are read only if the user
      turns it on (off by default), and are never written to by the indexer.
* D8  YAML frontmatter is parsed with PyYAML ``safe_load`` only.
* D13 project skills (``<repo>/.agents/skills``) load only for repos the user
      marked as trusted (off by default).
* D16 no remote catalogs/hubs: nothing here touches the network.

Skill ``scripts/`` are never executed; ``allowed-tools`` is ignored.
"""
from __future__ import annotations

# Submodules are imported explicitly (``from ops_dashboard.skills import catalog``)
# so that importing the package never requires PyYAML.

"""Brain templates: pre-canned question + system prompt tweaks for common tasks.

Usage:
  from ops_dashboard import templates
  q, sys_extra = templates.get("weekly-review")
"""

TEMPLATES: dict[str, dict] = {
    "weekly-review": {
        "label": "Weekly review",
        "description": "Summarize this week: what changed, what broke, what's pending.",
        "question": "Dame un resumen ejecutivo de esta semana: qué cambió en mis proyectos, qué URLs fallaron, qué TODOs nuevos aparecieron. Usa diff_since_yesterday y recent_documents.",
        "system_extra": "Format as: 'esta semana' section with bullet points, then 'pendiente crítico' section. Be specific, no fluff.",
    },
    "project-status": {
        "label": "Project status",
        "description": "Status of one specific project.",
        "question": "Cuál es el estado actual del proyecto {project}? Fase, repos, URLs, TODOs abiertos, último commit, próximo milestone.",
        "system_extra": "Use the project_status tool. Format: phase, URLs ok/total, repos count, TODOs open, last activity, key blockers.",
    },
    "what-changed": {
        "label": "What changed",
        "description": "Diff between today and the previous snapshot.",
        "question": "Qué cambió desde ayer? diff_since_yesterday por favor.",
        "system_extra": "List only the deltas. Group: docs, URLs, TODOs, activity. Be terse.",
    },
    "morning-briefing": {
        "label": "Morning briefing",
        "description": "5-line summary of the day.",
        "question": "Briefing matutino: estado de los 12 proyectos, URLs fallando, top 3 TODOs prioritarios.",
        "system_extra": "5 lines max. Lead with the most important thing. Spanish.",
    },
    "weekly-snapshot": {
        "label": "Weekly snapshot",
        "description": "Markdown summary of the week, file-ready.",
        "question": "Genera un snapshot semanal en markdown: tabla de días con docs/URLs/TODOs, total deltas, principales cambios.",
        "system_extra": "Markdown ready to write to a file. Include a header table and a 'principales cambios' section.",
    },
    "todo-priorities": {
        "label": "TODO priorities",
        "description": "Top open TODOs across all projects.",
        "question": "Cuáles son los TODOs abiertos más importantes ahora mismo? List_open_todos y agrupa por proyecto.",
        "system_extra": "Group by project, list each item. Highlight anything related to revenue or live systems first.",
    },
    "find-decision": {
        "label": "Find decision",
        "description": "Find a specific decision you made.",
        "question": "Busca en el vault cuándo y por qué se decidió: {topic}",
        "system_extra": "Use vault_search. Show date and excerpt of the decision.",
    },
}


def get(key: str, **vars) -> tuple[str, str] | None:
    """Return (question, system_extra) for a template. Substitutes {vars}."""
    t = TEMPLATES.get(key)
    if not t:
        return None
    q = t["question"]
    sx = t["system_extra"]
    try:
        q = q.format(**vars)
    except KeyError:
        pass
    try:
        sx = sx.format(**vars)
    except KeyError:
        pass
    return q, sx


def list_templates() -> list[dict]:
    return [{"key": k, "label": v["label"], "description": v["description"]} for k, v in TEMPLATES.items()]

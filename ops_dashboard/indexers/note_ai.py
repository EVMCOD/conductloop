"""Note AI actions — summarize, expand, translate, key-points, action-items.

Takes a note (by id) and runs an LLM transformation on its content.
Uses the same brain provider config (Anthropic/OpenAI/Ollama) as daily summary.

Endpoints:
  POST /api/notes/<id>/ai-action {action: 'summarize'|'expand'|'translate'|'key_points'|'action_items',
                                   target_lang?: 'es'|'en'|...}

For 'translate', target_lang defaults to 'en'.
The result is returned as JSON and can be applied to a new note or
appended to the existing one (caller's choice).

CLI:
  python -m ops_dashboard.cli note-ai <doc_id> summarize [--lang es]
"""
from __future__ import annotations

import time
from typing import Any

from .. import brain, db

ACTIONS = ("summarize", "expand", "translate", "key_points", "action_items", "title_suggest")

ACTION_PROMPTS = {
    "summarize": (
        "Summarize the following note in 3-5 bullet points (max 15 words each). "
        "Output ONLY the bullet list, no preamble. "
        "Write in {lang}."
    ),
    "expand": (
        "Expand the following note into a richer version with more detail, "
        "examples, and structure. Keep the same tone. Use markdown. "
        "Write in {lang}."
    ),
    "translate": (
        "Translate the following note to {lang}. Preserve markdown formatting, "
        "code blocks, links, and technical terms. Output ONLY the translation, "
        "no preamble."
    ),
    "key_points": (
        "Extract the 3-7 most important takeaways from this note. "
        "One line each, no preamble. Write in {lang}."
    ),
    "action_items": (
        "Extract concrete action items / next steps / TODOs from this note. "
        "Each on its own line starting with '- [ ] '. If no actions, return "
        "'(no actions found)'. Write in {lang}."
    ),
    "title_suggest": (
        "Suggest 3 alternative titles for this note. Be concise (max 8 words each). "
        "One per line, no numbering or preamble. Write in {lang}."
    ),
}

DEFAULT_LANG = "en"


def get_note_content(doc_id: int) -> tuple[dict | None, str | None]:
    """Fetch a note's content by doc_id. Returns (row, error_message)."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, source, path, title, content FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    if not row:
        return None, "doc not found"
    content = row["content"] or ""
    if not content.strip():
        return None, "note is empty"
    return dict(row), None


def _truncate(content: str, max_chars: int = 12000) -> str:
    """Trim long content to fit LLM context windows."""
    if len(content) <= max_chars:
        return content
    half = max_chars // 2
    return content[:half] + "\n\n[... truncated for length ...]\n\n" + content[-half:]


def run_action(doc_id: int, action: str, *, target_lang: str = DEFAULT_LANG,
                provider_name: str | None = None) -> dict[str, Any]:
    """Run an AI action on a note. Returns {doc_id, action, model, result, lang, took_ms}."""
    if action not in ACTIONS:
        return {"error": f"unknown action '{action}', valid: {ACTIONS}"}

    row, err = get_note_content(doc_id)
    if err:
        return {"error": err, "doc_id": doc_id}

    from ..redact import redact_secrets
    # The note is sent to the LLM provider: strip credentials first. Redact
    # before truncating, or a cut through a key/PEM header would hide it.
    content = _truncate(redact_secrets(row["content"]))
    prompt_template = ACTION_PROMPTS[action]
    user_msg = prompt_template.format(lang=target_lang) + "\n\n---\n\n" + content
    system = (
        "You are ConductLoop's note assistant. Apply the requested transformation "
        "precisely. Output only the result, no preamble. Preserve technical terms and "
        "proper nouns. Match the user's preferred language."
    )

    t0 = time.perf_counter()
    try:
        provider = brain.resolve_provider(provider_name)
        result = provider.ask(system, [{"role": "user", "content": user_msg}])
        if not result or not result.strip():
            return {"error": "AI returned empty result", "doc_id": doc_id, "action": action}
        model_name = getattr(provider, "model", "unknown") or "unknown"
    except brain.ProviderUnavailableError as e:
        return {**e.payload(), "doc_id": doc_id, "action": action}
    except Exception as e:
        return {"error": f"AI call failed: {e}", "doc_id": doc_id, "action": action}
    took_ms = int((time.perf_counter() - t0) * 1000)

    # Log to activity feed
    try:
        db.add_activity(
            "note_ai",
            f"{action} on doc {doc_id} '{row['title'][:40]}' (lang={target_lang}, model={model_name}, {took_ms}ms)",
        )
    except Exception:
        pass

    return {
        "doc_id": doc_id,
        "doc_title": row["title"],
        "action": action,
        "model": model_name,
        "lang": target_lang,
        "result": result,
        "input_chars": len(content),
        "took_ms": took_ms,
    }

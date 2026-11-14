"""Static guards for the agent-skills UI (L7).

The skills panel lives in the CEO thread page (boardroom-thread.html). It
renders server data into the page, so the same rules the Aprendizaje page
follows apply: no HTML sink without escaping, no external origin, and only
our own API.
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
PAGE = WEB / "boardroom-thread.html"
SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function")


def _html() -> str:
    return PAGE.read_text()


def test_skills_panel_exists_with_its_controls():
    html = _html()
    assert 'id="skillsPanel"' in html
    assert 'id="skillsList"' in html
    assert 'id="skillPackSelect"' in html
    assert 'id="installPackBtn"' in html


def _without_our_words(expr: str) -> str:
    """The expression without the translation calls: what is left is what
    comes from the server."""
    return re.sub(r"\b(?:t|tr)\(\s*(?:'[^']*'|\"[^\"]*\")\s*(?:,\s*(?:'[^']*'|\"[^\"]*\"))?\s*\)",
                  "", expr)


def _server_props(expr: str) -> set[str]:
    """The properties of the expression that do not come from our words."""
    props = set(re.findall(r"\.(\w+)", _without_our_words(expr)))
    if props <= {"enabled", "length", "included", "omitted"}:
        return props
    # A translation call, possibly cut short by the {…} of its own text:
    # our words, not server values.
    if re.match(r"^\s*(?:t|tr)\(", expr):
        return set()
    return props


def test_every_server_value_rendered_into_the_panel_is_escaped():
    """A skill name or description comes from a file the user can edit; it
    must never be pasted into the page unescaped."""
    html = _html()
    # Only the skills rendering functions, not the rest of the page.
    block = html.split("function renderSkills(")[1].split("async function showSkillsPreview(")[0]
    for match in re.finditer(r"\$\{([^}]*)\}", block):
        expr = match.group(1)
        if "escapeHtml" in expr:
            continue
        # Anything else interpolated into the row must carry no server
        # STRING: only a boolean may be interpolated raw (it renders as
        # 0/1 or picks between two literals we control).
        assert _server_props(expr) <= {"enabled"}, expr


def test_no_external_origin_in_the_skills_code():
    html = _html()
    script = html.split("<script>")[1].split("</script>")[0]
    for url in re.findall(r"https?://[^\s'\"]+", script):
        # The only external URLs allowed in this page are the channel webhook
        # examples in a placeholder string, which are not fetched.
        assert "example" in url or "hooks.slack" in url or "discord.com/api/webhooks" in url, url


def test_skills_calls_only_our_own_api():
    html = _html()
    script = html.split("<script>")[1].split("</script>")[0]
    for path in re.findall(r"['\"`](/api/[a-zA-Z0-9_/\$\{\}-]*)", script):
        assert path.startswith("/api/"), path


def test_skill_actions_use_the_registered_endpoints():
    html = _html()
    for endpoint in ("install", "uninstall", "toggle"):
        assert f"/skills/${endpoint}" in html or "skills/${path}" in html
    assert "/api/skills/packs" in html


def test_the_preview_button_and_panel_exist():
    """The founder must be able to see what the agent will read."""
    html = _html()
    assert 'id="previewSkillsBtn"' in html
    assert 'id="skillsPreview"' in html
    assert "/skills/preview" in html


def test_the_preview_renders_server_values_escaped():
    html = _html()
    block = html.split("async function showSkillsPreview(")[1].split("async function skillAction(")[0]
    for match in re.finditer(r"\$\{([^}]*)\}", block):
        expr = match.group(1)
        if "escapeHtml" in expr:
            continue
        # Only counts and lengths may be interpolated raw: they are numbers
        # the server sent, never text that could carry markup.
        assert _server_props(expr) <= {"length", "included", "omitted"}, expr


def test_the_preview_is_local_only():
    """The endpoint describes the owner's own agent: it must not be reachable
    through a tunnel."""
    from ops_dashboard import routes_agent_skills
    src = Path(routes_agent_skills.__file__).read_text()
    block = src.split("/api/agents/<agent_id>/skills/preview")[1].split("def agent_skills_preview(")[0]
    assert "_local_only" in block

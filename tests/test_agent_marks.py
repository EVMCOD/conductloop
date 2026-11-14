"""The marks of the agents: the identity of ConductLoop, drawn small.

The twelve marks come from the identity kit. The script
``scripts/build_agent_marks.py`` cropped each one out of the board and
wrote the movement next to them; these tests check that what is in the
repository still goes with what the code asks for.
"""
from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from ops_dashboard import routes_marks, team_chat

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
MARKS_DIR = WEB / "assets" / "agents"
CSS = WEB / "agent-marks.css"

# The twelve roles of the starter pack are the twelve roles of the kit.
STARTER_ROLES = {
    "cso", "cfo", "cto", "cmo",
    "vp_design", "vp_frontend", "vp_mobile", "vp_devops",
    "head_product", "head_sales", "general_counsel", "head_cs",
}


def test_the_kit_has_a_mark_for_every_role_of_the_starter_pack():
    """The roles of the starter pack are the roles of the kit."""
    assert routes_marks.MARKS == STARTER_ROLES


def test_every_mark_has_its_file():
    for role in sorted(routes_marks.MARKS):
        path = MARKS_DIR / f"{role}.png"
        assert path.exists(), f"{role}.png is missing"
        assert path.stat().st_size > 0, f"{role}.png is empty"


def test_the_marks_are_small():
    """They were 66 MB in the kit. They are drawn at 56 pixels."""
    for role in sorted(routes_marks.MARKS):
        size = (MARKS_DIR / f"{role}.png").stat().st_size
        assert size < 60 * 1024, f"{role}.png is {size // 1024} KB"
    total = sum((MARKS_DIR / f"{r}.png").stat().st_size for r in routes_marks.MARKS)
    assert total < 512 * 1024, f"the marks weigh {total // 1024} KB"


def test_the_marks_are_png_of_the_expected_size():
    """They are the cell of the board, drawn small."""
    for role in sorted(routes_marks.MARKS):
        raw = (MARKS_DIR / f"{role}.png").read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{role}.png is not a PNG"
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        assert (width, height) == (128, 128), f"{role}.png is {width}x{height}"


def test_every_mark_has_its_movement():
    css = CSS.read_text(encoding="utf-8")
    for role in sorted(routes_marks.MARKS):
        assert f"@keyframes mark-{role}" in css, f"{role} does not move"
        rule = re.search(rf"\.mark-{role} \{{ ([^}}]+) \}}", css)
        assert rule, f"{role} has no rule"
        assert f"mark-{role}" in rule.group(1)
        assert "infinite" in rule.group(1)


def test_the_movement_uses_percentages():
    """The kit moves the mark in the units of the board. The avatar is drawn
    at 24, 32 and 56 pixels, so the movement has to scale with it."""
    css = CSS.read_text(encoding="utf-8")
    body = "\n".join(re.findall(r"@keyframes mark-\w+ \{.*?\}\}", css))
    assert body, "no keyframes at all"
    assert "px" not in body, "the movement is still in pixels"
    assert "%" in body


def test_somebody_who_asked_for_less_motion_gets_still_marks():
    css = CSS.read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    block = css[css.index("prefers-reduced-motion"):]
    assert "animation: none" in block


def test_the_script_that_builds_them_is_in_the_repository():
    script = Path(__file__).resolve().parent.parent / "scripts" / "build_agent_marks.py"
    assert script.exists(), "the marks cannot be rebuilt without the script"
    text = script.read_text(encoding="utf-8")
    assert "agentes.json" in text, "the script does not read the kit"


def test_the_css_says_it_is_generated():
    head = CSS.read_text(encoding="utf-8")[:400]
    assert "build_agent_marks.py" in head, "nobody would know it is generated"


def test_the_role_of_an_agent_gives_its_mark():
    assert team_chat.mark_for("cso") == "cso"
    assert team_chat.mark_for("vp_design") == "vp_design"
    assert team_chat.mark_for("general_counsel") == "general_counsel"
    # The chat's builtins use shorter names for the same roles.
    assert team_chat.mark_for("product") == "head_product"
    assert team_chat.mark_for("design") == "vp_design"
    assert team_chat.mark_for("cs") == "head_cs"
    # Nobody has a mark: the emoji stays.
    assert team_chat.mark_for("ceo") == ""
    assert team_chat.mark_for("") == ""
    assert team_chat.mark_for("someone else") == ""


def test_every_mark_the_code_asks_for_exists():
    """A mark that is not a file is a broken image in the panel."""
    for role, mark in team_chat._MARKS.items():
        assert mark in routes_marks.MARKS, f"{role} asks for the unknown mark {mark}"
        assert (MARKS_DIR / f"{mark}.png").exists(), f"{mark}.png is missing"


def test_the_agent_of_the_team_carries_its_mark():
    assert "mark" in team_chat._member_agent(
        {"id": 1, "name": "Avery", "role": "cso", "capabilities": ""}
    )


def test_the_mark_is_served_and_the_route_refuses_the_rest():
    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/assets/agents/cso.png", timeout=8) as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/png"
            assert r.read()[:8] == b"\x89PNG\r\n\x1a\n"
        with urllib.request.urlopen(base + "/agent-marks.css", timeout=8) as r:
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("text/css")
        # A name that is not a mark is a 404, not a file read.
        for bad in ("../team_chat", "nope", "cso%2F..%2Fapp"):
            try:
                urllib.request.urlopen(base + "/assets/agents/" + bad + ".png", timeout=8)
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"{bad} answered {e.code}"
            else:
                raise AssertionError(f"{bad} was served")
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_panel_draws_the_mark_and_falls_back_to_the_emoji():
    src = (WEB / "chat.js").read_text(encoding="utf-8")
    assert "/assets/agents/" in src, "the chat does not draw the marks"
    assert "mark-" in src
    # An agent without a mark keeps the emoji.
    assert "agent.avatar" in src


def test_the_agents_page_draws_the_mark():
    src = (WEB / "agents.html").read_text(encoding="utf-8")
    assert "/assets/agents/" in src, "the agents page does not draw the marks"
    assert "/agent-marks.css" in src, "the agents page does not load the movement"

"""L7: end-to-end check of /learning ("Aprendizaje") in headless Chrome
against the real local server. No network (Python sockets limited to
loopback), no keys, no model calls.

Flow (es-ES): tabs + keyboard + #hash, 👎 → lesson proposal → approve,
skill from a vault note → linter blocks hostile HTML → edit → approve →
propose archive → approve (moved, never deleted), failure-log toggle,
read-only Evals panel; then en-US strings. Hostile content (<script>,
<img onerror>) in answers, comments, notes and eval prompts must render as
text and never execute; the page is served with a strict CSP and raises no
CSP violations or JS errors.

Skipped when Playwright or Chrome is missing, unless LEARNING_BROWSER_REQUIRED=1
(the CI step that installs Playwright sets it, so there it must run).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REQUIRED = os.environ.get("LEARNING_BROWSER_REQUIRED") == "1"
CHROME = (os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser"))
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None  # type: ignore[assignment]
if not REQUIRED:
    pytestmark = pytest.mark.skipif(sync_playwright is None or not CHROME, reason="no Playwright/Chrome")

XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'


@pytest.fixture
def site(tmp_path, monkeypatch):
    assert sync_playwright is not None and CHROME, "Playwright and Chrome are required (LEARNING_BROWSER_REQUIRED=1)"
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)

    from ops_dashboard import db, db_brain, learning
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "l.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("OPS_SKILLS_DIR", raising=False)
    db.init_db()
    vault = tmp_path / "vault"
    (vault / "skills").mkdir(parents=True)
    (vault / "Procesos").mkdir()
    (vault / "Procesos" / "Backups.md").write_text(f"Copia diaria del vault {XSS}.\n\nComprueba el fichero.\n")
    conv = db_brain.save_conversation_turn(title="t", user_content="¿precio?", assistant_content=f"Cuesta 10 $ {XSS}",
                                           provider="fakeprov", model="f1")
    with db.db() as c:
        mid = c.execute("SELECT id FROM messages WHERE conversation_id=? AND role='assistant'", (conv,)).fetchone()[0]
    learning.record_feedback(target_kind="brain_message", target_id=mid, rating=-1, reason_code="style",
                             comment=f"usa euros {XSS}")
    ev = tmp_path / "evals"
    (ev / "brain-rag").mkdir(parents=True)
    (ev / "brain-rag" / "evals.json").write_text(json.dumps({
        "conductloop": {"suite": "brain-rag", "target": "brain.ask"},
        "evals": [{"id": 1, "prompt": f"precio {XSS}",
                   "conductloop": {"key": "rag-pricing", "required": True, "checks": [{"type": "must_cite"}]}}]}))
    (ev / "baseline.json").write_text(json.dumps({"overall": {"passed": 1, "total": 1, "score": 100.0},
                                                  "suites": {"brain-rag": {"passed": 1, "total": 1, "score": 100.0}},
                                                  "cases": {"brain-rag/rag-pricing": True}}))
    monkeypatch.setenv("OPS_EVALS_DIR", str(ev))

    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", vault
    finally:
        srv.shutdown()
        srv.server_close()


def _visible(pg, sel: str) -> bool:
    return pg.is_visible(sel)


def test_learning_page_full_flow(site, tmp_path):
    base, vault = site
    from ops_dashboard import learning
    from ops_dashboard.learning import lessons as L
    from ops_dashboard.skills import propose as P
    shots = Path(os.environ.get("LEARNING_SCREENS") or tmp_path)
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="es-ES")
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}")
                  if "Content Security Policy" in m.text or (m.type == "error" and "Failed to load resource" not in m.text)
                  else None)
            # HTTP errors are checked by URL: only the expected 409 (linter block) is allowed.
            pg.on("response", lambda r: errors.append(f"http {r.status} {r.url}")
                  if r.status >= 400 and not r.url.endswith("/favicon.ico")
                  and not (r.status == 409 and r.url.endswith("/proposals/approve")) else None)
            pg.on("dialog", lambda d: d.accept(""))

            resp = pg.goto(base + "/learning")
            csp = resp.headers.get("content-security-policy", "")
            assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
            assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
            pg.wait_for_selector("#lessons-card:not([hidden])")
            assert pg.title().startswith("Aprendizaje")
            for hidden in ("#feedback-card", "#skills-card", "#failures-card", "#evals-card"):
                assert not _visible(pg, hidden), hidden

            # 👎 → lesson proposal (no AI) → approve with an edit
            pg.click("#tab-feedback")
            pg.wait_for_selector("#feedback-card:not([hidden]) #feedback-list li")
            assert XSS in pg.text_content("#feedback-list")  # shown as text
            pg.click("#feedback-list button:text-is('Proponer lección (sin IA)')")
            pg.click("#tab-lessons")
            pg.wait_for_selector("#lessons-list li textarea")
            assert L.summary()["proposed"] == 1 and L.select_for_prompt("precio") == []
            pg.fill("#lessons-list li textarea", "Dar los precios en euros.")
            pg.click("#lessons-list li button:text-is('Aprobar')")
            pg.wait_for_selector("#lessons-list .pill.st-active")
            assert [r["text"] for r in L.select_for_prompt("precios")] == ["Dar los precios en euros."]

            # skill from a note: hostile HTML blocks approval; edit → approve → archive (moved, not deleted)
            pg.keyboard.press("Tab")
            pg.focus("#tab-lessons")
            pg.keyboard.press("ArrowRight")
            pg.wait_for_selector("#skills-card:not([hidden])")
            assert pg.evaluate("document.activeElement.id") == "tab-skills"
            pg.fill("#sp-ref", "Procesos/Backups.md")
            pg.click("#sp-create")
            pg.wait_for_selector("#sp-list li textarea")
            assert "hidden_text" in pg.text_content("#sp-list")
            pg.click("#sp-list li button:text-is('Aprobar')")
            pg.wait_for_selector("#global-error:not([hidden])")
            # The error path reloads the page data; wait for it before editing,
            # or the re-render can wipe the cleaned text (flaky in CI).
            pg.wait_for_load_state("networkidle")
            assert not (vault / "skills" / "backups").exists()
            clean = pg.input_value("#sp-list li textarea").replace(XSS, "")
            pg.fill("#sp-list li textarea", clean)
            pg.click("#sp-list li button:text-is('Aprobar')")
            pg.wait_for_selector("#skills-list li strong:text-is('backups')")
            assert (vault / "skills" / "backups" / "SKILL.md").is_file()
            pg.click("#skills-list li button:text-is('Proponer archivar')")
            pg.wait_for_selector("#sp-list li .pill:text-is('archivar')")
            pg.click("#sp-list li button:text-is('Aprobar')")
            pg.wait_for_selector("#skills-empty:not([hidden])")
            assert not (vault / "skills" / "backups").exists()
            archived = list((vault / "skills" / ".archive").iterdir())
            assert len(archived) == 1 and (archived[0] / "SKILL.md").is_file()
            assert P.summary()["approved"] == 2

            # failure log toggle (opt-in)
            pg.click("#tab-failures")
            pg.wait_for_selector("#failures-card:not([hidden])")
            assert not learning.failure_log_enabled()
            pg.check("#failure-toggle")
            pg.wait_for_selector("#failures-empty:has-text('Sin fallos')")  # (wait_for_function needs eval: CSP)
            assert learning.failure_log_enabled()

            # read-only Evals
            pg.click("#tab-evals")
            pg.wait_for_selector("#evals-suites li")
            txt = pg.text_content("#evals-card")
            assert "brain-rag" in txt and "rag-pricing" in txt and "solo lectura" in txt and "100.00" in txt
            assert XSS in txt
            assert pg.locator("#evals-card button").count() == 0  # nothing to run from the page
            assert pg.url.endswith("#evals")
            pg.screenshot(path=str(shots / "learning-es.png"), full_page=True)

            # L8: "¿Aprendo bien?" — metrics render, A/B (off by default) and curator settings
            from ops_dashboard.learning import metrics as M
            pg.click("#tab-metrics")
            pg.wait_for_selector("#metrics-card:not([hidden]) #m-details li")
            assert pg.input_value("#m-ab") == "0" and pg.is_checked("#m-curator")
            assert "poca señal" in pg.text_content("#m-signals")
            pg.fill("#m-ab", "20")
            pg.click("#m-ab-save")
            pg.wait_for_timeout(400)
            assert M.ab_holdout_pct() == 20
            pg.uncheck("#m-curator")
            pg.wait_for_timeout(400)
            assert M.curator_enabled() is False
            pg.screenshot(path=str(shots / "learning-metrics-es.png"), full_page=True)

            # deep link + English
            en = browser.new_context(locale="en-US").new_page()
            en.on("pageerror", lambda e: errors.append(f"pageerror(en): {e}"))
            en.goto(base + "/learning#evals")
            en.wait_for_selector("#evals-card:not([hidden]) #evals-suites li")
            assert en.title().startswith("Learning")
            assert en.text_content("#tab-lessons") == "What I have learned"
            assert "read only" in en.text_content("#evals-card")
            en.click("#tab-skills")
            en.wait_for_selector("#skill-proposals-card:not([hidden])")
            assert "The agent only proposes" in en.text_content("#skill-proposals-card")
            en.click("#tab-metrics")
            en.wait_for_selector("#m-details li")
            assert "Am I learning well?" in en.text_content("#metrics-card")
            en.screenshot(path=str(shots / "learning-en.png"), full_page=True)

            for page in (pg, en):
                assert page.evaluate("window.__xss") is None
        finally:
            browser.close()
    assert errors == [], errors

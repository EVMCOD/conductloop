#!/usr/bin/env python3
"""End-to-end smoke for the connectors that need no account (P1-4).

Drives a *running* ConductLoop server (the packaged app's sidecar or
``python -m ops_dashboard serve``) through its HTTP API, like the Conectores
page does:

* MCP client over **stdio**: registers ``tests/fixtures/mcp/fake_mcp_server.py``.
* MCP client over **Streamable HTTP**: starts
  ``tests/fixtures/mcp/fake_mcp_http_server.py`` on a real loopback socket.
* For both: trust read-only hints, refresh (initialize + tools/list), call a
  read tool, check a write tool is refused while read-only and queued for
  confirmation once write is allowed, confirm it, then remove the server.
* **RSS** (optional, ``--rss-url https://…``): configure, connect, sync.
  This one hits the public internet, so it is never run by CI.

Only test servers named ``smoke-*`` are created and they are removed at the
end. Point it ONLY at a server started with a temporary ``OPS_DATA_DIR``
(never your real data): ``--base http://127.0.0.1:<port>``.

Exit code 0 = all checks passed. Prints one line per check.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "mcp"


class SmokeError(AssertionError):
    pass


def _req(base: str, path: str, body: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=h,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def _check(cond: bool, label: str, detail: object = "") -> None:
    print(("ok   " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        raise SmokeError(label)


def _text(res: dict) -> str:
    return json.dumps(res, ensure_ascii=False)


def _mcp_flow(base: str, name: str, server: dict, read_tool: str, read_args: dict, expect: str,
              out: Path, extra_reads: tuple[tuple[str, dict, str], ...] = ()) -> None:
    st, b = _req(base, "/api/connectors/mcp/servers", {"name": name, **server})
    _check(st == 200, f"{name}: save server", b)
    try:
        st, b = _req(base, f"/api/mcp/servers/{name}", {"trust_readonly": True}, method="PUT")
        _check(st == 200, f"{name}: trust read-only hints", b)
        st, b = _req(base, f"/api/connectors/mcp/servers/{name}/refresh", {})
        _check(st == 200 and b.get("server", {}).get("status") == "connected", f"{name}: connect + list tools", b)
        names = {t["name"] for t in b["server"]["tools"]}
        _check(read_tool in names and "create_note" in names, f"{name}: tools discovered", sorted(names))
        for tool, args, want in ((read_tool, read_args, expect), *extra_reads):
            st, r = _req(base, f"/api/connectors/mcp/servers/{name}/tools/{tool}/call", {"arguments": args})
            _check(st == 200 and r.get("result", {}).get("untrusted") is True and want in _text(r),
                   f"{name}: read tool {tool} runs, result marked untrusted", r)
        st, r = _req(base, f"/api/connectors/mcp/servers/{name}/tools/create_note/call", {"arguments": {"title": "t"}})
        _check(st == 403 and r.get("error") == "write_not_permitted", f"{name}: write refused while read-only", r)
        st, r = _req(base, f"/api/connectors/mcp/servers/{name}/permission", {"permission": "write"})
        _check(st == 200, f"{name}: allow writes", r)
        st, q = _req(base, f"/api/connectors/mcp/servers/{name}/tools/create_note/call",
                     {"arguments": {"title": "smoke"}})
        _check(st == 200 and q.get("status") == "confirmation_required" and not out.exists(),
               f"{name}: write queued for confirmation, not executed", q)
        st, r = _req(base, f"/api/connectors/actions/{q['action_id']}/confirm", {"payload_hash": q["payload_hash"]})
        _check(st == 200 and out.exists() and "smoke" in out.read_text(encoding="utf-8"),
               f"{name}: confirmed write reaches the server", r)
    finally:
        st, b = _req(base, f"/api/connectors/mcp/servers/{name}/remove", {})
        _check(st == 200, f"{name}: remove server", b)


def run_stdio(base: str, python: str, tmp: Path) -> None:
    out = tmp / "stdio-side.txt"
    _mcp_flow(base, "smoke-stdio", {"transport": "stdio", "command": [python, str(FIXTURES / "fake_mcp_server.py")],
                                    "env": {"FAKE_MCP_OUT": str(out)}},
              "search_notes", {"q": "Pro"}, "plan Pro", out)


def run_http(base: str, python: str, tmp: Path, token: str | None = None) -> None:
    out = tmp / "http-side.txt"
    cmd = [python, str(FIXTURES / "fake_mcp_http_server.py"), "--out", str(out)] + (["--token", token] if token else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        _check(line.startswith("PORT "), "http fake MCP server listening", line)
        url = f"http://127.0.0.1:{int(line.split()[1])}/mcp"
        server: dict = {"transport": "http", "url": url}
        if token:
            server["token"] = token
        _mcp_flow(base, "smoke-http", server, "search_notes", {"q": "Pro"}, "HTTP note for Pro", out,
                  extra_reads=(("echo_sse", {"text": "hola"}, "sse:hola"),))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def run_rss(base: str, feed_url: str) -> None:
    st, b = _req(base, "/api/connectors/token/rss/config", {"config": {"feeds": feed_url}})
    _check(st == 200, "rss: save feed list", b)
    try:
        st, b = _req(base, "/api/connectors/token/rss/token", {})
        _check(st == 200, "rss: connect (no account)", b)
        st, b = _req(base, "/api/connectors/token/rss/sync", {})
        sync = b.get("sync") or {}
        _check(st == 200 and int(sync.get("fetched") or 0) > 0, "rss: sync ingests entries", b)
    finally:
        st, b = _req(base, "/api/connectors/token/rss/forget", {})
        _check(st == 200, "rss: forget", b)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="server base URL, e.g. http://127.0.0.1:8765")
    ap.add_argument("--python", default=sys.executable, help="interpreter for the fake MCP servers")
    ap.add_argument("--rss-url", help="public https RSS/Atom feed to sync (network; optional)")
    ap.add_argument("--http-token", help="bearer token for the HTTP fake (needs a keychain in the server)")
    ap.add_argument("--skip-stdio", action="store_true")
    ap.add_argument("--skip-http", action="store_true")
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    try:
        with tempfile.TemporaryDirectory(prefix="cl-smoke-") as d:
            tmp = Path(d)
            if not a.skip_stdio:
                run_stdio(base, a.python, tmp)
            if not a.skip_http:
                run_http(base, a.python, tmp, a.http_token)
            if a.rss_url:
                run_rss(base, a.rss_url)
    except SmokeError as e:
        print(f"smoke failed: {e}", file=sys.stderr)
        return 1
    print("all connector smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""MCP (Model Context Protocol) client — stdio and Streamable HTTP transports.

Replaces the minimal client in ``mcp.py`` (which now delegates here). Fixes
over the old client: request ids are matched per request (the old stdio
discovery waited for id 3 after sending id 1, so it always timed out), stdio
servers no longer inherit the parent environment, HTTP speaks Streamable HTTP
(JSON *or* SSE responses, ``Mcp-Session-Id``, ``MCP-Protocol-Version``), tool
lists are paginated, and every response is size-bounded.

Security model (see ``mcp_hub`` for policy):
* **stdio**: the child gets an allowlisted environment (PATH, HOME, locale…)
  plus only the variables the user configured for that server; secrets for it
  come from the keychain. Nothing else from our environment (API keys, tokens)
  is inherited. No shell: ``command`` is an argv list.
* **HTTP**: https only, except loopback (``http://127.0.0.1``/``localhost``)
  for local servers; no redirects; bearer token from the keychain.
* Everything a server returns (tool names, descriptions, results) is untrusted
  data; this module only parses it, ``mcp_hub`` sanitizes and fences it.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "conductloop", "version": "1"}
MAX_MESSAGE = 4 * 1024 * 1024          # bytes per JSON-RPC message
MAX_TOOLS = 500
DEFAULT_TIMEOUT = 30.0
SAFE_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
                 "SYSTEMROOT", "WINDIR")


class MCPError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def safe_env(extra: dict[str, str] | None = None, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a stdio server: allowlisted keys from ``base`` (default
    ``os.environ``) plus ``extra`` (the server's own configured variables)."""
    src = os.environ if base is None else base
    env = {k: src[k] for k in SAFE_ENV_KEYS if k in src}
    for k, v in (extra or {}).items():
        env[str(k)] = str(v)
    return env


def check_http_url(url: str) -> str:
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme == "https" and host:
        return url
    if u.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return url
    raise MCPError("bad_url", "MCP HTTP servers must use https (http only on loopback)")


# ── JSON-RPC session (transport-agnostic) ───────────────────────────────────

class _Transport:
    def request(self, msg: dict, timeout: float) -> dict:          # returns the response for msg["id"]
        raise NotImplementedError
    def notify(self, msg: dict) -> None: ...
    def close(self) -> None: ...


class Session:
    """One initialized MCP session. Use as a context manager."""

    def __init__(self, transport: _Transport, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.t = transport
        self.timeout = timeout
        self._id = 0
        self.server_info: dict = {}
        self.protocol_version = ""
        self.capabilities: dict = {}

    def __enter__(self) -> Session:
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()

    def close(self) -> None:
        self.t.close()

    def _next(self) -> int:
        self._id += 1
        return self._id

    def call(self, method: str, params: dict | None = None) -> Any:
        msg = {"jsonrpc": "2.0", "id": self._next(), "method": method, "params": params or {}}
        resp = self.t.request(msg, self.timeout)
        if not isinstance(resp, dict):
            raise MCPError("bad_response", "response is not an object")
        if "error" in resp:
            err = resp.get("error") or {}
            raise MCPError("rpc_error", str(err.get("message") or err)[:300])
        return resp.get("result")

    def initialize(self) -> None:
        res = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": CLIENT_INFO})
        if not isinstance(res, dict):
            raise MCPError("bad_initialize", "initialize returned no result")
        self.protocol_version = str(res.get("protocolVersion") or "")
        si, caps = res.get("serverInfo"), res.get("capabilities")
        self.server_info = si if isinstance(si, dict) else {}
        self.capabilities = caps if isinstance(caps, dict) else {}
        if isinstance(self.t, HttpTransport):
            self.t.protocol_version = self.protocol_version or PROTOCOL_VERSION
        self.t.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _page in range(50):
            res = self.call("tools/list", {"cursor": cursor} if cursor else {})
            page = (res or {}).get("tools") if isinstance(res, dict) else None
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict))
            cursor = (res or {}).get("nextCursor") if isinstance(res, dict) else None
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    def call_tool(self, name: str, arguments: dict) -> dict:
        res = self.call("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(res, dict):
            raise MCPError("bad_result", "tools/call returned no result object")
        return res


# ── stdio ───────────────────────────────────────────────────────────────────

class StdioTransport(_Transport):
    def __init__(self, command: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        if not command or not all(isinstance(c, str) and c for c in command):
            raise MCPError("bad_command", "command must be a non-empty argv list")
        child_env = safe_env(env)
        exe = command[0]
        if os.sep not in exe:
            exe = shutil.which(exe, path=child_env.get("PATH")) or exe
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, user-configured server
                [exe, *command[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=child_env, cwd=cwd, start_new_session=True, close_fds=True)
        except FileNotFoundError as e:
            raise MCPError("command_not_found", str(command[0])) from e
        except OSError as e:
            raise MCPError("spawn_failed", type(e).__name__) from e
        self._q: queue.Queue[dict | None] = queue.Queue()
        self.stderr_tail = b""
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(lambda: self.proc.stdout.readline(MAX_MESSAGE + 1), b""):  # type: ignore[union-attr]
            if len(line) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue                         # servers may log non-JSON to stdout; ignore
            if isinstance(msg, dict):
                self._q.put(msg)
        self._q.put(None)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in iter(lambda: self.proc.stderr.read(4096), b""):  # type: ignore[union-attr]
            self.stderr_tail = (self.stderr_tail + chunk)[-2048:]

    def _send(self, msg: dict) -> None:
        if self.proc.stdin is None or self.proc.poll() is not None:
            raise MCPError("server_exited", f"exit code {self.proc.poll()}")
        try:
            self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError("server_exited", type(e).__name__) from e

    def request(self, msg: dict, timeout: float) -> dict:
        self._send(msg)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MCPError("timeout", f"no response to {msg.get('method')} after {timeout:.0f}s")
            try:
                m = self._q.get(timeout=left)
            except queue.Empty:
                continue
            if m is None:
                raise MCPError("server_exited", f"exit code {self.proc.poll()}")
            if m.get("id") == msg["id"] and ("result" in m or "error" in m):
                return m
            if "method" in m and "id" in m:      # server→client request (sampling, roots…): refuse
                self._send({"jsonrpc": "2.0", "id": m["id"],
                            "error": {"code": -32601, "message": "not supported by this client"}})

    def notify(self, msg: dict) -> None:
        self._send(msg)

    def close(self) -> None:
        p = self.proc
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)


# ── Streamable HTTP ─────────────────────────────────────────────────────────

_http_transport: httpx.BaseTransport | None = None


def set_http_transport(t: httpx.BaseTransport | None) -> httpx.BaseTransport | None:
    """Tests: plug an ``httpx.MockTransport`` fake MCP server."""
    global _http_transport
    prev, _http_transport = _http_transport, t
    return prev


def _sse_events(lines: Iterator[str]) -> Iterator[str]:
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


class HttpTransport(_Transport):
    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = check_http_url(url)
        self.token = token
        self.session_id: str | None = None
        self.protocol_version = ""
        self.client = httpx.Client(transport=_http_transport, timeout=DEFAULT_TIMEOUT, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _post(self, msg: dict, timeout: float) -> httpx.Response:
        try:
            req = self.client.build_request("POST", self.url, json=msg, headers=self._headers(), timeout=timeout)
            return self.client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise MCPError("network", type(e).__name__) from e

    def request(self, msg: dict, timeout: float) -> dict:
        r = self._post(msg, timeout)
        try:
            if r.status_code in (401, 403):
                raise MCPError("unauthorized", f"HTTP {r.status_code}")
            if r.status_code == 404 and self.session_id:
                raise MCPError("session_expired", "server dropped the session")
            if r.status_code >= 300:
                raise MCPError("http_error", f"HTTP {r.status_code}")
            sid = r.headers.get("Mcp-Session-Id")
            if sid and msg.get("method") == "initialize":
                self.session_id = sid[:256]
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                lines: list[str] = []
                for line in _bounded_lines(r):
                    lines.append(line)
                    if line == "":
                        for data in _sse_events(iter(lines)):
                            m = _loads(data)
                            if isinstance(m, dict) and m.get("id") == msg["id"] and ("result" in m or "error" in m):
                                return m
                        lines = []
                for data in _sse_events(iter(lines)):
                    m = _loads(data)
                    if isinstance(m, dict) and m.get("id") == msg["id"]:
                        return m
                raise MCPError("no_response", "SSE stream ended without a response")
            if ctype == "application/json":
                body = b"".join(_bounded_chunks(r))
                m = _loads(body.decode("utf-8", "replace"))
                if isinstance(m, list):            # batched
                    m = next((x for x in m if isinstance(x, dict) and x.get("id") == msg["id"]), None)
                if not isinstance(m, dict):
                    raise MCPError("bad_response", "not a JSON-RPC object")
                return m
            raise MCPError("bad_content_type", ctype or "none")
        finally:
            r.close()

    def notify(self, msg: dict) -> None:
        r = self._post(msg, 10.0)
        try:
            if r.status_code >= 300:
                raise MCPError("http_error", f"notification HTTP {r.status_code}")
        finally:
            r.close()

    def close(self) -> None:
        try:
            if self.session_id:
                self.client.delete(self.url, headers=self._headers(), timeout=5.0)
        except httpx.HTTPError:
            pass
        finally:
            self.client.close()


def _bounded_chunks(r: Any) -> Iterator[bytes]:
    """The response body in chunks, aborting as soon as it passes MAX_MESSAGE
    (an untrusted HTTP server must not make us buffer an unbounded body)."""
    size = 0
    for chunk in r.iter_bytes():
        size += len(chunk)
        if size > MAX_MESSAGE:
            raise MCPError("too_large", "response exceeds limit")
        yield chunk


def _bounded_lines(r: Any) -> Iterator[str]:
    """SSE lines from a bounded byte stream (a single endless line is caught
    too, which ``iter_lines`` would buffer whole)."""
    buf = b""
    for chunk in _bounded_chunks(r):
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            yield buf[:nl].rstrip(b"\r").decode("utf-8", "replace")
            buf = buf[nl + 1:]
    if buf:
        yield buf.rstrip(b"\r").decode("utf-8", "replace")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def open_session(server: dict, *, token: str | None = None, env: dict[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> Session:
    """Build an (uninitialized) session for a server config dict
    (``transport``, ``command``, ``url``). Use ``with open_session(...) as s``."""
    transport = server.get("transport")
    if transport == "stdio":
        return Session(StdioTransport(list(server.get("command") or []), env=env), timeout)
    if transport == "http":
        return Session(HttpTransport(str(server.get("url") or ""), token=token), timeout)
    if transport == "sse":
        raise MCPError("unsupported_transport", "legacy HTTP+SSE is deprecated; use Streamable HTTP")
    raise MCPError("unsupported_transport", str(transport))

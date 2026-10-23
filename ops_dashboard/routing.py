"""Minimal route registry — the incremental path off the ~100-branch if/elif
dispatch in server.py.

Handlers register a (method, path-pattern) and the dispatcher matches a request
against them *before* falling through to the legacy ``_route``. Patterns use
``<name>`` for a path segment captured as a keyword arg:

    from .routing import router

    @router.get("/api/health")
    def health(handler):
        handler._send(200, b'{"ok": true}', "application/json")

    @router.get("/api/tasks/<task_id>")
    def one_task(handler, task_id):
        ...

Because every route is registered in one place they can be *enumerated*, so a
declared-but-unhandled endpoint (the class of bug that made /api/health 404)
becomes a test assertion instead of a silent miss.
"""
from __future__ import annotations

import re
from collections.abc import Callable

_SEG = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")


def _compile(path: str) -> re.Pattern:
    """"/api/tasks/<id>" -> ^/api/tasks/(?P<id>[^/]+)$"""
    parts, pos = [], 0
    for m in _SEG.finditer(path):
        parts.append(re.escape(path[pos:m.start()]))
        parts.append(f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    parts.append(re.escape(path[pos:]))
    return re.compile("^" + "".join(parts) + "$")


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Callable, str]] = []

    def add(self, method: str, path: str, fn: Callable) -> None:
        self._routes.append((method.upper(), _compile(path), fn, path))

    def route(self, method: str, path: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.add(method, path, fn)
            return fn
        return deco

    def get(self, path: str) -> Callable:
        return self.route("GET", path)

    def post(self, path: str) -> Callable:
        return self.route("POST", path)

    def put(self, path: str) -> Callable:
        return self.route("PUT", path)

    def delete(self, path: str) -> Callable:
        return self.route("DELETE", path)

    def dispatch(self, handler, method: str, path: str) -> bool:
        """Call the first matching handler; return True if one handled it."""
        for m, rx, fn, _raw in self._routes:
            if m != method.upper():
                continue
            match = rx.match(path)
            if match:
                fn(handler, **match.groupdict())
                return True
        return False

    def registered(self) -> list[tuple[str, str]]:
        return [(m, raw) for m, _rx, _fn, raw in self._routes]


router = Router()

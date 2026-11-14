"""Guards the route registry (routing.py) and the migrated health route."""
from __future__ import annotations

import json

from ops_dashboard.routing import Router


def test_static_and_param_dispatch():
    r = Router()
    hits = []

    @r.get("/api/ping")
    def ping(handler):
        hits.append(("ping",))

    @r.get("/api/tasks/<task_id>")
    def one(handler, task_id):
        hits.append(("task", task_id))

    assert r.dispatch(None, "GET", "/api/ping") is True
    assert r.dispatch(None, "GET", "/api/tasks/42") is True
    assert hits == [("ping",), ("task", "42")]
    # method mismatch + unknown path do not dispatch
    assert r.dispatch(None, "POST", "/api/ping") is False
    assert r.dispatch(None, "GET", "/api/nope") is False


def test_param_does_not_cross_slash():
    r = Router()

    @r.get("/api/tasks/<task_id>")
    def one(handler, task_id):
        pass

    assert r.dispatch(None, "GET", "/api/tasks/1/extra") is False


def test_health_registered_and_dispatches():
    from ops_dashboard import routes_meta  # noqa: F401  registers routes
    from ops_dashboard.routing import router

    assert ("GET", "/api/health") in router.registered()
    assert ("GET", "/health") in router.registered()
    # routes migrated off server.py's if/elif onto the registry
    assert ("GET", "/api/pricing-tier") not in router.registered()  # prices removed (free)
    for p in ("/api/stats", "/api/tags", "/api/playbooks", "/api/activity",
              "/api/todos/summary", "/api/tasks/stats", "/api/usage"):
        assert ("GET", p) in router.registered(), p

    captured = {}

    class _FakeHandler:
        def _send(self, status, body, content_type="text/html"):
            captured["status"] = status
            captured["body"] = body

    assert router.dispatch(_FakeHandler(), "GET", "/api/health") is True
    assert captured["status"] in (200, 503)
    body = json.loads(captured["body"])
    assert "service" in body
    # enriched for observability (#8)
    assert "version" in body and "schema_version" in body

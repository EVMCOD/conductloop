"""ConductLoop is free: no Stripe, no checkout, no prices in the app. Offline."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _req(url, method="GET", body=None):
    opener = urllib.request.build_opener(_NoRedirect)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import os
    import threading
    from http.server import ThreadingHTTPServer

    db_path = tmp_path_factory.mktemp("ops") / "billing.db"
    os.environ["CONDUCTLOOP_DB_PATH"] = str(db_path)
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler
    db.DB_PATH = db_path  # type: ignore[attr-defined]
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()


@pytest.mark.parametrize("path", [
    "/api/billing/checkout-lifetime", "/api/billing/checkout-subscription",
    "/api/billing/cancel-subscription", "/api/billing/stripe-webhook",
])
def test_retired_billing_endpoints_are_gone(server, path):
    status, _, body = _req(server + path, "POST", {})
    assert status == 410, (path, status)
    assert json.loads(body)["free_edition"] is True


def test_billing_success_redirects_home(server):
    status, headers, _ = _req(server + "/billing/success?session_id=x")
    assert status == 302 and headers.get("Location") == "/"


def test_billing_page_has_no_prices(server):
    status, _, body = _req(server + "/billing")
    assert status == 200
    text = body.decode()
    assert not re.search(r"\$\s?\d", text), "no prices on /billing"
    assert "pricing-tier" not in text and "checkout" not in text.lower()


def test_pricing_tier_endpoint_removed(server):
    status, _, _ = _req(server + "/api/pricing-tier")
    assert status == 404


def test_no_stripe_in_server_source():
    src = (ROOT / "ops_dashboard" / "server.py").read_text(encoding="utf-8")
    assert "api.stripe.com" not in src
    assert "_stripe_secret" not in src and "STRIPE_SECRET_KEY" not in src

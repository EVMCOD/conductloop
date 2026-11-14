"""Centralized logging for ConductLoop.

Replaces scattered ``print(..., file=sys.stderr)`` calls with a real logger:
a rotating file handler (``<data-dir>/logs/conductloop.log``, 5MB x 5) plus
stderr. Configured once, idempotently.

    from .logging_setup import get_logger
    log = get_logger(__name__)
    log.info("serving on %s", url)
    log.exception("download failed")   # inside an except: includes traceback

Env: OPS_LOG_LEVEL (default INFO), OPS_LOG_DIR (default <data-dir>/logs).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = "conductloop"
_configured = False


def _log_dir() -> Path:
    env = os.environ.get("OPS_LOG_DIR")
    if env:
        return Path(env)
    from . import db  # reuse the already-resolved data dir (lazy: no import cycle)
    return Path(db._DB_DIR) / "logs"


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    lvl = getattr(logging, (level or os.environ.get("OPS_LOG_LEVEL") or "INFO").upper(), logging.INFO)
    root = logging.getLogger(_ROOT)
    root.setLevel(lvl)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Rotating file handler — best-effort (read-only/shared installs skip it).
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(d / "conductloop.log", maxBytes=5_000_000,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass

    _init_sentry()
    _configured = True


def _init_sentry() -> None:
    """Opt-in error self-reporting. If SENTRY_DSN is set and sentry-sdk is
    installed (requirements-optional), init it so unhandled 500s are reported.
    No-op otherwise — the app already reads Sentry, this lets it report too."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0)
        logging.getLogger(_ROOT).info("sentry error reporting enabled")
    except Exception:
        logging.getLogger(_ROOT).debug("sentry init skipped", exc_info=True)


def report_exception(exc: BaseException) -> None:
    """Send an exception to Sentry if configured; silent no-op otherwise."""
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def get_logger(name: str = _ROOT) -> logging.Logger:
    """Return a namespaced child of the configured ``conductloop`` logger."""
    setup_logging()
    if not name or name == _ROOT:
        return logging.getLogger(_ROOT)
    # accept __name__ ('ops_dashboard.server') -> 'conductloop.server'
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"{_ROOT}.{short}")

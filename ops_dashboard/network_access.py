"""Whether the server answers on the local network, or only on this computer.

The companion iPhone app pairs with a QR and then reaches the server over the
LAN or Tailscale, so the server must listen on the network for it. From 1.2.15
the desktop app opened the network on every launch (OPS_ALLOW_MOBILE=1), for
everyone, although the iPhone app is not published: every install put port
7777 on its Wi-Fi for a feature nobody could use. Every request from the
network needs a paired device's token or a session, but a port nobody needs is
attack surface nobody needs.

Now it is the person's decision, made on the Pair a phone page:

* ``OPS_ALLOW_MOBILE=1`` (a server started by hand) still opens the network:
  the operator asked for it explicitly.
* ``OPS_ALLOW_MOBILE=setting`` (what the desktop app passes) follows the
  stored choice in ``<data dir>/network.json``.
* With no stored choice the network stays closed, unless a device is already
  paired: an install that paired a phone keeps working after the update.
* Anything else: loopback only.

The choice takes effect when the server starts (the socket is bound once).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import app_paths

_FILE = "network.json"
ENV = "OPS_ALLOW_MOBILE"


def _path() -> Path:
    return app_paths.data_dir() / _FILE


def stored() -> bool | None:
    """The person's choice, or None if they never made one."""
    try:
        value = json.loads(_path().read_text(encoding="utf-8")).get("lan")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def _has_paired_device() -> bool:
    try:
        from . import db
        return any(not d.get("revoked") for d in db.list_device_tokens())
    except Exception:
        # A fresh install has no table yet; any failure here means "no".
        return False


def wanted() -> bool:
    """Whether the server should open the network at the next start."""
    mode = os.environ.get(ENV, "")
    if mode == "1":
        return True
    if mode != "setting":
        return False
    choice = stored()
    if choice is not None:
        return choice
    return _has_paired_device()


def managed_by_env() -> bool:
    """True when the environment decides and the page's switch would do nothing."""
    return os.environ.get(ENV, "") not in ("setting",)


def set_wanted(lan: bool) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"lan": bool(lan)}), encoding="utf-8")
    os.replace(tmp, p)

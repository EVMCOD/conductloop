"""End-to-end encrypted sync bundle.

Exports your brain data (memory + decisions + captures) into a single
passphrase-encrypted file you can move to another device (AirDrop, USB, cloud
drive) and import there. The passphrase never leaves your head: the key is
derived with PBKDF2-HMAC-SHA256 and the payload sealed with Fernet (AES-CBC +
HMAC), so whoever holds the file learns nothing without the passphrase.

    ops sync-export --out ~/brain.cbundle       # on device A
    ops sync-import ~/brain.cbundle              # on device B (same passphrase)

Not live sync — a manual, zero-trust transfer primitive. Live multi-device sync
would layer a relay on top of this bundle format.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

from . import db

log = logging.getLogger("conductloop.sync")

_MAGIC = b"CLBUNDLE1"
_ITERATIONS = 200_000


def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _payload() -> dict:
    from . import capture
    return {
        "version": 1,
        "exported_at": int(time.time()),
        "memory": db.memory_list(),
        "decisions": db.list_decisions(limit=100000),
        "captures": capture.list_captures(only_open=False),
    }


def export_bundle(passphrase: str, out_path: str | Path) -> dict:
    if not passphrase:
        raise ValueError("passphrase required")
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    token = Fernet(_key(passphrase, salt)).encrypt(json.dumps(_payload(), default=str).encode())
    # magic + b64(salt) + ':' + token  (base64 has no ':', so split is safe)
    blob = _MAGIC + b"\n" + base64.b64encode(salt) + b":" + token
    out = Path(out_path).expanduser()
    out.write_bytes(blob)
    try:
        out.chmod(0o600)
    except OSError:
        pass
    log.info("wrote encrypted bundle %s", out)
    return {"path": str(out), "bytes": len(blob)}


def import_bundle(path: str | Path, passphrase: str) -> dict:
    from cryptography.fernet import Fernet
    raw = Path(path).expanduser().read_bytes()
    if not raw.startswith(_MAGIC):
        raise ValueError("not a ConductLoop bundle")
    body = raw.split(b"\n", 1)[1]
    salt_b64, token = body.split(b":", 1)
    salt = base64.b64decode(salt_b64)
    try:
        data = json.loads(Fernet(_key(passphrase, salt)).decrypt(token))
    except Exception as e:
        raise ValueError("wrong passphrase or corrupted bundle") from e

    mem = 0
    for m in data.get("memory", []):
        db.memory_set(m["key"], m["value"])  # upsert by key
        mem += 1
    dec = 0
    for d in data.get("decisions", []):
        try:
            db.add_decision(title=d.get("title") or "imported", decision=d.get("decision") or "",
                            project_key=d.get("project_key"), context=d.get("context"),
                            consequences=d.get("consequences"), tags=d.get("tags"))
            dec += 1
        except Exception:
            log.debug("skip decision import", exc_info=True)
    return {"memory": mem, "decisions": dec, "exported_at": data.get("exported_at")}

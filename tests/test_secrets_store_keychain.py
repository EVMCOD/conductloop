"""secrets_store's macOS Keychain path, without macOS or a real keychain.

A fake ``security`` executable records every invocation's argv and stdin and
keeps items in a JSON file. Its ``-i`` mode parses stdin with a port of Apple's
``split_line`` (SecurityTool/macOS/security.c), so the quoting/escaping we
send is checked against the real parser's rules. Secret values must never
appear in any argv — only on stdin, hex-encoded.
"""
from __future__ import annotations

import json
import logging
import stat
import sys
import textwrap

import pytest

FAKE_SECURITY = textwrap.dedent(r'''
    #!PYTHON
    """Fake /usr/bin/security for tests."""
    import json, os, sys
    state = os.environ["FAKE_SEC_STATE"]
    db_path = state + "/items.json"
    log_path = state + "/calls.jsonl"
    items = json.load(open(db_path)) if os.path.exists(db_path) else {}

    def split_line(line):  # port of Apple's split_line state machine
        args, cur, state_, q = [], None, "SKIP", None
        for ch in line:
            if state_ == "SKIP":
                if ch.isspace():
                    continue
                if ch in "\"'":
                    q, state_, cur = ch, "Q", ""
                    continue
                state_, cur = "A", ""
            if state_ == "A":
                if ch == "\\":
                    state_ = "AE"; continue
                if ch.isspace():
                    args.append(cur); state_ = "SKIP"; continue
                cur += ch; continue
            if state_ == "Q":
                if ch == "\\":
                    state_ = "QE"; continue
                if ch == q:
                    args.append(cur); state_ = "SKIP"; continue
                cur += ch; continue
            if state_ == "AE":
                cur += ch; state_ = "A"; continue
            if state_ == "QE":
                cur += ch; state_ = "Q"; continue
        if state_ != "SKIP":
            args.append(cur)
        return args

    def opts(av):
        o, i = {}, 0
        while i < len(av):
            a = av[i]
            if a in ("-U",):
                o[a] = True; i += 1
            else:
                o[a] = av[i + 1] if i + 1 < len(av) else None; i += 2
        return o

    def run(av):
        mode = os.environ.get("FAKE_SEC_MODE", "")
        if mode == "fail" or (mode == "fail-exit0" and av[0] == "add-generic-password"):
            sys.stderr.write("security: SecKeychainItemCreateFromContent: failed\n"); return 45
        cmd, o = av[0], opts(av[1:])
        key = o.get("-s", "") + "/" + o.get("-a", "")
        if cmd == "add-generic-password":
            if mode == "silent-noop":
                return 0
            if "-X" in o:
                val = bytes.fromhex(o["-X"]).decode("utf-8")
            else:
                val = o.get("-w") or ""
            if key in items and "-U" not in o:
                return 45
            items[key] = val
            json.dump(items, open(db_path, "w"))
            return 0
        if cmd == "delete-generic-password":
            if items.pop(key, None) is None:
                return 44
            json.dump(items, open(db_path, "w"))
            return 0
        if cmd == "find-generic-password":
            if key not in items:
                sys.stderr.write("security: The specified item could not be found in the keychain.\n")
                return 44
            v = items[key]
            print(v if v.isascii() and v.isprintable() else v.encode().hex())
            return 0
        return 2

    stdin = sys.stdin.read() if sys.argv[1:] == ["-i"] else ""
    with open(log_path, "a") as f:
        f.write(json.dumps({"argv": sys.argv, "stdin": stdin}) + "\n")
    if sys.argv[1:] == ["-i"]:
        rc = 0
        for line in stdin.split("\n"):
            if line.strip():
                assert len(line) < 4096
                rc = run(split_line(line)) or rc
        sys.exit(0 if os.environ.get("FAKE_SEC_MODE") == "fail-exit0" else rc)
    sys.exit(run(sys.argv[1:]))
''').lstrip()


@pytest.fixture()
def kc(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "security"
    fake.write_text(FAKE_SECURITY.replace("#!PYTHON", "#!" + sys.executable, 1))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FAKE_SEC_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.delenv("OPS_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "files"))
    from ops_dashboard import secrets_store
    monkeypatch.setattr(secrets_store.sys, "platform", "darwin")
    monkeypatch.setattr(secrets_store, "_SECURITY", str(fake))

    def calls():
        p = state / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def items():
        p = state / "items.json"
        return json.loads(p.read_text()) if p.exists() else {}

    return secrets_store, calls, items, tmp_path


SPECIALS = [
    "sk-ant-api03-plain_VALUE-123",
    'quote"double',
    "quote'single",
    "back\\slash\\\\n",
    "spaces  and\ttab",
    "$(touch /tmp/pwned) `id` ; | & > <",
    "-U -w injected -s other",
    "ünïcödé-ключ-🔑",
    "multi\nline",
]


@pytest.mark.parametrize("secret", SPECIALS)
def test_value_never_in_argv_only_on_stdin(kc, secret):
    store, calls, items, _ = kc
    store.set_secret("byok-anthropic", secret)
    log = calls()
    assert log, "security was not called"
    for c in log:
        assert secret not in " ".join(c["argv"])
        for arg in c["argv"]:
            assert secret not in arg
    writes = [c for c in log if c["argv"][1:] == ["-i"]]
    assert len(writes) == 1
    # Value travels hex-encoded on stdin; the raw secret isn't even on stdin.
    assert secret.encode().hex() in writes[0]["stdin"]
    assert secret not in writes[0]["stdin"]
    # Every call (write, read-back, cleanup) carries at most the item name.
    assert all(c["argv"][1] in ("-i", "find-generic-password", "delete-generic-password")
               for c in log)
    # The fake parsed the line exactly like Apple's split_line would; values that
    # `-w` can't print verbatim fail the read-back and live in the file instead.
    if secret.isascii() and secret.isprintable():
        assert items()["com.conductloop/byok-anthropic"] == secret
    else:
        assert "com.conductloop/byok-anthropic" not in items()
    assert store.get_secret("byok-anthropic") == secret


@pytest.mark.parametrize("secret", [s for s in SPECIALS if s.isascii() and s.isprintable()])
def test_printable_values_roundtrip_through_keychain(kc, secret):
    store, _calls, _items, tmp = kc
    store.set_secret("k1", secret)
    assert store.get_secret("k1") == secret
    assert not (tmp / "files" / ".k1.secret").exists()  # stayed in the keychain


def test_non_printable_value_falls_back_to_file_not_garbage(kc):
    # `find-generic-password -w` prints such data as hex: the read-back check
    # fails, so set_secret keeps the historic file fallback, never a corrupt value.
    store, _calls, _items, tmp = kc
    store.set_secret("k2", "multi\nline")
    assert store.get_secret("k2") == "multi\nline"
    assert (tmp / "files" / ".k2.secret").exists()
    assert "com.conductloop/k2" not in _items()  # undecodable item removed


def test_failed_update_does_not_leave_stale_keychain_value(kc, monkeypatch):
    store, _calls, items, _ = kc
    store.set_secret("k3", "old-value")
    assert items()["com.conductloop/k3"] == "old-value"
    monkeypatch.setenv("FAKE_SEC_MODE", "fail-exit0")
    store.set_secret("k3", "new-value")
    assert "com.conductloop/k3" not in items()
    assert store.get_secret("k3") == "new-value"  # from the file fallback


def test_update_existing_item_uses_U(kc):
    store, calls, items, _ = kc
    store.set_secret("k", "one")
    store.set_secret("k", "two")
    assert store.get_secret("k") == "two"
    assert all("-U" in c["stdin"] for c in calls() if c["argv"][1:] == ["-i"])


def test_names_with_quotes_are_escaped(kc):
    store, _calls, items, _ = kc
    store.set_secret('we"ird\\name x', "v")
    assert items()['com.conductloop/we"ird\\name x'] == "v"
    assert store.get_secret('we"ird\\name x') == "v"


def test_read_passes_only_the_name(kc):
    store, calls, _items, _ = kc
    store.set_secret("k", "s3cr3t-value")
    store.get_secret("k")
    reads = [c for c in calls() if "find-generic-password" in c["argv"]]
    assert reads and all("s3cr3t-value" not in " ".join(c["argv"]) for c in reads)
    assert all(c["stdin"] == "" for c in reads)


@pytest.mark.parametrize("mode", ["fail", "fail-exit0", "silent-noop"])
def test_write_errors_fall_back_and_never_log_value(kc, monkeypatch, caplog, mode):
    store, _calls, _items, tmp = kc
    monkeypatch.setenv("FAKE_SEC_MODE", mode)
    with caplog.at_level(logging.DEBUG, logger="conductloop.secrets"):
        assert store._kc_set("k", "s3cr3t-value") is False
        store.set_secret("k", "s3cr3t-value")
    assert (tmp / "files" / ".k.secret").read_text() == "s3cr3t-value"
    assert "s3cr3t-value" not in caplog.text


def test_too_long_value_refused_not_truncated(kc):
    store, calls, _items, _ = kc
    assert store._kc_set("k", "x" * 3000) is False  # 6000 hex chars > 4096 line
    assert not [c for c in calls() if c["argv"][1:] == ["-i"]]  # nothing written


def test_subprocess_timeout_is_handled(kc, monkeypatch, caplog):
    store, *_ = kc
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=10)

    monkeypatch.setattr(store.subprocess, "run", boom)
    with caplog.at_level(logging.DEBUG, logger="conductloop.secrets"):
        assert store._kc_set("k", "s3cr3t-value") is False
        assert store._kc_get("k") is None
    assert "s3cr3t-value" not in caplog.text


def test_missing_item_reads_none(kc):
    store, *_ = kc
    assert store._kc_get("nope") is None
    assert store.get_secret("nope") is None


def test_public_api_unchanged(kc):
    store, *_ = kc
    for fn in ("get_secret", "set_secret", "get_or_create_secret"):
        assert callable(getattr(store, fn))
    assert store.get_or_create_secret("fernet", lambda: "gen-1") == "gen-1"
    assert store.get_or_create_secret("fernet", lambda: "gen-2") == "gen-1"


def test_failed_update_keeps_the_old_value_when_the_fallback_fails(kc, monkeypatch):
    # Keychain write fails AND the file fallback fails (read-only / full disk):
    # the previous keychain value must survive (Codex on #17).
    store, _calls, items, _ = kc
    store.set_secret("k4", "old-value")
    monkeypatch.setenv("FAKE_SEC_MODE", "fail-exit0")

    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(store.os, "open", boom)
    with pytest.raises(OSError):
        store.set_secret("k4", "new-value")
    assert items()["com.conductloop/k4"] == "old-value"

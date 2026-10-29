"""secrets_store's Linux backend (freedesktop Secret Service), with a fake.

There is no Secret Service in CI or in a headless box, so ``keyring`` is
replaced by a fake module that has the same names the code imports
(``keyring.backends.SecretService.Keyring``). The tests check what the code
does with it: which class it accepts, what it stores and under which names, what
happens when the service is missing, hangs or lies, and that macOS, Windows and
``OPS_DISABLE_KEYCHAIN=1`` never reach it. Nothing here talks to D-Bus.
UNTESTED on a real Linux desktop: see docs/PORTABILITY.md.
"""
from __future__ import annotations

import logging
import sys
import threading
import types
from pathlib import Path

import pytest
from posix_perms import assert_mode

SECRET = "sk-test-VALUE-0123456789"


class FakeKeyring:
    """What ``keyring.backends.SecretService.Keyring`` looks like from outside."""

    def __init__(self, *, priority_error=None, mode=""):
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []
        self.priority_error = priority_error
        # "", "hang-get", "hang-set", "slow-set" (commits after release), "lie", "fail-set", "fail-get"
        self.mode = mode
        self.finished = threading.Event()     # set when a slow-set worker is done
        self.release = threading.Event()

    # keyring exposes ``priority`` as a class property that raises when unusable
    @property
    def priority(self):
        if self.priority_error:
            raise RuntimeError(self.priority_error)
        return 5

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        if self.mode == "hang-get" and not username.startswith("__conductloop-probe"):
            self.release.wait(30)
        if self.mode == "fail-get" and not username.startswith("__conductloop-probe"):
            raise OSError("locked")
        if self.mode == "lie":
            return "something else"
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.calls.append(("set", service, username))
        if self.mode == "slow-set" and not username.startswith("__conductloop-probe"):
            self.release.wait(30)               # the call outlives the caller's timeout...
            self.items[(service, username)] = password   # ...and then commits
            return
        if self.mode == "hang-set" or (self.mode == "fail-set" and not username.startswith("__conductloop-probe")):
            if self.mode == "hang-set":
                self.release.wait(30)
            raise OSError("boom " + password)       # the value must never be logged
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        if (service, username) not in self.items:
            raise KeyError("No such password!")
        del self.items[(service, username)]


def _install(monkeypatch, instance, *, module_name="keyring.backends.SecretService", cls_name="Keyring"):
    inst = instance
    inst.__class__ = type(cls_name, (FakeKeyring,), {"__module__": module_name})
    mod = types.ModuleType("keyring.backends.SecretService")
    mod.Keyring = lambda: inst
    backends = types.ModuleType("keyring.backends")
    backends.SecretService = mod
    pkg = types.ModuleType("keyring")
    pkg.backends = backends
    monkeypatch.setitem(sys.modules, "keyring", pkg)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.SecretService", mod)
    return inst


@pytest.fixture()
def ss(monkeypatch, tmp_path):
    from ops_dashboard import secrets_store
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("OPS_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(secrets_store, "_SS_PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(secrets_store, "_SS_OP_TIMEOUT", 0.3)
    secrets_store._ss_reset()
    kr = _install(monkeypatch, FakeKeyring())
    yield secrets_store, kr
    kr.release.set()
    secrets_store._ss_reset()


# ── the happy path ───────────────────────────────────────────────────────────

def test_roundtrip_under_the_same_names_as_macos(ss):
    s, kr = ss
    assert s.keychain_available() is True
    assert s.keychain_status() == {"backend": "secret-service", "available": True, "reason": None}
    assert s.keychain_set("byok.openai.default", SECRET) is True
    assert kr.items == {("com.conductloop", "byok.openai.default"): SECRET}
    assert s.keychain_get("byok.openai.default") == SECRET
    assert s.keychain_get("missing") is None
    assert s.keychain_delete("byok.openai.default") is True
    assert kr.items == {} and s.keychain_get("byok.openai.default") is None
    assert s.keychain_delete("never-there") is True


def test_the_probe_leaves_nothing_behind(ss):
    s, kr = ss
    assert s.keychain_available()
    assert kr.items == {}
    assert [c[0] for c in kr.calls[:3]] == ["set", "get", "delete"]
    assert all(c[2].startswith("__conductloop-probe-") for c in kr.calls)


def test_the_probe_runs_once(ss):
    s, kr = ss
    for _ in range(5):
        assert s.keychain_available()
    assert [c for c in kr.calls if c[0] == "set"] == kr.calls[:1]


def test_set_secret_uses_the_keychain_and_writes_no_file(ss, tmp_path):
    s, kr = ss
    s.set_secret("fernet-key", SECRET)
    assert s.get_secret("fernet-key") == SECRET
    assert kr.items == {("com.conductloop", "fernet-key"): SECRET}
    assert not (tmp_path / "files").exists()


def test_unicode_and_empty_values_roundtrip(ss):
    s, _ = ss
    for v in ("ünïcödé-ключ-🔑", "multi\nline", "quote\"'\\"):
        assert s.keychain_set("x", v) is True and s.keychain_get("x") == v
    assert s.keychain_set("e", "") is True and s.keychain_get("e") == ""


def test_bad_names_are_refused(ss):
    s, kr = ss
    for n in ("", "a\nb", "a\0b", "a\rb"):
        assert s.keychain_set(n, "v") is False
    assert kr.items == {}


# ── no Secret Service: a clear, safe failure ─────────────────────────────────

def test_no_secret_service_is_unavailable_and_says_why(ss, monkeypatch):
    s, _ = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="Unable to initialize SecretService"))
    assert s.keychain_available() is False
    assert s.keychain_status() == {"backend": None, "available": False, "reason": "no_secret_service"}
    assert s.keychain_set("byok.openai.default", SECRET) is False
    assert s.keychain_get("byok.openai.default") is None
    assert s.keychain_delete("byok.openai.default") is False


def test_without_a_secret_service_the_keychain_only_api_never_writes_a_file(ss, monkeypatch, tmp_path):
    s, _ = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    assert s.keychain_set("byok.openai.default", SECRET) is False
    assert not (tmp_path / "files").exists()


def test_set_secret_falls_back_to_a_0600_file_and_warns_without_the_value(ss, monkeypatch, tmp_path, caplog):
    s, _ = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    with caplog.at_level(logging.WARNING, logger="conductloop.secrets"):
        s.set_secret("fernet-key", SECRET)
        s.set_secret("fernet-key", SECRET + "2")
    f = tmp_path / "files" / ".fernet-key.secret"
    assert f.read_text() == SECRET + "2"
    assert_mode(f, 0o600)
    assert s.get_secret("fernet-key") == SECRET + "2"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("0600 file" in m and "fernet-key" in m for m in msgs)
    assert sum("0600 file" in m for m in msgs) == 1              # once per secret
    assert any("unavailable (no_secret_service)" in m for m in msgs)
    assert not any(SECRET in m for m in msgs)


def test_byok_goes_to_the_encrypted_vault_not_to_plaintext(ss, monkeypatch):
    from ops_dashboard import byok_keys
    s, _ = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    cfg: dict = {}
    assert byok_keys.store_api_key("openai", SECRET, cfg) == "vault"
    assert SECRET not in repr(cfg)


def test_connector_tokens_are_refused_without_a_keychain_and_never_written(ss, monkeypatch, tmp_path):
    from ops_dashboard import connector_tokens as tokens
    s, _ = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    b = tokens._KeychainBackend()
    assert b.available() is False
    with pytest.raises(tokens.TokenStoreUnavailable):
        b.set("connector-token:x", SECRET)
    assert not (tmp_path / "files").exists()


def test_connector_tokens_roundtrip_through_the_secret_service(ss):
    from ops_dashboard import connector_tokens as tokens
    s, kr = ss
    b = tokens._KeychainBackend()
    assert b.available() is True
    b.set("connector-token:x", SECRET)
    assert b.get("connector-token:x") == SECRET
    b.delete("connector-token:x")
    assert kr.items == {} and b.get("connector-token:x") is None


def test_connector_tokens_have_no_literal_security_call():
    src = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "connector_tokens.py").read_text()
    assert "delete-generic-password" not in src and "subprocess" not in src


def test_keyring_not_installed(ss, monkeypatch):
    s, _ = ss
    s._ss_reset()
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setitem(sys.modules, "keyring.backends", None)
    monkeypatch.setitem(sys.modules, "keyring.backends.SecretService", None)
    assert s.keychain_available() is False
    assert s.keychain_status()["reason"] == "keyring_missing"
    assert s.keychain_set("a", "b") is False


# ── only the allowed backend ─────────────────────────────────────────────────

@pytest.mark.parametrize("module,cls", [
    ("keyrings.alt.file", "PlaintextKeyring"),
    ("keyring.backends.fail", "Keyring"),
    ("keyring.backends.chainer", "ChainerBackend"),
    ("keyring.backends.null", "Keyring"),
    ("keyring.backends.SecretService", "Evil"),
])
def test_any_other_backend_is_refused(ss, monkeypatch, module, cls):
    s, _ = ss
    s._ss_reset()
    other = _install(monkeypatch, FakeKeyring(), module_name=module, cls_name=cls)
    assert s.keychain_available() is False
    assert s.keychain_status()["reason"] == "backend_not_allowed"
    assert s.keychain_set("a", "b") is False
    assert other.calls == [] and other.items == {}         # not even the probe touched it


# ── a service that hangs, lies or fails ──────────────────────────────────────

def test_a_hanging_probe_times_out_and_is_cached(ss, monkeypatch):
    s, _ = ss
    s._ss_reset()
    kr = _install(monkeypatch, FakeKeyring(mode="hang-set"))
    assert s.keychain_available() is False
    assert s.keychain_status()["reason"] == "timeout"
    n = len(kr.calls)
    assert s.keychain_available() is False and len(kr.calls) == n    # cached: no second probe
    kr.release.set()


def test_a_failed_probe_is_retried_later(ss, monkeypatch):
    s, _ = ss
    s._ss_reset()
    bad = _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    assert s.keychain_available() is False
    good = _install(monkeypatch, FakeKeyring())
    assert s.keychain_available() is False                  # still inside the retry window
    monkeypatch.setattr(s, "_SS_RETRY_AFTER", 0.0)
    assert s.keychain_available() is True and good.calls and not bad.items


def test_a_service_that_does_not_return_what_it_stored_is_not_used(ss, monkeypatch):
    s, kr = ss
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(mode="lie"))
    assert s.keychain_available() is False
    assert s.keychain_status()["reason"] == "probe_failed"


def test_a_hanging_read_returns_none_instead_of_blocking(ss):
    s, kr = ss
    assert s.keychain_set("k", SECRET) is True
    kr.mode = "hang-get"
    assert s.keychain_get("k") is None
    # A delete that cannot be confirmed is not reported as done.
    assert s.keychain_delete("k") is False
    kr.release.set()


def test_a_failed_write_is_false_and_never_logs_the_value(ss, caplog):
    s, kr = ss
    assert s.keychain_available()
    kr.mode = "fail-set"
    with caplog.at_level(logging.DEBUG, logger="conductloop.secrets"):
        assert s.keychain_set("k", SECRET) is False
    assert SECRET not in caplog.text and "boom" not in caplog.text


def test_a_write_that_does_not_read_back_is_a_failure(ss):
    s, kr = ss
    assert s.keychain_available()
    kr.mode = "lie"
    assert s.keychain_set("k", SECRET) is False


def test_set_secret_with_a_failing_write_keeps_the_value_in_the_file(ss, tmp_path):
    s, kr = ss
    assert s.keychain_available()
    kr.mode = "fail-set"
    s.set_secret("fernet-key", SECRET)
    assert (tmp_path / "files" / ".fernet-key.secret").read_text() == SECRET
    kr.mode = ""
    assert s.get_secret("fernet-key") == SECRET


# ── the switches and the other platforms ─────────────────────────────────────

def test_ops_disable_keychain_never_reaches_the_secret_service(ss, monkeypatch):
    s, kr = ss
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    assert s.keychain_available() is False
    assert s.keychain_status() == {"backend": None, "available": False, "reason": "disabled"}
    assert s.keychain_set("a", "b") is False and s.keychain_get("a") is None
    assert s.keychain_delete("a") is False
    assert kr.calls == []


def test_macos_does_not_touch_the_secret_service(ss, monkeypatch):
    s, kr = ss
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(s.shutil, "which", lambda n: "/usr/bin/security" if n == "security" else None)
    assert s._keychain_enabled() is True
    assert s.keychain_status() == {"backend": "macos-security", "available": True, "reason": None}
    monkeypatch.setattr(s.shutil, "which", lambda n: None)
    assert s._keychain_enabled() is False
    assert s.keychain_status()["reason"] == "no_security_tool"
    assert kr.calls == []


def test_windows_and_others_have_no_keychain_yet(ss, monkeypatch):
    s, kr = ss
    for plat in ("win32", "freebsd14", "cygwin"):
        monkeypatch.setattr(sys, "platform", plat)
        assert s._keychain_enabled() is False and s.keychain_available() is False
        assert s.keychain_status()["reason"] == "unsupported_platform"
    assert kr.calls == []


def test_the_keychain_private_names_still_exist():
    from ops_dashboard import secrets_store as s
    for n in ("_kc_get", "_kc_write", "_kc_delete", "_kc_set", "_keychain_enabled", "_SERVICE",
              "keychain_available", "keychain_get", "keychain_set", "keychain_delete"):
        assert hasattr(s, n), n


# ── what the app tells the user ──────────────────────────────────────────────

def test_platform_caps_explain_a_missing_secret_service(ss, monkeypatch):
    from ops_dashboard import platform_caps
    s, _ = ss
    caps = platform_caps.capabilities(backend="bwrap")["keychain"]
    assert caps == {"available": True, "reason": None, "message": ""}
    s._ss_reset()
    _install(monkeypatch, FakeKeyring(priority_error="no bus"))
    caps = platform_caps.capabilities(backend="bwrap")["keychain"]
    assert caps["available"] is False and caps["reason"] == "no_secret_service"
    assert "D-Bus" in caps["message"] and "vault" in caps["message"]


def test_platform_caps_message_is_unchanged_off_linux(monkeypatch):
    from ops_dashboard import platform_caps
    monkeypatch.setattr(sys, "platform", "win32")
    msg = platform_caps.keychain_message("unsupported_platform")
    assert msg.startswith("Keys are kept in an encrypted file") and "Windows" in msg


# ── the sandbox keeps working, and keeps the keyring away ────────────────────

def test_sandbox_never_needs_the_secret_service_or_inherits_the_bus():
    from ops_dashboard.sessions import sandbox
    env = sandbox.build_env(path_dirs=[], home="/h", tmp="/t")
    assert not [k for k in env if k.startswith(("DBUS", "XDG_RUNTIME", "SSH_", "GNOME_KEYRING"))]
    src = Path(sandbox.__file__).read_text()
    assert "secrets_store" not in src and "import keyring" not in src


def test_bwrap_hides_the_keyring_files_and_macos_profile_is_unchanged(tmp_path, monkeypatch):
    from ops_dashboard.sessions import sandbox
    home = tmp_path / "home"
    (home / ".local/share/keyrings").mkdir(parents=True)
    (home / ".local/share/kwalletd").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "_home", lambda: home.resolve())
    wt, tmp = tmp_path / "wt", tmp_path / "run"
    wt.mkdir()
    tmp.mkdir()
    argv = sandbox.bwrap_argv(["true"], worktree=wt, tmp=tmp, cwd=wt)
    hidden = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert str((home / ".local/share/keyrings").resolve()) in hidden
    assert str((home / ".local/share/kwalletd").resolve()) in hidden
    assert "keyrings" not in sandbox.seatbelt_profile(worktree=wt, tmp=tmp)
    assert ".local/share/keyrings" not in "".join(sandbox.SENSITIVE_HOME_PATHS)


# ── review findings (PR 72) ──────────────────────────────────────────────────

def test_a_failed_read_never_creates_a_replacement_secret(ss):
    """A read that errors is not "no such item": the Fernet key must not be
    regenerated (and overwrite the real one) because the keychain hiccuped."""
    s, kr = ss
    s.set_secret("fernet-key", SECRET)
    kr.mode = "fail-get"
    made = []
    with pytest.raises(s.SecretUnavailable):
        s.get_or_create_secret("fernet-key", lambda: made.append(1) or "NEW")
    assert made == [] and kr.items == {("com.conductloop", "fernet-key"): SECRET}
    kr.mode = "hang-get"
    with pytest.raises(s.SecretUnavailable):
        s.get_or_create_secret("fernet-key", lambda: made.append(1) or "NEW")
    assert made == [] and kr.items == {("com.conductloop", "fernet-key"): SECRET}
    kr.mode = ""
    assert s.get_or_create_secret("fernet-key", lambda: "NEW") == SECRET     # recovers


def test_a_missing_secret_is_still_created_when_the_keychain_answers(ss):
    s, kr = ss
    assert s.get_or_create_secret("fernet-key", lambda: SECRET) == SECRET
    assert kr.items == {("com.conductloop", "fernet-key"): SECRET}


def test_plain_readers_still_get_none_on_a_failed_read(ss):
    s, kr = ss
    s.set_secret("k", SECRET)
    kr.mode = "fail-get"
    assert s.get_secret("k") is None and s.keychain_get("k") is None


def test_a_flaky_service_at_startup_does_not_create_the_secret(ss, monkeypatch):
    """The probe timed out (a service is there but not answering): the secret may
    live in it, so do not create one. With no service at all, the file path stays."""
    s, kr = ss
    kr.mode = "hang-get"
    monkeypatch.setattr(s, "_SS_PROBE_TIMEOUT", 0.2)
    # the probe's own read hangs only for non-probe names, so make the probe time out
    kr.priority_error = None
    s._ss_state.update(kr=None, reason="timeout", at=s.time.monotonic())
    with pytest.raises(s.SecretUnavailable):
        s.get_or_create_secret("fernet-key", lambda: "NEW")
    s._ss_state.update(kr=None, reason="no_secret_service", at=s.time.monotonic())
    assert s.get_or_create_secret("fernet-key", lambda: "NEW") == "NEW"
    assert (Path(s._secrets_dir()) / ".fernet-key.secret").read_text() == "NEW"


def test_a_file_copy_is_used_even_when_the_keychain_cannot_be_read(ss):
    s, kr = ss
    s.set_secret("k", SECRET)
    s._file("k").parent.mkdir(parents=True, exist_ok=True)
    s._file("k").write_text("from-file")
    kr.mode = "fail-get"
    assert s.get_or_create_secret("k", lambda: "NEW") == "from-file"


def test_db_connections_fail_loudly_instead_of_replacing_the_key(ss):
    from ops_dashboard import db_connections
    s, kr = ss
    s.set_secret("fernet-key", "dummy")
    kr.mode = "fail-get"
    with pytest.raises(s.SecretUnavailable):
        db_connections._fernet_key()
    assert kr.items[("com.conductloop", "fernet-key")] == "dummy"
    assert db_connections._decrypt_credentials("enc:AAAA") == {}      # no crash, no new key


def _wait(pred, timeout=3.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_a_timed_out_write_cannot_reappear_after_the_failure(ss):
    s, kr = ss
    assert s.keychain_available()
    kr.mode = "slow-set"
    assert s.keychain_set("late", SECRET) is False          # timed out, reported as failed
    assert ("com.conductloop", "late") not in kr.items
    kr.release.set()                                         # the daemon thread now finishes...
    assert _wait(lambda: any(c[0] == "delete" and c[2] == "late" for c in kr.calls))
    assert _wait(lambda: ("com.conductloop", "late") not in kr.items)   # ...and undoes itself
    assert s.keychain_get("late") is None
    assert not s._ss_name_lock("late").locked() or _wait(lambda: not s._ss_name_lock("late").locked())


def test_a_hung_change_blocks_the_next_one_for_that_name_only(ss):
    s, kr = ss
    assert s.keychain_available()
    kr.mode = "slow-set"
    assert s.keychain_set("a", SECRET) is False
    kr.mode = ""
    assert s._ss_write("a", "second") is False               # still busy: no interleaving
    assert s._ss_write("b", "other") is True                 # other secrets are not affected
    kr.release.set()
    assert _wait(lambda: not s._ss_name_lock("a").locked())
    assert ("com.conductloop", "a") not in kr.items          # the late write was undone
    assert s._ss_write("a", "third") is True and s.keychain_get("a") == "third"


def test_a_successful_write_is_not_undone(ss):
    s, kr = ss
    assert s.keychain_set("ok", SECRET) is True
    import time
    time.sleep(0.1)
    assert kr.items == {("com.conductloop", "ok"): SECRET}
    assert not any(c[0] == "delete" and c[2] == "ok" for c in kr.calls)

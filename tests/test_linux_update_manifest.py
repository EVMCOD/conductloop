"""scripts/linux_update_manifest.py: the Linux AppImage updater manifest.

Nothing here signs with a real key: the repository's own public signature of
v1.2.0 (key id 770EA4C292DC6BD2) stands in for "a signature from the release
key", and a throwaway Ed25519 key is generated for the tests that verify a
signature over a file. The script never publishes anything, so neither do these.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "linux_update_manifest.py"
_spec = importlib.util.spec_from_file_location("linux_update_manifest", SCRIPT)
assert _spec and _spec.loader
lum = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lum)

CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
# Public signature of ConductLoop-1.2.0.app.tar.gz (from tests/test_updater_pubkey.py).
RELEASE_KEY_SIG = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVUU2E5eVN3cVFPZDREd1FpVHdTMkEyYy9TQURvUEZNVmNB"
    "QlU4TlJVSklmM05BanB1RnZHa3grYXZRYTROOHg0QU92UFExWHVXOGRsUVVDU1VPMFRVRm1PZGJxU3IxTXdVPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVz"
    "dGFtcDoxNzg0ODI4NTM1CWZpbGU6Q29uZHVjdExvb3AuYXBwLnRhci5negpjeHg3SktjN1dCUk1wQXlRZXVQMUtzRFcrc255aGNzQjNORUZIK0FvUFdD"
    "WDQydFRXQWhnUjR5T1NvYU1XdU9XcDU4Y1MzNEVrNzNvTEJkRWI0ZFZCZz09Cg=="
)
DATE = "2026-01-02T03:04:05Z"


@pytest.fixture()
def sig_file(tmp_path):
    p = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage.sig"
    p.write_text(RELEASE_KEY_SIG + "\n")
    return p


def _make(sig_file, **kw):
    kw.setdefault("version", "1.2.3")
    kw.setdefault("pub_date", DATE)
    return lum.make_manifest(sig_file=sig_file, **kw)


# ── a throwaway minisign key, to verify a signature over a file ──────────────

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


class Throwaway:
    def __init__(self, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.sk = Ed25519PrivateKey.generate()
        self.keyid = bytes.fromhex("0123456789ABCDEF")[::-1]
        raw = self.sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        blob = _b64(b"Ed" + self.keyid + raw)
        pub_text = "untrusted comment: throwaway test pubkey\n" + blob + "\n"
        conf = json.loads(CONFIG.read_text())
        conf["plugins"]["updater"]["pubkey"] = _b64(pub_text.encode())
        self.config = tmp_path / "tauri.conf.json"
        self.config.write_text(json.dumps(conf))

    def sign(self, data: bytes, algo: bytes = b"ED", comment: str = "timestamp:1\tfile:x", global_ok: bool = True,
             lines: int = 4) -> str:
        """A full minisign envelope: file signature, trusted comment, global signature."""
        msg = hashlib.blake2b(data, digest_size=64).digest() if algo == b"ED" else data
        file_sig = self.sk.sign(msg)
        blob = algo + self.keyid + file_sig
        global_sig = self.sk.sign(file_sig + comment.encode()) if global_ok else bytes(64)
        parts = ["untrusted comment: signature from tauri secret key", _b64(blob),
                 "trusted comment: " + comment, _b64(global_sig)]
        return _b64(("\n".join(parts[:lines]) + "\n").encode())


@pytest.fixture()
def tk(tmp_path):
    pytest.importorskip("cryptography")
    return Throwaway(tmp_path)


# ── the manifest ─────────────────────────────────────────────────────────────

def test_manifest_has_exactly_the_appimage_entry(sig_file):
    m = _make(sig_file)
    assert m == {
        "version": "1.2.3",
        "pub_date": DATE,
        "platforms": {"linux-x86_64-appimage": {
            "signature": RELEASE_KEY_SIG,
            "url": "https://github.com/EVMCOD/conductloop-releases/releases/download/v1.2.3/"
                   "ConductLoop-1.2.3-x86_64.AppImage"}},
    }
    assert lum.check_manifest(json.loads(json.dumps(m))) == "1.2.3"


def test_same_shape_as_the_macos_manifest(sig_file):
    mac = (ROOT / "scripts" / "release-desktop.sh").read_text()
    for field in ('"version"', '"pub_date"', '"platforms"', '"signature"', '"url"'):
        assert field in mac
    assert set(_make(sig_file)) == {"version", "pub_date", "platforms"}
    assert set(_make(sig_file)["platforms"]["linux-x86_64-appimage"]) == {"signature", "url"}


def test_notes_are_optional(sig_file):
    assert "notes" not in _make(sig_file)
    assert _make(sig_file, notes="Linux AppImage")["notes"] == "Linux AppImage"


def test_a_custom_asset_name_changes_only_the_url(sig_file):
    m = _make(sig_file, asset="ConductLoop_1.2.3_amd64.AppImage")
    assert m["platforms"]["linux-x86_64-appimage"]["url"].endswith("/v1.2.3/ConductLoop_1.2.3_amd64.AppImage")


@pytest.mark.parametrize("asset", ["ConductLoop.deb", "ConductLoop_1.2.3_amd64.deb", "x.tar.gz", "../x.AppImage",
                                   "a/b.AppImage", ".AppImage", "x.AppImage?raw=1", "x.AppImage\n"])
def test_only_a_plain_appimage_name_is_accepted(sig_file, asset):
    with pytest.raises(lum.ManifestError):
        _make(sig_file, asset=asset)


@pytest.mark.parametrize("version", ["", "1.2", "v1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3/../x", "latest", "1.2.x"])
def test_bad_versions_are_refused(sig_file, version):
    with pytest.raises(lum.ManifestError):
        _make(sig_file, version=version)


def test_prerelease_versions_are_allowed(sig_file):
    assert _make(sig_file, version="1.3.0-rc.1")["version"] == "1.3.0-rc.1"


@pytest.mark.parametrize("repo", ["", "conductloop", "a/b/c", "a b/c", "https://x/y"])
def test_bad_repos_are_refused(sig_file, repo):
    with pytest.raises(lum.ManifestError):
        _make(sig_file, repo=repo)


def test_bad_pub_date_is_refused(sig_file):
    with pytest.raises(lum.ManifestError):
        _make(sig_file, pub_date="yesterday")


# ── the signature is required, and must be the release key's ─────────────────

def test_no_signature_means_no_manifest(tmp_path):
    for content in ("", "\n", "TODO-signature", "a\nb\n", "<SIGNATURE>"):
        p = tmp_path / "x.sig"
        p.write_text(content)
        with pytest.raises(lum.ManifestError):
            _make(p)
    with pytest.raises(lum.ManifestError):
        _make(tmp_path / "missing.sig")


def test_a_signature_from_another_key_is_refused(tk, tmp_path):
    p = tmp_path / "x.sig"
    p.write_text(tk.sign(b"data"))
    with pytest.raises(lum.ManifestError, match="mismatch"):
        _make(p)                                   # default config: the release key


def test_expected_key_id_is_enforced(sig_file):
    with pytest.raises(lum.ManifestError, match="mismatch"):
        _make(sig_file, expected_keyid="0123456789ABCDEF")
    assert _make(sig_file, expected_keyid="770EA4C292DC6BD2")


def test_the_pinned_key_id_is_the_release_key():
    assert (ROOT / "desktop" / "UPDATER_KEY_ID").read_text().strip() == "770EA4C292DC6BD2"
    assert lum.cup.signature_keyid(RELEASE_KEY_SIG) == "770EA4C292DC6BD2"


# ── verifying the signature over the AppImage ────────────────────────────────

def test_signature_over_the_appimage_is_verified(tk, tmp_path):
    img = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage"
    img.write_bytes(b"\x7fELF fake appimage" * 1000)
    sig = tmp_path / "x.sig"
    sig.write_text(tk.sign(img.read_bytes()))
    m = _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")
    assert m["platforms"]["linux-x86_64-appimage"]["signature"] == sig.read_text().strip()


def test_a_changed_appimage_fails_verification(tk, tmp_path):
    img = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage"
    img.write_bytes(b"original")
    sig = tmp_path / "x.sig"
    sig.write_text(tk.sign(b"original"))
    img.write_bytes(b"tampered")
    with pytest.raises(lum.ManifestError, match="does not match"):
        _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")


def test_the_appimage_must_be_the_asset_the_url_names(tk, tmp_path):
    img = tmp_path / "other.AppImage"
    img.write_bytes(b"x")
    sig = tmp_path / "x.sig"
    sig.write_text(tk.sign(b"x"))
    with pytest.raises(lum.ManifestError, match="release asset"):
        _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")


def test_a_legacy_signature_is_refused_because_the_updater_rejects_it(tk, tmp_path):
    img = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage"
    img.write_bytes(b"x")
    sig = tmp_path / "x.sig"
    sig.write_text(tk.sign(b"x", algo=b"Ed"))
    with pytest.raises(lum.ManifestError, match="legacy"):
        _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")


def test_the_real_release_signature_does_not_verify_some_other_file(sig_file, tmp_path):
    pytest.importorskip("cryptography")
    img = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage"
    img.write_bytes(b"not the file that was signed")
    with pytest.raises(lum.ManifestError, match="does not match"):
        _make(sig_file, appimage=img)


# ── checking an existing manifest ────────────────────────────────────────────

def _good(sig_file):
    return json.loads(json.dumps(_make(sig_file)))


def test_check_rejects_what_the_linux_manifest_must_never_hold(sig_file):
    def bad(mutate, match=None):
        m = _good(sig_file)
        mutate(m)
        with pytest.raises(lum.ManifestError, match=match):
            lum.check_manifest(m)

    def plats(m):
        return m["platforms"]

    bad(lambda m: plats(m).update({"linux-x86_64-deb": plats(m)["linux-x86_64-appimage"]}), "exactly")
    bad(lambda m: plats(m).update({"darwin-aarch64": plats(m).pop("linux-x86_64-appimage")}), "exactly")
    bad(lambda m: plats(m).update({"linux-x86_64": plats(m).pop("linux-x86_64-appimage")}), "exactly")
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(url="http://github.com/a/b/releases/download/v1.2.3/x.AppImage"))
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(
        url="https://example.com/a/b/releases/download/v1.2.3/x.AppImage"))
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(
        url="https://github.com/EVMCOD/conductloop-releases/releases/download/v9.9.9/x.AppImage"), "v9.9.9")
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(
        url="https://github.com/EVMCOD/conductloop-releases/releases/download/v1.2.3/x.deb"))
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(signature=""))
    bad(lambda m: plats(m)["linux-x86_64-appimage"].update(extra="x"), "exactly")
    bad(lambda m: m.pop("pub_date"), "pub_date")
    bad(lambda m: m.update(version="one"))
    with pytest.raises(lum.ManifestError):
        lum.check_manifest([])


# ── the command line ─────────────────────────────────────────────────────────

def _run(*args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TAURI_SIGNING")}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=60)


def test_cli_prints_to_stdout_and_writes_nothing_by_default(sig_file, tmp_path):
    r = _run("--version", "1.2.3", "--sig-file", str(sig_file), "--pub-date", DATE, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["platforms"]["linux-x86_64-appimage"]["signature"] == RELEASE_KEY_SIG
    assert not list(tmp_path.glob("*.json"))


def test_cli_output_and_check_round_trip(sig_file, tmp_path):
    out = tmp_path / "update-linux-x86_64.json"
    r = _run("--version", "1.2.3", "--sig-file", str(sig_file), "--output", str(out))
    assert r.returncode == 0 and out.is_file()
    assert "nothing was signed, uploaded or published" in r.stderr
    c = _run("--check", str(out))
    assert c.returncode == 0 and "1.2.3" in c.stdout


def test_cli_never_overwrites_the_macos_manifest_name(sig_file, tmp_path):
    r = _run("--version", "1.2.3", "--sig-file", str(sig_file), "--output", str(tmp_path / "update-macos-aarch64.json"))
    assert r.returncode == 1 and "macOS manifest" in r.stderr
    assert not (tmp_path / "update-macos-aarch64.json").exists()


def test_cli_fails_cleanly_without_a_signature(tmp_path):
    empty = tmp_path / "x.sig"
    empty.write_text("")
    r = _run("--version", "1.2.3", "--sig-file", str(empty))
    assert r.returncode == 1 and r.stderr.startswith("✗") and r.stdout == ""
    assert _run("--version", "1.2.3").returncode == 2        # --sig-file is required


def test_the_script_asks_for_no_key_and_reads_no_secret():
    src = SCRIPT.read_text()
    for banned in ("TAURI_SIGNING", "PRIVATE_KEY", ".conductloop-keys", "os.environ", "subprocess",
                   "urllib", "requests", "httpx", "gh release", "socket"):
        assert banned not in src, banned


# ── what this change must not touch ──────────────────────────────────────────

def test_the_macos_updater_is_untouched():
    conf = json.loads(CONFIG.read_text())
    assert conf["plugins"]["updater"]["endpoints"] == [
        "https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json"]
    assert conf["bundle"]["targets"] == ["dmg", "app"] and conf["bundle"]["createUpdaterArtifacts"] is True
    mac = (ROOT / "scripts" / "release-desktop.sh").read_text()
    assert "darwin-aarch64" in mac and "linux" not in mac.lower() and "appimage" not in mac.lower()


def test_the_linux_build_still_has_no_updater():
    linux = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.linux.conf.json").read_text())
    assert linux["bundle"]["createUpdaterArtifacts"] is False
    assert "plugins" not in linux
    build = (ROOT / "scripts" / "build-linux.sh").read_text()
    assert "unset TAURI_SIGNING_PRIVATE_KEY" in build
    assert lum.MANIFEST_NAME not in build and "linux_update_manifest" not in build
    lib = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    assert "!cfg!(target_os = \"linux\")" in lib          # no update check at start-up on Linux


def test_the_bundle_workflow_publishes_nothing():
    wf = (ROOT / ".github" / "workflows" / "linux-bundles.yml").read_text()
    assert "runs-on: ubuntu-22.04" in wf and "contents: read" in wf
    for banned in ("gh release", "softprops", "create-release", "contents: write", "secrets.", "TAURI_SIGNING",
                   "conductloop-releases", "linux_update_manifest", "update-linux"):
        assert banned not in wf, banned


# ── review findings (PR 74) ──────────────────────────────────────────────────

def _signed_appimage(tk, tmp_path, **sign_kw):
    img = tmp_path / "ConductLoop-1.2.3-x86_64.AppImage"
    img.write_bytes(b"appimage bytes" * 100)
    sig = tmp_path / "x.sig"
    sig.write_text(tk.sign(img.read_bytes(), **sign_kw))
    return img, sig


def test_the_real_release_signature_passes_the_global_signature_check():
    pub = lum.cup.embedded_pubkey(CONFIG)
    lum.verify_global_signature(RELEASE_KEY_SIG, pub)          # does not raise


def test_a_complete_envelope_with_a_valid_global_signature_is_accepted(tk, tmp_path):
    img, sig = _signed_appimage(tk, tmp_path)
    assert _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")


@pytest.mark.parametrize("kw", [
    {"global_ok": False},                       # right file signature, garbage global signature
    {"lines": 2},                               # no trusted comment, no global signature
    {"lines": 3},                               # trusted comment but no global signature
])
def test_a_bad_global_signature_is_refused_with_and_without_the_appimage(tk, tmp_path, kw):
    img, sig = _signed_appimage(tk, tmp_path, **kw)
    with pytest.raises(lum.ManifestError, match="global signature|trusted comment"):
        _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")
    with pytest.raises(lum.ManifestError, match="global signature|trusted comment"):
        _make(sig, config=tk.config, expected_keyid="0123456789ABCDEF")      # even without --appimage


def test_an_edited_trusted_comment_is_refused(tk, tmp_path):
    img, sig = _signed_appimage(tk, tmp_path)
    text = base64.b64decode(sig.read_text()).decode().replace("timestamp:1", "timestamp:2")
    sig.write_text(_b64(text.encode()))
    with pytest.raises(lum.ManifestError, match="does not match the trusted comment"):
        _make(sig, config=tk.config, appimage=img, expected_keyid="0123456789ABCDEF")


def test_the_real_signature_with_an_edited_trusted_comment_is_refused(sig_file, tmp_path):
    text = base64.b64decode(RELEASE_KEY_SIG).decode().replace("timestamp:", "timestamp:9")
    bad = tmp_path / "bad.sig"
    bad.write_text(_b64(text.encode()))
    with pytest.raises(lum.ManifestError, match="trusted comment"):
        _make(bad)


def test_check_manifest_refuses_a_signature_with_a_broken_global_signature(tk, tmp_path):
    _, sig = _signed_appimage(tk, tmp_path, global_ok=False)
    good = {"version": "1.2.3", "pub_date": DATE, "platforms": {"linux-x86_64-appimage": {
        "signature": sig.read_text().strip(),
        "url": "https://github.com/EVMCOD/conductloop-releases/releases/download/v1.2.3/"
               "ConductLoop-1.2.3-x86_64.AppImage"}}}
    with pytest.raises(lum.ManifestError, match="global signature"):
        lum.check_manifest(good, config=tk.config, expected_keyid="0123456789ABCDEF")


@pytest.mark.parametrize("date", ["2026-02-31T03:04:05Z", "2026-02-29T03:04:05Z", "2026-04-31T00:00:00Z",
                                  "2026-13-01T00:00:00Z", "2026-00-10T00:00:00Z", "2026-01-00T00:00:00Z",
                                  "2026-01-32T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T23:60:00Z",
                                  "2026-01-01T23:59:60Z", "0000-01-01T00:00:00Z", "2026-1-1T00:00:00Z",
                                  "2026-01-01T00:00:00+01:00", "2026-01-01 00:00:00Z", "2026-01-01T00:00:00Z\n"])
def test_impossible_or_non_utc_dates_are_refused_in_both_paths(sig_file, date):
    with pytest.raises(lum.ManifestError, match="pub_date"):
        _make(sig_file, pub_date=date)
    m = _make(sig_file)
    m["pub_date"] = date
    with pytest.raises(lum.ManifestError, match="pub_date"):
        lum.check_manifest(m)


@pytest.mark.parametrize("date", ["2028-02-29T23:59:59Z", "2026-12-31T00:00:00Z", "2026-01-01T00:00:00Z"])
def test_real_dates_including_a_leap_day_are_accepted(sig_file, date):
    assert _make(sig_file, pub_date=date)["pub_date"] == date


def test_the_cli_rejects_an_impossible_date(sig_file):
    r = _run("--version", "1.2.3", "--sig-file", str(sig_file), "--pub-date", "2026-02-31T03:04:05Z")
    assert r.returncode == 1 and "pub_date" in r.stderr and r.stdout == ""

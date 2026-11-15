"""scripts/release-platforms.sh: publishing the Linux and Windows builds.

The Linux and Windows releases used to be done by hand, one `gh workflow run`
at a time, with the downloads, the checksums, the upload and the notes as
separate steps. These tests pin what the script must do (dispatch both
workflows, wait, download, checksum, upload to the existing tag, rewrite the
notes, check every URL) and, above all, that the dry run touches nothing:
no dispatch, no download, no upload, no release edit, no URL fetched.

Nothing here touches the network, GitHub or any credential: the script is run
against a `gh` and a `curl` that answer from a directory of the test, and the
dry run is run against a `gh` and a `curl` that fail loudly if they are ever
called.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release-platforms.sh"
NOTES = ROOT / "scripts" / "release_notes.py"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
TEXT = SCRIPT.read_text(encoding="utf-8")
CODE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))

LINUX_WORKFLOW = ROOT / ".github" / "workflows" / "linux-bundles.yml"
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "windows-bundles.yml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _artifact_names(path: Path) -> set[str]:
    """The bundle artifacts of a workflow (not its smoke-test screenshots)."""
    doc = _workflow(path)
    names = set()
    for job in doc["jobs"].values():
        for step in job["steps"]:
            with_ = step.get("with") or {}
            if str(step.get("uses", "")).startswith("actions/upload-artifact@") and "bundle" in str(
                with_.get("path", "")
            ):
                names.add(with_["name"])
    return names


# ── the script itself ────────────────────────────────────────────────────────


@pytest.mark.posix_perms
def test_the_script_is_executable_and_valid_bash():
    assert SCRIPT.stat().st_mode & 0o111, "scripts/release-platforms.sh must be executable"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_it_never_creates_a_tag_or_a_release():
    """The tag is the macOS release's: scripts/release-desktop.sh creates it.
    This script only adds files to that release."""
    for word in ("release create", "release delete", "git tag", "--target", "--draft"):
        assert word not in CODE, word


def test_it_never_pushes_anything():
    for word in ("git push", "git commit", "git remote"):
        assert word not in CODE, word


def test_it_dispatches_the_workflows_that_exist():
    """The workflows are named after the files in .github/workflows/."""
    for path in (LINUX_WORKFLOW, WINDOWS_WORKFLOW):
        assert path.name in CODE, path.name
    assert "gh workflow run" in CODE
    assert "--ref main" in CODE


def test_it_downloads_the_artifacts_the_workflows_upload():
    for name in _artifact_names(LINUX_WORKFLOW) | _artifact_names(WINDOWS_WORKFLOW):
        assert name in CODE, name
    assert "gh run download" in CODE


def test_it_waits_for_the_runs_before_downloading_them():
    assert "gh run watch" in CODE
    assert "--exit-status" in CODE
    assert CODE.index("gh run watch") < CODE.index("gh run download")


def test_it_checksums_the_four_linux_and_windows_files():
    assert "SHA256SUMS" in CODE
    for template in (
        "ConductLoop_${VERSION}_amd64.AppImage",
        "ConductLoop_${VERSION}_amd64.deb",
        "ConductLoop_${VERSION}_x64-setup.exe",
        "ConductLoop_${VERSION}_x64_en-US.msi",
    ):
        assert template in CODE, template
    assert "sha256sum" in CODE


def test_it_uploads_to_the_existing_tag_and_rewrites_the_notes():
    assert "gh release upload" in CODE
    assert "gh release edit" in CODE
    assert "--clobber" in CODE
    assert "EVMCOD/conductloop-releases" in CODE
    assert "release_notes.py" in CODE
    assert "--notes-file" in CODE


def test_it_verifies_every_download_url():
    assert "%{http_code}" in CODE
    assert '"200"' in CODE
    assert "curl" in CODE
    # The URLs are the ones the notes publish, so the check and the page
    # cannot drift apart.
    assert "release_notes.py" in CODE and "--urls" in CODE


def test_it_checks_the_tag_exists_before_publishing_anything():
    assert "gh release view" in CODE
    assert CODE.index("gh release view") < CODE.index('gh workflow run "$LINUX_WORKFLOW"')


def test_the_dry_run_is_documented_in_the_header():
    assert "--dry-run" in TEXT
    assert "touches nothing" in TEXT


def test_an_unknown_option_is_refused():
    r = subprocess.run([str(SCRIPT), VERSION, "--nope"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown option" in r.stderr


def test_the_help_says_what_it_would_do():
    r = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.startswith("release-platforms.sh")
    assert "--dry-run" in r.stdout


def test_a_bad_version_is_refused():
    r = subprocess.run([str(SCRIPT), "1.2"], capture_output=True, text=True)
    assert r.returncode == 2 and "X.Y.Z" in r.stderr


def test_it_needs_a_version():
    r = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 2


# ── the dry run ──────────────────────────────────────────────────────────────


def _fake_bin(tmp_path: Path) -> Path:
    """A directory with a `gh` and a `curl` that fail if they are ever called."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "called"
    for name in ("gh", "curl"):
        path = bindir / name
        path.write_text(f"#!/bin/sh\ntouch '{marker}'\necho '{name} was called' >&2\nexit 99\n")
        path.chmod(0o755)
    return bindir


def _dry_run(tmp_path: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bindir = _fake_bin(tmp_path)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    env.update(extra_env or {})
    return subprocess.run(
        [str(SCRIPT), VERSION, "--dry-run"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def test_the_dry_run_calls_nothing(tmp_path):
    """No dispatch, no download, no upload, no release edit, no URL fetched."""
    r = _dry_run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "called").exists(), "the dry run called gh or curl"
    assert not (tmp_path / "bin" / "called").exists()


def test_the_dry_run_writes_no_file(tmp_path):
    r = _dry_run(tmp_path)
    assert r.returncode == 0
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "bin"]
    assert leftovers == [], leftovers


def test_the_dry_run_needs_no_credential(tmp_path):
    r = _dry_run(tmp_path, {"GH_TOKEN": "", "GITHUB_TOKEN": "", "GH_CONFIG_DIR": str(tmp_path / "gh")})
    assert r.returncode == 0, r.stderr


def test_the_dry_run_prints_the_plan(tmp_path):
    r = _dry_run(tmp_path)
    out = r.stdout
    assert "Dry run" in out
    assert "gh workflow run linux-bundles.yml --ref main" in out
    assert "gh workflow run windows-bundles.yml --ref main" in out
    assert "gh release upload v1.2.28" in out
    assert "gh release edit v1.2.28" in out
    for name in _artifact_names(LINUX_WORKFLOW) | _artifact_names(WINDOWS_WORKFLOW):
        assert name in out, name


def test_the_dry_run_prints_the_commands_it_would_run(tmp_path):
    """Not only the plan: the commands too, so the maintainer can read what
    the script is going to do before letting it do it."""
    r = _dry_run(tmp_path)
    for command in (
        "gh workflow run",
        "gh run watch",
        "gh run download",
        "sha256sum",
        "gh release upload",
        "gh release edit",
        "curl",
    ):
        assert command in r.stdout, command


# ── the URLs it checks are the ones the notes publish ───────────────────────


def _notes_urls() -> list[str]:
    out = subprocess.run(
        [sys.executable, str(NOTES), VERSION, "--urls"], capture_output=True, text=True, check=True
    ).stdout.split()
    return out


def test_every_download_url_of_the_release_is_checked():
    urls = _notes_urls()
    assert len(urls) == 5
    # The script gets them from the notes builder, so it checks every one.
    assert "--urls" in CODE
    for url in urls:
        assert url.split("/")[-1] in CODE or "--urls" in CODE


def test_the_checked_urls_are_the_ones_of_the_tag():
    base = f"https://github.com/EVMCOD/conductloop-releases/releases/download/v{VERSION}/"
    assert all(u.startswith(base) for u in _notes_urls())


# ── the publish path, against a gh and a curl that answer from the test ─────

FAKE_GH = """#!/bin/sh
# A fake gh: it never touches the network, it only records what it was asked.
echo "$@" >> "$FAKE_GH_LOG"
cmd="$1"; shift
case "$cmd" in
  auth) exit 0 ;;
  workflow) exit 0 ;;
  release)
    sub="$1"; shift
    case "$sub" in
      view)
        if [ -n "$FAKE_GH_NO_TAG" ]; then exit 1; fi
        exit 0 ;;
      upload)
        for arg in "$@"; do
          if [ -f "$arg" ]; then
            echo "$(basename "$arg")" >> "$FAKE_GH_UPLOADED"
            if [ "$(basename "$arg")" = "SHA256SUMS" ]; then
              cp "$arg" "$FAKE_GH_SUMS"
            fi
          fi
        done
        exit 0 ;;
      edit)
        while [ $# -gt 0 ]; do
          if [ "$1" = "--notes-file" ]; then cp "$2" "$FAKE_GH_NOTES"; fi
          shift
        done
        exit 0 ;;
    esac
    ;;
  run)
    sub="$1"; shift
    case "$sub" in
      list) echo "424242" ;;
      watch) exit 0 ;;
      download)
        name=""; dir=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -n) name="$2"; shift ;;
            -D) dir="$2"; shift ;;
          esac
          shift
        done
        mkdir -p "$dir"
        if [ -n "$FAKE_GH_EMPTY_ARTIFACT" ]; then exit 0; fi
        case "$name" in
          conductloop-linux-appimage) : > "$dir/ConductLoop_${FAKE_VERSION}_amd64.AppImage" ;;
          conductloop-linux-deb) : > "$dir/ConductLoop_${FAKE_VERSION}_amd64.deb" ;;
          conductloop-windows-nsis) : > "$dir/ConductLoop_${FAKE_VERSION}_x64-setup.exe" ;;
          conductloop-windows-msi) : > "$dir/ConductLoop_${FAKE_VERSION}_x64_en-US.msi" ;;
        esac
        exit 0 ;;
    esac
    ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/sh
# A fake curl: it answers 200 for the release URLs and records them.
# printf, not echo -n: /bin/sh is POSIX-mode bash here, where echo -n prints
# the flag itself.
echo "$*" >> "$FAKE_CURL_LOG"
url=""
for arg in "$@"; do url="$arg"; done
case "$url" in
  *conductloop-releases/releases/download/*)
    if [ -n "$FAKE_CURL_FAIL" ]; then printf 404; else printf 200; fi ;;
  *) printf 404 ;;
esac
"""


@pytest.fixture()
def fake_gh(tmp_path: Path) -> dict:
    """A gh and a curl that answer from the test, and the files they write."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bindir / "curl").write_text(FAKE_CURL, encoding="utf-8")
    for name in ("gh", "curl"):
        (bindir / name).chmod(0o755)
    files = {
        "log": tmp_path / "gh.log",
        "uploaded": tmp_path / "uploaded.txt",
        "sums": tmp_path / "SHA256SUMS",
        "notes": tmp_path / "notes.md",
        "urls": tmp_path / "urls.txt",
    }
    for path in files.values():
        path.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_VERSION": VERSION,
        "FAKE_GH_LOG": str(files["log"]),
        "FAKE_GH_UPLOADED": str(files["uploaded"]),
        "FAKE_GH_SUMS": str(files["sums"]),
        "FAKE_GH_NOTES": str(files["notes"]),
        "FAKE_CURL_LOG": str(files["urls"]),
    }
    return {"env": env, **files}


def test_the_publish_path_uploads_the_four_files_and_the_checksums(fake_gh):
    r = subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT
    )
    assert r.returncode == 0, r.stderr
    uploaded = fake_gh["uploaded"].read_text(encoding="utf-8").split()
    for name in (
        f"ConductLoop_{VERSION}_amd64.AppImage",
        f"ConductLoop_{VERSION}_amd64.deb",
        f"ConductLoop_{VERSION}_x64-setup.exe",
        f"ConductLoop_{VERSION}_x64_en-US.msi",
        "SHA256SUMS",
    ):
        assert name in uploaded, uploaded


def test_the_checksums_cover_the_four_files(fake_gh):
    subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT, check=True
    )
    lines = fake_gh["sums"].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4, lines
    for line in lines:
        m = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        assert m, f"not a sha256sum line: {line!r}"
    assert {m.group(2) for m in (re.fullmatch(r"([0-9a-f]{64})  (\S+)", l) for l in lines)} == {
        f"ConductLoop_{VERSION}_amd64.AppImage",
        f"ConductLoop_{VERSION}_amd64.deb",
        f"ConductLoop_{VERSION}_x64-setup.exe",
        f"ConductLoop_{VERSION}_x64_en-US.msi",
    }


def test_the_release_notes_are_rewritten(fake_gh):
    subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT, check=True
    )
    notes = fake_gh["notes"].read_text(encoding="utf-8")
    assert notes.startswith(f"ConductLoop {VERSION} for macOS, Linux and Windows.")
    assert "## Downloads" in notes
    assert f"ConductLoop_{VERSION}_amd64.AppImage" in notes


def test_every_download_url_is_fetched_and_checked(fake_gh):
    subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT, check=True
    )
    fetched = fake_gh["urls"].read_text(encoding="utf-8")
    for url in _notes_urls():
        assert url in fetched, url


def test_it_dispatches_both_workflows_on_main(fake_gh):
    subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT, check=True
    )
    log = fake_gh["log"].read_text(encoding="utf-8")
    assert "workflow run linux-bundles.yml --ref main" in log
    assert "workflow run windows-bundles.yml --ref main" in log
    assert "run watch" in log and "run download" in log
    assert "release upload" in log and "release edit" in log


def test_it_never_creates_the_tag(fake_gh):
    subprocess.run(
        [str(SCRIPT), VERSION], capture_output=True, text=True, env=fake_gh["env"], cwd=ROOT, check=True
    )
    log = fake_gh["log"].read_text(encoding="utf-8")
    assert "release create" not in log
    assert "release delete" not in log


# ── the failures ─────────────────────────────────────────────────────────────


def test_a_missing_tag_stops_the_run_before_anything_is_published(fake_gh):
    """The tag is the macOS release's. If it is not there, this script has
    nothing to add to and must say so instead of creating it."""
    env = {**fake_gh["env"], "FAKE_GH_NO_TAG": "1"}
    r = subprocess.run([str(SCRIPT), VERSION], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "release-desktop.sh" in r.stderr
    assert "workflow run" not in fake_gh["log"].read_text(encoding="utf-8")


def test_a_url_that_does_not_answer_200_fails_the_run(fake_gh):
    env = {**fake_gh["env"], "URL_ATTEMPTS": "2", "URL_WAIT_SECONDS": "0", "FAKE_CURL_FAIL": "1"}
    r = subprocess.run([str(SCRIPT), VERSION], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "not 200" in r.stderr


def test_bundles_built_for_another_version_are_refused(fake_gh):
    """The version is in the file name. A bundle built for another version
    published under this release's name is the one mistake this script cannot
    make."""
    env = {**fake_gh["env"], "FAKE_VERSION": "9.9.9"}
    r = subprocess.run([str(SCRIPT), VERSION], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "was not built for version" in r.stderr
    assert fake_gh["uploaded"].read_text(encoding="utf-8").strip() == ""


def test_an_artifact_without_a_file_stops_the_run(fake_gh):
    """The build produced nothing: the script must say so and publish
    nothing, instead of uploading an empty file."""
    env = {**fake_gh["env"], "FAKE_GH_EMPTY_ARTIFACT": "1"}
    r = subprocess.run([str(SCRIPT), VERSION], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "the build produced no" in r.stderr
    assert fake_gh["uploaded"].read_text(encoding="utf-8").strip() == ""


def test_a_changelog_without_the_section_publishes_nothing(fake_gh, tmp_path):
    """The notes are built before the upload: a changelog that says nothing
    about this version stops the run with the release untouched."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## 0.0.1 - 2026-01-01 - Something else\n\n- Nothing.\n")
    env = {**fake_gh["env"], "CHANGELOG_FILE": str(changelog)}
    r = subprocess.run([str(SCRIPT), VERSION], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "no '##" in r.stderr
    assert fake_gh["uploaded"].read_text(encoding="utf-8").strip() == ""

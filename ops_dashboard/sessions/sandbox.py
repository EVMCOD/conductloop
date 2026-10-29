"""Run whitelisted test/lint commands for a session: no shell, clean env,
timeout, output cap, file-size cap, and an OS sandbox when one is available.

What the OS sandbox gives (design doc D2):

* **Linux — bubblewrap** (``bwrap``): own network namespace with no
  interfaces except loopback (no network at all), the whole filesystem
  mounted read-only except the worktree and a private temp dir, credential
  dirs (``~/.ssh``, ``~/.aws``…) and the ConductLoop data dir hidden,
  PID namespace + ``--die-with-parent`` so nothing outlives the run, and the
  Unix sockets on the filesystem (D-Bus, X11, ssh/gpg agent, Docker…) covered
  so they cannot be connected to (see "filesystem Unix sockets" below).
* **macOS — ``sandbox-exec``** (Seatbelt): ``(deny network*)``, writes denied
  outside the worktree / temp dir / ``/dev``, reads of credential dirs and
  the Keychain denied. ``sandbox-exec`` is deprecated by Apple but still
  shipped and used by major tools; the profile is probed at start-up and a
  profile that fails to load fails CLOSED (the command doesn't run).
* **Windows**: there is no backend and ``run`` always refuses, even with
  ``allow_unsandboxed`` (see ``platform_caps``).
* **No backend**: commands are REFUSED unless the caller passes
  ``allow_unsandboxed=True``; then they run with only the environment
  protections below and the result says ``sandbox: "none"``. Environment
  scrubbing does NOT stop network access: code under test (written by the
  agent) can open sockets. That is the honest limit of the fallback.

Always, with or without a sandbox: argv list (never a shell), environment
built from an allow-list (no inherited variables, so no API keys/tokens/
proxies/credential helpers), stdin closed, wall-clock timeout that kills the
whole process group, output capped (head + tail kept), RLIMIT_FSIZE on any
file written, no core dumps, npm/pip forced offline.
"""
from __future__ import annotations

import collections
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import platform_caps

DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_OUTPUT = 64 * 1024            # bytes kept (half head, half tail)
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024  # RLIMIT_FSIZE for the run

# Credential locations hidden from (Linux) or unreadable by (macOS) commands.
SENSITIVE_HOME_PATHS = (".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker",
                        ".config/gcloud", ".config/gh", ".config/hub", ".netrc",
                        ".git-credentials", ".npmrc", ".pypirc", ".yarnrc",
                        ".config/conductloop", "Library/Keychains",
                        "Library/Application Support/ConductLoop")


class SandboxUnavailable(RuntimeError):
    """No OS sandbox and the caller didn't explicitly allow running without one."""


@dataclass
class RunResult:
    exit_code: int | None
    output: str
    output_bytes: int          # total produced (before truncation)
    truncated: bool
    timed_out: bool
    duration_ms: int
    sandbox: str               # "bwrap" | "sandbox-exec" | "none"


# ── environment ────────────────────────────────────────────────────────────

def build_env(*, path_dirs: list[str], home: str, tmp: str) -> dict[str, str]:
    """A fresh environment: nothing is inherited from the dashboard process."""
    seen: list[str] = []
    for d in [*path_dirs, "/usr/bin", "/bin"]:
        if d and d not in seen:
            seen.append(d)
    return {
        "PATH": os.pathsep.join(seen),
        "HOME": home, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp,
        "XDG_CACHE_HOME": os.path.join(tmp, "cache"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1", "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        "MYPY_CACHE_DIR": os.path.join(tmp, "mypy_cache"),
        "RUFF_CACHE_DIR": os.path.join(tmp, "ruff_cache"),
        "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "npm_config_offline": "true", "npm_config_update_notifier": "false",
        "npm_config_audit": "false", "npm_config_fund": "false",
        "npm_config_cache": os.path.join(tmp, "npm-cache"),
        "no_proxy": "*", "NO_PROXY": "*",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


# ── backends ───────────────────────────────────────────────────────────────

def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def _data_dir() -> Path | None:
    try:
        from .. import db
        return Path(db._DB_DIR).resolve()
    except Exception:  # pragma: no cover - db import never fails in practice
        return None


# ── Linux: filesystem Unix sockets ─────────────────────────────────────────
# ``--ro-bind / /`` is recursive and ``--unshare-net`` only hides *abstract*
# sockets, so a command inside bwrap could still ``connect()`` to a socket that
# lives on the filesystem: the D-Bus session bus (``$XDG_RUNTIME_DIR/bus``), the
# X11 server (``/tmp/.X11-unix``), ssh-agent / gpg-agent, the Docker socket…
# Those are credentials and a way out of the sandbox (a bus or Docker call runs
# code outside it). Read-only does not stop ``connect()``; the paths must be
# covered. Directories that hold sockets get a tmpfs; every other socket found
# under /tmp and /run is covered with /dev/null (connect() then fails).
# Limit: sockets deeper than ``_SOCKET_SCAN_DEPTH`` or in other directories are
# not found; a seccomp filter on socket(AF_UNIX) would be the complete fix.

_SOCKET_SCAN_ROOTS = ("/tmp", "/run")
_SOCKET_SCAN_DEPTH = 3
_SOCKET_SCAN_MAX_ENTRIES = 20_000
_SOCKET_DIRS = ("/run/user", "/run/dbus", "/tmp/.X11-unix", "/tmp/.ICE-unix")
# Never replaced wholesale by a tmpfs, whatever an environment variable says.
_NEVER_HIDE = frozenset({"/", "/tmp", "/run", "/var", "/usr", "/etc", "/bin", "/lib", "/home",
                         "/dev", "/proc", "/sys", "/opt", "/root"})


def _is_socket(p: str | Path) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(p).st_mode)
    except OSError:
        return False


def _env_socket_paths(environ: Mapping[str, str]) -> list[str]:
    """Socket paths the parent's environment points at (agents, buses, X)."""
    out: list[str] = []
    for part in environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        for kv in part.removeprefix("unix:").split(","):
            if kv.startswith("path="):
                out.append(kv[5:])
    out.append(environ.get("SSH_AUTH_SOCK", ""))
    out.append(environ.get("GPG_AGENT_INFO", "").split(":")[0])
    host = environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        out.append(host[7:])
    xdg = environ.get("XDG_RUNTIME_DIR", "")
    wl = environ.get("WAYLAND_DISPLAY", "")
    if xdg and wl:
        out.append(wl if os.path.isabs(wl) else os.path.join(xdg, wl))
    disp = environ.get("DISPLAY", "")
    m = re.match(r"^[^:]*:(\d+)", disp)
    if m:
        out.append(f"/tmp/.X11-unix/X{m.group(1)}")
    return [p for p in out if p and os.path.isabs(p)]


def _walk_sockets(root: str, skip: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    seen = 0
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > _SOCKET_SCAN_MAX_ENTRIES:
                        return found
                    if any(e.path == k or e.path.startswith(k + os.sep) for k in skip):
                        continue
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if depth + 1 < _SOCKET_SCAN_DEPTH:
                            stack.append((e.path, depth + 1))
                    elif stat.S_ISSOCK(e.stat(follow_symlinks=False).st_mode):
                        found.append(e.path)
        except OSError:
            continue
    return found


def socket_hides(*, worktree: Path, tmp: Path,
                 environ: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """``(dirs, files)`` to cover inside bwrap: ``dirs`` get a tmpfs, ``files``
    (sockets) get /dev/null bound over them. The worktree and the run's private
    temp dir are never touched."""
    env = os.environ if environ is None else environ
    keep = (str(worktree), str(tmp))
    dirs: list[str] = []
    xdg = env.get("XDG_RUNTIME_DIR", "")
    for d in (*_SOCKET_DIRS, xdg):
        if not d or not os.path.isabs(d) or os.path.realpath(d) in _NEVER_HIDE:
            continue
        if os.path.isdir(d) and not os.path.islink(d) and d not in dirs:
            dirs.append(d)
    files: list[str] = []
    for p in [*_env_socket_paths(env),
              *(q for r in _SOCKET_SCAN_ROOTS for q in _walk_sockets(r, keep))]:
        if p in files or any(p == d or p.startswith(d + os.sep) for d in dirs):
            continue
        if any(p == k or p.startswith(k + os.sep) for k in keep):
            continue
        if _is_socket(p):
            files.append(p)
    return dirs, files


def bwrap_argv(argv: list[str], *, worktree: Path, tmp: Path, cwd: Path) -> list[str]:
    exe = shutil.which("bwrap") or "bwrap"
    out = [exe, "--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
           "--die-with-parent", "--new-session",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    home = _home()
    for rel in SENSITIVE_HOME_PATHS:
        p = home / rel
        if p.is_dir():
            out += ["--tmpfs", str(p)]
        elif p.exists():
            out += ["--ro-bind", os.devnull, str(p)]
    data = _data_dir()
    if data and data.is_dir() and data != worktree and data not in tmp.parents:
        # Hide the ConductLoop data dir (DB, fallback secrets); the worktree is
        # bound back below when it lives inside it.
        out += ["--tmpfs", str(data)]
    sock_dirs, sock_files = socket_hides(worktree=worktree, tmp=tmp)
    for d in sock_dirs:
        out += ["--tmpfs", d]
    for f in sock_files:
        out += ["--ro-bind", os.devnull, f]
    out += ["--bind", str(worktree), str(worktree), "--bind", str(tmp), str(tmp),
            "--chdir", str(cwd), "--", *argv]
    return out


def _sb_quote(p: str | Path) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(*, worktree: Path, tmp: Path) -> str:
    home = _home()
    deny_reads = [home / rel for rel in SENSITIVE_HOME_PATHS]
    data = _data_dir()
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        # DNS / URL-session daemons could fetch on the process's behalf.
        '(deny mach-lookup (global-name "com.apple.mDNSResponder") '
        '(global-name "com.apple.nsurlsessiond") (global-name "com.apple.nesessionmanager"))',
        "(deny file-write* (require-not (require-any "
        f"(subpath {_sb_quote(worktree)}) (subpath {_sb_quote(tmp)}) "
        '(subpath "/dev"))))',
        "(deny file-read* " + " ".join(f"(subpath {_sb_quote(p)})" for p in deny_reads) + ")",
    ]
    if data:
        lines.append(f"(deny file-read* (require-all (subpath {_sb_quote(data)}) "
                     f"(require-not (subpath {_sb_quote(worktree)}))))")
    return "\n".join(lines)


_PROBED: dict[str, bool] = {}


def _probe(name: str, argv: list[str]) -> bool:
    if name not in _PROBED:
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                               env={"PATH": "/usr/bin:/bin"})
            _PROBED[name] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _PROBED[name] = False
    return _PROBED[name]


def detect_backend() -> str | None:
    """'bwrap' / 'sandbox-exec' when present AND working, else None."""
    forced = os.environ.get("CONDUCTLOOP_SANDBOX", "").strip().lower()
    if forced == "none":
        return None
    if not platform_caps.code_sessions_supported():
        return None  # Windows: no OS sandbox exists
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        tmp = Path(tempfile.gettempdir()).resolve()
        if _probe("sandbox-exec", ["/usr/bin/sandbox-exec", "-p",
                                   seatbelt_profile(worktree=tmp, tmp=tmp), "/usr/bin/true"]):
            return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        if _probe("bwrap", [shutil.which("bwrap") or "bwrap", "--unshare-net", "--ro-bind",
                            "/", "/", "--dev", "/dev", "--proc", "/proc", "true"]):
            return "bwrap"
    return None


# ── execution ──────────────────────────────────────────────────────────────

def _limits(max_file_bytes: int):  # type: ignore[no-untyped-def]
    def apply() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


class _Capture:
    def __init__(self, keep: int):
        self.head = bytearray()
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_len = 0
        self.total = 0
        self.half = max(1, keep // 2)

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail.append(chunk)
            self.tail_len += len(chunk)
            while self.tail_len - len(self.tail[0]) >= self.half:
                self.tail_len -= len(self.tail.popleft())

    def text(self) -> tuple[str, bool]:
        tail = b"".join(self.tail)[-self.half:]
        truncated = self.total > len(self.head) + len(tail)
        sep = f"\n… [{self.total - len(self.head) - len(tail)} bytes omitted] …\n".encode() \
            if truncated else b""
        return (bytes(self.head) + sep + tail).decode("utf-8", "replace"), truncated


def run(argv: list[str], *, worktree: Path, cwd: Path, path_dirs: list[str],
        timeout_s: float = DEFAULT_TIMEOUT_S, max_output: int = DEFAULT_MAX_OUTPUT,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES, backend: str | None = None,
        allow_unsandboxed: bool = False) -> RunResult:
    if not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if not platform_caps.code_sessions_supported():
        # No sandbox backend exists here (and the POSIX-only limits below would
        # not apply). Refused even with allow_unsandboxed or an explicit backend.
        raise SandboxUnavailable(platform_caps.code_message(platform_caps.PLATFORM_UNSUPPORTED))
    backend = backend if backend is not None else detect_backend()
    if backend not in (None, "bwrap", "sandbox-exec"):
        # A typo ("bubblewrap") must not fall through to running unsandboxed.
        raise SandboxUnavailable(f"unknown sandbox backend {backend!r}")
    if backend is None and not allow_unsandboxed:
        raise SandboxUnavailable(
            "no OS sandbox available (bwrap on Linux, sandbox-exec on macOS); "
            "refusing to run without explicit allow_unsandboxed")
    tmp = Path(tempfile.mkdtemp(prefix="cl-run-")).resolve()
    home = tmp / "home"
    home.mkdir()
    env = build_env(path_dirs=path_dirs, home=str(home), tmp=str(tmp))
    wt, cwd = worktree.resolve(), cwd.resolve()
    if backend == "bwrap":
        full = bwrap_argv(argv, worktree=wt, tmp=tmp, cwd=cwd)
    elif backend == "sandbox-exec":
        full = ["/usr/bin/sandbox-exec", "-p", seatbelt_profile(worktree=wt, tmp=tmp), *argv]
    else:
        full, backend = list(argv), "none"
    cap = _Capture(max_output)
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(full, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, preexec_fn=_limits(max_file_bytes),
                                close_fds=True)
        assert proc.stdout is not None
        out = proc.stdout

        def pump() -> None:
            while True:
                chunk = out.read(65536)
                if not chunk:
                    break
                cap.feed(chunk)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        # Kill the whole group either way: nothing started by the run survives it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        reader.join(timeout=5)
        out.close()
        code = None if timed_out else proc.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    text, truncated = cap.text()
    return RunResult(exit_code=code, output=text, output_bytes=cap.total, truncated=truncated,
                     timed_out=timed_out, duration_ms=int((time.monotonic() - t0) * 1000),
                     sandbox=backend)

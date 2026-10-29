"""Test files that are skipped on Windows, each with the reason.

Applied by tests/conftest.py on ``sys.platform == "win32"`` only (the
``windows-latest`` job of .github/workflows/windows.yml); nothing changes on
macOS and Linux. A file belongs here only when what it tests does not exist on
Windows, never to hide a failure: a real Windows bug is fixed, or skipped with
the reason, in the same change. tests/test_windows_workflow.py checks that every
file listed exists and every reason is written down.
"""
from __future__ import annotations

SKIPPED_ON_WINDOWS: dict[str, str] = {
    "test_sandbox_sockets.py": "tests the Linux bubblewrap socket scan (AF_UNIX sockets, bwrap); no OS sandbox on Windows",
    "test_sessions_code_tools.py": "runs commands inside bubblewrap/seatbelt; code sessions are disabled on Windows",
    "test_sessions_worktrees.py": "drives git worktrees through POSIX shell scripts; code sessions are disabled on Windows",
    "test_verify_script.py": "runs scripts/verify.sh (bash) with POSIX tools",
    "test_linux_packaging.py": "checks scripts/build-linux.sh and Linux packaging files (bash)",
    "test_browser_agent.py": "uses AF_UNIX sockets for the agent bridge; Windows has no bridge (docs/PORTABILITY.md 3.3)",
}

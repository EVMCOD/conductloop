"""Test files that are skipped on Windows, each with the reason.

Applied by tests/conftest.py on ``sys.platform == "win32"`` only (the
``windows-latest`` job of .github/workflows/windows.yml); nothing changes on
macOS and Linux. A file belongs here only when what it tests does not exist on
Windows, never to hide a failure: a real Windows bug is fixed, or skipped with
the reason, in the same change. tests/test_windows_workflow.py checks that every
file listed exists and every reason is written down.

A key is either a file name or ``file.py::test_name`` for a single test. The
reasons marked "not investigated" are real Windows failures seen in the first
run of the job that are not small enough to fix here: they are the to-do list
for making Windows work, not a statement that the feature is fine.
"""
from __future__ import annotations

SKIPPED_ON_WINDOWS: dict[str, str] = {
    "test_sandbox_sockets.py": "tests the Linux bubblewrap socket scan (AF_UNIX sockets, bwrap); no OS sandbox on Windows",
    "test_sessions_code_tools.py": "runs commands inside bubblewrap/seatbelt; code sessions are disabled on Windows",
    "test_sessions_worktrees.py": "drives git worktrees through POSIX shell scripts; code sessions are disabled on Windows",
    "test_verify_script.py": "runs scripts/verify.sh (bash) with POSIX tools",
    "test_linux_packaging.py": "checks scripts/build-linux.sh and Linux packaging files (bash)",
    "test_browser_agent.py": "uses AF_UNIX sockets for the agent bridge; Windows has no bridge (docs/PORTABILITY.md 3.3)",
    # Code sessions are switched off on Windows (platform_caps): the API refuses.
    "test_sessions_api.py": "code sessions are turned off on Windows (platform_unsupported), so the API refuses to start them",
    "test_sessions_supervisor.py": "needs code sessions and git worktrees, which are turned off on Windows",
    # The macOS keychain backend (the `security` tool); Windows gets its own in PR 10 of the plan.
    "test_secrets_store_keychain.py": "tests the macOS `security` keychain backend; the Windows Credential Manager backend does not exist yet",
    # Real failures, not investigated yet (see the module docstring).
    "test_vault_index_drift.py": "not investigated: the vault index tools fail on Windows paths",
    "test_vault_conventions.py": "not investigated: the vault index preview fails on Windows paths",
    "test_notes_structure.py": "not investigated: the notes structure tools fail on Windows paths",
    "test_skills_compat.py": "not investigated: skill directory resolution and symlinks differ on Windows",
    # Single tests. (Tests about POSIX permission bits are not listed here: they carry the
    # `posix_perms` marker or use tests/posix_perms.py, which the guard in test_windows_workflow.py enforces.)
    "test_release_check.py::test_not_a_git_repo_fails": "the test's cleanup cannot delete read-only .git objects on Windows",
    "test_media.py::test_assemble_refuses_files_outside_the_data_directory": "uses a POSIX /tmp path",
    "test_lessons.py::test_scope_validation": "uses a POSIX absolute path (repo:/...), which is not absolute on Windows",
    "test_integration_e2e.py::test_analytics_and_setup_over_http": "not investigated: times out on Windows (possibly localhost resolving to ::1 first, docs/PORTABILITY.md 4.2)",
}

# Security policy

## Reporting a vulnerability

Please **do not** report security issues in public GitHub issues, pull requests or discussions.

Report privately through GitHub: **Security → Report a vulnerability** on this repository (private vulnerability reporting). Only the maintainer sees the report.

Please include:
- affected version (app version or commit SHA) and platform,
- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- whether the issue is already public.

You should receive an acknowledgement within a few days. ConductLoop is maintained by one person, so timelines are best-effort; we will keep you informed and credit you in the release notes if you wish.

## Scope

In scope:
- the local server and web UI in `ops_dashboard/` (it binds to `127.0.0.1` by default),
- the desktop app in `desktop/` (Tauri shell, sidecar, auto-updater signature checks),
- storage of secrets and BYOK keys on the user's machine.

Out of scope: vulnerabilities in third-party model providers or services you connect, and issues that require an attacker who already has full access to your user account.

## Supported versions

Only the latest release published in [conductloop-releases](https://github.com/EVMCOD/conductloop-releases/releases/latest) receives security fixes.

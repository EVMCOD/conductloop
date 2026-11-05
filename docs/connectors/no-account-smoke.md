# Connectors smoke test (no accounts)

`scripts/smoke-connectors.py` drives a running ConductLoop server through the
same HTTP API the Conectores page uses. It covers the connectors that need no
account:

| Connector | How it is exercised | Network |
|---|---|---|
| MCP client, stdio | `tests/fixtures/mcp/fake_mcp_server.py` as a real child process | none |
| MCP client, Streamable HTTP | `tests/fixtures/mcp/fake_mcp_http_server.py` on a real `127.0.0.1` socket (JSON + SSE responses, session id, optional bearer) | loopback only |
| RSS / Atom | `--rss-url https://…` configure → connect → sync → forget | public internet (manual only) |

For each MCP transport it checks: connect + `tools/list`, a read tool runs and
its result is marked untrusted, a write tool is refused while the server is
read-only, then queued for confirmation (not executed) once writes are
allowed, and only reaches the server after confirmation. The test servers are
named `smoke-*` and removed at the end.

CI runs the same flows hermetically in `tests/test_connectors_no_account_smoke.py`
(RSS with recorded responses and offline DNS).

## Against the packaged sidecar

Never point it at your real data. Build the sidecar and run it with a
temporary data dir, HOME and file-based secrets:

```bash
.venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm --distpath /tmp/cl/dist --workpath /tmp/cl/pyi
T=$(mktemp -d); mkdir -p "$T/home" "$T/data" "$T/sec"
HOME="$T/home" OPS_DATA_DIR="$T/data" OPS_DISABLE_KEYCHAIN=1 OPS_SECRETS_DIR="$T/sec" PORT=18777 \
  /tmp/cl/dist/conductloop-server &
.venv/bin/python scripts/smoke-connectors.py --base http://127.0.0.1:18777 \
  --rss-url https://github.com/EVMCOD/conductloop/commits/main.atom
kill %1; rm -rf "$T" /tmp/cl
```

(`--http-token` needs a server with a keychain; with `OPS_DISABLE_KEYCHAIN=1`
leave it out. The bearer path is covered by the pytest.)

## Connectors that need an account (manual)

These cannot be verified end to end without a real account. Tokens and
client secrets are entered in the app only, never shared in chat.

| Connector | Account / credential needed |
|---|---|
| Gmail, Google Calendar, Google Drive | Google account + own OAuth client (BYOK) in a Google Cloud project, see `oauth-byok-guide.md` |
| Microsoft 365 (Outlook, Calendar, OneDrive) | Microsoft account (personal or Entra ID work/school) + own app registration in Entra ID (BYOK) |
| Slack | Slack workspace where you can install an app + own Slack app (client id/secret) |
| GitHub | GitHub account; a ConductLoop GitHub connection or a fine-grained PAT (Issues / Pull requests read) |
| Linear | Linear workspace; personal API key |
| Notion | Notion workspace; internal integration token with "Read content", pages shared with it |
| Readwise | Readwise account; access token (readwise.io/access_token) |
| Todoist | Todoist account; API token (Settings → Integrations → Developer) |
| Real third-party MCP servers | Whatever that server needs (many HTTP servers need a bearer token or OAuth) |

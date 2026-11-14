"""External service connectors.

Each connector has:
  - test(credentials) -> (ok: bool, error: str | None, info: dict)
  - tools() -> list[dict]  (function-calling schema, for agent integration)

Providers currently supported:
  - cloudflare   (api_token + account_id)
  - github       (personal access token)
  - notion       (integration token + workspace_id)
  - linear       (api key)
  - slack        (bot token)
  - mcp          (uses mcp_servers table; tools come from MCP servers)
  - openai       (api key)
  - anthropic    (api key)
  - minimax      (api key + optional base_url)
  - custom       (any HTTP endpoint with bearer auth)

Each test makes a lightweight API call and returns (ok, error, info) where
info is provider-specific (account name, repo count, etc.) for display.
"""
from __future__ import annotations

import httpx


def _get(url: str, headers: dict, *, timeout: float = 8.0, follow_redirects: bool = True):
    try:
        return httpx.get(url, headers=headers, timeout=timeout, follow_redirects=follow_redirects)
    except Exception as e:
        return e


def test_cloudflare(creds: dict) -> tuple[bool, str | None, dict]:
    token = (creds.get("api_token") or creds.get("token") or "").strip()
    if not token:
        return False, "missing api_token", {}
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = _get("https://api.cloudflare.com/client/v4/user", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json().get("result") or {}
    return True, None, {
        "email": data.get("email"),
        "id": data.get("id"),
        "username": (data.get("username") or ""),
        "two_factor_authentication_enabled": data.get("two_factor_authentication_enabled", False),
    }


def test_github(creds: dict) -> tuple[bool, str | None, dict]:
    token = (creds.get("token") or creds.get("api_key") or "").strip()
    if not token:
        return False, "missing token", {}
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    r = _get("https://api.github.com/user", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json()
    # Also get rate limit info
    rl = _get("https://api.github.com/rate_limit", h)
    rate_info = {}
    if not isinstance(rl, Exception) and rl.status_code == 200:
        core = rl.json().get("resources", {}).get("core", {})
        rate_info = {"limit": core.get("limit"), "remaining": core.get("remaining"),
                     "reset": core.get("reset")}
    return True, None, {
        "login": data.get("login"),
        "name": data.get("name"),
        "public_repos": data.get("public_repos"),
        "private_repos": data.get("total_private_repos"),
        "rate": rate_info,
    }


def test_notion(creds: dict) -> tuple[bool, str | None, dict]:
    token = (creds.get("token") or creds.get("integration_token") or "").strip()
    if not token:
        return False, "missing token", {}
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    r = _get("https://api.notion.com/v1/users/me", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json()
    bot = data.get("bot") or {}
    return True, None, {
        "id": data.get("id"),
        "name": bot.get("workspace_name") or data.get("name"),
        "type": data.get("type"),
    }


def test_linear(creds: dict) -> tuple[bool, str | None, dict]:
    api_key = (creds.get("api_key") or creds.get("token") or "").strip()
    if not api_key:
        return False, "missing api_key", {}
    h = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"query": "{ viewer { id name email } }"}
    try:
        r = httpx.post("https://api.linear.app/graphql", headers=h, json=body, timeout=8.0)
    except Exception as e:
        return False, f"network: {e}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json().get("data", {}).get("viewer", {})
    return True, None, {"id": data.get("id"), "name": data.get("name"), "email": data.get("email")}


def test_slack(creds: dict) -> tuple[bool, str | None, dict]:
    token = (creds.get("bot_token") or creds.get("token") or "").strip()
    if not token:
        return False, "missing bot_token", {}
    h = {"Authorization": f"Bearer {token}"}
    r = _get("https://slack.com/api/auth.test", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}", {}
    data = r.json()
    if not data.get("ok"):
        return False, data.get("error", "unknown"), {}
    return True, None, {
        "user": data.get("user"),
        "team": data.get("team"),
        "team_id": data.get("team_id"),
        "url": data.get("url"),
    }


def test_openai(creds: dict) -> tuple[bool, str | None, dict]:
    key = (creds.get("api_key") or "").strip()
    if not key:
        return False, "missing api_key", {}
    h = {"Authorization": f"Bearer {key}"}
    r = _get("https://api.openai.com/v1/models", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json().get("data") or []
    return True, None, {"models_count": len(data), "sample": [m.get("id") for m in data[:3]]}


def test_anthropic(creds: dict) -> tuple[bool, str | None, dict]:
    key = (creds.get("api_key") or "").strip()
    if not key:
        return False, "missing api_key", {}
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    # Anthropic has no /v1/models read endpoint for plain API keys; send a tiny
    # message to /v1/messages and check for 200/4xx (auth error vs other).
    body = {"model": "claude-3-5-haiku-latest", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", headers=h, json=body, timeout=10.0)
    except Exception as e:
        return False, f"network: {e}", {}
    if r.status_code == 200:
        return True, None, {"model": body["model"], "auth": "ok"}
    if r.status_code == 401:
        return False, "invalid api_key", {}
    if r.status_code == 400 and "credit" in (r.text or "").lower():
        return True, None, {"model": body["model"], "auth": "ok", "note": "no credits"}
    return False, f"HTTP {r.status_code}: {r.text[:200]}", {}


def test_minimax(creds: dict) -> tuple[bool, str | None, dict]:
    key = (creds.get("api_key") or "").strip()
    base_url = (creds.get("base_url") or "https://api.MiniMax.chat/v1").strip()
    if not key:
        return False, "missing api_key", {}
    h = {"Authorization": f"Bearer {key}"}
    r = _get(f"{base_url.rstrip('/')}/models", h)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", {}
    data = r.json().get("data") or []
    return True, None, {"models_count": len(data), "sample": [m.get("id") for m in data[:3]]}


def test_custom(creds: dict) -> tuple[bool, str | None, dict]:
    """Generic HTTP endpoint with bearer auth."""
    url = (creds.get("url") or "").strip()
    if not url:
        return False, "missing url", {}
    h = {"Authorization": f"Bearer {(creds.get('token') or creds.get('api_key') or '').strip()}"}
    r = _get(url, h, timeout=5.0)
    if isinstance(r, Exception):
        return False, f"network: {r}", {}
    if r.status_code >= 500:
        return False, f"HTTP {r.status_code}", {}
    return True, None, {"status": r.status_code}


TESTERS = {
    "cloudflare": test_cloudflare,
    "github": test_github,
    "notion": test_notion,
    "linear": test_linear,
    "slack": test_slack,
    "openai": test_openai,
    "anthropic": test_anthropic,
    "minimax": test_minimax,
    "custom": test_custom,
}


SUPPORTED_PROVIDERS = list(TESTERS.keys()) + ["mcp"]


# ============================================================
# Tools each provider exposes (function-calling schema for agents)
# ============================================================

def _openai_tool(name: str, description: str, params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                              "parameters": params}}


CLOUDFLARE_TOOLS = [
    _openai_tool("cf_list_zones", "List all Cloudflare zones (domains) in the account.",
                 {"type": "object", "properties": {}, "required": []}),
    _openai_tool("cf_list_workers", "List all Cloudflare Workers scripts.",
                 {"type": "object", "properties": {}, "required": []}),
    _openai_tool("cf_deploy_worker", "Deploy a Worker script.",
                 {"type": "object",
                  "properties": {
                      "name": {"type": "string"},
                      "content": {"type": "string"},
                  },
                  "required": ["name", "content"]}),
    _openai_tool("cf_purge_cache", "Purge Cloudflare cache for a URL or zone.",
                 {"type": "object",
                  "properties": {"url": {"type": "string"}},
                  "required": ["url"]}),
]

GITHUB_TOOLS = [
    _openai_tool("gh_list_repos", "List GitHub repos for the authenticated user.",
                 {"type": "object", "properties": {}, "required": []}),
    _openai_tool("gh_create_issue", "Create an issue on a repo.",
                 {"type": "object",
                  "properties": {"repo": {"type": "string"}, "title": {"type": "string"},
                                 "body": {"type": "string"}},
                  "required": ["repo", "title"]}),
    _openai_tool("gh_list_prs", "List open PRs on a repo.",
                 {"type": "object",
                  "properties": {"repo": {"type": "string"}},
                  "required": ["repo"]}),
]

NOTION_TOOLS = [
    _openai_tool("notion_search", "Search Notion pages and databases.",
                 {"type": "object",
                  "properties": {"query": {"type": "string"}},
                  "required": ["query"]}),
    _openai_tool("notion_create_page", "Create a new Notion page.",
                 {"type": "object",
                  "properties": {"parent_id": {"type": "string"},
                                 "title": {"type": "string"},
                                 "content": {"type": "string"}},
                  "required": ["parent_id", "title"]}),
]

PROVIDER_TOOLS = {
    "cloudflare": CLOUDFLARE_TOOLS,
    "github": GITHUB_TOOLS,
    "notion": NOTION_TOOLS,
    "linear": [
        _openai_tool("linear_list_teams", "List Linear teams.",
                     {"type": "object", "properties": {}, "required": []}),
    ],
    "slack": [
        _openai_tool("slack_send_message", "Send a message to a Slack channel.",
                     {"type": "object",
                      "properties": {"channel": {"type": "string"},
                                    "text": {"type": "string"}},
                      "required": ["channel", "text"]}),
    ],
}


def tools_for_provider(provider: str) -> list[dict]:
    """Return the list of function-calling tools a provider exposes."""
    return PROVIDER_TOOLS.get(provider, [])


def list_provider_catalog() -> list[dict]:
    """Static catalog of supported providers for the UI."""
    return [
        {"id": "cloudflare", "name": "Cloudflare", "category": "infra",
         "description": "Workers, Pages, R2, KV, DNS",
         "fields": [{"key": "api_token", "label": "API Token", "type": "password"},
                    {"key": "account_id", "label": "Account ID (optional)", "type": "text"}],
         "tools_count": len(CLOUDFLARE_TOOLS)},
        {"id": "github", "name": "GitHub", "category": "code",
         "description": "Repos, issues, PRs, gists",
         "fields": [{"key": "token", "label": "Personal Access Token", "type": "password"}],
         "tools_count": len(GITHUB_TOOLS)},
        {"id": "notion", "name": "Notion", "category": "docs",
         "description": "Pages, databases, blocks",
         "fields": [{"key": "token", "label": "Integration Token", "type": "password"}],
         "tools_count": len(NOTION_TOOLS)},
        {"id": "linear", "name": "Linear", "category": "pm",
         "description": "Issues, projects, cycles",
         "fields": [{"key": "api_key", "label": "API Key", "type": "password"}],
         "tools_count": len(PROVIDER_TOOLS.get("linear", []))},
        {"id": "slack", "name": "Slack", "category": "comms",
         "description": "Messages, channels, threads",
         "fields": [{"key": "bot_token", "label": "Bot User OAuth Token (xoxb-...)", "type": "password"}],
         "tools_count": len(PROVIDER_TOOLS.get("slack", []))},
        {"id": "openai", "name": "OpenAI", "category": "ai",
         "description": "GPT-4o, o1, embeddings",
         "fields": [{"key": "api_key", "label": "API Key (sk-...)", "type": "password"}],
         "tools_count": 0},
        {"id": "anthropic", "name": "Anthropic", "category": "ai",
         "description": "Claude 3.5 Sonnet, Haiku, Opus",
         "fields": [{"key": "api_key", "label": "API Key (sk-ant-...)", "type": "password"}],
         "tools_count": 0},
        {"id": "minimax", "name": "MiniMax", "category": "ai",
         "description": "MiniMax M2.7 (OpenAI-compatible)",
         "fields": [{"key": "api_key", "label": "API Key", "type": "password"},
                    {"key": "base_url", "label": "Base URL (optional)", "type": "text"}],
         "tools_count": 0},
        {"id": "custom", "name": "Custom HTTP", "category": "other",
         "description": "Any HTTP endpoint with bearer auth (health-check style)",
         "fields": [{"key": "url", "label": "URL", "type": "text"},
                    {"key": "token", "label": "Bearer Token (optional)", "type": "password"}],
         "tools_count": 0},
        {"id": "mcp", "name": "MCP Server", "category": "other",
         "description": "Model Context Protocol server (stdio or HTTP)",
         "fields": [{"key": "transport", "label": "Transport (stdio|http|sse)", "type": "text"},
                    {"key": "command", "label": "Command (for stdio)", "type": "text"},
                    {"key": "url", "label": "URL (for http/sse)", "type": "text"}],
         "tools_count": None},
    ]


def test_connection(provider: str, credentials: dict) -> tuple[bool, str | None, dict]:
    """Run the test for a provider. Returns (ok, error, info)."""
    if provider not in TESTERS:
        return False, f"unsupported provider: {provider}", {}
    try:
        return TESTERS[provider](credentials)
    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}", {}

"""Small curated catalog: official sources, no implicit installation or login."""

CATALOG = [
    {"name": "context7", "title": "Context7", "description": "Current library documentation", "source": "https://github.com/upstash/context7", "config": {"url": "https://mcp.context7.com/mcp"}},
    {"name": "cloudflare-docs", "title": "Cloudflare Docs", "description": "Cloudflare developer documentation", "source": "https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/", "config": {"url": "https://docs.mcp.cloudflare.com/mcp"}},
    {"name": "github", "title": "GitHub", "description": "Repositories, issues and pull requests · account required", "source": "https://github.com/github/github-mcp-server", "config": {"url": "https://api.githubcopilot.com/mcp/"}},
    {"name": "sentry", "title": "Sentry", "description": "Error investigation · account required", "source": "https://github.com/getsentry/sentry-mcp", "config": {"url": "https://mcp.sentry.dev/mcp"}},
    {"name": "playwright", "title": "Playwright", "description": "Structured isolated-browser automation · Node.js required", "source": "https://github.com/microsoft/playwright-mcp", "config": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.83", "--isolated", "--headless"]}},
    {"name": "git", "title": "Git", "description": "Repository inspection · install mcp-server-git first", "source": "https://github.com/modelcontextprotocol/servers/tree/main/src/git", "config": {"command": "python", "args": ["-m", "mcp_server_git"]}},
]


def list_catalog(registry):
    installed = {s["name"]: s for s in registry.list_servers()}
    return [{**entry, "installed": entry["name"] in installed,
             "status": installed.get(entry["name"], {}).get("status", "NOT_INSTALLED")}
            for entry in CATALOG]

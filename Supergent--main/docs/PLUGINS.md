# WISE MCP Market and plugin packages

The default **MCP Market** page (`#plugins`) is a directory of MCP connections:
43 reviewed cloud service endpoints, the Figma Desktop bridge, and four reviewed
local package connections (48 reviewed entries), plus a large bounded snapshot
of active publisher listings from the MCP Registry and online discovery.
Community entries are identified as community, not certified integrations.
The local bundle import button is removed; bundle APIs remain compatible. See
[the connection marketplace guide](MCP_CONNECTION_MARKETPLACE.md) for supported
formats, native OAuth/CIMD, installation, safety boundaries and verification.
Cloud connections do not download plugin archives. Choose a service, then
Connect: WISE performs a real handshake and, if required, starts the browser
OAuth flow automatically. Local applications must already be running, expose
their supported bridge, and receive explicit local-access consent. They are not
installed or launched by this directory. Vendor setup guides accompany each row.

Direct connections reuse WISE's existing registry, protected secret store,
PKCE/state/callback verification and capability router. A configured connection
is reused without duplicate server configurations or claiming ownership of its
credentials. Only an actual live handshake reports Connected. Disconnecting a
store-owned connection removes local credentials, not the external account;
shared configurations remain managed in MCP Settings.

Not every vendor permits unregistered desktop OAuth clients. WISE cannot inherit
Claude's vendor partnerships or impersonate its client identity. Services that
require a registered application, organizational approval, subscriptions or an
API key still require those prerequisites. Postman is explicitly key-based;
public documentation services can connect without an account. Listing a service
means its documented endpoint is supported, not that all account logins have
been completed or certified. OAuth account consent remains the user's action.

Calendly is deliberately excluded: its production DCR endpoint rejected WISE's
loopback callback (`invalid_client_metadata`: HTTPS required) in a real probe.
See https://developer.calendly.com/docs/mcp/calendly-mcp-server . Supporting it
would require a properly owned HTTPS callback architecture, not a copied client
ID or removal of redirect/PKCE checks. Netlify's documented public desktop flow
is included instead.

The separately supported package marketplace remains at `#plugins/packages`;
existing package installation records and active workflows are preserved.
Neither page lists WISE's base tools, skill inventory or arbitrary raw MCP
recipes as service plugins. Raw MCP management stays in Settings. No Nango
account, broker secret or second agent engine is required.

## Advanced package catalogs, not generated integrations

Default sources are the published `anthropics/claude-plugins-official`,
`anthropics/knowledge-work-plugins`, `anthropics/life-sciences`, `microsoft/skills`,
`hashicorp/agent-skills`, `trailofbits/skills` and `wshobson/agents` marketplaces.
Their actual manifests provide package
names, descriptions, sources and categories. The catalog maintainer is not
automatically attributed as the package's author. Browsing is metadata-only;
package information is available immediately from cached catalog metadata.
Opening a listing does not fetch or extract an archive. Download and compatibility
inspection begin only when the user requests installation or management.

The advanced marketplace API accepts public GitHub repositories containing
`.claude-plugin/marketplace.json` or `.agents/plugins/marketplace.json`. It validates
the real manifest before saving. Local relative, GitHub, Git URL and Git-subdir
package sources are interpreted; unsupported sources are counted separately,
not invented as working plugins. The normal store has no Add marketplace button,
source tabs, extra View buttons, or manual Show more controls. Featured packages
and task-oriented category sections lead to complete automatically loaded lists.
Search/categories/pagination are local to the
cached catalogs. Daily refresh changes metadata only; it never silently updates
installed executable code. Offline errors retain previous catalog entries.
New built-in catalogs refresh automatically on upgrade even if the previous
snapshot is fresh, with a five-minute failure backoff to avoid request storms.
Known brand marks use retained local artwork or bounded Simple Icons fetches.
Publisher avatars are never substituted for a service logo. Known services use
reviewed brand artwork or favicons served by their own websites; packages with
no verified mark show a neutral icon. Text initials remain an offline fallback.

## Package compatibility

WISE reads root `plugin.json` (Agent Plugins), `.codex-plugin/plugin.json`,
`.claude-plugin/plugin.json` and `.agent-plugin/plugin.json`. Conventional Claude
marketplace packages may omit the manifest. Names must match the marketplace.
Inline OpenAI presentation overlays and explicit component paths are supported.

Supported components:

- `skills/` instruction workflows with supporting resources intact;
- Markdown `commands/` exposed as namespaced workflows, not an emulated Claude CLI;
- bundled stdio or public HTTPS Streamable HTTP MCP connections, including
  separately requested credentials and plugin-root variable substitution.

Installed workflows augment WISE's **existing** index/search/loader on demand.
They are namespaced and are not copied over base skills. Disabled or removed
packages leave the index immediately, including after a backend restart.
The execution agent reads package instructions using the stored workflow file
(including command Markdown), rather than incorrectly requiring the base skills
directory. Each read rechecks active ownership and the completion marker.
`read_skill` also accepts a package-relative `resource` with bounded `offset` and
`limit` to read supporting references. Resource reads do not falsely mark the
main procedure as read. Neither path permits arbitrary filesystem access.

Host-specific hooks, agents, LSP servers, app-broker connections, monitors,
forked-skill execution, userConfig and automatic dependency installation are
**not implemented** by these adapters. Preview lists those limitations. Partial
installation requires a separate explicit consent; packages with no supported
components cannot be installed. These are portability limits, not a claim that
all listed packages have been functionally certified on WISE.

## Review → install → set up connections

1. Inspect a real package pinned to a Git commit. Downloaded code remains inert.
2. Review publisher and compatibility, then explicitly trust and install it.
3. Supported workflows become discoverable internally. Bundled servers are not
   started by installation; set them up in the package's connection section.
4. Local server execution requires a separate approval. It runs with the owner's
   account permissions on Windows, **not a claimed sandbox**. Required runtimes
   and any external dependencies must be present.
5. Service credentials go into the existing protected store, not renderer output.
   OAuth reuses discovery/PKCE and the system browser. Some vendors require a
   registered client or decline third-party hosts; their policy cannot be bypassed.

An Installed package is not a Connected service. Only a live server handshake and
tools/list produce connected tools. They remain under WISE's routing and
confirmation policy. Authorization does not grant unrestricted writes/deletes.

Disable stops owned bundled servers and excludes package workflows. Uninstall
removes active ownership and owned connections/credentials, not external account
data or unrelated manually configured MCPs. Inert version files are retained in
the bounded per-package cache; this is not a whole-cache cleanup operation.
Connected package updates currently require removal/reinstallation; credentials
are not silently migrated. Unconnected updates require reviewing the new commit.

## Boundaries and evidence

Downloads use public-address checked/pinned HTTPS with no redirects or inherited
proxy environment. Archive/member sizes, member counts, traversal, Windows device
paths, case collisions and symlinks are checked. Completion is marked only after
validated contents are copied. State files are atomically written and corrupt
state fails closed. Foreign hooks and package lifecycle scripts never auto-run.

`manager.py` owns the reviewed service connection lifecycle and remains the
internal MCP component adapter. Its arbitrary community/native inventory is
**not** the default service catalog. `store.py`, `marketplace.py` and `packages.py`
own the separate advanced package identity, source snapshots, review and activation.

Validation commands:

```powershell
.\.tooling\qa-venv\Scripts\python.exe -m pytest tests/test_plugin_packages.py tests/test_plugins.py -q
.\.tooling\qa-venv\Scripts\python.exe qa/acceptance/plugin_regression.py
.\.tooling\qa-venv\Scripts\python.exe qa/acceptance/package_store_journeys.py
```

The package journey uses an owned keyless WISE backend and headless Brave, real
public catalogs and a real public plugin archive. It checks search, installation,
restart persistence, disable/removal, errors and desktop widths. Component tests
also run an actual controlled stdio server and call its tools. Neither proves that
every catalog package, private account OAuth, or the whole product is production
ready. Catalog size and successfully tested packages must be reported separately.

Format references: [Claude marketplaces](https://code.claude.com/docs/en/plugin-marketplaces),
[Claude manifest](https://code.claude.com/docs/en/plugins-reference),
[portable/Codex packages](https://developers.openai.com/plugins/build/plugins).

# WISE MCP Market

The default **MCP Market** view contains reviewed service connections and
publisher-provided MCP Registry listings, not WISE's native tool or skill
inventory. Existing package workflows remain available on their
explicit route and continue using the canonical agent/capability router.

## Connection types

| Type | Human action | What makes it connected |
| --- | --- | --- |
| Cloud MCP | Connect; complete provider consent in the browser when required | Actual initialize/tools/list succeeds with the account credential |
| Existing desktop app | Enable the application's documented MCP bridge; consent in WISE | Actual handshake, not a process/executable/port check |
| Reviewed local package | Choose its folder/settings; trust its publisher; Install and connect | Pinned dependency installation and an actual stdio handshake |
| Imported MCPB (backend compatibility) | Import via API; review author/version/digest; fill typed settings; trust code | Safe extraction, supported manifest/config, integrity checks and actual stdio handshake |

## Expanded directory

The bundled metadata snapshot contains 7,085 active latest-version registry
listings: a bounded general scan, GitHub community namespace, and selected
ecosystem searches including Microsoft, AWS, MongoDB, Google, Qdrant, Jina,
Brave, Playwright and others. Actual Market deduplication produced 7,124 rows
in the acceptance run, including the existing reviewed connections. Counts
change with registry updates. This is **not** a complete copy of every MCP on
the internet, a security endorsement, or 7,124 successfully tested connections.

The upstream registry supports name search and cursor pagination; see the
[official API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md).
WISE refreshes metadata in the background and supplements searches online.
The snapshot explicitly records `complete=false`; discovery caches are bounded
at 8,000 entries. Offline metadata remains browsable. Complete refreshes remove
deprecated discovery entries without deleting installed/pinned configurations.

The page has task categories, Browse all with automatic scroll pagination,
publisher-name search, descriptions, publisher namespace, source links,
versions, registry update dates and setup requirements. Unsupported transport
or package recipes remain visible as manual setup, never a fictitious Connect
button. Registry listing status is not a claim that the publisher is safe.
Pinned community npm/PyPI packages use the same isolated, script-disabled /
wheel-only installer as reviewed recipes, after explicit local-code consent.

Publisher-provided artwork and declared website favicons use bounded public
fetches and a validated disk cache (64 MB / 4,096 entries). Cached artwork can
survive restart and loss of connectivity. Missing/invalid artwork uses a neutral
name monogram, **not** a made-up brand logo or an unrelated publisher avatar.
The local-bundle button is removed from Market; existing bundle APIs/configs
remain for compatibility and regression testing.

44 reviewed service/application entries remain intact. Four deterministic local
recipes are added, including Microsoft Playwright with an isolated installed
Brave instance and reference filesystem/Git/Fetch. The archived SQLite reference
server was excluded rather than counted as a maintained connection.
Local packages never use signed-in browser profiles implicitly. The approved
Figma endpoint is the only built-in local HTTP bridge; no network-wide scan or
password/token harvesting occurs.

## OAuth

Native public-client DCR sends `application_type=native`, PKCE S256, the exact
ephemeral loopback callback and resource identifier. WISE also supports CIMD
when the authorization server advertises it and a public WISE-owned document
has actually been deployed. Set `WISE_MCP_CLIENT_METADATA_URL`, or provide the
URL under a service's advanced connection settings. WISE does not bundle a
borrowed client ID, broker key, fake metadata URL or someone else's OAuth app.

A deployment document resembles the following; replace both example URLs with
the real HTTPS URL you control before publishing it. Native OAuth permits the
loopback port to change, not the host/path.

```json
{
  "client_id": "https://your-domain.example/wise-client.json",
  "client_name": "WISE",
  "application_type": "native",
  "redirect_uris": ["http://127.0.0.1/oauth/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

Service allowlists/registration restrictions remain service policies, even with
CIMD. Token-only services show real token fields. Account authentication must
be performed by the human, never simulated by an automated test. Existing
public connections remain usable during sign-in. Credentials, refresh state
and the chosen issuer use the protected local secret store.

## MCPB compatibility and safety

MCPB is the open desktop bundle format documented by
[the MCP project](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md).
WISE's adapter reuses its existing safe package-path validation and interprets
the documented 0.1–0.3 formats. It supports node/python/binary entry points,
platform overrides, typed string/number/boolean/file/directory settings,
multiple values, bounded variables and explicit runtime comparison ranges.

Uploads are capped at 64 MB; expanded contents at 256 MB and 20,000 entries.
Traversal, Win32 aliases/devices/ADS, case collisions, links, encrypted ZIPs,
unknown variables and secrets in arguments are rejected. Bundles are addressed
by archive SHA256. A file inventory and hashes are rechecked before *every*
registry start, including after restart. SHA256 is identity, not proof that a
publisher is trustworthy. Unsigned packages need explicit human trust.

Not supported silently: newer manifest versions, `uv` manifests, lifecycle
hooks, arbitrary shell commands, external commands in binary bundles, or npm
semver shorthand (`^`, `~`, `||`) in runtime requirements. Python bundles must
include compatible dependencies themselves; WISE does not guess/install them
from READMEs. Missing runtimes or unsupported manifests produce a real setup
error, never a fictitious Connected state.

Reviewed npm packages install with lifecycle scripts disabled in WISE-owned
environments; Python recipes use separate virtual environments and pinned
wheel-only installation. Installer subprocesses do not inherit model keys,
proxy credentials, user npm config or custom pip index config. Bounded installer
logs stay local. Installation doesn't lock the entire catalog. Local execution
uses account permissions, **not** administrator permissions or a Windows sandbox;
tool confirmation policy remains enabled and no blanket auto-approve is added.

## Verification

### MCP Market expansion — final evidence, 2026-10-12 (Baghdad)

Application source fingerprint:
`c018ffef3248800b0bc49be3fd9d2a3812396f5ad8d247e4348806462b5a49fb`.
Both runs below report `source_changed_during_run=false`.

| Check | Result | Evidence |
| --- | --- | --- |
| Connection/package/security regression | 349 PASS, 1 SKIP; no failures/errors | `qa-results/plugins-regression-20261011T210008Z/report.json` |
| Actual owned Brave journeys | 7 PASS; 7,124 browsable rows | `qa-results/mcp-market-20261011T210516Z/report.json` |
| Rendered page identity/nonblank/no overlay | PASS, screenshots inspected | Same journey report and screenshots |
| JS/console health | No page errors or console messages in the final seven cases | Per-case `.log.json` files |
| Interactions | Real search, scroll pagination, provenance/detail, decoded known-brand artwork, public MCP connection | Same journey report |
| Desktop/compact | 1440, 1024 and 800 widths, no horizontal document overflow | `detail-*.png`, `market-800.png` |

The held-session-request case is deliberate fault injection; it supplies no
mocked MCP/model response. It proves the Market route renders before ancillary
session data completes. Read-only API loads now have bounded timeouts; mutations
are neither timed out nor automatically retried. Atomic registry/plugin state
publication retries brief Windows sharing locks without changing ACLs or losing
the old complete file. Earlier failures and their screenshots remain preserved.

Known-brand checks explicitly reject neutral fallbacks for Canva, Cloudflare,
Hugging Face, Prisma, Neon and Airtable; generic/local or iconless publisher
entries may intentionally use neutral monograms. The Browser plugin was absent;
the existing owned headless Brave/Playwright harness was used. No owner account
consent, paid model calls or external messages occurred. Native window chrome,
all registry servers and private account authorizations are not certified by
these renderer/connection tests. The one regression skip requires Windows
source-symlink privileges and is not counted as a pass.

`tests/test_mcp_bundles.py` covers adversarial archives, genuine stdio protocol
fixtures, canonical agent tool exposure, typed config, protected reconnect
settings, restart, integrity rejection and same-origin upload restrictions.
`tests/test_mcp_oauth_flow.py` adds native DCR/CIMD protocol cases. Fixtures are
identified as fixtures, not evidence of a real owner's provider consent.

`qa/acceptance/connection_marketplace_journeys.py --output qa-results/<new-run>`
uses owned headless Brave against a real isolated WISE backend. It installs the
actual pinned reference filesystem server through the UI, packages that actual
server/dependencies into an MCPB, imports/configures/connects it, restarts WISE,
and reconnects saved settings. It also tests public Cloudflare MCP, application
readiness without access, search and 1440/1024/800 desktop layouts. Screenshots,
traces, logs and a source fingerprint accompany PASS/FAIL evidence. No owner
accounts, exposed tokens, paid LLM calls or external messages are used.

This verifies the connection subsystem; it is not proof that every vendor
accepts WISE, nor a whole-product zero-defect certification.

### Previous connection-layer evidence — 2026-10-11

These three final runs share application source fingerprint
`7b2eebda67d87dbeb80570745faa2b54e29e78886b535b10b5e39933772c8edd`;
each records `source_changed_during_run=false`.

| Run | Observed result | Evidence |
| --- | --- | --- |
| Deterministic connection/package/security regressions | 333 PASS, 1 SKIP, 0 failures/errors | `qa-results/plugins-regression-20261011T200212Z/report.json` |
| Owned Brave user journeys | 8 PASS; real pinned install, MCPB, canonical file read, restart/reconnect, cloud handshake, readiness, search/layout | `qa-results/connection-marketplace-20261011-final5/report.json` |
| Public tool execution | Cloudflare documentation search and Context7 library resolution both PASS through canonical MCP wrappers | `qa-results/plugin-live-20261011T200812Z/report.json` |

The skipped test requires Windows source-symlink privileges; it is not counted
as a pass. The rendered run has no JavaScript exceptions. One initial console
404 is specifically `/favicon.ico`, not a failed connection or missing service
logo. Screenshots were inspected at desktop and 800-pixel widths. The Browser
plugin was unavailable, so the existing owned Playwright/Brave harness was used;
no personal browser profile or visible desktop takeover was involved.

Earlier failed reports remain preserved. Windows publication locks are handled
by bounded retries without lowering ACLs or accepting partial contents. Registry
state publication recreates its owned parent after recovery. The final UI harness
waits for the connection POST and refreshed detail to finish; a previously
displayed Connected label cannot make a reconnect journey pass prematurely.
In particular, the preceding `final4` failure occurred before the cloud button
was clicked and is not presented as a successful cloud connection test.

No paid model calls, owner account authorizations or external messages occurred.
Earlier real Notion/Linear/Netlify probes verified metadata/DCR/PKCE preparation
and cancellation only, not completed consent or token exchange. No shared WISE
client-metadata document has been deployed by this implementation. Vendor
account consent, app-registration/allowlist restrictions and native-window
behavior still require their own verification; 48 reviewed listings do **not**
mean 48 account integrations have been end-to-end certified.

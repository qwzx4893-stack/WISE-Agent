# WISE Plugins (local, not included in the public pre-Plugins snapshot)

Plugins is a primary item in the existing sidebar. The catalog/detail views
reuse WISE HTML/CSS/JS, navigation, controls, type and color tokens. No framework
or second sidebar is added. The route is `/app/#plugins[/integration-id]`.

## Configure

1. Create a Nango environment. Configure the integrations and their OAuth
   applications/scopes in Nango. Start with read-only GitHub scopes.
2. Set `NANGO_SECRET_KEY` in the **backend environment** (or an ignored `.env`).
   `NANGO_BASE_URL` defaults to `https://api.nango.dev`. For a `.env` use
   `python -m uvicorn api.server:app --env-file .env --host 127.0.0.1 --port 8765`.
   Restart the native host/backend after changing its environment.
3. Include Nango API scopes for integrations/provider metadata, connect sessions,
   listing/deleting connections and proxy reads. Prefer a scoped environment
   key, not an account-wide administrator key. No frontend public key is used.
4. Open Plugins. The public catalog includes 1,038 provider metadata entries;
   browsing that catalog is not proof that a provider is configured or connected.
   Connect is enabled only for integrations configured in that Nango environment.
   Without configuration the rest of WISE works and Plugins shows an honest
   unconfigured state, not fabricated accounts. Metadata is paginated/searched;
   local official logos cover common brands, with remote/fallback handling for others.

Your own OAuth application name/logo must be configured in Nango/provider
dashboards to show WISE on provider consent screens. Code alone cannot change
provider-approved branding. Nango costs and available APIs depend on your plan.

## Flow and boundaries

`IntegrationProvider` abstracts the provider. `NangoProvider` uses official
[integration metadata](https://nango.dev/docs/reference/backend/http-api/integration/list),
[Connect sessions](https://nango.dev/docs/reference/backend/http-api/connect/sessions/create)
and [tag-filtered connections](https://nango.dev/docs/reference/backend/http-api/connections/list).
The backend launches the returned hosted Connect link in the **default system
browser**, not an embedded WebView. Nango handles provider OAuth redirects,
state and PKCE. WISE polls the provider and checks installation identity,
integration, provider and unique attempt tag before accepting success. No
renderer-reported account ID is sufficient.

SQLite in `memory/integrations.sqlite3` stores only account metadata, local
identity and attempt status. OAuth credentials stay in Nango. The Connect URL
and session token stay in backend memory and expire; neither enters SQLite,
renderer, logs or localStorage. After a backend restart a pending attempt can
still be verified, but re-opening its old link requires a new attempt.

Cancel stops accepting that attempt locally; it does not revoke provider consent
completed later in an already-open external browser. Remove any such orphaned
connection in Nango. Disconnect removes the Nango connection, not the provider's
entire OAuth application grant; revoke that separately in the provider if desired.
Failed remote deletion never becomes a fake disconnected success. Reconnect
uses the official reconnect-session endpoint and verifies the resulting tags.

These endpoints require same-origin action headers, check Origin/Fetch metadata
and reject non-loopback Host values (DNS rebinding). Existing API auth/loopback
controls still apply. This is a single-installation desktop identity, **not a
multi-user SaaS authentication system**.

## Agent tools and extension

`core/integrations/bindings.py` adds only verified connected GitHub tools to the
existing canonical router: repository search, repository listing and bounded
file reading. Every execution rechecks remote ownership/authentication. Results
are untrusted external data, not authority to execute instructions. Missing
providers' tool adapters are explicitly disclosed in their details page.

Connection is not a permission grant for arbitrary API calls. No arbitrary
proxy, URL, method or secret-reading endpoint is exposed to models. New tools
must declare a schema, risk and category through `register_extension` and the
existing security gate. WRITE/SEND/DELETE require their own reviewed adapters
and approval tiers; they are **not implemented or silently enabled here**.

To add a service: configure its integration in Nango (catalog/logo/auth metadata
arrive automatically), add optional concise description/category to
`core/integrations/metadata.json`, then implement and test reviewed tools in the
provider-independent service/bindings. Metadata alone never creates tools.
Catalog metadata has a five-minute cache, UI search is debounced/paginated and
logos load lazily with reserved dimensions and browser HTTP caching. Self-hosted
Nango uses the same provider interface; explicitly allow trusted **HTTPS**
Connect/logo origins with the environment fields in `.env.example`.

## Verification

`tests/test_plugins.py` mocks only the credential-dependent Nango HTTP boundary.
It tests cache/filtering, connection ownership, persistence, cancellation,
expiry, reconnect, failures, disconnect, safe redirects, proxy field filtering,
CSRF/rebinding and canonical registry/security routing.

`qa/acceptance/plugin_journeys.py` runs actual WISE UI/backend flows in headless
Brave with screenshots, traces, logs and reproduction steps. Its unconfigured
run has no API mocks; its configured run mocks only external Nango/browser
launch. It does not certify real account OAuth or Windows-native backdrop.
No real-account end-to-end success is claimed until Nango is configured and a
user-authorized account connection is tested.

# Integration catalog and brand artwork

Display metadata and SVG brand assets were read from the official
[NangoHQ/nango repository](https://github.com/NangoHQ/nango) at revision
`0138ede24ade643d53b9c8201f89333ee683fc6f` on 2026-10-02.
The minimized catalog records its exact source and SHA-256 digest; it contains
no executable integration code, credentials, or authorization URLs.

Local SVG paths are unchanged. Other logos refer to verified asset paths at
the same fixed upstream revision. WISE displays a visible text monogram if an
asset cannot load; that monogram is a fallback, not a replacement brand mark.
Brand marks remain the property of their owners. Consult the upstream project
licenses and each brand's usage requirements before redistribution.

Catalog membership means Nango has provider metadata, not that an account is
connected, an integration has been configured, or WISE has implemented tools
for that service. These states are tracked separately.

Refresh display metadata with `qa/acceptance/update_provider_catalog.py` and
review the emitted data before saving it. Production never auto-downloads or
executes provider catalog updates.

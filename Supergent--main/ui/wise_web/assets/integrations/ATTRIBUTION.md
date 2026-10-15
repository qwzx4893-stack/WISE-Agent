# Integration catalog and brand artwork

Display metadata and SVG brand assets were read from the official
[NangoHQ/nango repository](https://github.com/NangoHQ/nango) at revision
`0138ede24ade643d53b9c8201f89333ee683fc6f` on 2026-10-02.
The provider integration engine and its metadata catalog have been removed.
Previously acquired static artwork is retained with its original attribution;
it contains no executable integration code, credentials, or authorization URLs.

Local SVG paths are unchanged. Other logos refer to verified asset paths at
the same fixed upstream revision. WISE displays a visible text monogram if an
asset cannot load; that monogram is a fallback, not a replacement brand mark.
Brand marks remain the property of their owners. Consult the upstream project
licenses and each brand's usage requirements before redistribution.

WISE's main plugin store lists reviewed service connections. An explicit package
route retains the separately supported package ecosystem. Neither lists the
agent's internal tools or skills as service integrations. Known marks use Simple
Icons (https://github.com/simple-icons/simple-icons), retained local SVGs, or the
service's official website/favicon. The vendor-hosted URLs are listed in
`core/plugins/connectors.py` and were checked on 2026-10-11. BugSnag's image URL
comes from its homepage's favicon link; Tavily's comes from its official docs.
Marks remain their owners' property; review each brand's usage terms before
distribution. Publisher avatars are no longer substituted for service logos.
Unknown packages show a neutral icon. Artwork redirects are separately
HTTPS/public-address checked and pinned on every hop; SVG active content and
oversized images are rejected. Artwork is cached in process for one day.

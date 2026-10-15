# Maintaining the repository presentation

This page is for maintainers. The public landing page is [README.md](../README.md).

## Identity and assets

- [The banner](../docs/assets/wise-banner.svg) uses the geometric WISE wordmark from the current desktop interface. It is a vector identity/architecture illustration, not a product screenshot.
- Keep graphics self-contained, with readable contrast and descriptive alternative text. Do not add trackers or remote scripts to documentation assets.
- Use real screenshots only when they show the current build and contain no private conversation, account or local-path data.
- The English README is the full entry point; [the Arabic guide](../docs/README.ar.md) is a concise companion, not a mechanically identical translation.

## Honest badges and evidence

Workflow badges point to actual repository workflows on `main`. Platform/runtime badges describe the documented target, not platform certification. The licensing badge links to notices; do not replace it with MIT/Apache without an owner licensing decision.

Do not add fabricated coverage, release, security-score, download or performance badges. Keep result tables tied to a commit and distinguish source, renderer, native, hardware and real-account evidence.

## GitHub-native structure

The README uses relative links, a contents section, expandable details, an architecture diagram and readable capability tables. Issue forms, a pull request template, contribution guidance, security policy, support resources and conduct guidelines provide the community entry points.

These are supported by GitHub's [README formatting](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes), [diagram rendering](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams) and [community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file). They do not require an application UI redesign.

## Before publishing documentation changes

1. Check relative links and heading anchors against the actual public export, not only the development checkout.
2. Check issue-form YAML, image dimensions, SVG safety and code fence/HTML block balance.
3. Render the banner and review the README on GitHub at desktop and narrow widths.
4. Scan changed content for secrets; preserve existing reviewed export redactions.
5. Update `PUBLICATION_MANIFEST.json` hashes through the reviewed publication path and verify the remote tree after publishing.

The repository About description/topics, social-preview upload, Discussions, private vulnerability reporting and branch protection are **repository settings**, not features enabled by adding a README file. Verify settings separately; do not claim they are active merely because a template or asset exists. Do not invent funding links or create a fake release to fill a checklist.

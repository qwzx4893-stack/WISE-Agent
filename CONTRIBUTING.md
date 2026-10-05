# Contributing

Read architecture, validation and distribution notes. Preserve unrelated edits and service/profile isolation. Use a separate branch, scoped changes and behavioral regression tests.

Agent OS is in `Supergent--main`, using Python 3.11 and its own environment. Leon has separate `AGENTS.md` instructions and uses pnpm/source-local manifests. Do not merge secrets stores or install optional tensor/scanner runtimes into the base environment by default.

Run focused checks and appropriate regression tests. State skipped/mock/paid/account/hardware test boundaries. Provide build/version information and redacted reproduction steps. Mock tests do not prove live OAuth or native acceptance.

Do not add large weights, generated environments, runtime state or restricted assets. Retain license notices; do not assume WISE-wide reuse rights without an owner license decision.

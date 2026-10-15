# Contributing to WISE

Thanks for helping improve a coordinated desktop Agent OS. Prefer focused changes with reproducible evidence over broad rewrites or unsupported capability claims.

## Before you start

1. Read the [architecture](docs/ARCHITECTURE.md), [installation](docs/INSTALLATION.md) and [validation boundaries](docs/VALIDATION.md).
2. Search [existing issues](https://github.com/qwzx4893-stack/WISE-Agent/issues). Discuss architecture, new permissions and dependency-heavy work before implementing.
3. Review [licensing status](Supergent--main/docs/releases/LICENSING_STATUS.md). Public visibility is not a repository-wide license grant; do not assume contribution/reuse terms not established by the owner.
4. Follow applicable local `AGENTS.md` instructions. Leon has its own source-local workflow.

## Development setup

Use Windows and Python 3.11 for the documented desktop path. Create the project environment with `tools/Install-WISE.ps1`; keep optional voice/tensor/scanner environments separate. Leon uses its own Node.js/pnpm manifests and lockfiles.

Use a scoped branch in your own checkout. Preserve unrelated edits. Never commit generated environments, private state, browser profiles, recordings or weights; do not combine account keystores across services.

## What a good change includes

- A clear problem statement and a small, reviewable implementation.
- A behavioral regression test; ideally demonstrate the failure before the fix.
- Checks at the final execution boundary for permission-sensitive operations—not just a prompt or UI confirmation.
- Updated setup/migration/compatibility notes when the public contract changes.
- Upstream provenance and applicable notices for imported code/assets.
- Honest evidence: commands, environment, results and skipped/mocked/native/account limitations.

## Validation

Run focused tests first. From `Supergent--main`, the CI-aligned runners are:

```powershell
.\.venv\Scripts\python.exe -m qa.acceptance.source_regression
.\.venv\Scripts\python.exe -m qa.acceptance.security_regression --output qa-results/security-local --timeout 900
```

Dependency sets are listed in [source CI](.github/workflows/validate.yml) and [security CI](.github/workflows/security-regressions.yml). Optional native/vendor/runtime checks need separate provisioning. Read [Validation](docs/VALIDATION.md) before interpreting results.

Renderer changes should include before/after screenshots with synthetic data and interaction checks. Native desktop acceptance must be identified separately from headless tests.

## Pull request checklist

- Explain the change and link the relevant issue, if one exists.
- Describe permission, outgoing-data, persistence and dependency impacts.
- Report tests actually run, including failures and unavailable coverage.
- Remove secrets, account identifiers and private content from examples/evidence.
- Keep code, documentation and setup instructions consistent.
- Do not describe mocks, catalog counts or test totals as universal acceptance.

Use the [pull request template](.github/PULL_REQUEST_TEMPLATE.md). Vulnerabilities belong in the [private disclosure process](SECURITY.md). Participation follows the [conduct guidelines](.github/CODE_OF_CONDUCT.md).

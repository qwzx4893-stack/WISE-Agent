# WISE production acceptance gate

This directory contains the evidence-producing acceptance gate for WISE.  It
is intentionally separate from production dependencies and follows the ECC
eval-harness, browser-qa, Windows desktop E2E, production-audit, and
agent-architecture-audit guidance.

The gate combines deterministic tests, read-only API contract probes, browser
and accessibility checks, native-window lifecycle checks, security scanners,
and opt-in live agent scenarios.  No individual stage can declare the product
ready by itself.

## Run

The user-style layer runs **real Playwright interactions in headless Brave**
against a new, isolated WISE runtime. It does not mock backend responses and
does not alter your existing conversations, settings, browser profile or accounts.
FlaUI 5.0.0 checks the native Windows window through UI Automation patterns,
bound to the exact process family the test launched (Windows venv shims spawn
the actual window in a child process). No physical mouse/keyboard input.

One-command UI + native + regression evidence: `qa\acceptance\Run-UserAcceptance.ps1`.
Paid execution requires explicit approval and `-ApprovedAdditionalUsd`; it
uses the same shared ledger, never a new per-run allowance. For the approved
scope: `qa\acceptance\Run-UserAcceptance.ps1 -Live -Project -ApprovedAdditionalUsd 1`.
It keeps collecting recorded failures and uses only newly-created unique
reports. Its bounded artifact grader is not independent semantic sign-off.

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\flaui_bootstrap.py
.tooling\qa-venv\Scripts\python.exe qa\acceptance\user_journeys.py --native --network-mcp
```

Each case produces PASS/FAIL, numbered reproduction steps, a PNG, console/errors
and stack trace JSON, a Playwright trace ZIP and the backend log. The native case
also captures its real window and the accessibility tree on a UI failure. If
the native window never appears, capture is unavailable and explicitly reported,
not replaced with a misleading browser image.

Native captures are checked for a blank client area separately from UIA control
availability. Up to five bounded paint observations retain every frame; no
control action is repeated. `NOT_BLANK` is not a semantic visual/UX certification.
The original blank captures remain failures, not retroactively relabeled.

View traces using:

```powershell
.tooling\qa-venv\Scripts\playwright.exe show-trace <case>.trace.zip
```

Optional paid UI journeys (greeting → create HTML → edit → restart and verify)
use the configured OpenRouter credential and a price-bounded economic model:

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\live_ui_journeys.py --max-additional-usd 1
.tooling\qa-venv\Scripts\python.exe qa\acceptance\project_journeys.py --max-additional-usd 1 --trials 2 --soak-seconds 900
.tooling\qa-venv\Scripts\python.exe qa\acceptance\research_receipt_journey.py --max-additional-usd 1
```

This opt-in creates encrypted credential copies only under its own `.tooling`
runtime and removes them at completion. Never put keys on the command line.
Both suites share `qa/results/additional-budget-20261002.json`: each actual
completion reserves a conservative token-price bound before dispatch, then
settles with the provider's attributed generation cost. Unknown charges retain
their reservation; restarting a suite cannot reset the allowance. The approved
additional allowance is at most $1, not a promise to spend the whole amount.
Do not create fresh ledger paths to evade that shared ceiling. Read-only key
usage totals are supplementary evidence, not billing attribution for this run.
Real account OAuth completion and outgoing messages are not automated.
Project diagnostics may opt into `--capture-public-writes` to retain only the
allowlisted synthetic public research write proposals, actual CISA/FIRST
observations and attachment-scoped Bandit summaries. It is not a general prompt
or private-file recorder. `--compact-trace` retains actions, network trace,
final screenshots and logs, without continuous visual snapshots during polling.
Reports distinguish exact owned-turn generation cost from the shared ledger's
before/after delta, which can include other authorized runners during an unpaid
soak. Unknown charges keep their reservations in that same ledger.

The narrower `structured_write_diagnostic_journey.py --max-additional-usd 1`
also reuses this exact ledger. Its field/receipt presence grader does not prove
semantic correctness: independent review of the actual saved report against its
own source observations is still required. Historical FAIL evidence is retained.
The receipt journey is one targeted real-UI source/provenance regression,
not the full live/project matrix. `--capture-public-draft` records only the
explicit public CVE test report in its owned synthetic runtime, not arbitrary
user files or complete model prompts.

From the repository root, using the isolated QA environment:

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\run_acceptance.py --native
```

Reports and screenshots are written under `qa-results/<UTC timestamp>/`.
The coordinator is keyless and owns an isolated runtime; `--live` and attaching
to a production backend are rejected. Paid trials use the separate shared-budget
commands above. Secrets are never copied into its evidence directory.

Final evidence can be combined without replacing historical results:

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\quality_gate.py --regression <regression.json> --ui <ui-report.json> --live <live-report.json> --project <project-report.json> --semantic-review <semantic-review.json> --output <new-quality-gate.json>
```

Use actual absolute report paths. The gate checks reproduction assets, current
source stamps and independent correctness; a stale report cannot become current
by relabeling it. Manual release gates remain explicit even if execution passes.

`review_project.py --report <owned-project-report.json> --output <new-review.json>`
keeps missing/corrupt required artifacts as blocking FAIL findings and checks
the other surviving files. It does not create substitute files, overwrite prior
reviews or attribute an old project to the current checker application stamp.

## Release rule

For a future full live-UI run, opt into `--capture-public-research` to retain
the actual bounded public Gemini/Cloudflare source bodies and proposals for
independent claim review. Execution PASS without those observations does not
prove the research answer. This option never supplements or relabels earlier
non-instrumented reports with later fetched pages.

`project_journeys.py --only-follow-up --trials 2 --soak-seconds 0` is a
focused paid diagnostic under the same existing ledger, **not** repeated full
project acceptance. Required analysis/source/code scenarios remain absent;
the quality gate rejects their absence. New project reports also bind the
runner script at startup. Normal-mode paid admission allows at most 13 requests
(one real preflight classification plus the executor's existing maximum 12),
without changing the shared dollar cap or clearing unknown reservations.

`READY` requires every release-critical deterministic, API, UI, native, and
live-agent stage to pass.  Static and dependency scanners are evidence inputs:
their findings require severity triage and cannot be converted to a pass merely
because the scanner process exited successfully.

Passing the user journeys is **UI PASS**, not product certification. Current
release reports stay NOT_READY while real-account OAuth, voice/GPU coexistence
and long-running reliability lack acceptance evidence. Unavailable optional
features and skipped tests are stated explicitly. No finite test suite can
prove zero future bugs or cover every third-party service automatically.

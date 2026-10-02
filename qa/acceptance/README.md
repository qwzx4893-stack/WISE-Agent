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

One-command UI + native + regression gate (add `-Live` only when cloud spending
is authorized): `qa\acceptance\Run-UserAcceptance.ps1 -Live`.

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\flaui_bootstrap.py
.tooling\qa-venv\Scripts\python.exe qa\acceptance\user_journeys.py --native --network-mcp
```

Each case produces PASS/FAIL, numbered reproduction steps, a PNG, console/errors
and stack trace JSON, a Playwright trace ZIP and the backend log. The native case
also captures its real window and the accessibility tree on a UI failure. If
the native window never appears, capture is unavailable and explicitly reported,
not replaced with a misleading browser image. View traces using:

```powershell
.tooling\qa-venv\Scripts\playwright.exe show-trace <case>.trace.zip
```

Optional paid UI journeys (greeting → create HTML → edit → restart and verify)
use the configured OpenRouter credential and a price-bounded economic model:

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\live_ui_journeys.py
```

This opt-in creates encrypted credential copies only under its own `.tooling`
runtime and removes them at completion. Never put keys on the command line.
Repeated live runs incur additional costs; the script's per-run bound is not
a shared billing ledger. Real account OAuth completion is not automated.

From the repository root, using the isolated QA environment:

```powershell
.tooling\qa-venv\Scripts\python.exe qa\acceptance\run_acceptance.py --live --native
```

Reports and screenshots are written under `qa-results/<UTC timestamp>/`.
Secrets are never copied into that directory.  The live stage resolves the
already-encrypted provider configuration through WISE itself.

## Release rule

`READY` requires every release-critical deterministic, API, UI, native, and
live-agent stage to pass.  Static and dependency scanners are evidence inputs:
their findings require severity triage and cannot be converted to a pass merely
because the scanner process exited successfully.

Passing the user journeys is **UI PASS**, not product certification. Current
release reports stay NOT_READY while real-account OAuth, voice/GPU coexistence
and long-running reliability lack acceptance evidence. Unavailable optional
features and skipped tests are stated explicitly. No finite test suite can
prove zero future bugs or cover every third-party service automatically.

# Validation status — 2026-10-15 publication preparation

**Development preview / NOT_READY for unconditional daily-use reliability.** Regression success, renderer scenarios, model execution, research correctness, real-account authorization and native/hardware acceptance are separate gates.

The October security audit added source-bound, offline regression evidence for
HTTP/WebSocket ingress, OAuth, operation consent, file/preview containment,
secret persistence, official UAC catalogue contracts, native bridge exposure,
untrusted file observations, TLS and voice worker resource/loading boundaries.
The October 13 aggregate passed **544 tests without failures/errors/skips**;
this is historical contract evidence, not certification of later edits.
October 15 preparation additionally passed **561 offline security/publication
tests across 22 suites**, with zero failures/errors/skips and no omitted suites,
and verified exported Python 3.11 syntax and required files. These checks do not
close the release gates below. Private raw reports, owner state and credential candidates are not public
release artifacts. Documentation changes are not runtime acceptance.

| Evidence | Outcome | Boundary |
| --- | --- | --- |
| Non-interactive regression v21 | 1,492 PASS, 4 SKIP, 2 DESELECT | Not native acceptance; unavailable/environment-specific and legacy interactive cases excluded. |
| Renderer user scenarios | 18/18 PASS | Real local backend, headless Brave; not native frame/input. |
| Plugins renderer scenarios | 12/12 PASS | Includes explicit external Nango mocks; not real-account OAuth/messaging. |
| Bounded live-model UI scenarios | 8/8 PASS | Execution behavior, not every answer's factual correctness. |
| Latest focused project follow-up | 3 PASS, 1 FAIL | Research quality failed; no current-build long-duration soak acceptance. |
| Latest aggregate quality gate | FAIL / NOT_READY | Remaining gates are not waived by regression counts. |

The table above is historical product evidence from the earlier preparation;
its counts are not added to the security aggregate or represented as a current
all-feature acceptance run. Nango mocks do not represent the present direct-MCP
authentication implementation.

## Security-specific limits

- Current dependency inventories still contain unresolved advisory observations,
  especially in optional audio runtimes. Advisory records are not automatically
  distinct exploitable findings; compatibility-tested remediation remains open.
- Complete private-data export policy, production connector identity/consent and
  reliable owner-approval UI resume still need end-to-end acceptance.
- No isolated Windows environment was available. Real UAC, native sender/frame
  behavior, clean installation/update/rollback and two/eight-hour native soak
  are unverified, not inferred from mocks or test counts.
- Tests deliberately use synthetic inputs, without owner accounts, paid models,
  actual external messaging or untrusted model/checkpoint loading.

## Open acceptance gates

1. Research correctness, complete sourced outputs and reliable tool/model failure continuation.
2. Native Windows UIAutomation/FlaUI, actual window lifecycle and input.
3. Owner OAuth login/refresh/expiry/disconnection and account isolation; messaging needs separate permission.
4. Voice microphone/echo/interruption, Arabic/English clarity and GPU coexistence.
5. Clean Windows installation, upgrade/recovery/uninstall, sign-in background mode and UAC.
6. Sustained UI/backend/model/worker use; short backend-only memory sampling is insufficient.
7. Upstream provenance review and WISE owner license decision before claiming complete reuse rights.

Acceptance tooling records screenshots, logs, traces, reproduction details and build stamps. Paid tests need explicit budgets. Never embed tokens, reuse account cookies in fixtures, or call mock connections successful live integrations.

# Validation status — 2026-10-05 publication preparation

**Development preview / NOT_READY for unconditional daily-use reliability.** Regression success, renderer scenarios, model execution, research correctness, real-account authorization and native/hardware acceptance are separate gates.

Most recently reviewed Agent OS implementation stamp:
`ea8c46b40d52d4c8041f309a1b0259162c70e88123c6f050aa1a62baada17d28`.
Documentation/publication changes are not new runtime acceptance. Private reports are not copied to public source.

| Evidence | Outcome | Boundary |
| --- | --- | --- |
| Non-interactive regression v21 | 1,492 PASS, 4 SKIP, 2 DESELECT | Not native acceptance; unavailable/environment-specific and legacy interactive cases excluded. |
| Renderer user scenarios | 18/18 PASS | Real local backend, headless Brave; not native frame/input. |
| Plugins renderer scenarios | 12/12 PASS | Includes explicit external Nango mocks; not real-account OAuth/messaging. |
| Bounded live-model UI scenarios | 8/8 PASS | Execution behavior, not every answer's factual correctness. |
| Latest focused project follow-up | 3 PASS, 1 FAIL | Research quality failed; no current-build long-duration soak acceptance. |
| Latest aggregate quality gate | FAIL / NOT_READY | Remaining gates are not waived by regression counts. |

## Open acceptance gates

1. Research correctness, complete sourced outputs and reliable tool/model failure continuation.
2. Native Windows UIAutomation/FlaUI, actual window lifecycle and input.
3. Owner OAuth login/refresh/expiry/disconnection and account isolation; messaging needs separate permission.
4. Voice microphone/echo/interruption, Arabic/English clarity and GPU coexistence.
5. Clean Windows installation, upgrade/recovery/uninstall, sign-in background mode and UAC.
6. Sustained UI/backend/model/worker use; short backend-only memory sampling is insufficient.
7. Upstream provenance review and WISE owner license decision before claiming complete reuse rights.

Acceptance tooling records screenshots, logs, traces, reproduction details and build stamps. Paid tests need explicit budgets. Never embed tokens, reuse account cookies in fixtures, or call mock connections successful live integrations.

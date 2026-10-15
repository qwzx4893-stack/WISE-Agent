# Validation status

[README](../README.md) · [Architecture](ARCHITECTURE.md) · [Security](../SECURITY.md)

**Development preview / NOT_READY for unconditional daily-use reliability.** Source regressions, renderer scenarios, model execution, research correctness, real-account authorization and native/hardware acceptance are separate gates.

## Verified source snapshot — 15 October 2026

Executable-source commit: [`71a4217ef07bca63b14525e24417d8d2c86ee177`](https://github.com/qwzx4893-stack/WISE-Agent/commit/71a4217ef07bca63b14525e24417d8d2c86ee177).

| Layer | Result | Boundary |
| --- | --- | --- |
| Local offline security | 591 passed; zero failures/errors/skips; 24 suites | Audit checkout, synthetic/component checks; not product certification. |
| [GitHub focused security](https://github.com/qwzx4893-stack/WISE-Agent/actions/runs/38780577374), Windows / Linux, Python 3.11 | 584 / 581 passed; zero failures/errors/skips | Optional vendor/platform omissions recorded separately. |
| [GitHub broad source](https://github.com/qwzx4893-stack/WISE-Agent/actions/runs/38780577135), Windows | 2,148 passed; 14 skipped; 11 deselected | Non-interactive source checks; zero failed. |
| Same broad source, Linux | 2,144 passed; 15 skipped; 14 deselected | Source compatibility; zero failed; not native Linux acceptance. |

The suites overlap and their counts must not be summed. An omitted suite is not a passed suite; skips and deselections do not establish feature safety. Documentation-only commits may follow this snapshot; workflow badges track their own latest branch run.

The focused audit covers tested HTTP/WebSocket ingress, OAuth, scoped consent, filesystem/preview containment, secret persistence, official UAC catalog contracts, native bridge exposure, untrusted file observations, TLS and voice loading/resource boundaries. It does **not** establish exhaustive SAST triage, complete fuzz coverage or absence of unknown vulnerabilities.

## Historical product evidence

These older runs are useful context, **not acceptance of the current complete product**:

| Earlier layer | Recorded result | Limit |
| --- | --- | --- |
| Non-interactive regression v21 | 1,492 passed, 4 skipped, 2 deselected | Environment-specific/legacy cases excluded. |
| Renderer user scenarios | 18/18 passed | Local backend and headless Brave; not native frame/input. |
| Former plugins renderer scenarios | 12/12 passed | Explicit Nango mocks; not current direct-MCP account acceptance. |
| Bounded live-model UI scenarios | 8/8 passed | Behavior observations; not universal factual correctness. |
| Focused quality follow-up | 3 passed, 1 failed | Research-quality failure; no current long-duration native acceptance. |

Older 544/561-test security aggregates are superseded by the source-bound October 15 evidence above. None of these layers closes the release gates below.

## Security-specific limits

- Dependency inventories contain unresolved advisory observations, particularly optional audio environments. An advisory record is not automatically a distinct exploitable finding; compatibility-tested remediation remains open.
- A complete destination/data/scope private-data egress policy remains unfinished across all execution surfaces.
- Same-user/source/settings self-protection and native bridge sender/frame attestation remain incomplete.
- Owner consent presentation, run resumption, live connector identity and account isolation need end-to-end acceptance.
- Credential candidate triage/rotation is not proven complete. No published credentials should be reused.
- Decoder isolation, actual voice resources/hashes and full SAST/fuzzing acceptance remain unverified.
- No isolated interactive Windows environment was available. UAC, native races, clean install/update/rollback and two/eight-hour native soak are unverified.
- Synthetic checks do not exercise owner accounts, paid models, actual external messaging or untrusted checkpoint loading. Optional vendor/native tools omitted in CI are recorded as unavailable, not safe.

## Open acceptance gates

1. Research correctness, complete sourced outputs and reliable continuation after tool/model failure.
2. Native Windows UIAutomation/FlaUI, frame/input lifecycle and bridge sender attestation.
3. Real OAuth login/refresh/expiry/revocation, account isolation and complete approval/resume UX. External messaging needs separate authorization.
4. Voice microphone/echo/interruption, Arabic/English clarity and GPU coexistence.
5. Clean installation, upgrade/recovery/uninstall, sign-in background mode and official UAC.
6. Sustained UI/backend/model/worker use, including two/eight-hour native soak.
7. Dependency remediation, private-data egress controls, same-user self-protection and complete sensitive-boundary triage.
8. Upstream provenance review and an owner license decision before claiming complete reuse rights.

## Reproducing and interpreting evidence

Use the CI dependency sets and runners in [source CI](../.github/workflows/validate.yml) and [security CI](../.github/workflows/security-regressions.yml). Artifacts are associated with workflow runs and may expire under retention settings.

Acceptance tooling records build stamps, logs, traces, reproduction details and screenshots where applicable. Private raw audit reports, state and credential candidates are not public release artifacts. Paid tests need explicit budgets.

See the [acceptance plan](../Supergent--main/docs/WISE_FINAL_VALIDATION_PLAN.md). Never call mock connections successful live integrations, or infer native acceptance from a browser screenshot.

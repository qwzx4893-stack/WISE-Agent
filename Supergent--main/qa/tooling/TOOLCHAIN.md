# WISE acceptance toolchain

The acceptance harness keeps audit tools outside the production dependency
set.  It combines deterministic tests with user-level desktop journeys and an
LLM judge; no single tool may award a production-ready verdict by itself.

| Layer | Tool | Purpose |
| --- | --- | --- |
| Windows desktop | pywinauto/UIA | Native-window launch, focus, controls, resize, and recovery |
| WebView UI | Playwright + Axe | Interaction, screenshots, responsive states, and accessibility |
| API contract | Schemathesis + Hypothesis | OpenAPI fuzzing and invariant/edge-case generation |
| Regression | pytest + xdist + timeout + JSON report | Reproducible functional evidence |
| Supply chain | pip-audit | Known Python dependency advisories |
| Static security | Bandit | Python security findings requiring manual triage |
| Agent surface | ECC AgentShield | Agent/MCP/hook/secret configuration scan |
| Agent behavior | OpenRouter-backed scenario judge | Tool use, memory, recovery, refusal, and task-quality rubrics |

ECC source is reviewed from the official `affaan-m/ECC` repository at tag
`v2.2.1` / commit `5064474d4d762dc9640234a41617cccb79185cec`.  It is kept in
`.tooling/ecc-2.2.1` until the user explicitly approves installing its native
Codex plugin, because the plugin enables write-capable skills, MCP definitions,
and lifecycle hooks.

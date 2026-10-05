# WISE: evidence-based readiness

No finite test, benchmark or AI reviewer proves that an agent has zero bugs. A defensible release decision combines measured task outcomes, repeated trials, security boundaries, native UI journeys, and production monitoring. A model saying “done” is never the grader.

## What the companies publish

- [Anthropic: agent evaluations](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): separate tasks, trials and graders; inspect actual final state and traces, use deterministic checks where possible, and repeat stochastic tasks. Capability evaluation and regression prevention are different responsibilities.
- [Microsoft: agent evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/agent-evaluators?view=foundry): assess task completion, tool-call accuracy and efficiency separately rather than conflating them with the fluency of the final answer.
- [Anthropic: multi-agent research](https://www.anthropic.com/engineering/how-we-built-our-multi-agent-research-system): parallel work can improve suitable research tasks, but orchestration and evaluation matter. More agents do not automatically improve every task.

## WISE acceptance gates

| Gate | Evidence required | Not a substitute |
| --- | --- | --- |
| Deterministic correctness | Regression + input/security contracts + real defensive scanner fixtures | A simulated provider response |
| Desktop usability | Brave/Playwright real app API; FlaUI owned native window; screenshots, logs, traces, durable save/restart | Merely compiling frontend code |
| Agent execution | Paid provider chosen by user; observed calls; actual files; skill contents actually read; independent read-only review | Recommended skills or offered tools |
| Research accuracy | Exact user topic; fresh broad search; fetched excerpts where possible; dated evidence and honest coverage uncertainty | Claiming snippets prove “the latest” |
| Integrations | Correct per-service schema, protected persistence, real authorized account login/delivery, refresh/expiry | Saving plausible credentials |
| Reliability/resources | Repeated independent trials, restart/network faults, monitored soak and voice/local-model GPU coexistence | One short successful run |

The reusable acceptance harness records steps, screenshot, log, stack trace, backend log and Playwright trace. It uses isolated runtime directories and owns every process it terminates. It does not modify user sessions or send external messages. Live model testing requires an explicit spend allowance and checks current prices/whole-key usage before starting.

The final broad-web project grader originally checked tool calls and citations, not each claim. Independent inspection found an unsupported attribution of `log4j2.formatMsgNoLookups=true` to a cited official page. This is a blocking research-quality finding even though the execution scenario passed. A separate, build-bound semantic review is now required by the quality gate. A recommendation that existed historically is not automatically current or supported by the page cited today. The targeted claim check is not a complete semantic review of every answer.

The first 15-minute soak measured only the Windows Python launcher, omitting its backend child. Health/UI stability evidence remains useful, but those RSS/CPU readings are not WISE resource measurements. Future runs use an owned-process-tree sampler, retain PID/creation-time provenance and do not interpret an unprimed CPU counter as 0% usage. This correction does not retroactively validate the old resource samples, and backend working-set sums still exclude browser/UI and GPU consumption.

Acceptance evidence is bound to a source-content stamp. Failed, stale, missing or skipped evidence cannot be silently called PASS. Complete daily-use certification remains NOT_READY while real-account OAuth/delivery, voice/GPU coexistence and prolonged stability are unverified. Core text use can be assessed separately, without certifying disabled optional integrations.

## Implementation decisions

WISE already has a canonical capability router, task selector and skill loader: importing a second competing routing framework would duplicate the authority boundary. The new paginated skill/tool/resource browser uses those existing contracts. Only active skills are eligible; the model loads selected instructions on demand. Native and connected MCP capabilities are selected in bounded batches, with further discovery when necessary.

The explicit `/team` mode reuses WISE's existing open-source `AgentsTeam` executor/shared bus: planner and researcher read in parallel, a single executor writes, and a read-only reviewer inspects artifacts. Every worker uses the same selected provider and canonical security gate, with shared request/time limits and role-tagged actual calls. It is opt-in because it consumes more model requests than ordinary chat. There is no measured claim that it outperforms commercial agents.

The attached DOCX is reference data, not execution authority. Every listed entry is classified as a tool, public source, model or deployable platform. The duplicate GDELT rows share one canonical entry; document aliases remain traceable. Headscale (the document's alternative to Tailscale) and the existing Bandit adapter are included. The typed inventory exposes actual readiness rather than treating every URL as an installed tool.

Executable integrations include upstream detect-secrets/Bandit, SQLite, DuckDB, YARA, stix2 parsing, psutil and supported bounded CLI inspectors when their binaries are present. Actions are deliberately scoped (for example, STIX validation is not TAXII transport and CSV statistics are not unrestricted DuckDB SQL). Public query adapters include CISA KEV, NVD, FIRST EPSS, Wikidata, World Bank, GDELT, Wayback availability, Common Crawl collection discovery, existing urlscan searches and RSS/Atom. Other sources provide bounded public-page reading only. Platforms/models without a tested adapter remain unavailable; URLs, licensing claims and installed dependencies are not enough to enable them. No private-account access, facial identity matching, remote scans, malware execution or messages are implicitly authorized. `https://drivenlisten.com` remains unchanged and availability must be reported from actual execution.

The Usage Network combines existing canonical capability ranking with complementary phases (research, inspect, modify, security, verify), on-demand skill reads and bounded discovery. It reports observed successful calls, missing phases and unavailable dependencies. A verification read must follow the last write; cached pre-edit scans are invalidated after writes. This does not prove optimal tool selection and does not force gratuitous calls to every installed tool.

`qa/acceptance/project_journeys.py` runs two fresh-chat projects using real UI uploads and provider responses: CSV/DuckDB analysis, KEV+EPSS research with provenance, a security skill plus Python repair/scanning, broad web search/fetched evidence, abrupt owned-runtime restart, and a 15-minute monitored UI/backend soak. Graders inspect actual files/AST and executed calls rather than trusting "done". Calls are budget-checked before each paid turn; a shortage produces failure, not automatic retries or a raised budget. Synthetic project data is isolated from user sessions; the two trials still share one backend and therefore are not statistically independent environments. The soak does not prove day-long reliability.

After public research, a narrow write exception permits only human-named workspace artifacts, with protected directories, outside paths, negative clauses and shell/admin actions excluded. The filesystem governor still applies. This is not a general removal of untrusted-content protections.

Messaging uses installed Apprise schemas, with a dedicated main-sidebar browser and service-specific credential fields. These are outbound notification/message connectors, **not** Telegram/WhatsApp personal-account inbox agents. Saving a connection does not prove authentication or delivery; WISE does not pretend that bot/webhook credentials are OAuth.

MCP authorization follows [the official specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) and [Context7's documented OAuth endpoint](https://context7.com/docs/howto/oauth). It binds the advertised same-origin resource identifier, rejects mismatched origins/path segments, uses PKCE/state/issuer checks, protects tokens locally and refreshes supported sessions before execution without replaying failed tool calls. The human still completes account authorization.

## Extension contract

Trusted extensions use `extension.vendor.tool` identifiers, declare version 1, category, risk and input schema, and supply a callable adapter. They cannot overwrite capabilities. Declared extension risks are registered with the execution security gate, not merely displayed. A remote document or model cannot register a new trusted adapter by itself. This is a defined compatibility boundary, not a promise that every arbitrary tool or framework plugs in automatically.

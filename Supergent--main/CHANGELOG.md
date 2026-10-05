# Changelog

All notable changes to Agent OS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-04-27

Initial production release. The first stable, fully audited version
of Agent OS — a self-evolving agentic operating system.

### Highlights

- **115 REST endpoints** + 1 WebSocket (`/chat/stream`) with full
  protocol (`session` / `token` / `tool_call` / `tool_result` /
  `think` / `final` / `error`) and client-driven `cancel`.
- **290 registered tools** (admin, preview, Google, skill facade,
  native adapters, MCP, 190 JSON-pack manifests).
- **1,725 skills** indexed by `SkillIndexer` with semantic search and
  upload-only `POST /admin/skills/upload`.
- **172 Apprise channel schemes** through `core/channels/unified.py`.
- **15 RAG sources** (Wikipedia, arXiv, PubMed, GitHub, Semantic
  Scholar, OpenAlex, Crossref, CVE NVD, MITRE ATT&CK, GTFOBins,
  LOLBAS, …).
- **Multi-platform packaging** via
  `.github/workflows/release.yml` — Android APK on `ubuntu-latest`,
  Linux `.tar.gz` on `ubuntu-latest`, Windows `.zip` on
  `windows-latest`.
- **442 tests passing** (416 unit + 8 e2e + 18 Flutter).

### Added

- `core/preview_server.py` — localhost-only HTTP server for serving
  agent-built artifacts behind a clickable link, with three callable
  tools (`preview.serve` / `preview.stop` / `preview.list`) and three
  admin endpoints.
- `core/oauth/` — OAuth 2.0 + PKCE flow, loopback callback, encrypted
  token storage in `KeyStore`, providers (Google to start), and 6
  Google account tools (Gmail / Calendar / Drive).
- `core/admin_tools.py` — 17 admin actions exposed as agent-callable
  tools so the agent can self-configure (add API keys, register MCP
  servers, switch modes) directly from chat.
- `core/multi_strategy.py` — multi-strategy executor: pick the best
  tool from candidates, fall back on failure.
- `core/intent_classifier.py` — routes chat input to admin / chat /
  workflow paths.
- `core/self_test.py` + `/admin/self-test` + `/admin/self-test/cached`
  — onboarding self-test gate.
- `tests/test_phase11_audit.py` (56 tests) — backend audit covering
  paths, tool registry sizing, ReAct loop with mock LLM, full GET
  surface, Apprise scheme count, RAG source count.
- `tests/test_phase11_layers.py` (15 tests) — per-layer smoke for
  RAG / Skills / Channels / Scheduler / Workforce / Self-Healing /
  Self-Modify / OAuth / Preview server.
- `ui/flutter_app/test/phase11_ui_audit_test.dart` (14 tests) — every
  Settings tab + theme/language toggles.
- Flutter UI: chat + 12 settings tabs (Models, Instructions, API
  Keys, MCP, Channels, Scheduler, Skills, Appearance, Accounts,
  Resources, Self-Test, Dashboard).
- `docs/RELEASE_NOTES.md`, `docs/RELEASE_README.txt`.

### Changed

- `core/paths.py` — `TOOLS_PACKS_DIR` and `SKILLS_DIR` now fall back
  from the legacy `~/agent-os` runtime tree to the repo checkout when
  the runtime tree is empty or contains only dotfiles. This raised
  the registered tool count from ~88 to 290 and the skill index from
  0 to 1,725.
- `api/server.py` — dropped `Header(convert_underscores=False)` from
  79 endpoints (silently broke auth on every admin route).
- `core/channels/unified.py` — Apprise scheme enumeration now reads
  the live module each call so monkeypatched test stubs don't leak.
- `requirements.txt` — added `python-docx`, `openpyxl`, `python-pptx`
  for `core/documents.py`.
- README — full v1.0.0 rewrite with architecture map, API table,
  configuration guide, environment variables, packaging,
  acknowledgments.

### Fixed

- Stale-loop bug in `core/sessions.SessionRegistry` exposed by the
  WebSocket runner.
- Auth header parameter name mismatch (`x_agent_token` →
  `X-Agent-Token`) caused by FastAPI underscore conversion.
- Skill index returned 0 when `AGENT_OS_ROOT` pointed at a runtime
  tree with only `.index_cache.json`.

### Security

- Preview server binds exclusively to `127.0.0.1`.
- OAuth tokens encrypted at rest via `core/llm/keystore.py`.
- Rate-limit middleware on `/chat` and `/execute` (token-bucket).
- Admin endpoints gated by `AGENT_API_TOKEN` Bearer header (or
  `?token=` query param for WebSockets).

## [0.x] — Pre-1.0

Development phases tracked in PRs #1 and #2:

- Phase 1–6: kernel, ToolRegistry, ReAct loop, KnowledgeSources,
  SkillIndexer, ToolIntelligence, SystemAwareness, FastAPI shell,
  initial Flutter UI.
- Phase 7: Workforce, Scheduler, Documents, Learner, Streaming
  sessions, MCP integration.
- Phase 8: Apprise channels (124 schemes initially), token streaming
  on OpenAI / Anthropic / Gemini, advanced compression
  (LLMLingua / Un-LOCC / Chonkify).
- Phase 9: Public API surface (`/tools/{name}`, `/skills`,
  `/skills/search`, `/health/detailed`), WebSocket `/chat/stream`,
  self-test layer, mock fixtures + 8 e2e scenarios, pytest-cov in CI.
- Phase 10: Agent autonomy (admin tools + multi-strategy + intent
  classifier), Local Preview Server, OAuth + Google tools, Flutter UI
  rewrite (12 settings tabs).
- Phase 11: this release — full audit, layer testing, multi-platform
  packaging.

[1.0.0]: https://github.com/ZXM878/Supergent-/releases/tag/v1.0.0

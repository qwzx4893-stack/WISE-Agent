# WISE — Local-first Agent OS

**A Windows-first agent workspace connecting conversation, cognition, multi-agent execution, tools, skills, research, persistent state and desktop automation.**

WISE is more than a chat interface around a model. Its Agent OS core coordinates task execution, discovers capabilities on demand, manages model and worker resources, persists conversations and recoverable task state, and connects a native desktop workspace to local and remote services. The wider workspace retains its Leon assistant integration and process supervisor.

> **Development preview, not a certified production release.** Implemented capabilities are not a guarantee of error-free autonomous operation. Recent non-interactive regression passed 1,492 tests, but research-quality acceptance still has failures and native Windows, real-account OAuth and voice hardware acceptance remain incomplete. See [validation status](docs/VALIDATION.md).

## What WISE brings together

| Layer | Responsibilities |
| --- | --- |
| Conversation & cognition | Streaming turns, intent and task routing, planning, execution contracts, outcome checks and persistent user instructions. |
| Capability / Usage Network | Task-oriented discovery of tools, grouped skills, MCP services and research sources, rather than injecting the entire inventory into every prompt. |
| Multi-agent execution | Specialized workers with a single-writer coordination path, bounded execution and evidence checks for requested outputs. |
| Models & efficiency | Explicit user-selected cloud/local models; context and request optimizations, caches, resource admission and isolated worker lifecycle. Provider failures do not silently change the user's chosen provider. |
| Research & intelligence | Web search, source retrieval, document inspection, structured public-source adapters, evidence checks and on-demand security-tool recipes. Catalog membership is not proof that an executable or adapter is ready. |
| State & recovery | Durable conversations, artifacts, task checkpoints, scheduling, recovery and diagnostic logs. Recovery is not universal rollback of arbitrary machine changes. |
| Integrations | MCP configuration and OAuth flows, a Plugins catalog with Nango-backed connections, and messaging connectors with configuration-aware status. External accounts require separate setup. |
| Windows & browser | Native pywebview host, file and desktop operations, opt-in background/system integration, managed browser sessions and explicitly requested device-browser access. |
| Optional voice | Isolated IndexTTS synthesis and CPU Whisper transcription, interruption handling and VRAM admission guards. Custom voice assets are private runtime inputs. |

## Workspace layout

```text
WISE-Agent/
  Supergent--main/       Agent OS core, API, current WISE UI, skills, tools, tests
  leon-develop/          Leon source integration, SDKs, tools and lockfiles
  config/               Cross-service configuration code (not account secrets)
  wise_supervisor.py    Multi-process supervisor for Agent OS and Leon services
  wise_desktop.py       Workspace desktop entry point
  tools/                Workspace setup utilities
  tests/                Cross-service integration tests
  docs/                 Installation, validation and distribution notes
```

This layout replaces the older flattened pre-Plugins publication. Existing clones should follow the new paths below. The current product interface lives in `Supergent--main/ui/wise_web`; no comparison UI is required to run it.

## Start the native desktop

Requirements: **Windows 10/11, Python 3.11, Git, Microsoft Edge WebView2 Runtime**. WebView2 is the native rendering engine, not a request to open the interface in the Edge browser. Brave is needed for the installed-Brave automation path.

```powershell
git clone https://github.com/qwzx4893-stack/WISE-Agent.git
cd WISE-Agent
powershell -NoProfile -File tools/Install-WISE.ps1
.\Supergent--main\.venv\Scripts\python.exe .\Supergent--main\wise_desktop.py --native-only
```

Configure a provider in **Settings → Models and providers**, choose a model in the composer, then start a conversation. Credentials are supplied locally; none are bundled. Do not copy someone else's browser profile, runtime state or keystore into a new installation.

The default install is text-first. Voice, semantic search, advanced compression, external accounts and security executables are optional—not silently downloaded or represented as connected. See [installation and dependency matrix](docs/INSTALLATION.md).

For backend development only:

```powershell
cd Supergent--main
.\.venv\Scripts\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8765
```

## Optional integrations

- **Leon:** source with manifests and lockfiles; Node.js 24+ and pnpm required. Follow [workspace setup](docs/INSTALLATION.md#leon-workspace-integration) before running the root supervisor. Core desktop chat does not require booting Leon.
- **MCP:** import server configuration in Settings. Install stdio commands or configure remote endpoints and the server's supported authentication method.
- **Plugins / messaging:** configure Nango and provider OAuth applications using [Plugins setup](Supergent--main/docs/PLUGINS_NANGO.md). A browsable catalog is not a connected account.
- **Voice:** provision the compatible pinned IndexTTS runtime, model weights and permitted custom assets separately. See [voice setup](docs/INSTALLATION.md#voice).
- **Security / OSINT:** lightweight adapters and isolated on-demand recipes are distinct from sources and deferred platforms. MISP, OpenCTI and CAPE are not deployed automatically. See [optional security tooling](Supergent--main/docs/OPTIONAL_SECURITY_TOOLS.md).

## Safety boundaries

Keep the API loopback-only unless you configure authentication and trusted origins. Browser/account access, file edits, command execution and external messaging have real consequences; use a dedicated workspace and review high-impact actions.

Opt-in system integration uses official Windows mechanisms. Administrative operations require UAC consent and a fixed broker allowlist; WISE does **not** bypass UAC or grant unrestricted invisible administrator access. Windows host allowlists are not OS sandboxing. Model instructions alone are not a security boundary.

Secrets, sessions, profiles, logs, installed environments, weights, recordings and training data are excluded from public source. Package manifests, install recipes and examples are included instead. See [distribution contract](docs/DISTRIBUTION.md) and [security reporting](SECURITY.md).

## Development & validation

```powershell
cd Supergent--main
.\.venv\Scripts\python.exe -m pip install pytest pytest-cov
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

Unit/regression, API/security, recovery, renderer/user-scenario, live-model and native-observation test layers are included. Live tests need explicit credentials, a bounded budget and isolated runtime state. Mocked connections and headless UI results are never evidence of native desktop or real-account acceptance. See [validation scope](docs/VALIDATION.md) and the [acceptance plan](Supergent--main/docs/WISE_FINAL_VALIDATION_PLAN.md).

## Documentation

- [Agent OS overview](Supergent--main/README.md)
- [Installation & dependencies](docs/INSTALLATION.md)
- [Architecture map](docs/ARCHITECTURE.md)
- [Validation & remaining gates](docs/VALIDATION.md)
- [Contribution workflow](CONTRIBUTING.md)
- [Third-party attribution](THIRD_PARTY_NOTICES.md)

## License & provenance

WISE-specific source currently has **no repository-wide license grant**. Public visibility does not grant reuse rights. Third-party components retain their own licenses and notices; the root package's historical MIT field does not license the entire workspace. Known non-transferable skills and private voice assets are excluded. See [licensing status](Supergent--main/docs/releases/LICENSING_STATUS.md) and the publication manifest. WISE is not affiliated with or endorsed by upstream projects it integrates.

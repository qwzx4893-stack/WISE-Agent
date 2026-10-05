# WISE Agent OS core

The active WISE execution system and native desktop workspace: conversation orchestration, cognition/task engine, multi-agent capability execution, tool/skill/MCP routing, models/resources, research, persistent state, integrations and Windows/voice adapters.

See the [workspace README](../README.md) for the whole product and setup. The directory name is retained for cross-service compatibility, not as the interface's product name.

## Architecture

| Component | Source |
| --- | --- |
| Conversation | `agent_core.py`, `core/brain/conversational_core.py`, `api/server.py` |
| Cognition / durable execution | `core/brain/`, `core/contracts.py`, `core/durable_io.py`, `core/session_service.py` |
| Capability discovery / teams | `core/capability_network.py`, `core/usage_network.py`, `core/capability_router.py`, `core/brain/capability_agent.py`, `core/brain/capability_team.py` |
| Models / resource lifecycle | `core/llm/`, `core/models/`, `core/resource/`, `core/optimization/` |
| Skills / tools / MCP | `core/skills/`, `skills/`, `tools/packs/`, `core/tool_intelligence/`, `core/mcp/` |
| Research / intelligence | `core/web_research.py`, `core/intelligence/`, `core/security/optional_tools.py`, `config/intelligence_resources.json` |
| State / schedules / instructions | `core/memory_service.py`, `core/scheduler.py`, `core/user_instructions.py` |
| Plugins / messaging | `api/plugin_routes.py`, `core/integrations/`, `core/channels/` |
| Desktop / system | `wise_desktop.py`, `core/windows/`, `core/system_integration.py`, `core/idle_controller.py` |
| Voice | `core/voice/`, isolated IndexTTS / Whisper workers |
| Current interface | `ui/wise_web/` |

Discovery is progressive: relevant capability guidance instead of the entire skill corpus. Tool selection depends on availability, scope and task contracts. Implemented multi-agent coordination does not prove every task used several agents successfully.

## Run

Install via root `tools/Install-WISE.ps1`, then:

```powershell
.\Supergent--main\.venv\Scripts\python.exe .\Supergent--main\wise_desktop.py --native-only
```

From this app directory, alternatively:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe wise_desktop.py --native-only
```

Python 3.11 and WebView2 are required for the native Windows path. Supply credentials in settings and choose a model explicitly. Voice/accounts require separate provisioning.

## Configuration & state

`config/server.json.example` and `.env.example` are templates. Immutable skills/packs come from source; `WISE_RUNTIME_ROOT` isolates writable state and `WISE_CONFIG_DIR` selects explicit configuration. Keep keys, sessions, profiles and account data out of Git.

See [voice](docs/VOICE_RUNTIME.md), [Plugins / Nango](docs/PLUGINS_NANGO.md), [security tools](docs/OPTIONAL_SECURITY_TOOLS.md) and [acceptance plan](docs/WISE_FINAL_VALIDATION_PLAN.md).

## Readiness & license

**Development preview / NOT_READY for blanket daily-use reliability.** Regression/renderer success does not close research, native Windows, real accounts, voice/hardware, installation or sustained-use acceptance. See [validation summary](../docs/VALIDATION.md).

No WISE-wide license has been selected. Preserve upstream licenses/notices. [Licensing status](docs/releases/LICENSING_STATUS.md).

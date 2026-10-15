# WISE architecture

[README](../README.md) · [Installation](INSTALLATION.md) · [Validation](VALIDATION.md)

## System model

WISE is a Windows-first Agent OS workspace. The primary runtime in `Supergent--main` connects conversation/cognition to capability discovery, multi-agent execution, persistent state, models and execution surfaces. The API serves the WISE renderer; the native host owns the window lifecycle and backend supervision path.

The [README diagram](../README.md#how-the-system-fits-together) is a logical map, not a claim that every component shares a process or identical permission mechanism.

## Source map

Paths are relative to `Supergent--main/` unless stated otherwise.

| Responsibility | Starting points |
| --- | --- |
| Conversation and local API | `agent_core.py`, `core/brain/conversational_core.py`, `api/server.py` |
| Cognition and durable execution | `core/brain/`, `core/contracts.py`, `core/durable_io.py`, `core/session_service.py` |
| Discovery and teams | `core/capability_network.py`, `core/usage_network.py`, `core/capability_router.py`, `core/brain/capability_agent.py`, `core/brain/capability_team.py` |
| Model / worker lifecycle | `core/llm/`, `core/models/`, `core/resource/`, `core/optimization/` |
| Skill, tool and MCP inventories | `core/skills/`, `skills/`, `tools/packs/`, `core/tool_intelligence/`, `core/mcp/` |
| Research / intelligence | `core/web_research.py`, `core/intelligence/`, `core/security/optional_tools.py`, `config/intelligence_resources.json` |
| Memory, scheduling and instructions | `core/memory_service.py`, `core/scheduler.py`, `core/user_instructions.py` |
| MCP Market / messaging | `api/plugin_routes.py`, `core/plugins/`, `core/channels/` |
| Desktop and system integration | `wise_desktop.py`, `core/windows/`, `core/system_integration.py`, `core/idle_controller.py` |
| Optional voice | `core/voice/`, separately provisioned IndexTTS / Whisper workers |
| Current renderer | `ui/wise_web/` |
| Regressions and evidence | `tests/`, `qa/acceptance/` |

## Work lifecycle

1. A turn arrives with its selected model, session context and persistent user instructions.
2. Cognition and task-contract paths determine the work and relevant capabilities.
3. The Capability / Usage Network narrows tools, skills, MCP services and sources toward the task.
4. A direct path or bounded team coordinates work through the applicable permission boundaries.
5. Outputs, artifacts and evidence feed outcome checks and durable state.

Details vary by route. Discovery is not installation; installation is not account authorization; execution success is not factual correctness.

## Runtime boundaries

- **Desktop/API:** a native host and local backend/renderer, not a hosted web product by default.
- **Optional services:** root `wise_supervisor.py` can coordinate Agent OS, Leon HTTP and Leon audio. The primary desktop path need not start all three.
- **Separate ecosystems:** Leon retains its own source, SDKs, manifests and profile secrets—not a stale duplicate WISE interface.
- **Heavy environments:** tensor/audio dependencies and security scanners are separately provisioned.
- **External connections:** models, MCP, web sources and messaging cross trust/data boundaries.

Worker isolation helps dependency/lifecycle management; it is not complete OS sandboxing. Native interaction, real accounts, hardware capacity and complete egress policy need acceptance independent of mocks and source tests.

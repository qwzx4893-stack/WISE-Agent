# Installation and dependencies

## Core desktop

Use Python 3.11 on Windows 10/11, Git and Microsoft WebView2 Runtime. Run `tools/Install-WISE.ps1`, then launch using the **explicit virtual-environment Python** in the README. `Start-WISE.ps1` chooses Python from PATH; activate the environment first if using it.

`Supergent--main/requirements.txt` is the base manifest. Its version ranges are not a fully locked cross-platform dependency set. A successful install is not clean-machine/native acceptance. No `node_modules` or Python environment is shipped. Configure provider credentials and models locally. Install Brave for installed-browser automation; never publish browser storage.

## Dependency matrix

| Component | Provisioning | Core text chat? |
| --- | --- | --- |
| Agent OS / native host | `Supergent--main/requirements.txt`; external WebView2 | Required |
| WISE renderer | Static `Supergent--main/ui/wise_web`, served by API | Required |
| Leon services | `leon-develop/package.json`, `pnpm-lock.yaml`, source-local Python manifests/locks and setup scripts | Optional |
| Intelligence extras | `Supergent--main/requirements-intelligence.txt` | Optional |
| Semantic skill search | `Supergent--main/requirements-semantic-search.txt` | Optional |
| Advanced compression | `Supergent--main/requirements-optimization.txt`; tensor/model downloads | Optional |
| Voice STT | `Supergent--main/tools/voice_runtime_requirements.txt`, `.voice-venv` | Optional |
| Voice TTS | Pinned IndexTTS source, `uv.lock`, weights and permitted custom assets | Optional |
| Security executables | Isolated recipes in `core/security/optional_tools.py` | Optional |
| MCP | Server-specific dependencies/transport/authentication | Optional |
| Plugins / messaging | Nango, provider OAuth applications / channel credentials | Optional |
| QA | `Supergent--main/qa/tooling/` manifests; native assemblies separately | Optional |

## Leon workspace integration

Leon is retained source, not a second stale WISE interface. Use Node.js **24+** and **pnpm** (manifest requests pnpm 11.1.1). Follow its README/setup requirements, run `pnpm install --frozen-lockfile` inside `leon-develop`, then `pnpm run build:server`. Its install lifecycle provisions managed dependencies and can download substantial resources; review setup before opting in. Source-local bridge/tool Python manifests and locks are retained.

Run root `wise_supervisor.py` with the Agent OS Python after **all three services' prerequisites** are installed. It manages ports 8765 (Agent OS), 5366 (Leon HTTP), 5367 (Leon audio). Source alone does not supply audio models or managed binaries. Leon account/profile secrets remain separate; root `.env.example` documents service settings.

## Voice

The runtime expects the official IndexTTS checkout at `Supergent--main/tools/index-tts`. Install it separately, preserving upstream licensing:

```powershell
git clone https://github.com/index-tts/index-tts.git Supergent--main/tools/index-tts
git -C Supergent--main/tools/index-tts checkout ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c
```

Install `uv`, review that checkout's code/model licenses, then run `Supergent--main/tools/bootstrap_voice.ps1` with Python 3.11. This reproduces the upstream **locked environment**, not weights/custom assets. Follow the pinned upstream README for compatible IndexTTS-2.5 weights. Default weights directory:

`Supergent--main/tools/index-tts/assets/voice/models/index-tts-2.5`

Private approved inputs are **not published**:

- `Supergent--main/assets/voice/models/wise-indextts-2.5-gpt.pth`
- `Supergent--main/assets/voice/wise_indextts_reference_canonical.wav`

Only provision assets you have permission to use. Absence means unavailable voice, not a fictitious installed voice. Configure your own compatible paths in voice settings. See [GPU policy](../Supergent--main/docs/VOICE_RUNTIME.md). Bootstrap requires the upstream checkout and does not fetch it automatically.

## Optional accounts, services and models

See [Plugins / Nango](../Supergent--main/docs/PLUGINS_NANGO.md); supply `NANGO_SECRET_KEY` only to the backend. Environment examples do not imply all launchers automatically load dotenv files—export variables or use supported settings.

Local LLM weights, app registration secrets, MCP commands and large intelligence platforms need separate provisioning. Included adapters/recipes are not external-service access grants. Failures must remain visible rather than fictitious successful connections.

# WISE architecture

The primary runtime is the Agent OS in `Supergent--main`. Its API hosts the current WISE interface and conversation, settings, state and integration operations. The native Windows launcher supervises this backend and owns the desktop window lifecycle.

```text
Native desktop / WISE renderer
        │ local API and streaming turns
Conversation → cognition / task contracts → capability / usage discovery
        │                                      │
        ├─ persistent sessions and memory       ├─ bounded team execution
        ├─ instructions and schedules           ├─ tools / skills / MCP
        └─ checkpoints / recovery / diagnostics └─ sources / files / browser / OS
                           │
                 model / worker resource admission
```

The root supervisor additionally orchestrates the Agent OS API, Leon HTTP service and Leon audio service. Leon source and bridge SDKs remain separate components with separate profile secrets and package ecosystems. The primary desktop path need not start these additional services.

Optional environments isolate tensor/audio dependencies and on-demand security scanners from the text runtime. Worker isolation is a dependency/lifecycle boundary, not complete OS sandboxing. Catalog discovery, installed executables, authenticated services and proven task results are distinct states. Native interaction, real accounts and hardware acceptance must be assessed independently of mocks or renderer tests.

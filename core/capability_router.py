# ==============================================================================
# WISE Canonical Capability Registry & Router
# Consolidates all WISE capabilities into one queryable, executable, and
# security-gated registry:
# - Native tools (Python built-in)
# - MCP tools (dynamic via MCPRegistry)
# - SkillNet (procedural skills)
# - RAG (deep research & web knowledge)
# - Memory (session facts & semantic memory)
# - Browser actions (Playwright automation)
# - Computer actions (WISEHands OS automation)
# ==============================================================================

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

from core.contracts import (
    CapabilityMetadata,
    CapabilitySource,
    ToolCall,
    ToolResult,
)
from core.security import get_security_gate, SecurityContext, ActionTier

LOG = logging.getLogger("wise.capability_router")


@dataclass
class CapabilityDescriptor:
    id: str
    name: str
    source: CapabilitySource
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[Dict[str, Any]] = None
    availability: bool = True
    risk_level: str = "LOW"  # LOW | MEDIUM | HIGH | CRITICAL
    runtime_status: str = "READY"  # READY | CONNECTED | UNAVAILABLE | ERROR | STOPPED
    execution_adapter: Optional[Callable[[Dict[str, Any]], Any]] = None
    missing_reason: Optional[str] = None
    category: str = ""
    contract_version: int = 1

    def to_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            id=self.id,
            source=self.source,
            name=self.name,
            description=self.description,
            parameters_schema=self.input_schema,
            risk_level=self.risk_level,
            available=self.availability,
            missing_dependency=self.missing_reason,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source.value if hasattr(self.source, "value") else str(self.source),
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "availability": self.availability,
            "risk_level": self.risk_level,
            "runtime_status": self.runtime_status,
            "missing_reason": self.missing_reason,
            "category": self.category,
            "contract_version": self.contract_version,
        }


class CapabilityRouter:
    """
    Authoritative Capability Registry & Router for WISE.
    Prevents fragmentation by providing one coherent interface for capability discovery
    and security-governed execution across all subsystems.
    """

    def __init__(self) -> None:
        self._capabilities: Dict[str, CapabilityDescriptor] = {}
        self._load_native_capabilities()
        from core.intelligence.bindings import register_intelligence
        register_intelligence(self)
        self._load_rag_capabilities()
        self._load_memory_capabilities()
        self._load_skillnet_capabilities()
        self._load_computer_use_capabilities()
        self.refresh_mcp_capabilities()

    # --------------------------------------------------------------------------
    # Subsystem loaders
    # --------------------------------------------------------------------------
    def _load_native_capabilities(self) -> None:
        """Loads native Python built-in tools from core.tools_bridge."""
        try:
            from core.tools_bridge import _DISPATCH, call_python_builtin_strict
            descriptions = {
                "read_file": ("Read UTF-8 content from a workspace file path.", {"path": {"type": "string", "description": "Relative file path"}}, "LOW"),
                "write_file": ("Write text content to a workspace file.", {"path": {"type": "string"}, "content": {"type": "string"}}, "MEDIUM"),
                "list_directory": ("List filenames in a workspace directory.", {"path": {"type": "string", "default": "."}}, "LOW"),
                "grep_file": ("Search for regular expression pattern inside a file.", {"path": {"type": "string"}, "pattern": {"type": "string"}}, "LOW"),
                "execute_python": ("Execute short Python code snippet inside workspace sandbox.", {"code": {"type": "string"}}, "HIGH"),
                "git_clone": ("Clone a git repository into the workspace.", {"repo": {"type": "string"}, "dest": {"type": "string"}}, "MEDIUM"),
                "web_fetch": ("Fetch URL content via HTTP GET.", {"url": {"type": "string"}}, "LOW"),
                "web_search": ("Search knowledge sources (Wikipedia/web) for a query.", {"query": {"type": "string"}}, "LOW"),
                "run_shell": ("Run shell command inside workspace boundary.", {"command": {"type": "string"}}, "HIGH"),
                "leon_speak": ("Speak message via Leon audio TTS.", {"text": {"type": "string"}}, "LOW"),
                "leon_send_ui": ("Send message card to Leon UI.", {"message": {"type": "string"}}, "LOW"),
                "security_scan": ("Run an offline local defensive scan inside the workspace. Returns locations only, never secret values. At most 100 files of 256KB; no network scanning.", {"scanner":{"type":"string","enum":["detect-secrets","bandit"]},"path":{"type":"string","default":"."}}, "LOW"),
            }
            for tool_name in _DISPATCH:
                if tool_name in {"browse_skills", "browse_tools", "browse_resources"}:
                    descriptions[tool_name] = (
                        "Browse grouped " + tool_name[7:] + ". Small metadata pages only; references are not installed executable tools.",
                        {"query":{"type":"string"}, "category":{"type":"string"}, "offset":{"type":"integer"}, "limit":{"type":"integer"}}, "LOW")
                desc, schema, risk = descriptions.get(
                    tool_name,
                    (f"Native tool {tool_name}", {}, "MEDIUM")
                )
                self.register(
                    CapabilityDescriptor(
                        id=f"native.{tool_name}",
                        name=tool_name,
                        source=CapabilitySource.NATIVE,
                        description=desc,
                        input_schema=schema,
                        risk_level=risk,
                        availability=True,
                        runtime_status="READY",
                        execution_adapter=lambda params, tn=tool_name: call_python_builtin_strict(tn, params),
                    )
                )
        except Exception as exc:
            LOG.warning("Failed to load native capabilities: %s", exc)

    def _load_rag_capabilities(self) -> None:
        """Loads deep research & knowledge retrieval capabilities."""
        def _rag_adapter(params: Dict[str, Any]) -> Any:
            from core.brain.cognitive_decision_engine import get_cognitive_decision_engine
            cde = get_cognitive_decision_engine()
            return cde.research_and_synthesize(params.get("query", ""))

        self.register(
            CapabilityDescriptor(
                id="rag.deep_research",
                name="deep_research",
                source=CapabilitySource.RAG,
                description="Perform multi-source web and knowledge retrieval, evidence extraction, and synthesis.",
                input_schema={"query": {"type": "string", "description": "Research inquiry"}},
                risk_level="LOW",
                availability=True,
                runtime_status="READY",
                execution_adapter=_rag_adapter,
            )
        )

    def _load_memory_capabilities(self) -> None:
        """Loads semantic and session memory capabilities."""
        def _memory_search_adapter(params: Dict[str, Any]) -> Any:
            from core.memory_service import get_memory_service
            ms = get_memory_service()
            return ms.search(params.get("query", ""), limit=int(params.get("limit", 5)))

        def _memory_store_adapter(params: Dict[str, Any]) -> Any:
            from core.memory_service import get_memory_service
            ms = get_memory_service()
            ms.add(params.get("key", "fact"), params.get("value", ""), metadata=params.get("metadata", {}))
            return "STORED"

        self.register(
            CapabilityDescriptor(
                id="memory.search",
                name="search_memory",
                source=CapabilitySource.MEMORY,
                description="Search persistent agent semantic and episodic memory.",
                input_schema={"query": {"type": "string"}},
                risk_level="LOW",
                availability=True,
                runtime_status="READY",
                execution_adapter=_memory_search_adapter,
            )
        )
        self.register(
            CapabilityDescriptor(
                id="memory.store",
                name="store_memory",
                source=CapabilitySource.MEMORY,
                description="Persist valuable user facts or session context to long-term memory.",
                input_schema={"key": {"type": "string"}, "value": {"type": "string"}},
                risk_level="LOW",
                availability=True,
                runtime_status="READY",
                execution_adapter=_memory_store_adapter,
            )
        )

    def _load_skillnet_capabilities(self) -> None:
        """Loads procedural skill discovery and execution capabilities."""
        def _skill_search_adapter(params: Dict[str, Any]) -> Any:
            from core.skills.search import SkillSearch
            s_search = SkillSearch()
            results = s_search.search(params.get("intent", ""), top_k=int(params.get("top_k", 5)))
            return [{"name": r[0], "score": round(r[1], 4), "description": r[2]} for r in results]

        self.register(
            CapabilityDescriptor(
                id="skillnet.search",
                name="search_skills",
                source=CapabilitySource.SKILLNET,
                description="Search 1,700+ indexed procedural skill procedures and domain workflows.",
                input_schema={"intent": {"type": "string"}},
                risk_level="LOW",
                availability=True,
                runtime_status="READY",
                execution_adapter=_skill_search_adapter,
            )
        )

    def _load_computer_use_capabilities(self) -> None:
        """Loads OS computer automation capabilities via WISEHands."""
        def _computer_action_adapter(params: Dict[str, Any]) -> Any:
            from core.hands.computer_use import get_computer_use
            cu = get_computer_use()
            action = params.get("action", "")
            return cu.execute_action(action, params)

        self.register(
            CapabilityDescriptor(
                id="computer.execute_action",
                name="computer_action",
                source=CapabilitySource.COMPUTER_USE,
                description="Execute closed-loop OS automation action (open_app, type_text, click, hotkey).",
                input_schema={"action": {"type": "string"}, "params": {"type": "object"}},
                risk_level="MEDIUM",
                availability=True,
                runtime_status="READY",
                execution_adapter=_computer_action_adapter,
            )
        )

    # --------------------------------------------------------------------------
    # Dynamic MCP capabilities
    # --------------------------------------------------------------------------
    def refresh_mcp_capabilities(self) -> None:
        """Queries MCPRegistry alive clients and registers their tools dynamically."""
        try:
            from core.mcp import get_registry
            reg = get_registry()
            # Disconnected/removed servers must disappear from the live manifest.
            self._capabilities = {key: cap for key, cap in self._capabilities.items()
                                  if not key.startswith("mcp.")}
            from core.mcp.registry import build_mcp_tools
            runners = build_mcp_tools(reg, auto_start=False)
            # Discovery must be read-only. Starting every configured MCP while
            # merely building a prompt can launch a subprocess or create an
            # unexpected network connection. Explicit MCP lifecycle controls
            # own startup; the network ranks connected servers only.

            for srv_name, client in reg.alive_clients().items():
                for tool in (client.tools or []):
                    tname = (tool or {}).get("name")
                    if not tname:
                        continue
                    cap_id = f"mcp.{srv_name}.{tname}"
                    tdesc = (tool or {}).get("description", f"MCP tool {tname} from {srv_name}")
                    tschema = (tool or {}).get("inputSchema", {})
                    cls = reg.classification_of(srv_name, tname)
                    risk_map = {"read": "LOW", "write": "MEDIUM", "dangerous": "CRITICAL"}
                    risk = risk_map.get(cls.kind if cls else "read", "LOW")

                    import re
                    runner = runners.get(f"mcp_{srv_name}_{re.sub(r'[^A-Za-z0-9_]', '_', tname)}")
                    def _mcp_adapter(params: Dict[str, Any], fn=runner) -> Any:
                        # A model may not supply human approval as a tool argument.
                        if "confirm" in params or "_wait" in params:
                            raise PermissionError("MCP approval must come from the user interface")
                        if fn is None:
                            raise RuntimeError("MCP tool is no longer connected")
                        return fn(**params)

                    self.register(
                        CapabilityDescriptor(
                            id=cap_id,
                            name=tname,
                            source=CapabilitySource.MCP,
                            description=tdesc,
                            input_schema=tschema,
                            risk_level=risk,
                            availability=True,
                            runtime_status="CONNECTED",
                            execution_adapter=_mcp_adapter,
                        )
                    )
        except Exception as exc:
            LOG.warning("Failed to refresh MCP capabilities: %s", exc)

    # --------------------------------------------------------------------------
    # Registration and Lookup
    # --------------------------------------------------------------------------
    def register(self, desc: CapabilityDescriptor) -> None:
        self._capabilities[desc.id] = desc

    def register_extension(self, desc: CapabilityDescriptor, *, trusted: bool = False) -> None:
        """Explicit trusted adapter contract; no automatic arbitrary imports."""
        import re
        if not trusted or not re.fullmatch(r"extension\.[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*", desc.id):
            raise ValueError("Extensions require explicit trust and a namespaced identifier")
        if desc.id in self._capabilities:
            raise ValueError("Extension cannot overwrite an existing capability")
        if desc.contract_version != 1 or not callable(desc.execution_adapter):
            raise ValueError("Unsupported extension contract or missing adapter")
        if desc.risk_level not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} or not desc.category:
            raise ValueError("Extension must declare risk and category")
        if not isinstance(desc.input_schema, dict):
            raise ValueError("Extension must declare an input schema")
        tiers = {"LOW":ActionTier.READ, "MEDIUM":ActionTier.MODERATE,
            "HIGH":ActionTier.ADMINISTRATIVE, "CRITICAL":ActionTier.DESTRUCTIVE}
        get_security_gate().register_action_tier(desc.id, tiers[desc.risk_level])
        self.register(desc)

    def get_capability(self, id_or_name: str) -> Optional[CapabilityDescriptor]:
        # Direct ID match
        if id_or_name in self._capabilities:
            return self._capabilities[id_or_name]
        # Match by name
        for desc in self._capabilities.values():
            if desc.name == id_or_name:
                return desc
        # Dynamic MCP lookup
        self.refresh_mcp_capabilities()
        if id_or_name in self._capabilities:
            return self._capabilities[id_or_name]
        for desc in self._capabilities.values():
            if desc.name == id_or_name:
                return desc
        return None

    def list_capabilities(
        self,
        source: Optional[Union[CapabilitySource, str]] = None,
        runtime_status: Optional[str] = None,
    ) -> List[CapabilityDescriptor]:
        self.refresh_mcp_capabilities()
        out = []
        for desc in self._capabilities.values():
            if source is not None:
                src_val = source.value if hasattr(source, "value") else str(source)
                desc_src_val = desc.source.value if hasattr(desc.source, "value") else str(desc.source)
                if src_val.upper() != desc_src_val.upper():
                    continue
            if runtime_status is not None and desc.runtime_status.upper() != runtime_status.upper():
                continue
            out.append(desc)
        return out

    # --------------------------------------------------------------------------
    # Central Execution with WindowsSecurityGate Governance
    # --------------------------------------------------------------------------
    def execute(
        self,
        id_or_name: str,
        params: Dict[str, Any],
        session_id: Optional[str] = None,
        confirmed: bool = False,
        confirmation_token: Optional[str] = None,
        untrusted_content: bool = False,
        project_write_scope: tuple = (),
    ) -> ToolResult:
        """
        Executes a capability through the central WindowsSecurityGate boundary.
        Enforces:
        1. Action authorization evaluation
        2. Anti-TOCTOU confirmation verification
        3. Execution adapter dispatch
        4. Structured ToolResult return
        """
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        cap = self.get_capability(id_or_name)
        if not cap:
            return ToolResult(
                call_id=call_id,
                tool_id=id_or_name,
                success=False,
                output=None,
                error=f"Capability '{id_or_name}' not found in registry.",
            )
        if not cap.availability:
            return ToolResult(call_id=call_id,tool_id=cap.id,success=False,output=None,
                error=cap.missing_reason or "Capability is unavailable")

        # 1. Evaluate with WindowsSecurityGate
        gate = get_security_gate()
        ctx = SecurityContext(
            caller=f"capability_router:{cap.id}",
            session_id=session_id or "default",
            confirmed=confirmed,
            confirmation_token=confirmation_token,
            is_untrusted_content=untrusted_content,
            project_write_scope=project_write_scope,
        )
        sec_eval = gate.evaluate_action(
            action_name=cap.id if cap.id.startswith("extension.") else cap.name,
            params=params,
            context=ctx,
        )

        if not sec_eval.allowed:
            LOG.warning("Capability '%s' blocked by SecurityGate: %s", cap.id, sec_eval.reason)
            return ToolResult(
                call_id=call_id,
                tool_id=cap.id,
                success=False,
                output=None,
                error=f"SecurityGate blocked: {sec_eval.reason}",
                metadata={"security_evaluation": sec_eval.to_dict()},
            )

        # 2. Execute via adapter
        t0 = time.perf_counter()
        if not cap.execution_adapter:
            return ToolResult(
                call_id=call_id,
                tool_id=cap.id,
                success=False,
                output=None,
                error=f"Capability '{cap.id}' has no execution adapter configured.",
            )

        try:
            from core.tools_bridge import builtin_security_context
            with builtin_security_context(ctx, cap.name, params):
                output = cap.execution_adapter(params)
            duration_ms = (time.perf_counter() - t0) * 1000
            failed_output = isinstance(output, dict) and (
                output.get("success") is False or output.get("isError") is True
                or (bool(output.get("error")) and output.get("success") is not True)
            )
            return ToolResult(
                call_id=call_id,
                tool_id=cap.id,
                success=not failed_output,
                output=output,
                error=str(output.get("error") or "Tool reported failure") if failed_output else None,
                duration_ms=duration_ms,
                metadata={"source": cap.source.value if hasattr(cap.source, "value") else str(cap.source)},
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - t0) * 1000
            LOG.error("Execution failed for capability '%s': %s", cap.id, exc)
            return ToolResult(
                call_id=call_id,
                tool_id=cap.id,
                success=False,
                output=None,
                error=str(exc),
                duration_ms=duration_ms,
            )


# Singleton instance
_ROUTER_INSTANCE: Optional[CapabilityRouter] = None
_ROUTER_LOCK = __import__("threading").Lock()


def get_capability_router() -> CapabilityRouter:
    global _ROUTER_INSTANCE
    if _ROUTER_INSTANCE is None:
        with _ROUTER_LOCK:
            if _ROUTER_INSTANCE is None:
                _ROUTER_INSTANCE = CapabilityRouter()
    return _ROUTER_INSTANCE


__all__ = [
    "CapabilityDescriptor",
    "CapabilityRouter",
    "get_capability_router",
]

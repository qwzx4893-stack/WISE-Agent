import asyncio
import os
from typing import List
from .manifest import ToolManifest
from .opa_client import OPAClient
from .observability import trace


_DANGEROUS_TOKENS = ["rm -rf /", "format", "shutdown", "mkfs", "dd if=", "> /dev/", "curl.*|.*sh"]


def _allowed_categories() -> set[str]:
    raw = os.environ.get("AGENT_ALLOWED_CATEGORIES", "").strip()
    if raw:
        return {c.strip() for c in raw.split(",") if c.strip()}
    return {"Execution", "Memory", "Orchestration", "Planning", "Code",
            "Knowledge", "Networking"}


_ALLOWED_CATEGORIES = _allowed_categories()


def _opa_disabled() -> bool:
    return os.environ.get("AGENT_OPA_DISABLED", "").lower() in {"1", "true", "yes"}


class PolicyEngine:
    def __init__(self, opa_client: OPAClient = None):
        self.opa = opa_client or OPAClient()
        self.disabled_tools: List[str] = []

    def disable_tool(self, name: str):
        if name not in self.disabled_tools:
            self.disabled_tools.append(name)

    def enable_tool(self, name: str):
        if name in self.disabled_tools:
            self.disabled_tools.remove(name)

    def _local_allow(self, manifest: ToolManifest, task_context: str = "") -> bool:
        if manifest.name in self.disabled_tools:
            return False
        if manifest.category.value not in _ALLOWED_CATEGORIES:
            return False
        if any(d in task_context.lower() for d in _DANGEROUS_TOKENS):
            return False
        return True

    def allow(self, manifest: ToolManifest, task_context: str = "") -> bool:
        """Synchronous policy check; uses local rules when OPA unreachable."""
        if manifest.name in self.disabled_tools:
            return False
        return self._local_allow(manifest, task_context)

    @trace
    async def allow_async(self, manifest: ToolManifest, task_context: str = "") -> bool:
        """Async path that consults OPA when available, falls back to local rules."""
        if manifest.name in self.disabled_tools:
            return False

        if _opa_disabled():
            return self._local_allow(manifest, task_context)
        input_data = {
            "name": manifest.name,
            "category": manifest.category.value,
            "task": task_context,
            "disabled_tools": self.disabled_tools,
        }
        try:
            return await self.opa.allow(input_data)
        except Exception:
            return self._local_allow(manifest, task_context)

    def check_task(self, task: str) -> bool:
        """Synchronous task safety check."""
        return not any(d in task.lower() for d in _DANGEROUS_TOKENS)

    @trace
    async def check_task_async(self, task: str) -> bool:
        if _opa_disabled():
            return self.check_task(task)
        input_data = {"task": task}
        try:
            return not await self.opa.is_dangerous_task(input_data)
        except Exception:
            return self.check_task(task)

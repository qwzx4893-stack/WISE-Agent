"""Tool Intelligence layer: discovery, policy, sandboxed execution."""
import atexit

from .manifest import ToolManifest, ToolCategory, ToolSecurity
from .registry import ToolRegistry
from .executor import ToolExecutor, ToolImplementations
from .policy import PolicyEngine
from .loader import ToolLoader
from .sandbox import SandboxManager
from .opa_client import OPAClient
from .observability import setup_logging, get_logger
from .seccomp import SeccompManager

setup_logging()
atexit.register(SeccompManager.cleanup)


class ToolIntelligence:
    def __init__(self, tools_dir: str = None):
        self.registry = ToolRegistry(tools_dir)
        self.policy = PolicyEngine(OPAClient())
        try:
            self.sandbox = SandboxManager()
        except Exception:
            self.sandbox = None
        self.loader = ToolLoader()
        self.logger = get_logger()
        try:
            from core.workspace import Workspace
            from core.memory import Memory
            self.workspace = Workspace
            self.memory = Memory()
        except Exception:
            self.workspace = None
            self.memory = None
        try:
            self.logger.info(
                "ToolIntelligence initialized",
                tools_loaded=len(self.registry.list_all()),
            )
        except Exception:
            pass

    def create_executor(self, task_context: str = "") -> ToolExecutor:
        return ToolExecutor(
            registry=self.registry,
            policy=self.policy,
            sandbox=self.sandbox,
            workspace=self.workspace,
            memory=self.memory,
            task_context=task_context,
        )

    def register_tool(self, manifest: ToolManifest):
        self.registry.register(manifest)

    def execute(self, tool_name: str, args: dict, task_context: str = "") -> str:
        executor = self.create_executor(task_context)
        return executor.execute(tool_name, args)


__all__ = [
    "ToolManifest", "ToolCategory", "ToolSecurity",
    "ToolRegistry", "ToolExecutor", "ToolImplementations",
    "PolicyEngine", "ToolLoader", "SandboxManager", "OPAClient",
    "setup_logging", "get_logger", "SeccompManager", "ToolIntelligence",
]

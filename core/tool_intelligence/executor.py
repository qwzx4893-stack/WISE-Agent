import os
import shlex
import subprocess
import shutil
from typing import Dict, Any, Optional, Callable
from .manifest import ToolManifest
from .registry import ToolRegistry
from .policy import PolicyEngine
from .sandbox import SandboxManager

class ToolImplementations:
    _impls: Dict[str, Callable] = {}
    @classmethod
    def register(cls, name: str, func: Callable): cls._impls[name] = func
    @classmethod
    def get(cls, name: str) -> Optional[Callable]: return cls._impls.get(name)

class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine,
                 sandbox: SandboxManager = None, workspace=None, memory=None,
                 task_context: str = ""):
        from ..paths import WORKSPACE_DIR
        self.registry = registry
        self.policy = policy
        self.sandbox = sandbox or SandboxManager()
        self.workspace_root = str(WORKSPACE_DIR)
        os.makedirs(self.workspace_root, exist_ok=True)
        self.task_context = task_context
        if workspace is not None or memory is not None:
            self.workspace = workspace
            self.memory = memory
        else:
            try:
                from core.workspace import Workspace
                from core.memory import Memory
                self.workspace = Workspace
                self.memory = Memory()
            except Exception:
                self.workspace = None
                self.memory = None

    def execute(self, tool_name: str, args: Dict[str, Any]) -> str:
        manifest = self.registry.get(tool_name)
        if not manifest:
            return f"❌ Unknown tool: {tool_name}"
        if not self.policy.allow(manifest, self.task_context):
            return f"❌ Tool blocked by policy: {tool_name}"
        
        required = manifest.input_schema.get("required", [])
        for field in required:
            if field not in args:
                return f"❌ Missing required field: {field}"

        if manifest.dependencies:
            for dep in manifest.dependencies:
                if not self._check_dependency(dep):
                    return f"❌ Missing dependency: {dep}"

        if manifest.implementation_type == "cli":
            return self._execute_cli(manifest, args)
        else:
            impl = ToolImplementations.get(tool_name)
            if not impl:
                return f"❌ No implementation for: {tool_name}"
            return impl(self, args)

    def _check_dependency(self, dep: str) -> bool:
        return shutil.which(dep) is not None

    def _execute_cli(self, manifest: ToolManifest, args: Dict[str, Any]) -> str:
        binary = manifest.cli_binary or shutil.which(manifest.cli_command.split()[0] if manifest.cli_command else "")
        if not binary:
            return f"❌ CLI binary not found: {manifest.cli_command}"

        cmd_parts = [binary]
        used_keys: set[str] = set()
        if manifest.cli_command:
            template_parts = manifest.cli_command.split()[1:]
            for part in template_parts:
                for key, value in args.items():
                    placeholder = f"{{{key}}}"
                    if placeholder in part:
                        part = part.replace(placeholder, shlex.quote(str(value)))
                        used_keys.add(key)
                if "{" not in part:
                    cmd_parts.append(part)
        # Forward any remaining args as `--key value` pairs so manifests
        # without a template still surface user-provided arguments.
        for key, value in args.items():
            if key in used_keys:
                continue
            cmd_parts.append(f"--{key}")
            cmd_parts.append(shlex.quote(str(value)))

        if manifest.security.requires_sandbox:
            return self.sandbox.run(
                cmd_parts,
                self.workspace_root,
                manifest.security.requires_network,
                manifest.timeout
            )
        else:
            try:
                result = subprocess.run(
                    cmd_parts,
                    capture_output=True, text=True,
                    timeout=manifest.timeout,
                    cwd=self.workspace_root
                )
                return result.stdout + result.stderr
            except subprocess.TimeoutExpired:
                return "TIMEOUT"
            except Exception as e:
                return str(e)

# Register core tools that use v24 modules
def _read_file(executor: ToolExecutor, args: dict) -> str:
    try:
        if executor.workspace:
            path = executor.workspace.resolve(args["path"])
            return path.read_text()
        else:
            with open(os.path.join(executor.workspace_root, args["path"]), 'r') as f:
                return f.read()
    except Exception as e:
        return str(e)

def _write_file(executor: ToolExecutor, args: dict) -> str:
    try:
        if executor.workspace:
            path = executor.workspace.resolve(args["path"])
            path.write_text(args["content"])
        else:
            with open(os.path.join(executor.workspace_root, args["path"]), 'w') as f:
                f.write(args["content"])
        return "OK"
    except Exception as e:
        return str(e)

def _list_directory(executor: ToolExecutor, args: dict) -> str:
    try:
        target = os.path.join(executor.workspace_root, args["path"])
        return "\n".join(os.listdir(target))
    except Exception as e:
        return str(e)

def _search_memory(executor: ToolExecutor, args: dict) -> str:
    if executor.memory:
        return "\n".join(executor.memory.search(args["query"]))
    return "Memory module not available"

def _remember(executor: ToolExecutor, args: dict) -> str:
    if executor.memory:
        executor.memory.add(args["key"], args["value"])
        return "REMEMBERED"
    return "Memory module not available"

ToolImplementations.register("read_file", _read_file)
ToolImplementations.register("write_file", _write_file)
ToolImplementations.register("list_directory", _list_directory)
ToolImplementations.register("search_memory", _search_memory)
ToolImplementations.register("remember", _remember)

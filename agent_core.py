#!/usr/bin/env python3
"""
Agent OS Core v13.0 - AGENT OS CORE
- Model Abstraction Layer (يدعم GGUF)
- Terminal Mode (وضع طرفية حقيقي)
- Tool Registry (سجل أدوات مركزي)
- Memory Layer (ذاكرة بسيطة)
- Runtime Context (سياق التشغيل)
- Sandbox Abstraction (طبقة عزل مجردة)
"""
import os
import re
import shlex
import signal
import subprocess
import sys
import json
import threading
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Tuple, Dict, Optional, Any, List
from abc import ABC, abstractmethod

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

try:
    from smolagents import tool
except ImportError:  # smolagents is optional; fall back to no-op decorator.
    def tool(fn):
        return fn

from core.agent_loop import react_loop
from core.api_models import TeamModel  # legacy fallback
from core.llm import KeyStore, build_router_from_keystore
from core.paths import (
    AGENT_OS_ROOT,
    CONFIG_DIR,
    LOGS_DIR,
    MEMORY_DIR as _PATHS_MEMORY_DIR,
    SANDBOX_ROOT as _PATHS_SANDBOX_ROOT,
    SESSIONS_DIR,
    TOOLS_DIR as _PATHS_TOOLS_DIR,
    ensure_runtime_dirs,
)
from core.system_awareness import SystemAwareness

ensure_runtime_dirs()

# ========== التكوين ==========
LOG_FILE = str(LOGS_DIR / "agent.log")
SESSION_FILE = str(SESSIONS_DIR / "default.json")
SANDBOX_ROOT = str(_PATHS_SANDBOX_ROOT)
PROOT_BIN = os.environ.get("PROOT_BIN", "/usr/bin/proot")
MEMORY_DIR = str(_PATHS_MEMORY_DIR)
TOOLS_DIR = str(_PATHS_TOOLS_DIR)

# حدود الأمان (قابلة للضبط عبر متغيرات البيئة)
MAX_COMMAND_LENGTH = int(os.environ.get("AGENT_MAX_CMD_LEN", "2048"))
MAX_OUTPUT_LENGTH = int(os.environ.get("AGENT_MAX_OUTPUT_LEN", "20000"))

_LOG_LOCK = threading.Lock()

# ========== 1. طبقة تجريد النموذج (Model Abstraction) ==========
class ModelInterface(ABC):
    """واجهة مجردة للنموذج - تدعم Ollama حالياً ويمكن توسيعها لـ llama.cpp"""
    @abstractmethod
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        pass

# ========== 2. طبقة العزل المجردة (Sandbox Abstraction) ==========
class SandboxRuntime(ABC):
    """طبقة عزل مجردة - تدعم proot حالياً ويمكن توسيعها لـ chroot أو Docker"""
    @abstractmethod
    def run(self, cmd_list: List[str], cwd: str, env: Dict[str, str], timeout: int = 120) -> Tuple[str, str, int]:
        pass

class ProotSandbox(SandboxRuntime):
    """تنفيذ proot كطبقة عزل (مع تراجع آمن إلى subprocess إن لم يتوفر proot)."""

    def __init__(self, rootfs: str, proot_bin: str = "/usr/bin/proot"):
        self.rootfs = rootfs
        self.proot_bin = proot_bin
        self.use_proot = (
            os.path.exists(proot_bin) and os.path.isdir(rootfs)
        )

    def run(self, cmd_list: List[str], cwd: str, env: Dict[str, str], timeout: int = 120) -> Tuple[str, str, int]:
        if self.use_proot:
            safe_cwd = os.path.normpath(cwd).lstrip("/")
            sandbox_cwd = os.path.join(self.rootfs, safe_cwd)
            proot_cmd = [self.proot_bin, "-r", self.rootfs, "-w", sandbox_cwd] + cmd_list
            run_cmd = proot_cmd
        else:
            run_cmd = cmd_list

        process_env = os.environ.copy()
        process_env.update(env)

        cpu_s = int(os.environ.get("AGENT_CPU_LIMIT_S", "60"))
        mem_mb = int(os.environ.get("AGENT_MEM_LIMIT_MB", "2048"))
        max_procs = int(os.environ.get("AGENT_MAX_PROCS", "256"))

        def _preexec():
            try:
                os.setsid()
            except OSError:
                pass
            try:
                import resource as _r
                # No core dumps; constrained hosts cannot afford them.
                try:
                    _r.setrlimit(_r.RLIMIT_CORE, (0, 0))
                except (ValueError, OSError):
                    pass
                if cpu_s > 0:
                    _r.setrlimit(_r.RLIMIT_CPU, (cpu_s, cpu_s + 5))
                if mem_mb > 0:
                    bytes_ = mem_mb * 1024 * 1024
                    try:
                        _r.setrlimit(_r.RLIMIT_AS, (bytes_, bytes_))
                    except (ValueError, OSError):
                        pass  # not supported on this platform
                if max_procs > 0:
                    try:
                        _r.setrlimit(_r.RLIMIT_NPROC, (max_procs, max_procs))
                    except (ValueError, OSError):
                        pass
            except Exception:
                pass

        if os.name == "nt" and run_cmd:
            import shutil
            if not shutil.which(run_cmd[0]) and run_cmd[0].lower() != "cmd.exe":
                run_cmd = ["cmd.exe", "/c", *run_cmd]

        try:
            process = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=process_env,
                cwd=cwd if not self.use_proot else None,
                preexec_fn=_preexec if os.name != "nt" else None,
            )
            use_setsid = (os.name != "nt")
        except (AttributeError, OSError, ValueError):
            process = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=process_env,
                cwd=cwd if not self.use_proot else None,
            )
            use_setsid = False

        def kill_process(p):
            try:
                if use_setsid:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                else:
                    p.kill()
            except Exception:
                pass

        timer = threading.Timer(timeout, kill_process, [process])
        timer.start()

        try:
            stdout, stderr = process.communicate()
            return stdout, stderr, process.returncode
        except Exception as e:
            return "", str(e), -1
        finally:
            timer.cancel()

# إعداد الساندبوكس الافتراضي
sandbox = ProotSandbox(SANDBOX_ROOT, PROOT_BIN)

# ========== 3. سجل الأدوات (Tool Registry) ==========
class ToolRegistry:
    """سجل أدوات مركزي - يحول النظام إلى Agent OS حقيقي"""
    def __init__(self):
        self.tools: Dict[str, Any] = {}

    def register(self, name: str, fn: Any):
        self.tools[name] = fn

    def get(self, name: str) -> Optional[Any]:
        return self.tools.get(name)

    def list_tools(self) -> List[str]:
        return list(self.tools.keys())

    def run(self, name: str, args: Dict[str, Any]) -> str:
        if name not in self.tools:
            return f"❌ أداة غير معروفة: {name}"
        try:
            return self.tools[name](**args)
        except Exception as e:
            return f"❌ فشل تنفيذ الأداة: {str(e)}"

tool_registry = ToolRegistry()

# ========== 4. طبقة الذاكرة (Memory Layer) ==========
class MemoryStore:
    """ذاكرة بسيطة للتخزين والاسترجاع"""
    def __init__(self, storage_dir: str):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _key_to_path(self, key: str) -> Path:
        hashed = hashlib.md5(key.encode()).hexdigest()
        return self.storage_dir / f"{hashed}.json"

    def save(self, key: str, value: Any) -> None:
        path = self._key_to_path(key)
        with open(path, "w") as f:
            json.dump({"key": key, "value": value, "timestamp": datetime.now().isoformat()}, f)

    def load(self, key: str) -> Optional[Any]:
        path = self._key_to_path(key)
        if path.exists():
            with open(path, "r") as f:
                data = json.load(f)
                return data.get("value")
        return None

    def search(self, query: str) -> List[Dict[str, Any]]:
        results = []
        for path in self.storage_dir.glob("*.json"):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                    if query.lower() in json.dumps(data).lower():
                        results.append(data)
            except Exception:
                pass
        return results

memory = MemoryStore(MEMORY_DIR)

# ========== 5. سياق التشغيل (Runtime Context) ==========
class RuntimeContext:
    """سياق التشغيل - يدير أوضاع النظام المختلفة"""
    MODE_AGENT = "agent"
    MODE_TERMINAL = "terminal"
    MODE_TOOL = "tool"

    def __init__(self):
        self.current_mode = self.MODE_AGENT

    def set_mode(self, mode: str) -> bool:
        if mode in [self.MODE_AGENT, self.MODE_TERMINAL, self.MODE_TOOL]:
            self.current_mode = mode
            return True
        return False

    def get_mode(self) -> str:
        return self.current_mode

runtime = RuntimeContext()

# ========== 6. جلسة الطرفية (Terminal Session) ==========
def _default_terminal_cwd() -> str:
    if sandbox.use_proot:
        return "/root"
    # When proot is unavailable use the runtime workspace directory.
    return str(_PATHS_TOOLS_DIR.parent / "workspace")


class TerminalSession:
    """جلسة طرفية حقيقية - تسمح بالتفاعل المباشر مع الأوامر"""
    def __init__(self):
        self.cwd = _default_terminal_cwd()
        os.makedirs(self.cwd, exist_ok=True)
        self.env = {}
        self.history: List[str] = []

    def execute(self, command: str) -> str:
        """تنفيذ أمر في وضع الطرفية"""
        self.history.append(command)

        # معالجة cd
        if command.startswith("cd "):
            target = command[3:].strip()
            new_path = os.path.normpath(os.path.join(self.cwd, target)) if not target.startswith("/") else target
            sandbox_path = os.path.join(SANDBOX_ROOT, new_path.lstrip("/"))
            if os.path.isdir(sandbox_path):
                self.cwd = new_path
                return f"تم تغيير المجلد إلى: {new_path}"
            return f"المجلد غير موجود: {new_path}"

        # معالجة export
        if command.startswith("export "):
            var_def = command[7:].strip()
            if "=" in var_def:
                var, val = var_def.split("=", 1)
                self.env[var.strip()] = val.strip()
                return f"تم تعيين {var}={val}"
            return "صيغة export غير صحيحة"

        # تنفيذ الأمر في الساندبوكس
        try:
            cmd_list = shlex.split(command)
        except ValueError:
            return "❌ صيغة أمر غير صالحة"

        stdout, stderr, returncode = sandbox.run(cmd_list, self.cwd, self.env)
        output = (stdout + stderr)[:MAX_OUTPUT_LENGTH]
        return output if output else _explain_exit(returncode)

terminal = TerminalSession()

# ========== السياسة (قائمة بيضاء مرنة) ==========
_DEFAULT_ALLOWED_COMMANDS = {
    # Files & search
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "find",
    "wc", "sort", "uniq", "tr", "sed", "awk", "cut", "tee", "xargs",
    "diff", "patch", "tree", "stat", "file", "less", "more",
    # FS ops
    "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "chown",
    "tar", "gzip", "gunzip", "zip", "unzip",
    # Env
    "echo", "printf", "date", "whoami", "id", "pwd", "cd", "export", "env",
    "which", "type", "test", "true", "false",
    # Sys info
    "ps", "top", "free", "df", "du", "uname", "uptime", "hostname",
    # Net
    "nmap", "curl", "wget", "ping", "host", "dig", "nslookup", "ssh", "scp",
    "rsync", "nc",
    # Tools
    "git", "jq", "yq", "make",
    # Runtimes
    "python", "python3", "pip", "pip3", "node", "npm", "npx", "yarn",
    "go", "java", "javac", "rustc", "cargo",
    # Pkg
    "apt", "apt-get", "dpkg", "pkg",
    # Shells
    "bash", "sh", "zsh",
}


def _load_allowed_commands() -> set[str]:
    extra = os.environ.get("AGENT_ALLOWED_CMDS", "")
    extras = {c.strip() for c in extra.split(",") if c.strip()}
    return _DEFAULT_ALLOWED_COMMANDS | extras


ALLOWED_COMMANDS = _load_allowed_commands()

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/", r"mkfs", r"dd\s+if=/dev/zero",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\};:", r"chmod\s+777\s+/",
    r">\s*/dev/sda", r"curl.*\|.*sh", r"wget.*\|.*sh"
]

# Pipelines (|, ;, &&, ||) are now allowed and validated per-segment.
# Command substitution is still blocked because validating it safely is hard.
HIGH_RISK_TOKENS = ["`", "$("]
PIPELINE_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")

# ========== دوال مساعدة ==========
def log(message: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _LOG_LOCK:
            with open(LOG_FILE, "a") as f:
                f.write(f"[{timestamp}] [{level}] {message}\n")
    except Exception:
        pass
    print(f"[{level}] {message}")

def _check_segment(segment: str) -> Tuple[bool, str]:
    seg = segment.strip()
    if not seg:
        return False, "أمر فارغ"
    try:
        tokens = shlex.split(seg)
    except ValueError:
        return False, f"صيغة غير صالحة: {seg}"
    if not tokens:
        return False, "أمر فارغ"
    base = tokens[0]
    # Treat ``ENV=value cmd`` as allowed prefix; pick the first non-assignment.
    while "=" in base and tokens:
        tokens = tokens[1:]
        if not tokens:
            return False, "أمر بيئة بدون أمر فعلي"
        base = tokens[0]
    if base not in ALLOWED_COMMANDS:
        return False, f"'{base}' غير مسموح به"
    return True, "مسموح"


def check_policy(command: str) -> Tuple[bool, str]:
    if not command.strip():
        return False, "أمر فارغ"

    # Reject command substitution outright; we can't validate it safely.
    for tok in HIGH_RISK_TOKENS:
        if tok in command:
            return False, f"نمط عالي الخطورة: {tok}"

    # Reject DANGEROUS_PATTERNS over the full command (catches piped attacks
    # like ``curl … | sh`` even though the pipeline is otherwise legal).
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return False, f"نمط خطير: {pattern}"

    # Validate every pipeline segment against the allowlist.
    segments = [s for s in PIPELINE_SPLIT_RE.split(command) if s.strip()]
    if not segments:
        return False, "أمر فارغ"
    for seg in segments:
        ok, msg = _check_segment(seg)
        if not ok:
            return False, msg
    return True, "مسموح"


def _is_pipeline(command: str) -> bool:
    return bool(PIPELINE_SPLIT_RE.search(command))


def _explain_exit(returncode: int) -> str:
    """ترجم رمز الخروج (خاصة سلبيات إشارات rlimit) لرسالة عربية مفهومة."""
    if returncode == 0:
        return "(exit 0)"
    if returncode > 0:
        return f"(exit {returncode})"
    sig = -returncode
    table = {
        9: "killed (SIGKILL — تجاوز الذاكرة على الأرجح)",
        15: "terminated (SIGTERM)",
        24: "cpu time limit exceeded (RLIMIT_CPU)",
        25: "file size limit exceeded (RLIMIT_FSIZE)",
        11: "segmentation fault",
        7: "bus error",
    }
    return f"(signal {sig}: {table.get(sig, 'killed by signal')})"

# ========== أدوات النواة (Kernel API) ==========
class KernelAPI:
    """واجهة النواة - تفصل التنفيذ عن الوكيل"""

    @staticmethod
    def execute_command(command: str) -> str:
        """تنفيذ أمر - يستخدمه الوكيل أو وضع الطرفية.

        يدعم الـpipelines (``|``, ``;``, ``&&``, ``||``). تتحقق ``check_policy``
        من كل قطعة على حدة وتُرفض ``$()`` و backticks.
        """
        if len(command) > MAX_COMMAND_LENGTH:
            return f"❌ الأمر طويل جداً (الحد الأقصى {MAX_COMMAND_LENGTH})"

        allowed, msg = check_policy(command)
        if not allowed:
            return f"❌ تم رفض الأمر: {msg}"

        if _is_pipeline(command):
            # Run the whole pipeline through bash -c (still allow-listed
            # per segment). bash itself is in ALLOWED_COMMANDS.
            cmd_list = ["bash", "-c", command]
        else:
            try:
                cmd_list = shlex.split(command)
            except ValueError:
                return "❌ صيغة أمر غير صالحة"

        stdout, stderr, returncode = sandbox.run(cmd_list, terminal.cwd, terminal.env)
        output = (stdout + stderr)[:MAX_OUTPUT_LENGTH]
        return output if output else _explain_exit(returncode)

# ========== تسجيل الأدوات في السجل ==========
tool_registry.register("execute_command", KernelAPI.execute_command)

# Self-modification toolkit (used by the model via ReAct, never by humans).
try:
    from core import self_modify as _sm
    from core import self_install as _si

    tool_registry.register("safe_edit_file", _sm.safe_edit_file)
    tool_registry.register("apply_patch", _sm.apply_patch)
    tool_registry.register("rollback_file", _sm.rollback_file)
    tool_registry.register("list_backups", _sm.list_backups)
    tool_registry.register("verify_python", _sm.verify_python)
    tool_registry.register("verify_json", _sm.verify_json)
    tool_registry.register("run_self_check", _sm.run_self_check)
    tool_registry.register("tail_log", _sm.tail_log)
    tool_registry.register("pip_install", _si.pip_install)
    tool_registry.register("apt_install", _si.apt_install)
    tool_registry.register("git_clone_repo", _si.git_clone_repo)
except Exception as _e:  # pragma: no cover
    log(f"تعذّر تسجيل أدوات التعديل الذاتي: {_e}", "WARN")

# ========== تعريف أداة smolagents (للتوافق مع الإصدارات السابقة) ==========
@tool
def execute_command(command: str) -> str:
    """
    تنفيذ أمر shell بشكل آمن داخل ساندبوكس proot.

    Args:
        command: الأمر المراد تنفيذه.
    """
    log(f"📥 طلب: {command[:100]}...")

    # معالجة cd و export (تحديث حالة terminal)
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "❌ صيغة أمر غير صالحة"

    if tokens:
        base = tokens[0]
        if base == "cd" and len(tokens) > 1:
            return terminal.execute(command)
        elif base == "export" and len(tokens) > 1:
            return terminal.execute(command)

    return KernelAPI.execute_command(command)

# ========== الدالة الرئيسية ==========
def build_runtime(*, start_mcp=True):
    """Wire up the model + tool registry + system awareness layer.

    Resolution order for the LLM:

    1. ``MEMORY_DIR/keys.json`` (managed via ``/admin/keys``) — preferred.
    2. ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` /
       any other ``*_API_KEY`` listed in ``core.llm.providers`` — picked
       up automatically by ``build_router_from_keystore``.
    3. ``config/team_config.json`` (legacy ``TeamModel``).

    Returned tuple is reused by both ``main`` and ``api/server.py``.
    """
    try:
        keystore = KeyStore()
        router = build_router_from_keystore(keystore)
        if router.backends:
            print(
                f"🔑 LLM router: {len(router.backends)} backend(s) — "
                f"{', '.join(b.llm.provider.id for b in router.backends)}"
            )
            model = router
        else:
            print("ℹ️ keystore فارغ — fallback إلى TeamModel (team_config.json).")
            model = TeamModel()
    except Exception as e:
        print(f"⚠️ keystore غير قابل للقراءة ({e}) — fallback إلى TeamModel.")
        model = TeamModel()

    try:
        from core.knowledge_sources import _register_knowledge_tools
        _register_knowledge_tools(tool_registry)
    except Exception as e:
        print(f"[تحذير] فشل تحميل المعرفة: {e}")

    try:
        from core.skills import SkillTool
        SkillTool.ensure_registered(tool_registry)
    except Exception as e:
        print(f"[تحذير] فشل تحميل المهارات: {e}")

    try:
        awareness = SystemAwareness()
        registered = awareness.register_all_tools(tool_registry)
        print(f"🧠 system_awareness: {registered} أداة جديدة سُجلت "
              f"(إجمالي بالحزم: {len(awareness.tools)} ومهارات: {awareness.skills_count})")
    except Exception as e:
        print(f"[تحذير] فشل بناء system_awareness: {e}")
        awareness = None

    # Native platform adapters (GitHub / AWS / Slack / Email / Browser / GitLab / Discord).
    try:
        from core.adapters import build_adapter_tools
        adapter_tools = build_adapter_tools()
        for tname, fn in adapter_tools.items():
            tool_registry.register(tname, fn)
        if adapter_tools:
            print(f"🔌 adapters: {len(adapter_tools)} أداة محلية مدمجة "
                  f"({', '.join(sorted({n.split('_', 1)[0] for n in adapter_tools}))})")
    except Exception as e:
        print(f"[تحذير] فشل تحميل adapters: {e}")

    # Admin tools (chat-driven self-modification: add api keys, MCP servers,
    # channels, schedules, switch mode, run self-test, repair, etc.).
    try:
        from core.admin_tools import register_admin_tools
        registered_admin = register_admin_tools(tool_registry)
        if registered_admin:
            print(f"🛠️  admin tools: {len(registered_admin)} مُسجَّل")
    except Exception as e:
        print(f"[تحذير] فشل تحميل admin tools: {e}")

    # Preview tools — let the agent serve a directory of generated files
    # over loopback HTTP and hand the user back a clickable URL.
    try:
        from core.preview_server import register_preview_tools
        registered_preview = register_preview_tools(tool_registry)
        if registered_preview:
            print(f"🌐 preview tools: {len(registered_preview)} مُسجَّل")
    except Exception as e:
        print(f"[تحذير] فشل تحميل preview tools: {e}")

    # Account-backed tools (Gmail / Calendar / Drive). They no-op until
    # the user has linked a Google account via /admin/oauth/link/start.
    try:
        from core.oauth.google import register_google_tools
        registered_google = register_google_tools(tool_registry)
        if registered_google:
            print(f"🔐 google tools: {len(registered_google)} مُسجَّل")
    except Exception as e:
        print(f"[تحذير] فشل تحميل google tools: {e}")

    # User-defined macros (composed tools).
    try:
        from core.composition import build_macro_tools
        macro_tools = build_macro_tools(tool_registry)
        for tname, fn in macro_tools.items():
            tool_registry.register(tname, fn)
        if macro_tools:
            print(f"🧩 macros: {len(macro_tools)} ماكرو مُسجَّل")
    except Exception as e:
        print(f"[تحذير] فشل تحميل macros: {e}")

    # Native Windows ComputerControl Capabilities
    try:
        from core.windows import get_computer_control
        cc = get_computer_control()
        tool_registry.register("computer_get_state", lambda **kw: json.dumps(cc.get_state(**kw), ensure_ascii=False))
        tool_registry.register("computer_open_app", lambda app, **kw: json.dumps(cc.open_app(app), ensure_ascii=False))
        tool_registry.register("computer_focus_window", lambda target, **kw: json.dumps(cc.focus_window(target), ensure_ascii=False))
        tool_registry.register("computer_close_app", lambda target, graceful=True, **kw: json.dumps(cc.close_app(target, graceful=graceful), ensure_ascii=False))
        tool_registry.register("computer_run_command", lambda command, **kw: json.dumps(cc.run_command(command), ensure_ascii=False))
        tool_registry.register("computer_read_file", lambda path, **kw: json.dumps(cc.read_file(path), ensure_ascii=False))
        tool_registry.register("computer_write_file", lambda path, content, **kw: json.dumps(cc.write_file(path, content), ensure_ascii=False))
        tool_registry.register("computer_move_file", lambda src, dst, **kw: json.dumps(cc.move_file(src, dst), ensure_ascii=False))
        tool_registry.register("computer_click", lambda x, y, button="left", double=False, **kw: json.dumps(cc.click(x, y, button, double), ensure_ascii=False))
        tool_registry.register("computer_type", lambda text, **kw: json.dumps(cc.type_text(text), ensure_ascii=False))
        tool_registry.register("computer_hotkey", lambda keys, **kw: json.dumps(cc.send_hotkey(keys), ensure_ascii=False))
        tool_registry.register("computer_verify", lambda condition, **kw: json.dumps(cc.verify(condition), ensure_ascii=False))
        print("🖥️  computer_control: 12 قدرة تحكم أصيلة مسجلة")
    except Exception as e:
        print(f"[تحذير] فشل تحميل computer_control: {e}")

    # MCP servers (Model Context Protocol — external tool servers).
    # The bridge wires every alive MCP tool into both ``tool_registry``
    # (so ReAct / Workflow / AgentsTeam can call them) and into
    # ``system_awareness.tools`` (so they show up in the prompt with
    # their read/write/dangerous classification).
    try:
        from core.mcp import get_registry
        mcp_registry = get_registry()
        # Boot any servers configured in mcp_servers.json.
        if start_mcp:
            mcp_registry.start_all()
        if awareness is not None:
            mcp_count = awareness.register_mcp_tools(tool_registry, mcp_registry)
        else:
            from core.mcp import build_mcp_tools
            wrappers = build_mcp_tools(mcp_registry, auto_start=False)
            for tname, fn in wrappers.items():
                tool_registry.register(tname, fn)
            mcp_count = len(wrappers)
        servers = sum(1 for s in mcp_registry.list_servers() if s["alive"])
        if mcp_count:
            print(f"🌐 mcp: {mcp_count} أداة من {servers} خادم MCP")
    except Exception as e:
        print(f"[تحذير] فشل تحميل MCP: {e}")

    # Tool installer: annotate awareness with install status and wrap
    # missing tools with helpful 'not installed' guards.
    try:
        from core.tool_installer import (annotate_awareness, get_installer,
                                          interactive_install_prompt,
                                          wrap_registry_with_install_check)
        installer = get_installer()
        rootfs = installer.rootfs_info()
        if not rootfs["exists"]:
            print(f"📦 tool-installer: rootfs غير موجود في {rootfs['path']}. "
                  f"شغّل: sudo bash sandbox/build_rootfs.sh لبناءه.")
        report = installer.missing_report()
        if awareness is not None:
            annotate_awareness(awareness, installer)
        wrap_registry_with_install_check(tool_registry, installer)
        if report["missing_count"]:
            print(f"📦 tool-installer: {report['available']}/"
                  f"{report['total']} أداة جاهزة، "
                  f"{report['missing_count']} تحتاج تثبيتاً "
                  f"(POST /admin/tools/install)")
            outcome = interactive_install_prompt(installer)
            if outcome and outcome != "skipped":
                # Re-annotate after attempted install so the prompt
                # reflects newly-available tools.
                if awareness is not None:
                    annotate_awareness(awareness, installer)
                wrap_registry_with_install_check(tool_registry, installer)
                print(f"📦 tool-installer auto-install: {outcome}")
        elif report["total"]:
            print(f"📦 tool-installer: كل الأدوات الـ{report['total']} جاهزة")
    except Exception as e:
        print(f"[تحذير] فشل tool-installer: {e}")

    # Optional hot-reload watcher (set AGENT_HOTRELOAD=1).
    try:
        from core.hotreload import maybe_start_default
        hr = maybe_start_default(tool_registry)
        if hr is not None:
            print("🔁 hot-reload watcher نشط")
    except Exception as e:
        print(f"[تحذير] فشل تشغيل hot-reload: {e}")

    # Build the semantic tool index (best effort, lazy on first query).
    try:
        from core.tool_search import refresh_index
        info = refresh_index(tool_registry, awareness)
        print(f"🔎 tool-search index: {info['docs']} أداة "
              f"({'embeddings' if info['embeddings'] else 'tfidf'})")
    except Exception as e:
        print(f"[تحذير] فشل بناء فهرس البحث: {e}")

    return model, awareness


def main():
    model, awareness = build_runtime()

    print(f"\n🔍 [تشخيص] الأدوات بعد التسجيل: {len(tool_registry.list_tools())}")
    print("🔧 Agent OS Core v14.0 - مفاتيح API + system_awareness")
    print(f"🛡️  عزل: proot (مجرد)")
    print(f"📁 الذاكرة: {MEMORY_DIR}")
    print(f"🔧 الأدوات المسجلة: {tool_registry.list_tools()}")
    print(f"📂 المجلد الحالي: {terminal.cwd}")
    print(f"🎛️  وضع التشغيل: {runtime.get_mode()}")
    print(f"🔒 حدود الأمان: أمر={MAX_COMMAND_LENGTH}، مخرجات={MAX_OUTPUT_LENGTH}")

    print("\n🤖 الوكيل جاهز. اكتب طلبك (أو 'exit'):")
    print("   - يمكنك استخدام 'mode terminal' للتبديل إلى وضع الطرفية")
    print("   - يمكنك استخدام 'mode agent' للعودة إلى وضع الوكيل")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n💬 طلبك: ").strip()
            if user_input.lower() in ["exit", "quit", "خروج"]:
                print("👋 إيقاف...")
                break
            if not user_input:
                continue

            # معالجة تغيير الوضع
            if user_input.lower() == "mode terminal":
                runtime.set_mode(RuntimeContext.MODE_TERMINAL)
                print("🎛️ تم التبديل إلى وضع الطرفية. اكتب أوامرك مباشرة (exit للعودة للوكيل).")
                while True:
                    try:
                        cmd = input("🐚 $ ").strip()
                        if cmd.lower() in ["exit", "quit"]:
                            break
                        if cmd:
                            result = terminal.execute(cmd)
                            print(result)
                    except KeyboardInterrupt:
                        print("\n")
                        break
                runtime.set_mode(RuntimeContext.MODE_AGENT)
                print("🎛️ تم العودة إلى وضع الوكيل.")
                continue

            if user_input.lower() == "mode agent":
                runtime.set_mode(RuntimeContext.MODE_AGENT)
                print("🎛️ تم التبديل إلى وضع الوكيل.")
                continue

            
            # ----- أوامر إدارة النماذج -----
            if user_input.lower().startswith("admin list-models"):
                try:
                    from pathlib import Path
                    config_path = CONFIG_DIR / "team_config.json"
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                    print("📋 النماذج المسجلة:")
                    for idx, m in enumerate(config.get("models", [])):
                        status = "✅" if m.get("enabled", True) else "❌"
                        print(f"{idx+1}. {status} {m.get('name', m.get('model_name', 'غير معروف'))} ({m.get('model_name', '')})")
                    if not config.get("models"):
                        print("   لا توجد نماذج مسجلة.")
                except Exception as e:
                    print(f"❌ خطأ: {e}")
                continue

            if user_input.lower().startswith("admin mode"):
                try:
                    parts = user_input.split()
                    if len(parts) < 3:
                        print("استخدام: admin mode <single|ensemble|fallback|first>")
                    else:
                        mode = parts[2]
                        if mode not in ["single", "ensemble", "fallback", "first"]:
                            print("❌ وضع غير صالح. اختر من: single, ensemble, fallback, first")
                        else:
                            config_path = CONFIG_DIR / "team_config.json"
                            with open(config_path, 'r') as f:
                                config = json.load(f)
                            config["mode"] = mode
                            if mode == "single" and config.get("models"):
                                config.setdefault("active_model", config["models"][0].get("name", ""))
                            with open(config_path, 'w') as f:
                                json.dump(config, f, indent=4, ensure_ascii=False)
                            print(f"✅ تم تغيير وضع التشغيل إلى: {mode}. أعد التشغيل لتفعيل التغيير.")
                except Exception as e:
                    print(f"❌ خطأ: {e}")
                continue

            if user_input.lower().startswith("admin set-active"):
                try:
                    name = user_input[18:].strip()
                    if not name:
                        print("استخدام: admin set-active <اسم النموذج>")
                    else:
                        config_path = CONFIG_DIR / "team_config.json"
                        with open(config_path, 'r') as f:
                            config = json.load(f)
                        # التحقق من وجود النموذج
                        found = any(m.get("name", m.get("model_name", "")) == name for m in config.get("models", []))
                        if not found:
                            print(f"❌ النموذج '{name}' غير موجود. استخدم admin list-models للعرض.")
                        else:
                            config["active_model"] = name
                            config["mode"] = "single"
                            with open(config_path, 'w') as f:
                                json.dump(config, f, indent=4, ensure_ascii=False)
                            print(f"✅ تم تعيين النموذج النشط إلى '{name}'. أعد التشغيل لتفعيل التغيير.")
                except Exception as e:
                    print(f"❌ خطأ: {e}")
                continue

            if user_input.lower().startswith("admin add-model"):
                try:
                    print("📝 أدخل بيانات النموذج الجديد:")
                    api_key = input("   مفتاح API (اختياري): ").strip()
                    base_url = input("   الرابط الأساسي (مثلاً https://api.openai.com/v1): ").strip()
                    model_name = input("   اسم النموذج (مثلاً gpt-4o-mini): ").strip()
                    name = input("   اسم وصفي (اختياري): ").strip()
                    if not base_url or not model_name:
                        print("❌ الرابط الأساسي واسم النموذج مطلوبان.")
                    else:
                        config_path = CONFIG_DIR / "team_config.json"
                        with open(config_path, 'r') as f:
                            config = json.load(f)
                        model = {
                            "api_key": api_key if api_key else None,
                            "base_url": base_url,
                            "model_name": model_name,
                            "enabled": True
                        }
                        if name:
                            model["name"] = name
                        else:
                            model["name"] = model_name
                        config.setdefault("models", []).append(model)
                        with open(config_path, 'w') as f:
                            json.dump(config, f, indent=4, ensure_ascii=False)
                        print(f"✅ تمت إضافة النموذج '{model['name']}'.")
                        print("   أعد تشغيل Agent OS لتفعيل النموذج الجديد.")
                except Exception as e:
                    print(f"❌ فشل إضافة النموذج: {e}")
                continue

            # معالجة أوامر الذاكرة
            if user_input.lower().startswith("memory save "):
                parts = user_input[12:].strip().split(" ", 1)
                if len(parts) == 2:
                    memory.save(parts[0], parts[1])
                    print(f"💾 تم حفظ {parts[0]}")
                else:
                    print("❌ استخدام: memory save <مفتاح> <قيمة>")
                continue

            if user_input.lower().startswith("memory load "):
                key = user_input[12:].strip()
                value = memory.load(key)
                if value is not None:
                    print(f"📄 {key}: {value}")
                else:
                    print(f"❓ لا توجد قيمة للمفتاح: {key}")
                continue

            # وضع الوكيل - معالجة الطلب باستخدام ReAct
            result = react_loop(user_input, model, tool_registry.tools,
                                system_awareness=awareness)
            print("\n📄 النتيجة:")
            print(result)

        except KeyboardInterrupt:
            print("\n👋 إيقاف...")
            break
        except Exception as e:
            log(f"خطأ حلقة: {e}", "ERROR")
            print(f"\n❌ خطأ غير متوقع: {e}")

if __name__ == "__main__":
    main()

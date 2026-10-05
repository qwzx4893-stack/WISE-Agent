"""
System Awareness Layer
======================

A single component that:

1. Reads every JSON tool pack from ``tools/packs/*.json`` and parses each entry
   into a :class:`ToolManifest`.
2. Surfaces all installed skills via :class:`SkillIndexer` (1000+ items).
3. Builds a compact, model-friendly description of the entire Agent OS that
   gets injected into the system prompt.
4. Provides ``register_all_tools(registry)`` which exposes every JSON tool to
   the runtime ``ToolRegistry`` from ``agent_core``: Python tools resolve to
   their kernel implementations (e.g. ``read_file``, ``execute_command``);
   CLI tools become wrappers that build and run the templated command line
   through the active sandbox.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .paths import TOOLS_PACKS_DIR, WORKSPACE_DIR


# Lazy-import these to avoid hard failures if optional deps are missing.
try:
    from .tool_intelligence.manifest import ToolManifest  # type: ignore
    _TI_AVAILABLE = True
except Exception:
    ToolManifest = None  # type: ignore
    _TI_AVAILABLE = False


# Names that the agent_core kernel already implements directly.
_KERNEL_IMPLEMENTED = {"execute_command", "search_knowledge",
                       "list_skills", "load_skill", "search_skills"}

# Mapping for python-implemented tools that we expose in-process.
_PYTHON_BUILTIN_NAMES = {
    "read_file", "write_file", "list_directory", "grep_file",
    "search_memory", "remember", "ask_user", "execute_python",
    "git_clone", "web_fetch", "web_search", "run_shell",
}


class SystemAwareness:
    """Aggregates packs + skills into one view consumed by the LLM."""

    def __init__(self, packs_dir: Optional[Path] = None,
                 max_tools_in_prompt: int = 80,
                 max_skills_in_prompt: int = 25):
        self.packs_dir = Path(packs_dir) if packs_dir else TOOLS_PACKS_DIR
        self.max_tools_in_prompt = max_tools_in_prompt
        self.max_skills_in_prompt = max_skills_in_prompt
        self.tools: Dict[str, Dict[str, Any]] = {}
        # Manifests are documentation until their backing implementation is
        # verified.  This map is populated by the local capability preflight.
        self.unavailable_tools: Dict[str, str] = {}
        self.skills_count: int = 0
        self.skill_names: List[str] = []
        self._load_tools()
        self._load_skills()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load_tools(self) -> None:
        if not self.packs_dir.exists():
            return
        for pack_file in sorted(self.packs_dir.glob("*.json")):
            try:
                with open(pack_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"⚠️ تعذر قراءة {pack_file.name}: {e}")
                continue

            entries = data if isinstance(data, list) else [data]
            for raw in entries:
                if not isinstance(raw, dict) or "name" not in raw:
                    continue
                name = raw["name"]
                # First definition wins; later packs can override only if the
                # version is strictly newer (string compare is good enough here).
                existing = self.tools.get(name)
                if existing and existing.get("version", "0") >= raw.get("version", "0"):
                    continue
                raw["__source_pack__"] = pack_file.name
                self.tools[name] = raw

    def _load_skills(self) -> None:
        try:
            from .skills import SkillIndexer
            indexer = SkillIndexer()
            self.skill_names = sorted(indexer.list_skills())
            self.skills_count = len(self.skill_names)
        except Exception as e:
            # Skills are optional at runtime; never block startup.
            print(f"⚠️ تخطي فهرسة المهارات في system_awareness: {e}")
            self.skill_names = []
            self.skills_count = 0

    def _availability_reason(self, name: str, manifest: Dict[str, Any]) -> Optional[str]:
        """Return why a manifest is not executable on this host.

        The check performs no installation and no network request.  It makes
        the prompt and the callable registry agree about what is real.
        """
        if manifest.get("replaces"):
            return "superseded"
        if manifest.get("__mcp__"):
            return None
        kind = str(manifest.get("implementation_type", "python")).lower()
        if kind == "python":
            if name in _PYTHON_BUILTIN_NAMES or name in _KERNEL_IMPLEMENTED:
                return None
            return "no local Python implementation"
        if kind == "cli":
            template = str(manifest.get("cli_command") or manifest.get("cli_binary") or "").strip()
            if not template:
                return "no CLI command declared"
            try:
                first = shlex.split(template)[0]
            except (ValueError, IndexError):
                return "invalid CLI command"
            # A dynamic binary cannot be preflighted; generic shell execution
            # remains available through execute_command instead.
            if "{" in first or "}" in first:
                return "dynamic command; use execute_command instead"
            if Path(first).is_file() or shutil.which(first):
                return None
            return f"CLI binary not installed: {first}"
        return f"unsupported implementation type: {kind}"

    def _executable_tools(self, tools: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        runnable: Dict[str, Dict[str, Any]] = {}
        self.unavailable_tools = {}
        for name, manifest in tools.items():
            reason = self._availability_reason(name, manifest)
            if reason is None:
                runnable[name] = manifest
            else:
                self.unavailable_tools[name] = reason
        return runnable

    # ------------------------------------------------------------------
    # Description for the system prompt
    # ------------------------------------------------------------------
    def get_full_system_description(self,
                                    task: Optional[str] = None,
                                    *,
                                    top_k: int = 15,
                                    history: Optional[List[Dict[str, Any]]] = None,
                                    banned: Optional[List[str]] = None) -> str:
        # Mode-aware filtering: hide Pro-only tools while in Lite Mode.
        try:
            from .platform_manager import get_platform_manager
            _pm = get_platform_manager()
            visible_tools = _pm.filter_tools(self.tools)
        except Exception:
            visible_tools = self.tools
        visible_tools = self._executable_tools(visible_tools)

        by_category: Dict[str, List[str]] = defaultdict(list)
        for name, t in visible_tools.items():
            # Skip duplicates that the auditor superseded.
            if t.get("replaces"):
                continue
            cat = t.get("category", "Execution")
            by_category[cat].append(name)

        lines: List[str] = [
            "## نظرة عامة على Agent OS",
            f"- الأدوات القابلة للتنفيذ الآن: **{len(visible_tools)}** (موزعة على {len(by_category)} فئة).",
            f"- إجمالي المهارات الموثقة: **{self.skills_count}** (تستطيع استدعاؤها عبر `load_skill` و `search_skills`).",
            "- لديك صلاحية تنفيذ أوامر طرفية محدودة عبر `execute_command` داخل ساندبوكس.",
            "- يمكنك البحث في معارف خارجية عبر `search_knowledge` (Wikipedia, arXiv, OpenAlex, Semantic Scholar).",
            "",
        ]

        # Inject Computer World Model (Environment Awareness)
        try:
            from .context import get_world_model_manager
            wmm = get_world_model_manager()
            world_summary = wmm.get_prompt_context()
            if world_summary:
                lines.append(world_summary)
                lines.append("")
        except Exception:
            pass

        # Task-aware ranked top-K with full descriptions, when a task
        # is supplied. Otherwise fall back to the static, category view.
        ranked_top: List[str] = []
        if task:
            try:
                from .tool_intelligence.ranker import rank_tools_for_task
                ranked = rank_tools_for_task(
                    task, visible_tools,
                    history=history or [],
                    banned=banned or [])
                visible = [r for r in ranked
                           if not r.manifest.get("replaces")
                           and r.score > 0]
                top = visible[:max(1, int(top_k))]
                if top:
                    lines.append(
                        f"### أفضل {len(top)} أداة لمهمتك الحالية "
                        f"(مرتَّبة):")
                    for s in top:
                        m = s.manifest
                        desc = (m.get("description") or "").strip()
                        if len(desc) > 160:
                            desc = desc[:157] + "…"
                        cap = m.get("capabilities") or []
                        cap_text = (", ".join(str(c) for c in cap[:4])
                                    if cap else "—")
                        flag = " (MCP)" if s.is_mcp else ""
                        lines.append(
                            f"- `{s.name}`{flag} — {desc} _قدرات_: {cap_text}.")
                        ranked_top.append(s.name)
                    lines.append("")
                    lines.append(
                        "💡 لمزيد من الأدوات استخدم "
                        "`search_tools(query)` أو راجع الفئات أدناه.")
                    lines.append("")
            except Exception:
                pass

            # The capability network joins native tools, connected MCP tools,
            # and indexed procedural skills. It is advisory context for the
            # model, not an execution shortcut; normal security gating still
            # applies to every selected capability.
            try:
                from .capability_network import get_capability_network
                network_plan = get_capability_network().plan(
                    task, limit=4, skill_limit=2)
                lines.append("")
                lines.append(network_plan.to_prompt())
                lines.append("")
            except Exception:
                pass

        lines.append("### الأدوات حسب الفئة (مختصر):")

        # Stable ordering of categories
        category_order = ["Execution", "Memory", "Orchestration", "Planning"]
        seen = set()
        for cat in category_order + sorted(by_category.keys()):
            if cat in seen or cat not in by_category:
                continue
            seen.add(cat)
            names = sorted(
                n for n in by_category[cat] if n not in ranked_top)
            if not names:
                continue
            preview = ", ".join(names[:20])
            extra = f" … (+{len(names) - 20})" if len(names) > 20 else ""
            lines.append(f"- **{cat}** ({len(names)}): {preview}{extra}")

        # Surface MCP-bridged tools with their classifications so the
        # LLM knows which calls are safe and which trigger the
        # confirmation gate.
        mcp_tools = [(n, t) for n, t in visible_tools.items()
                     if t.get("__mcp__")]
        if mcp_tools:
            lines.append("")
            lines.append("### أدوات MCP المتصلة:")
            for n, t in sorted(mcp_tools)[:20]:
                sec = t.get("security", {}) or {}
                kind = sec.get("kind", "read")
                tag = "📖 read"
                if kind == "write":
                    tag = "✏️ write (يحتاج confirm)"
                elif kind == "dangerous":
                    tag = "⚠️ dangerous (يحتاج موافقة UI)"
                lines.append(f"- `{n}` — {tag}")
            if len(mcp_tools) > 20:
                lines.append(f"… +{len(mcp_tools) - 20} أداة MCP أخرى.")

        # Surface tools whose backing binaries / Python packages are
        # absent in the sandbox so the model can install on demand
        # instead of crashing into 'command not found'.
        missing = [(n, self.tools[n]) for n in self.unavailable_tools
                   if n in self.tools]
        if missing:
            lines.append("")
            lines.append("### أدوات تحتاج تثبيتاً قبل الاستخدام:")
            lines.append("- بعض الأدوات موثقة في الحزم لكن ليس لها تنفيذ محلي متاح الآن.")
            lines.append("- لا يختارها الوكيل للتنفيذ حتى تصبح جاهزة فعلياً.")
            lines.append("- لتثبيتها: `POST /admin/tools/install` أو حدد قائمة `{tools: [...]}`.")
            for n, t in sorted(missing)[:15]:
                hint_str = f" — {self.unavailable_tools.get(n, 'غير متاح')}"
                lines.append(f"- `{n}`{hint_str}")
            if len(missing) > 15:
                lines.append(f"… +{len(missing) - 15} أداة ناقصة "
                             "(انظر `GET /admin/tools/missing`).")

        if self.skill_names:
            lines.append("")
            lines.append("### عينة من المهارات المتاحة:")
            sample = self.skill_names[: self.max_skills_in_prompt]
            lines.append(", ".join(sample))
            if len(self.skill_names) > self.max_skills_in_prompt:
                lines.append(f"… ومجموع {self.skills_count} مهارة. استخدم "
                             "`search_skills` لإيجاد ما يناسب المهمة، ثم "
                             "`load_skill` لتحميل تفاصيلها.")

        lines.append("")
        lines.append("### إرشادات اختيار الأداة:")
        lines.append("- ابدأ بإرجاع `Thought` يوضح خطوتك التالية.")
        lines.append("- استخدم أداة واحدة فقط في كل خطوة.")
        lines.append("- لا تعتمد على `execute_command` كأداة افتراضية لكل شيء — اختر الأداة الأكثر تخصصاً المتاحة.")
        lines.append("- إذا فشلت أداة 3 مرات في نفس الجلسة فستُحجب تلقائياً وسيُقترَح بديل.")
        lines.append("- إذا لم تجد أداة مناسبة، فاستخدم `search_tools` أو `search_skills` للبحث في المعرفة، أو `search_knowledge` للبحث الخارجي.")
        lines.append("- بعض الأدوات قد تحتاج تثبيتاً قبل أول استخدام (راجع قسم \"أدوات تحتاج تثبيتاً\" أعلاه).")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Wiring tools into the runtime ToolRegistry
    # ------------------------------------------------------------------
    def register_mcp_tools(self, registry: Any,
                            mcp_registry: Any = None) -> int:
        """Discover live MCP servers and surface their tools.

        For every alive MCP server, this:

        1. Builds wrappers via :func:`core.mcp.build_mcp_tools`
           (these already enforce the confirmation gate).
        2. Adds a manifest entry for each ``mcp_<server>_<tool>`` so
           the LLM sees it in :meth:`get_full_system_description` next
           to native tools — including the ``read | write | dangerous``
           classification surfaced as ``security.kind`` and
           ``security.confirm_required``.
        3. Registers the wrapper into ``registry`` so the
           ThinkingEngine / ReAct loop can invoke ``mcp_*`` tools just
           like native ones.

        Returns the number of newly registered MCP tools.
        """
        try:
            from .mcp import build_mcp_tools, get_registry
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ MCP bridge unavailable: {exc}")
            return 0

        mcp_reg = mcp_registry or get_registry()
        try:
            wrappers = build_mcp_tools(mcp_reg, auto_start=False)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ build_mcp_tools failed: {exc}")
            return 0

        added = 0
        for tool_name, fn in wrappers.items():
            meta = getattr(fn, "__mcp_meta__", {}) or {}
            server = meta.get("server", "")
            inner = meta.get("tool", "")
            kind = meta.get("kind", "read")
            try:
                registry.register(tool_name, fn)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ couldn't register MCP tool {tool_name}: {exc}")
                continue
            description = (fn.__doc__ or
                           f"MCP tool '{inner}' on server '{server}'.")
            self.tools[tool_name] = {
                "name": tool_name,
                "version": "mcp",
                "description": description[:240],
                "category": "MCP",
                "implementation_type": "python",
                "use_cases": [f"Call MCP server {server}.{inner}"],
                "security": {
                    "kind": kind,
                    "confirm_required":
                        meta.get("require_confirmation", True)
                        and kind != "read"
                        and not meta.get("auto_approved", False),
                    "auto_approved": meta.get("auto_approved", False),
                    "requires_sandbox": False,
                },
                "__source_pack__": f"mcp::{server}",
                "__mcp__": True,
            }
            added += 1
        if added:
            print(f"🔌 system_awareness: registered {added} MCP tools "
                  f"from {sum(1 for c in mcp_reg.alive_clients().values())}"
                  f" alive server(s)")
        return added

    def register_all_tools(self, registry: Any) -> int:
        """Bind every JSON-defined tool into ``registry`` (from ``agent_core``).

        Tools with implementations already provided by agent_core (notably
        ``execute_command``, ``search_knowledge``, ``list_skills``, ``load_skill``,
        ``search_skills``) are kept as-is.

        For the rest:
        - ``implementation_type == "cli"``: a wrapper that builds the command
          using ``cli_command`` template + ``args`` and runs it.
        - ``implementation_type == "python"``: registered only when backed by
          a known local implementation.

        Documentation-only manifests remain visible to the installer/auditor,
        but are never registered as callable stubs.
        """
        registered = 0
        for name, manifest in self.tools.items():
            unavailable = self._availability_reason(name, manifest)
            if unavailable is not None:
                self.unavailable_tools[name] = unavailable
                continue
            if name in _KERNEL_IMPLEMENTED and registry.get(name) is not None:
                continue
            if registry.get(name) is not None:
                # Already exposed (for example by SkillTool); don't overwrite.
                continue
            impl_type = manifest.get("implementation_type", "python")
            if impl_type == "cli":
                fn = self._make_cli_wrapper(name, manifest, registry)
            else:
                fn = self._make_python_wrapper(name, manifest, registry)
            try:
                registry.register(name, fn)
                registered += 1
            except Exception as e:
                print(f"⚠️ فشل تسجيل الأداة {name}: {e}")
        return registered

    # ------------------------------------------------------------------
    # Wrappers
    # ------------------------------------------------------------------
    def _make_cli_wrapper(self, name: str, manifest: Dict[str, Any],
                          registry: Any) -> Callable[..., str]:
        template: str = manifest.get("cli_command") or name
        timeout: int = int(manifest.get("timeout", 60))
        requires_sandbox = manifest.get("security", {}).get("requires_sandbox", True)

        def _run(**kwargs: Any) -> str:
            # Format placeholders {key} in the template using shlex.quote for safety.
            try:
                rendered = template.format(
                    **{k: shlex.quote(str(v)) for k, v in kwargs.items()}
                )
            except KeyError as e:
                return f"❌ مفتاح مفقود للأداة {name}: {e}"

            execute_command = registry.get("execute_command")
            if requires_sandbox and execute_command is not None:
                # Route through the kernel's policy/sandbox-aware command runner.
                return execute_command(rendered)

            try:
                # ``rendered`` was built with shlex.quote for each value, so
                # we can split with shlex and skip ``shell=True`` entirely.
                argv = shlex.split(rendered)
                proc = subprocess.run(
                    argv, shell=False, capture_output=True,
                    text=True, timeout=timeout, cwd=str(WORKSPACE_DIR),
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                return out if out else f"(exit {proc.returncode})"
            except subprocess.TimeoutExpired:
                return f"❌ انتهت مهلة الأداة {name} ({timeout}s)"
            except Exception as e:
                return f"❌ خطأ عند تنفيذ {name}: {e}"

        _run.__name__ = name  # type: ignore[attr-defined]
        _run.__doc__ = manifest.get("description", name)
        return _run

    def _make_python_wrapper(self, name: str, manifest: Dict[str, Any],
                             registry: Any) -> Callable[..., str]:
        description = manifest.get("description", name)

        # Dispatch to a known kernel builtin if the name matches.
        if name in _PYTHON_BUILTIN_NAMES:
            def _impl(**kwargs: Any) -> str:
                from .tools_bridge import call_python_builtin
                return call_python_builtin(name, kwargs)
            _impl.__name__ = name  # type: ignore[attr-defined]
            _impl.__doc__ = description
            return _impl

        # This is unreachable after the preflight in register_all_tools.
        # Raising is safer than returning a successful-looking placeholder if
        # a future caller bypasses that gate.
        def _unavailable(**_kwargs: Any) -> str:
            raise RuntimeError(
                f"Tool '{name}' has no local Python implementation and is not executable."
            )

        _unavailable.__name__ = name  # type: ignore[attr-defined]
        _unavailable.__doc__ = description
        return _unavailable

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    def list_tool_names(self) -> List[str]:
        return sorted(self.tools.keys())

    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tools.get(name)

    def stats(self) -> Dict[str, int]:
        cats: Dict[str, int] = defaultdict(int)
        for t in self.tools.values():
            cats[t.get("category", "Execution")] += 1
        return {
            "tools_total": len(self.tools),
            "tools_executable": len(self._executable_tools(self.tools)),
            "tools_unavailable": len(self.unavailable_tools),
            "skills_total": self.skills_count,
            "categories": dict(cats),
        }


__all__ = ["SystemAwareness"]

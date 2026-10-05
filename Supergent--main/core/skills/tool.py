"""Skill-related tools registered with the runtime ToolRegistry."""

from __future__ import annotations

import threading

from .indexer import SkillIndexer
from .loader import SkillLoader
from .search import SkillSearch


class SkillTool:
    _registration_state = {
        "list_skills": False,
        "load_skill": False,
        "search_skills": False,
        "list_skill_categories": False,
        "search_skills_by_category": False,
    }
    _lock = threading.Lock()

    @classmethod
    def register(cls, registry=None) -> bool:
        with cls._lock:
            try:
                if registry is None:
                    import sys

                    from ..paths import AGENT_OS_ROOT

                    sys.path.insert(0, str(AGENT_OS_ROOT))
                    from agent_core import tool_registry as registry  # type: ignore

                indexer = SkillIndexer()
                loader = SkillLoader()
                searcher = SkillSearch()

                def list_skills(category: str | None = None) -> str:
                    skills = indexer.list_skills()
                    if not skills:
                        return "لا توجد مهارات."
                    items: list[str] = []
                    index = indexer.get_index()
                    for name in skills:
                        info = index.get(name, {})
                        if category and info.get("category") != category:
                            continue
                        items.append(
                            f"- {name} [{info.get('category', 'general')}]: "
                            f"{info.get('description', '')}"
                        )
                    if not items:
                        return f"لا توجد مهارات في الفئة '{category}'."
                    return "**المهارات:**\n" + "\n".join(items)

                def load_skill(skill_name: str) -> str:
                    content, _ = loader.load(skill_name)
                    if content is None:
                        return f"المهارة '{skill_name}' غير موجودة."
                    return content

                def search_skills(query: str, top_k: int = 3) -> str:
                    results = searcher.search(query, top_k)
                    if not results:
                        return "لا توجد مهارات مطابقة."
                    out = [f"**نتائج البحث عن '{query}':**"]
                    for name, score, desc in results:
                        out.append(f"- {name} (تطابق {score:.2f}): {desc}")
                    return "\n".join(out)

                def list_skill_categories() -> str:
                    cats = indexer.list_categories()
                    if not cats:
                        return "لا توجد فئات."
                    lines = ["**فئات المهارات:**"]
                    for cat, names in cats.items():
                        lines.append(f"- {cat}: {len(names)} مهارة")
                    return "\n".join(lines)

                def search_skills_by_category(
                    category: str, query: str = "", top_k: int = 5
                ) -> str:
                    if not query:
                        names = indexer.list_categories().get(category, [])
                        if not names:
                            return f"لا توجد مهارات في '{category}'."
                        sample = names[:top_k]
                        return f"**{category}** ({len(names)}): " + ", ".join(sample)
                    results = searcher.search(query, top_k, category=category)
                    if not results:
                        return f"لا توجد مهارات مطابقة في '{category}'."
                    out = [f"**نتائج '{query}' في فئة '{category}':**"]
                    for name, score, desc in results:
                        out.append(f"- {name} (تطابق {score:.2f}): {desc}")
                    return "\n".join(out)

                tools = {
                    "list_skills": list_skills,
                    "load_skill": load_skill,
                    "search_skills": search_skills,
                    "list_skill_categories": list_skill_categories,
                    "search_skills_by_category": search_skills_by_category,
                }

                for tool_name, fn in tools.items():
                    if cls._registration_state[tool_name]:
                        continue
                    try:
                        registry.register(tool_name, fn)
                        cls._registration_state[tool_name] = True
                    except Exception as e:
                        print(f"⚠️ فشل تسجيل {tool_name}: {e}")

                if all(cls._registration_state.values()):
                    print("✅ سُجلت أدوات المهارات الخمسة.")
                return True
            except Exception as e:
                print(f"❌ فشل تسجيل أدوات المهارات: {e}")
                return False

    @classmethod
    def ensure_registered(cls, registry=None) -> None:
        if not all(cls._registration_state.values()):
            cls.register(registry)

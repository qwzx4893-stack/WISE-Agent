from pathlib import Path
from typing import Optional, Tuple
from .indexer import SkillIndexer
from .config import SkillConfig

class SkillLoader:
    def __init__(self):
        self.indexer = SkillIndexer()
        self.config = SkillConfig()
        self.max_length = self.config.get("max_skill_length", 4000)

    def load(self, skill_name: str) -> Tuple[Optional[str], bool]:
        info = self.indexer.get_skill_info(skill_name)
        if not info:
            return None, False
        skill_md = Path(info["path"]) / "SKILL.md"
        try:
            content = skill_md.read_text(encoding='utf-8')
            if len(content) > self.max_length:
                truncated = content[:self.max_length]
                last_para = truncated.rfind('\n\n')
                if last_para > self.max_length // 2:
                    truncated = truncated[:last_para]
                truncated += f"\n\n---\n⚠️ تم اقتطاع {len(content)-len(truncated)} حرف."
                return truncated, True
            return content, False
        except Exception as e:
            return f"❌ فشل التحميل: {e}", False

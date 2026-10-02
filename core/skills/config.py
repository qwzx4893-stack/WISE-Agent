import json
import threading
from pathlib import Path
from typing import Any, Dict

class SkillConfig:
    _instance = None
    _lock = threading.Lock()
    _config: Dict[str, Any] = {}

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._load()
        return cls._instance

    def _load(self):
        from ..paths import resolve_config_file
        config_path = resolve_config_file("skills_config.json")
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except Exception as e:
            import logging
            logging.getLogger("agent_os.skills").warning("Failed to load skills_config.json (%s); using defaults", e)
            self._config = {
                "max_skill_length": 4000,
                "index_refresh_interval": 300,
                "embedding_model": "all-MiniLM-L6-v2",
                "cache_embeddings": True,
                "embeddings_cache_file": ".embeddings_cache.npz"
            }

    def get(self, key: str, default=None):
        return self._config.get(key, default)

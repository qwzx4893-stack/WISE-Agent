import json
from pathlib import Path
from .registry import ToolRegistry
from .manifest import ToolManifest

class ToolLoader:
    @staticmethod
    def load_from_pack(registry: ToolRegistry, pack_file: str):
        with open(pack_file, 'r') as f:
            tools_data = json.load(f)
        for data in tools_data:
            manifest = ToolManifest(**data)
            registry.register(manifest)

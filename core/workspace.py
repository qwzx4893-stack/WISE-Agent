from pathlib import Path

class Workspace:
    ROOT = Path("~/agent-os/workspace").expanduser().resolve()
    ROOT.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve(cls, path: str) -> Path:
        target = (cls.ROOT / path).resolve()
        if cls.ROOT not in target.parents and target != cls.ROOT:
            raise ValueError("OUTSIDE WORKSPACE BLOCKED")
        return target

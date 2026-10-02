"""
Optional skill-reorganisation helper.

Moves skills from the legacy layout

    skills/<name>/<version>/SKILL.md

to the nested layout

    skills/<category>/<name>/<version>/SKILL.md

The script is **opt-in** — running ``python -m core.skills.reorganize``
prints a dry-run plan; pass ``--apply`` to actually move directories. The
indexer accepts both layouts so partial migrations are safe.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Dict, List

from .categories import infer_category
from .indexer import _KNOWN_CATEGORIES
from ..paths import SKILLS_DIR


def plan(skills_dir: Path = SKILLS_DIR) -> List[Dict[str, str]]:
    moves: List[Dict[str, str]] = []
    if not skills_dir.exists():
        return moves
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in _KNOWN_CATEGORIES:
            # Already a category folder — leave it alone.
            continue

        # Read metadata.json or top-level SKILL.md to infer category.
        metadata: Dict[str, str] = {}
        meta_path = entry / "metadata.json"
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        if not metadata:
            for sub in entry.rglob("metadata.json"):
                try:
                    metadata = json.loads(sub.read_text(encoding="utf-8"))
                    break
                except Exception:
                    continue

        cat = infer_category(
            name=entry.name,
            description=metadata.get("description", "") if metadata else "",
            path=entry,
            metadata=metadata or None,
        )
        moves.append({
            "name": entry.name,
            "category": cat,
            "from": str(entry),
            "to": str(skills_dir / cat / entry.name),
        })
    return moves


def apply(moves: List[Dict[str, str]]) -> List[Dict[str, str]]:
    done: List[Dict[str, str]] = []
    for m in moves:
        src = Path(m["from"])
        dst = Path(m["to"])
        if not src.exists():
            continue
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        done.append(m)
    return done


def _main() -> None:
    parser = argparse.ArgumentParser(description="Reorganise skills into the nested category layout.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually move directories (default: dry run).")
    args = parser.parse_args()

    moves = plan()
    print(json.dumps({"planned_moves": len(moves), "sample": moves[:10]},
                     indent=2, ensure_ascii=False))
    if not args.apply:
        print("Pass --apply to perform the moves.")
        return
    done = apply(moves)
    print(json.dumps({"applied": len(done)}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()

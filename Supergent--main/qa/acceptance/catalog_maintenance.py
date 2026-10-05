"""Deterministic catalog hygiene; archived removals remain recoverable.

No skill instructions or package lifecycle scripts are executed by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def skill_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^name:\s*['\"]?([^\r\n'\"]+)", text, re.M)
    if match:
        return match.group(1).strip()
    return path.parent.parent.name if re.fullmatch(r"\d+(?:\.\d+)*", path.parent.name) else path.parent.name


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(folder.rglob("*")):
        if not file.is_file() or file.is_symlink() or any(p.startswith('.') for p in file.relative_to(folder).parts):
            continue
        if file.name == "metadata.json":
            continue  # Imported catalog annotations are not skill payload.
        digest.update(file.relative_to(folder).as_posix().encode())
        digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def maintain(root: Path, apply: bool = False, replace_names=(), dedup_names=()) -> dict:
    root = root.resolve()
    skills = root / "skills"
    archive = root / "artifacts/catalog-backup-20261001" / uuid.uuid4().hex[:10]
    removed = []
    seen = {}
    all_skills = sorted(p for p in skills.rglob("SKILL.md") if not any(x.startswith('.') for x in p.relative_to(skills).parts))
    names = {}
    preferred = {}
    for md in all_skills:
        name = skill_name(md)
        if name in dedup_names:
            old = preferred.get(name)
            score = (md.stat().st_size, len(list(md.parent.rglob("*"))))
            if old is None or score > old[0]:
                preferred[name] = (score, md)
    for md in all_skills:
        folder = md.parent.resolve()
        if skills not in folder.parents:
            raise ValueError("Skill target outside catalog")
        name = skill_name(md)
        names.setdefault(name, []).append(str(md.relative_to(skills)))
        reason = None
        text = md.read_text(encoding="utf-8", errors="replace").strip()
        if name in replace_names:
            reason = "replaced_by_pinned_official_package"
        elif name in preferred and preferred[name][1] != md:
            reason = "redundant_same_purpose_skill"
        elif len(text) < 60:
            reason = "empty_or_incomplete_skill"
        elif len(text) < 200 and not text.startswith("---"):
            reason = "command_stub_without_procedural_guidance"
        else:
            fingerprint = tree_hash(folder)
            if fingerprint in seen:
                reason = "identical_payload_duplicate"
            else:
                seen[fingerprint] = str(folder.relative_to(skills))
        if reason:
            rel = folder.relative_to(skills)
            removed.append({"path": str(rel), "name": name, "reason": reason})
            if apply:
                destination = archive / "skills" / rel
                if archive not in destination.resolve().parents or destination.exists():
                    raise ValueError("Unsafe or conflicting archive target")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(destination))
    report = {"before": len(all_skills), "removed": removed,
              "remaining": len(all_skills) - len(removed),
              "duplicate_names": {k: v for k, v in names.items() if len(v) > 1},
              "archive": str(archive), "applied": apply}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace", nargs="*", default=[])
    parser.add_argument("--dedup-names", nargs="*", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = maintain(ROOT, args.apply, args.replace, args.dedup_names)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("before", "remaining", "applied")}))
    print("Disposition counts:", {r: sum(x["reason"] == r for x in report["removed"]) for r in {x["reason"] for x in report["removed"]}})


if __name__ == "__main__":
    main()

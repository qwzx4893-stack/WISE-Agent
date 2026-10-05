"""
Skill deduplication analyser.

Group functionally similar skills via TF-IDF + cosine similarity and pick
the single best representative per cluster. The other members are flagged
for archival to ``skills/.archive/<original-relative-path>``.

Default mode is **dry-run**: writes ``skills/.dedup_report.json`` and never
moves files. Pass ``--apply`` (or call ``dedup_skills(apply=True)``) to
actually relocate the duplicates after a manual review.

The implementation is intentionally dependency-free (no scikit-learn) so
the same code works inside the rootfs sandbox.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..paths import SKILLS_DIR

#: Words removed before vectorisation. Keep this short — the goal is to
#: drop English/Arabic glue, not to hand-tune a domain vocabulary.
_STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "you", "your", "our", "their", "this",
    "that", "these", "those", "from", "into", "over", "under", "without",
    "within", "between", "across", "about", "above", "below", "after",
    "before", "during", "while", "until", "since", "though", "although",
    "but", "yet", "still", "just", "only", "also", "such", "very", "much",
    "more", "less", "than", "then", "when", "where", "what", "which", "how",
    "why", "are", "was", "were", "been", "being", "has", "have", "had",
    "did", "doing", "does", "can", "will", "would", "should", "could",
    "may", "might", "must", "shall", "use", "used", "using", "uses",
    "via", "based", "build", "builds", "built", "make", "makes", "made",
    "skill", "skills", "agent", "agents",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


# --------------------------------------------------------------------------
# Tokenisation + TF-IDF
# --------------------------------------------------------------------------
def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOPWORDS]


def _doc_text(skill: Dict[str, Any], skill_dir: Path) -> str:
    parts: List[str] = [
        skill.get("name") or "",
        skill.get("description") or "",
        skill.get("excerpt") or "",
    ]
    md = skill_dir / "SKILL.md"
    if md.exists():
        try:
            parts.append(md.read_text(encoding="utf-8", errors="replace")[:4096])
        except Exception:
            pass
    return "\n".join(parts)


def _build_tfidf(docs: List[List[str]]) -> List[Dict[str, float]]:
    df: Counter[str] = Counter()
    for tokens in docs:
        df.update(set(tokens))
    n = max(len(docs), 1)
    idf: Dict[str, float] = {
        term: math.log((n + 1) / (count + 1)) + 1.0
        for term, count in df.items()
    }
    vectors: List[Dict[str, float]] = []
    for tokens in docs:
        if not tokens:
            vectors.append({})
            continue
        tf: Counter[str] = Counter(tokens)
        length = float(len(tokens))
        vec: Dict[str, float] = {}
        norm_acc = 0.0
        for term, count in tf.items():
            w = (count / length) * idf.get(term, 1.0)
            vec[term] = w
            norm_acc += w * w
        norm = math.sqrt(norm_acc) or 1.0
        for term in vec:
            vec[term] /= norm
        vectors.append(vec)
    return vectors


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(term, 0.0) for term, w in a.items())


# --------------------------------------------------------------------------
# Quality scoring
# --------------------------------------------------------------------------
def _quality_score(skill: Dict[str, Any], skill_dir: Path) -> float:
    """Score a skill so we can pick the best representative of a cluster.

    Heavier weight on documentation completeness (metadata, description
    length, README presence) so cluster winners are the most usable variant.
    """
    score = 0.0
    if skill.get("description"):
        score += min(len(skill["description"]) / 200.0, 1.0)
    if skill.get("excerpt"):
        score += min(len(skill["excerpt"]) / 600.0, 0.5)

    metadata = skill.get("metadata") or {}
    if isinstance(metadata, dict):
        score += 0.4 * sum(
            1
            for k in ("author", "license", "version", "category", "capabilities")
            if metadata.get(k)
        )
        # Bonus when capabilities/use_cases are populated arrays.
        for k in ("capabilities", "use_cases"):
            v = metadata.get(k)
            if isinstance(v, list) and v:
                score += min(len(v) / 8.0, 0.4)

    md = skill_dir / "SKILL.md"
    if md.exists():
        try:
            length = md.stat().st_size
            score += min(length / 4000.0, 1.2)
            text = md.read_text(encoding="utf-8", errors="replace")
            heading_count = sum(1 for ln in text.splitlines() if ln.startswith("##"))
            score += min(heading_count / 5.0, 0.5)
        except Exception:
            pass

    if (skill_dir / "metadata.json").exists():
        score += 0.5
    if (skill_dir / "README.md").exists():
        score += 0.3
    if (skill_dir / "tests").exists():
        score += 0.2

    # Dependency availability: skills whose dependencies map to known
    # auto-installable tools score higher than ones that point at
    # nothing/unknown packages.
    deps = []
    if isinstance(metadata, dict):
        deps = metadata.get("dependencies") or skill.get("dependencies") or []
    if isinstance(deps, list) and deps:
        installable = _count_installable_deps(deps)
        score += 0.3 * (installable / max(len(deps), 1))

    last = skill.get("last_modified") or 0.0
    if last:
        score += min(last / 1.0e10, 0.2)
    return score


_TOOL_PACKAGES_CACHE: Optional[Dict[str, Any]] = None


def _load_tool_packages() -> Dict[str, Any]:
    global _TOOL_PACKAGES_CACHE
    if _TOOL_PACKAGES_CACHE is not None:
        return _TOOL_PACKAGES_CACHE
    try:
        path = Path(__file__).resolve().parents[2] / "core" / "tool_packages.json"
        _TOOL_PACKAGES_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _TOOL_PACKAGES_CACHE = {}
    return _TOOL_PACKAGES_CACHE


def _count_installable_deps(deps: List[Any]) -> int:
    table = _load_tool_packages()
    if not table:
        # Conservative: when the curated map can't be read, treat every
        # dependency as installable so we don't penalise innocent skills.
        return len(deps)
    found = 0
    for d in deps:
        key = (d if isinstance(d, str) else (d.get("name") if isinstance(d, dict) else "")) or ""
        key = key.strip().lower()
        if not key:
            continue
        if key in table:
            found += 1
            continue
        # Heuristic fallback: any pip/apt/npm name with no spaces is
        # likely installable.
        if " " not in key and len(key) <= 64:
            found += 1
    return found


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
@dataclass
class DedupGroup:
    keep: str
    archive: List[str]
    similarity: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "keep": self.keep,
            "archive": self.archive,
            "similarity": round(self.similarity, 4),
        }


def analyse(index: Optional[Dict[str, Dict[str, Any]]] = None,
            *,
            similarity_threshold: float = 0.82,
            min_tokens: int = 6) -> List[DedupGroup]:
    """Group similar skills and return one ``DedupGroup`` per cluster."""
    if index is None:
        from .indexer import SkillIndexer
        index = SkillIndexer().get_index()

    names = list(index.keys())
    docs: List[List[str]] = []
    for name in names:
        skill = index[name]
        skill_dir = Path(skill.get("path") or SKILLS_DIR / name)
        docs.append(_tokenize(_doc_text(skill, skill_dir)))

    vectors = _build_tfidf(docs)

    parent = list(range(len(names)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    pair_sim: Dict[Tuple[int, int], float] = {}
    for i in range(len(names)):
        if len(docs[i]) < min_tokens:
            continue
        for j in range(i + 1, len(names)):
            if len(docs[j]) < min_tokens:
                continue
            sim = _cosine(vectors[i], vectors[j])
            if sim >= similarity_threshold:
                union(i, j)
                pair_sim[(i, j)] = sim

    clusters: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(names)):
        clusters[find(i)].append(i)

    groups: List[DedupGroup] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        scored = sorted(
            members,
            key=lambda i: _quality_score(index[names[i]], Path(index[names[i]]["path"])),
            reverse=True,
        )
        keep_idx = scored[0]
        archive_idx = scored[1:]
        # Average pairwise similarity within the cluster (rough quality of grouping).
        sims = [
            pair_sim.get(tuple(sorted((a, b))), 0.0)
            for a in members for b in members if a < b
        ]
        avg = sum(sims) / max(len(sims), 1)
        groups.append(DedupGroup(
            keep=names[keep_idx],
            archive=[names[i] for i in archive_idx],
            similarity=avg,
        ))
    groups.sort(key=lambda g: (-len(g.archive), -g.similarity))
    return groups


def quality_score(skill: Dict[str, Any], skill_dir: Optional[Path] = None) -> float:
    """Public wrapper around the internal scorer used by tests and tooling."""
    return _quality_score(skill, skill_dir or Path(skill.get("path") or "."))


def dedup_skills(*,
                 apply: bool = False,
                 similarity_threshold: float = 0.88,
                 min_quality: float = 0.0,
                 skills_dir: Optional[Path] = None,
                 report_path: Optional[Path] = None) -> Dict[str, Any]:
    """High-level entry point used by both the CLI and tests.

    By default this is a *dry run* — it writes a JSON report and returns it.
    When ``apply=True`` it additionally moves discarded duplicates into
    ``skills/.archive/<orig-relative>/`` so they remain on disk but are not
    indexed.

    Parameters
    ----------
    similarity_threshold:
        Cosine threshold for grouping. Defaults to ``0.88`` (conservative)
        — lower values risk merging unrelated skills.
    min_quality:
        Skills whose quality score is **strictly less than** this value are
        archived alongside duplicates (only when ``apply=True``). Defaults
        to ``0.0`` (disabled).
    """
    sk_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
    sk_dir.mkdir(parents=True, exist_ok=True)
    archive_root = sk_dir / ".archive"

    from .indexer import SkillIndexer
    if skills_dir:
        # Tests pass an isolated dir; build a fresh ad-hoc index.
        index = _walk_index(sk_dir)
    else:
        index = SkillIndexer().get_index()

    groups = analyse(index, similarity_threshold=similarity_threshold)

    # Identify low-quality skills that aren't already cluster-archive candidates.
    archive_already: set = set()
    for g in groups:
        archive_already.update(g.archive)
    low_quality: List[Dict[str, Any]] = []
    if min_quality > 0:
        for name, skill in index.items():
            if name in archive_already:
                continue
            sk_path = Path(skill.get("path") or sk_dir / name)
            score = _quality_score(skill, sk_path)
            if score < min_quality:
                low_quality.append({"name": name, "score": round(score, 3)})

    moved: List[Dict[str, str]] = []
    if apply:
        archive_root.mkdir(parents=True, exist_ok=True)
        targets: List[str] = []
        for group in groups:
            targets.extend(group.archive)
        for entry in low_quality:
            targets.append(entry["name"])
        for victim in targets:
            info = index.get(victim)
            if not info:
                continue
            src = Path(info["path"])
            if not src.exists():
                continue
            try:
                rel = src.relative_to(sk_dir)
            except ValueError:
                rel = Path(victim)
            dst = archive_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                continue
            try:
                shutil.move(str(src), str(dst))
            except Exception as exc:  # pragma: no cover
                continue
            moved.append({"name": victim, "from": str(src), "to": str(dst)})

    report = {
        "total_skills": len(index),
        "clusters": len(groups),
        "duplicate_skills": sum(len(g.archive) for g in groups),
        "low_quality_skills": len(low_quality),
        "low_quality_names": [e["name"] for e in low_quality[:200]],
        "groups": [g.to_dict() for g in groups],
        "applied": bool(apply),
        "moved": moved,
        "similarity_threshold": similarity_threshold,
        "min_quality": min_quality,
    }

    out = Path(report_path) if report_path else sk_dir / ".dedup_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return report


# --------------------------------------------------------------------------
# Helpers used by tests / standalone CLI
# --------------------------------------------------------------------------
def _walk_index(root: Path) -> Dict[str, Dict[str, Any]]:
    """Build a minimal index for a directory without going through the
    singleton SkillIndexer (handy in tests with a temp dir)."""
    out: Dict[str, Dict[str, Any]] = {}
    for skill_md in root.rglob("SKILL.md"):
        rel = skill_md.parent.relative_to(root)
        if rel.parts and rel.parts[0].startswith("."):
            continue
        name = "__".join(rel.parts) or "unknown"
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        first = (text.splitlines() or [""])[0].lstrip("# ").strip() or name
        out[name] = {
            "name": name,
            "version": "1.0.0",
            "description": first[:200],
            "excerpt": text[:300],
            "path": str(skill_md.parent),
            "last_modified": 0.0,
            "metadata": {},
        }
    return out


def _main() -> None:
    parser = argparse.ArgumentParser(description="Skill deduplication analyser.")
    parser.add_argument("--apply", action="store_true",
                        help="Move duplicates into skills/.archive/ (default: dry run).")
    parser.add_argument("--threshold", type=float, default=0.88)
    parser.add_argument("--min-quality", type=float, default=0.0,
                        help="Archive skills below this quality score (only with --apply).")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    report = dedup_skills(apply=args.apply,
                          similarity_threshold=args.threshold,
                          min_quality=args.min_quality,
                          report_path=args.report)
    print(json.dumps({
        "applied": report["applied"],
        "total_skills": report["total_skills"],
        "clusters": report["clusters"],
        "duplicate_skills": report["duplicate_skills"],
        "report_path": str(args.report or (SKILLS_DIR / '.dedup_report.json')),
    }, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()

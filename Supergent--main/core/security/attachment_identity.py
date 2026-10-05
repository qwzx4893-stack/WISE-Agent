"""Identity obligations for an explicit in-place edit of uploaded originals.

Derive only from the current trusted human goal/upload envelope. This grants no
filesystem permission: the canonical router/security gate still authorizes all
operations. A copied basename is not a successful modification of an upload.
"""
from pathlib import Path
import re


def in_place_attachments(goal):
    marker = "\n\nAttached local files:\n"
    if not isinstance(goal, str) or marker not in goal:
        return ()
    human, envelope = goal.split(marker, 1)
    edits = re.search(r"\b(?:edit|modify|fix|repair|replace|update)\b[^\n.!?;]{0,100}\b(?:attached|uploaded|imported)\b"
                      r"|(?:عدل|عدّل|أصلح|اصلح)[^\n.!?;]{0,100}(?:المرفق|المستورد)", human, re.I)
    if not edits or re.search(r"\b(?:do not|don't|never|must not)\s*$|لا\s*$", human[max(0, edits.start()-40):edits.start()], re.I):
        return ()
    if not re.search(r"\bin[- ]place\b|\b(?:actual|same|original)\s+(?:attached|uploaded|imported|workspace)\b"
                     r"|\bnot\s+(?:a\s+)?(?:new\s+)?copy\b|الملف\s+(?:الأصلي|الاصلي|نفسه)|ليس\s+نسخة", human, re.I):
        return ()
    from core.tools_bridge import WORKSPACE_DIR
    root = WORKSPACE_DIR.resolve()
    attachment_root = (root / "attachments").resolve()
    if root not in attachment_root.parents or attachment_root != root / "attachments":
        return ()  # An upload-directory junction is not a verified envelope root.
    rows = []
    for line in envelope.splitlines()[:100]:
        if not line.startswith("- ") or ": " not in line:
            continue
        label, raw_path = line[2:].split(": ", 1)
        if Path(label).name != label or not label.strip():
            continue
        target = Path(raw_path.strip())
        if not target.is_absolute():
            continue
        try:
            resolved = target.resolve()
            if resolved.parent != attachment_root or not resolved.is_file() or target.is_symlink():
                continue
            relative = str(resolved.relative_to(root)).replace("\\", "/")
        except (OSError, ValueError):
            continue
        rows.append({"label": label, "path": relative})
    mentioned = [row for row in rows if re.search(r"(?<![\w.-])" + re.escape(row["label"]) + r"(?![\w.-])", human, re.I)]
    # Do not guess a subset of ambiguous unnamed uploads. A single upload or
    # explicitly named uploads supplies a narrow concrete identity obligation.
    selected = mentioned or (rows if len(rows) == 1 else [])
    return tuple({(row["label"], row["path"]): row for row in selected}.values())


def canonical_identity_path(path, bindings):
    if not isinstance(path, str):
        return path
    from core.tools_bridge import _resolve
    try:
        target = _resolve(path)
        for row in bindings:
            if target == _resolve(row["path"]):
                return row["path"]
    except (TypeError, ValueError, OSError):
        pass
    return path


def misplaced_in_place_write(path, bindings, authorized_scope):
    if not bindings:
        return False
    from core.tools_bridge import _resolve
    try:
        target = _resolve(path)
        if any(target == _resolve(row["path"]) for row in bindings):
            return False
        # Other explicitly requested artifacts remain permitted, but a root
        # basename mentioned as the upload label never substitutes for it.
        if str(target) in authorized_scope and not any(target.name == row["label"] for row in bindings):
            return False
    except (TypeError, ValueError, OSError):
        pass
    return True


def assess_identity_outcome(plan, calls, bindings, assess):
    normalized = []
    for row in calls:
        if row.get("in_place_target_rejected"):
            continue  # No artifact at that copied target was authorized/written.
        normalized.append({**row, "path": canonical_identity_path(row.get("path"), bindings)})
    status = assess(plan, normalized)
    required = {row["path"] for row in bindings}
    missing = required - set(status["verified_artifacts"])
    status["unverified_artifacts"] = sorted(set(status["unverified_artifacts"]) | missing)
    status["artifact_obligations_met"] = status["artifact_obligations_met"] and not missing
    status["in_place_attachment_obligations"] = {"required_originals": sorted(required),
        "unverified_originals": sorted(missing), "satisfied": not missing,
        "rejected_copy_targets": [row.get("path") for row in calls if row.get("in_place_target_rejected")]}
    return status

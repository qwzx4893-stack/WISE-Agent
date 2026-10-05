"""Derive a narrow artifact-write scope from the CURRENT human goal only."""
import re
from pathlib import Path

_EXCLUDED={".git",".tooling","memory","sessions","config",".venv","node_modules"}
_WRITE=r"\b(?:create|write|save|edit|fix|repair|replace|modify|update|build)\b|أنشئ|انشئ|اكتب|احفظ|عدل|عدّل|أصلح|اصلح|استبدل"
_NAMED_FILE=r"(?<![\w])(?:[\w.-]+[/\\])*[\w.-]+\.(?:json|md|txt|csv|py|html|css|js)(?![\w])"


def canonical_artifact_path(path):
    """Normalize a workspace identity, without granting permission or writing."""
    if not isinstance(path, str) or not path.strip():
        return path
    from core.tools_bridge import WORKSPACE_DIR
    root = WORKSPACE_DIR.resolve()
    try:
        target = (root / path).resolve()
        if root not in target.parents:
            return path
        return target.relative_to(root).as_posix()
    except (ValueError, OSError):
        return path


def required_artifacts(goal, *, role_instruction=""):
    """Derive only mandatory, directly named outputs from the current human.

    This conservative parser is not general natural-language intent inference.
    Permissions remain separate: optional/alternative/conditional filenames,
    examples, input paths and upload labels are not new output obligations.
    The caller must never pass model plans, history or remote tool content here.
    Original in-place uploads retain their separate attachment identity contract.
    """
    if not isinstance(goal, str) or (role_instruction and not role_instruction.startswith("executor:")):
        return ()
    human = goal.split("\n\nAttached local files:\n", 1)[0]
    # Human filenames in quotes/backticks remain eligible. Whole reference
    # instructions in code fences, blockquotes or inline code do not.
    human = re.sub(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)", " ", human)
    human = re.sub(r"(?m)^\s*>[^\n]*", " ", human)
    def unquote(match):
        text = match.group(1)
        return " " if re.search(_WRITE, text, re.I) and re.search(r"\s", text) else text
    human = re.sub(r"`([^`\n]*)`", unquote, human)
    human = re.sub(r'"([^"\n]*)"', unquote, human)
    human = re.sub(r"(?<!\w)'([^'\n]+)'(?!\w)", unquote, human)
    allowed = set(authorized_artifacts(goal))
    from core.tools_bridge import WORKSPACE_DIR
    from core.security.attachment_identity import in_place_attachments
    root = WORKSPACE_DIR.resolve()
    bindings = in_place_attachments(goal)
    original_labels = {row["label"].casefold() for row in bindings}
    original_paths = {row["path"] for row in bindings}
    result = set()
    clauses = re.split(r"[\n;!?]|(?<=[.!?])\s+|(?<!\w)(?:then|but|ثم)(?!\w)", human, flags=re.I)
    optional = (r"\b(?:may|can|could|optional|optionally|either|if|unless|when|consider|suggest)\b"
                r"|\b(?:do not|don't|never|must not|how to|example|read.only)\b"
                r"|لا\s+(?:تعدل|تكتب|تغير|تمس|تنشئ)|يمكنك|اختياريا|اختياريًا|إذا|اذا")
    non_object = r"\b(?:from|using|based|source|reference|referring|read|inspect|review|fetch|research|search|explain)\b|اقرأ|افحص|مصدر"
    for clause in clauses:
        file_spans = [(match.start(), match.end()) for match in re.finditer(_NAMED_FILE, clause, re.I)]
        commands = [match for match in re.finditer(_WRITE, clause, re.I)
                    if not any(start <= match.start() < end for start, end in file_spans)]
        for index, command in enumerate(commands):
            if re.search(optional, re.sub(_NAMED_FILE, " ", clause[:command.start()], flags=re.I), re.I):
                continue
            end = commands[index + 1].start() if index + 1 < len(commands) else len(clause)
            segment = clause[command.end():end]
            if re.match(r"\s*(?:either\b|اختياريا|اختياريًا)", segment, re.I):
                continue
            previous_output_end = None
            for name in re.finditer(_NAMED_FILE, segment, re.I):
                before = segment[:name.start()]
                before_text = re.sub(_NAMED_FILE, " ", before, flags=re.I)
                after = segment[name.end():]
                # Alternative outputs (or chat instead) and immediately
                # conditional writes are not mandatory. Content qualifiers
                # later in the sentence, e.g. 'source if applicable', are not
                # confused with an optional output path.
                if re.match(r"\s*(?:or\b|if\b|unless\b|when\b|أو|او|إذا|اذا)", after, re.I):
                    continue
                listed = previous_output_end is not None and bool(re.fullmatch(
                    r"\s*(?:(?:,|and\b|و)\s*)+", segment[previous_output_end:name.start()], re.I))
                earlier_names = list(re.finditer(_NAMED_FILE, before, re.I))
                direct = not earlier_names and len(before) <= 100 and not re.search(non_object, before, re.I)
                destination = bool(re.search(r"\b(?:to|into|as)\s*$", before, re.I)) and not re.search(
                    r"\b(?:source|reference|referring|read|inspect|review|fetch|research|search|explain)\b", before_text, re.I)
                if not (listed or direct or destination) or re.search(r"\bor\b|(?<!\w)(?:أو|او)(?!\w)", before_text, re.I):
                    continue
                try:
                    target = (root / name.group()).resolve()
                    canonical = canonical_artifact_path(str(target))
                    if str(target) not in allowed:
                        continue
                    if target.name.casefold() in original_labels and canonical not in original_paths:
                        continue
                except (OSError, ValueError):
                    continue
                result.add(canonical)
                previous_output_end = name.end()
    return tuple(sorted(result))

def authorized_artifacts(goal):
    from core.tools_bridge import WORKSPACE_DIR
    root=WORKSPACE_DIR.resolve()
    # Human-mentioned relative file names plus trusted attachment-envelope paths.
    pattern=r"(?<![\w])(?:[\w.-]+[/\\])*[\w.-]+\.(?:json|md|txt|csv|py|html|css|js)(?![\w])"
    names=[]
    denied=set()
    for match in re.finditer(pattern,goal,re.I):
        preceding=goal[max(0,match.start()-180):match.start()]
        clause=re.split(r"[\n;!?]|(?<=[.!?])\s+|(?<!\w)(?:then|but)(?!\w)",preceding,flags=re.I)[-1]
        # Fail closed for negative/read-only clauses; filenames are not grants
        # merely because the user mentions them. Remote content never enters here.
        if re.search(r"\b(?:do not|don't|never|must not|read.only|without (?:editing|modifying))\b|لا\s+(?:تعدل|تكتب|تغير|تمس)|دون\s+(?:تعديل|تغيير)",clause,re.I):
            denied.add(match.group().casefold())
        elif re.search(_WRITE,clause,re.I):
            names.append(match.group())
    if "\n\nAttached local files:\n" in goal:
        human,envelope=goal.split("\n\nAttached local files:\n",1)
        # Uploading/mentioning a file for reading does not grant modification.
        if re.search(r"\b(?:edit|modify|fix|repair|replace|update)\b[^\n.!?;]{0,100}\b(?:attached|uploaded|imported)\b|(?:عدل|عدّل|أصلح|اصلح)[^\n.!?;]{0,100}(?:المرفق|المستورد)",human,re.I):
            for line in envelope.splitlines():
                if line.startswith("- ") and ": " in line: names.append(line.split(": ",1)[1].strip())
    result=[]
    for name in names[:100]:
        if name.casefold() in denied: continue
        target=(root/name).resolve()
        if root not in target.parents: continue
        relative=target.relative_to(root)
        if any(part.lower() in _EXCLUDED for part in relative.parts): continue
        if target.suffix.lower() not in {".json",".md",".txt",".csv",".py",".html",".css",".js"}: continue
        result.append(str(target))
    return tuple(sorted(set(result)))

def scoped_workspace_write(params,scope):
    from core.tools_bridge import _resolve,WORKSPACE_DIR
    try:
        target=_resolve(params.get("path", ""))
        if str(target) not in scope: return False
        return not any(part.lower() in _EXCLUDED for part in target.relative_to(WORKSPACE_DIR.resolve()).parts)
    except (TypeError,ValueError,OSError): return False

"""Derive a narrow artifact-write scope from the CURRENT human goal only."""
import re
from pathlib import Path

_EXCLUDED={".git",".tooling","memory","sessions","config",".venv","node_modules"}
_WRITE=r"\b(?:create|write|save|edit|fix|repair|replace|modify|update|build)\b|أنشئ|انشئ|اكتب|احفظ|عدل|عدّل|أصلح|اصلح|استبدل"

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

"""Document generation pipeline.

Inspired by Accomplish's open-source desktop document tooling
(https://github.com/accomplish-ai/accomplish — MIT). All code in this
module is **original** Python wrapping ``python-docx``, ``openpyxl``,
and ``python-pptx``. We provide a tiny, opinionated API focused on
the three deliverables an agent typically generates from a research
session:

* :func:`create_report`        — Word document (`.docx`)
* :func:`create_spreadsheet`   — Workbook (`.xlsx`)
* :func:`create_presentation`  — Slide deck (`.pptx`)

Inputs are JSON-friendly so the ReAct loop can call these tools with
plain dicts. Outputs go to ``WORKSPACE_DIR/documents/`` by default;
callers may override the destination directory or filename.

Imports of ``docx`` / ``openpyxl`` / ``pptx`` are deferred so the
module is importable even when the libs are missing — :func:`available`
exposes the dependency status to callers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import WORKSPACE_DIR


@dataclass
class DocResult:
    kind: str
    path: str
    bytes: int
    duration_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "path": self.path,
                 "bytes": self.bytes, "duration_ms": self.duration_ms}


def _docs_dir() -> Path:
    out = WORKSPACE_DIR / "documents"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_path(name: str, ext: str,
                   directory: Optional[str | Path] = None) -> Path:
    base = Path(directory) if directory else _docs_dir()
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_. ")
    safe = (safe or "document").strip().replace(" ", "_")
    if not safe.endswith(ext):
        safe += ext
    return base / safe


def available() -> Dict[str, bool]:
    """Return availability of each backend so callers can degrade gracefully."""
    out = {"docx": False, "xlsx": False, "pptx": False}
    try:
        import docx  # noqa: F401
        out["docx"] = True
    except Exception:
        pass
    try:
        import openpyxl  # noqa: F401
        out["xlsx"] = True
    except Exception:
        pass
    try:
        import pptx  # noqa: F401
        out["pptx"] = True
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Reports (.docx)
# ---------------------------------------------------------------------------
def create_report(*, title: str,
                   sections: List[Dict[str, Any]],
                   filename: Optional[str] = None,
                   directory: Optional[str | Path] = None) -> DocResult:
    """Generate a Word document.

    ``sections`` is a list of ``{"heading": str, "body": str | List[str]}``
    objects. Body may also be a list of strings (rendered as bullets) or
    a list of dicts ``{"bullet": "..."}``.
    """
    try:
        from docx import Document
    except Exception as e:
        raise RuntimeError(f"python-docx not installed: {e}") from e

    t0 = time.time()
    doc = Document()
    doc.add_heading(title or "Report", level=0)
    for sec in sections or []:
        heading = sec.get("heading", "").strip()
        if heading:
            doc.add_heading(heading, level=1)
        body = sec.get("body", "")
        if isinstance(body, str):
            for para in body.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        elif isinstance(body, list):
            for item in body:
                text = item.get("bullet") if isinstance(item, dict) else item
                doc.add_paragraph(str(text), style="List Bullet")
    out = _resolve_path(filename or title or "report", ".docx", directory)
    doc.save(str(out))
    return DocResult("docx", str(out), out.stat().st_size,
                       (time.time() - t0) * 1000.0)


# ---------------------------------------------------------------------------
# Spreadsheets (.xlsx)
# ---------------------------------------------------------------------------
def create_spreadsheet(*, title: str,
                        sheets: List[Dict[str, Any]],
                        filename: Optional[str] = None,
                        directory: Optional[str | Path] = None
                        ) -> DocResult:
    """Generate an Excel workbook.

    ``sheets`` is a list of ``{"name": str, "headers": List[str],
    "rows": List[List[Any]]}`` objects.
    """
    try:
        from openpyxl import Workbook
    except Exception as e:
        raise RuntimeError(f"openpyxl not installed: {e}") from e

    t0 = time.time()
    wb = Workbook()
    # openpyxl ships with a default sheet — use it for the first input.
    default = wb.active
    default.title = "Sheet1"
    default_used = False
    for sheet in sheets or []:
        name = (sheet.get("name") or "Sheet").strip()[:31] or "Sheet"
        if not default_used:
            ws = default
            ws.title = name
            default_used = True
        else:
            ws = wb.create_sheet(name)
        headers = sheet.get("headers") or []
        if headers:
            for c, h in enumerate(headers, start=1):
                ws.cell(row=1, column=c, value=h)
        for r, row in enumerate(sheet.get("rows") or [], start=2):
            for c, v in enumerate(row, start=1):
                ws.cell(row=r, column=c, value=v)
    out = _resolve_path(filename or title or "spreadsheet", ".xlsx",
                          directory)
    wb.save(str(out))
    return DocResult("xlsx", str(out), out.stat().st_size,
                       (time.time() - t0) * 1000.0)


# ---------------------------------------------------------------------------
# Presentations (.pptx)
# ---------------------------------------------------------------------------
def create_presentation(*, title: str,
                         slides: List[Dict[str, Any]],
                         filename: Optional[str] = None,
                         directory: Optional[str | Path] = None
                         ) -> DocResult:
    """Generate a PowerPoint deck.

    ``slides`` is a list of ``{"title": str, "bullets": List[str]}``.
    The first slide is rendered with the title-slide layout; the rest
    use the title-and-content layout.
    """
    try:
        from pptx import Presentation
    except Exception as e:
        raise RuntimeError(f"python-pptx not installed: {e}") from e

    t0 = time.time()
    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    content_layout = prs.slide_layouts[1]

    cover = prs.slides.add_slide(title_layout)
    cover.shapes.title.text = title or "Presentation"
    if cover.placeholders and len(cover.placeholders) > 1:
        cover.placeholders[1].text = ""

    for s in slides or []:
        slide = prs.slides.add_slide(content_layout)
        slide.shapes.title.text = s.get("title", "") or ""
        body_ph = None
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                body_ph = ph
                break
        bullets = s.get("bullets") or []
        if body_ph is not None and bullets:
            tf = body_ph.text_frame
            tf.text = str(bullets[0])
            for extra in bullets[1:]:
                p = tf.add_paragraph()
                p.text = str(extra)

    out = _resolve_path(filename or title or "presentation", ".pptx",
                          directory)
    prs.save(str(out))
    return DocResult("pptx", str(out), out.stat().st_size,
                       (time.time() - t0) * 1000.0)


__all__ = [
    "DocResult", "available",
    "create_report", "create_spreadsheet", "create_presentation",
]

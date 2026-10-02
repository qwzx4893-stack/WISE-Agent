"""Markitdown MCP Server for WISE.

Thin MCP server wrapping Microsoft's markitdown library to convert
documents (PDF, Word, Excel, PowerPoint, HTML, images, audio) to
Markdown via the Model Context Protocol.

Usage:
    python -m wise_markitdown_mcp

Registered in mcp_servers.json as "markitdown-mcp".
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from markitdown import MarkItDown

LOG = logging.getLogger("wise.markitdown_mcp")

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------

def _ok(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "convert_file",
        "description": (
            "Convert a local file (PDF, Word .docx, Excel .xlsx, PowerPoint .pptx, "
            "HTML, CSV, JSON, XML, ZIP, image, audio, or any text-based format) "
            "to clean Markdown. Returns the converted Markdown text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to convert.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "convert_url",
        "description": (
            "Fetch content from a URL and convert it to clean Markdown. "
            "Works with web pages, PDF links, and downloadable documents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and convert.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "convert_base64",
        "description": (
            "Convert a base64-encoded document to Markdown. "
            "Useful when the document content is available as a base64 string."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "Base64-encoded document content.",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename with extension (e.g., 'report.pdf').",
                },
            },
            "required": ["data", "filename"],
        },
    },
]

# Singleton converter
_md: Optional[MarkItDown] = None


def _get_converter() -> MarkItDown:
    global _md
    if _md is None:
        _md = MarkItDown()
    return _md


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_convert_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = args.get("file_path") or args.get("path") or args.get("input_path") or ""
    if not path or not Path(path).exists():
        return {"content": [{"type": "text", "text": f"Error: File not found: {path}"}], "isError": True}
    try:
        from core.security.security_gate import get_security_gate, SecurityContext
        decision = get_security_gate().evaluate("read_file", {"path": str(path)}, SecurityContext(caller="markitdown-mcp"))
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if not Path(path).is_file() or Path(path).stat().st_size > 25 * 1024 * 1024:
            raise ValueError("Expected a file no larger than 25 MiB")
        result = _get_converter().convert(str(path))
        return {"content": [{"type": "text", "text": result.text_content}]}
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"Error converting file: {exc}"}], "isError": True}


def _handle_convert_url(args: Dict[str, Any]) -> Dict[str, Any]:
    url = args.get("url", "")
    if not url:
        return {"content": [{"type": "text", "text": "Error: URL is required"}], "isError": True}
    try:
        result = _get_converter().convert(url)
        return {"content": [{"type": "text", "text": result.text_content}]}
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"Error converting URL: {exc}"}], "isError": True}


def _handle_convert_base64(args: Dict[str, Any]) -> Dict[str, Any]:
    data = args.get("data", "")
    filename = args.get("filename", "document.bin")
    if not data:
        return {"content": [{"type": "text", "text": "Error: data is required"}], "isError": True}
    try:
        if len(data) > 35 * 1024 * 1024:
            raise ValueError("Encoded document exceeds 35 MiB limit")
        raw = base64.b64decode(data, validate=True)
        ext = Path(filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            result = _get_converter().convert(tmp_path)
            return {"content": [{"type": "text", "text": result.text_content}]}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"Error converting base64 data: {exc}"}], "isError": True}


_TOOL_DISPATCH = {
    "convert_file": _handle_convert_file,
    "convert_base64": _handle_convert_base64,
}
TOOLS = [tool for tool in TOOLS if tool["name"] in _TOOL_DISPATCH]


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------

def handle_request(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = frame.get("method", "")
    rid = frame.get("id")
    params = frame.get("params", {})

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "markitdown-mcp", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        })

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = _TOOL_DISPATCH.get(tool_name)
        if not handler:
            return _ok(rid, {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}], "isError": True})
        result = handler(arguments)
        return _ok(rid, result)

    if method == "ping":
        return _ok(rid, {})

    return _error(rid, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# stdio main loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the markitdown MCP server over stdio (newline-delimited JSON-RPC)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(frame)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

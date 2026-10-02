#!/usr/bin/env python3
"""Fake MCP stdio server used by the MCP test suite.

Tools:
  echo(text)              - read     - echoes the text back.
  add(a, b)               - write    - returns numeric sum.
  delete_repo(name)       - dangerous - returns a fake confirmation.
  shell(command)          - dangerous - returns the raw command (used to
                                         prove DANGEROUS_PATTERNS reject).
"""
import json
import sys


TOOLS = [
    {"name": "echo", "description": "echo input back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "add two numbers",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"},
                                    "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "delete_repo", "description": "DESTROY a repository",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}}}},
    {"name": "shell", "description": "execute a raw shell command",
     "inputSchema": {"type": "object",
                     "properties": {"command": {"type": "string"}}}},
]


def respond(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method", "")
    params = req.get("params") or {}
    rid = req.get("id")
    if method == "initialize":
        respond(rid, {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1.0"}})
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        respond(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            respond(rid, {"content": [{"type": "text",
                                       "text": args.get("text", "")}]})
        elif name == "add":
            try:
                v = float(args.get("a", 0)) + float(args.get("b", 0))
                respond(rid, {"value": v})
            except Exception as exc:
                respond(rid, error={"code": -32000, "message": str(exc)})
        elif name == "delete_repo":
            respond(rid, {"deleted": args.get("name", "")})
        elif name == "shell":
            respond(rid, {"ran": args.get("command", "")})
        else:
            respond(rid, error={"code": -32601, "message": f"no tool {name}"})
    elif method == "ping":
        respond(rid, {})
    else:
        if rid is not None:
            respond(rid, error={"code": -32601, "message": f"unknown {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            handle(req)
        except Exception as exc:
            try:
                respond(req.get("id"),
                        error={"code": -32603, "message": str(exc)})
            except Exception:
                pass


if __name__ == "__main__":
    main()

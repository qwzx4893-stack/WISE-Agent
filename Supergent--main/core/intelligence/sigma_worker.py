"""Offline, bounded pySigma parsing. No SIEM, backend, pipeline or plugin loading."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import socket
import sys

MAX_INPUT = 256_000
MAX_DOCUMENTS = 32
EXCLUDED = {"memory", "sessions", ".git", ".tooling", ".venv", "node_modules"}
ALLOWED_MODIFIERS = {"contains", "startswith", "endswith", "all", "re", "windash", "cidr", "exists", "cased",
                     "base64", "base64offset", "utf16", "utf16le", "utf16be", "wide"}


def disable_network():
    def denied(*args, **kwargs): raise PermissionError("Offline Sigma worker networking disabled")
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, denied)
    socket.create_connection = denied
    socket.getaddrinfo = denied


def checked_path(root: Path, filename: str) -> Path:
    supplied = Path(filename)
    if supplied.is_symlink(): raise ValueError("Symlink rule input excluded")
    target = supplied.resolve()
    if root not in target.parents or not target.is_file(): raise ValueError("An authorized workspace rule file is required")
    if any(part.casefold() in EXCLUDED for part in target.relative_to(root).parts):
        raise ValueError("Sensitive/runtime rule path excluded")
    if target.suffix.casefold() not in {".yaml", ".yml"} or target.stat().st_size > MAX_INPUT:
        raise ValueError("A bounded YAML rule file is required")
    return target


def load_documents(text: str):
    import yaml
    from yaml.events import AliasEvent
    class BoundedSafeLoader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self.wise_nodes = 0
            self.wise_depth = 0

        def compose_node(self, parent, index):
            if self.check_event(AliasEvent): raise ValueError("YAML aliases are not accepted")
            self.wise_nodes += 1
            self.wise_depth += 1
            if self.wise_nodes > 10_000 or self.wise_depth > 20:
                raise ValueError("YAML node/depth budget exceeded")
            try: return super().compose_node(parent, index)
            finally: self.wise_depth -= 1

        def construct_mapping(self, node, deep=False):
            keys = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or len(key) > 200 or key in keys:
                    raise ValueError("Duplicate/non-string/oversized YAML key")
                keys.add(key)
            return super().construct_mapping(node, deep=deep)

    documents = []
    for document in yaml.load_all(text, Loader=BoundedSafeLoader):
        if len(documents) >= MAX_DOCUMENTS: raise ValueError("Sigma document budget exceeded")
        if not isinstance(document, dict): raise ValueError("Sigma rule documents must be mappings")
        documents.append(document)
    if not documents: raise ValueError("No Sigma rule document found")
    return documents


def _bounded_values(value, depth=0):
    if depth > 20: raise ValueError("Sigma value depth exceeded")
    if isinstance(value, str) and len(value) > 8192: raise ValueError("Sigma scalar budget exceeded")
    if isinstance(value, float) and not math.isfinite(value): raise ValueError("Nonfinite Sigma value excluded")
    if isinstance(value, dict):
        for key, child in value.items():
            if any(modifier not in ALLOWED_MODIFIERS for modifier in key.split("|")[1:]):
                raise ValueError("Unsupported Sigma modifier excluded")
            _bounded_values(child, depth + 1)
    elif isinstance(value, list):
        for child in value: _bounded_values(child, depth + 1)


def validate_text(text: str, limit=20):
    from sigma.rule import SigmaRule
    from sigma.exceptions import SigmaError
    documents = load_documents(text)
    records = []
    all_valid = True
    for index, document in enumerate(documents, 1):
        errors = []
        rule = None
        try:
            if "action" in document or "correlation" in document:
                raise ValueError("Collection/correlation rules are not supported by this adapter")
            _bounded_values(document)
            rule = SigmaRule.from_dict(document, collect_errors=True)
            errors.extend(type(error).__name__ for error in rule.errors)
            # Conditions are parsed lazily upstream. Force their syntax and
            # selector references without conversion, event matching or execution.
            if not errors:
                for condition in rule.detection.parsed_condition:
                    if len(condition.condition) > 1000: raise ValueError("Sigma condition budget exceeded")
                    _ = condition.parsed
        except (SigmaError, ValueError, TypeError, KeyError) as error:
            errors.append(type(error).__name__)
        valid = not errors
        all_valid = all_valid and valid
        record = {"document": index, "valid": valid, "diagnostics": sorted(set(errors))[:16]}
        if valid:
            record.update(id=str(rule.id) if rule.id else None,
                          status=rule.status.name if rule.status else None,
                          level=rule.level.name if rule.level else None,
                          detection_count=len(rule.detection.detections))
        records.append(record)
    return {"success": True, "resource_id": "sigma", "valid": all_valid,
            "records": records[:limit], "total": len(records), "limited": len(records) > limit,
            "parser": "pysigma", "parser_version": importlib.metadata.version("pysigma"),
            "coverage": "OFFLINE_SIGMA_PARSE_AND_CONDITION_VALIDATION_ONLY",
            "notice": "Syntax/structure only, not detection effectiveness or full Sigma specification validation. "
                      "No event logs, SIEM, conversion backend, external plugin or rule execution. "
                      "Diagnostics are classes only; source rule bodies and exception values are not returned."}


def main():
    disable_network()
    raw_request = sys.stdin.read(4097)
    if len(raw_request) > 4096: raise ValueError("Worker request budget exceeded")
    data = json.loads(raw_request)
    if set(data) != {"root", "path", "limit"}: raise ValueError("Invalid Sigma worker request")
    root = Path(data["root"]).resolve()
    path = checked_path(root, data["path"])
    with path.open("rb") as stream: raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT: raise ValueError("Sigma input budget exceeded")
    result = validate_text(raw.decode("utf-8-sig"), max(1, min(MAX_DOCUMENTS, int(data["limit"]))))
    result["content_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(result))


if __name__ == "__main__":
    try: main()
    except Exception:
        # Malformed YAML, missing packages, unexpected upstream faults fail
        # closed, without exposing private rule text/path/exception messages.
        print(json.dumps({"success": False, "error": "Offline Sigma parser unavailable or rejected bounded input"}))
        sys.exit(1)

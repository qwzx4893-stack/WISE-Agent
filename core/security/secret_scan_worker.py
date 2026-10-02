"""Isolated detect-secrets API worker; accepts only bounded validated files."""
import json
from pathlib import Path
import socket
import sys


def _no_network(*args, **kwargs):
    raise PermissionError("Secret verification over the network is disabled")


def main():
    payload = json.loads(sys.stdin.read(100_000))
    root = Path(payload["root"]).resolve()
    files = payload["files"]
    if not isinstance(files,list) or not 1 <= len(files) <= 100:
        raise ValueError("Invalid scan file count")
    verified = []
    for value in files:
        path = Path(value).resolve()
        if root not in path.parents or not path.is_file() or path.stat().st_size > 256_000:
            raise ValueError("Scan file is outside the approved boundary or exceeds limits")
        verified.append(path)
    socket.socket.connect = _no_network
    socket.socket.connect_ex = _no_network
    socket.create_connection = _no_network
    from detect_secrets import SecretsCollection
    from detect_secrets.settings import default_settings
    collection = SecretsCollection()
    with default_settings():
        for path in verified: collection.scan_file(str(path))
    # Remove hashes and values in the worker before crossing the process boundary.
    result = {filename:[{"type":row["type"],"line_number":row["line_number"]} for row in rows]
        for filename,rows in collection.json().items()}
    print(json.dumps({"results":result}))


if __name__ == "__main__": main()

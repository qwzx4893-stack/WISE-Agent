"""Read-mostly API acceptance probes with reproducible property checks."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


SAFE_GETS = (
    "/health",
    "/health/detailed",
    "/api/v2/settings",
    "/api/v2/system/telemetry",
    "/api/v2/system-integration",
    "/api/v2/voice/status",
    "/api/v2/models/external",
    "/api/v2/models/local",
    "/api/v2/tasks?limit=3",
    "/api/v2/sessions?limit=3",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    evidence: dict[str, object] = {"safe_gets": [], "properties": [], "failures": []}
    client = httpx.Client(base_url=args.base_url, timeout=30.0)

    for path in SAFE_GETS:
        started = time.perf_counter()
        try:
            response = client.get(path)
            latency = round((time.perf_counter() - started) * 1000, 2)
            body = response.json()
            ok = response.status_code == 200 and body is not None
            evidence["safe_gets"].append({
                "path": path,
                "status": response.status_code,
                "latency_ms": latency,
                "ok": ok,
            })
            if not ok:
                evidence["failures"].append(f"GET {path} returned {response.status_code}")
        except Exception as exc:  # evidence, not an unhandled harness crash
            evidence["failures"].append(f"GET {path}: {type(exc).__name__}: {exc}")

    # Exercise real session persistence while cleaning up only the QA-owned row.
    session_id = ""
    try:
        created = client.post("/api/v2/sessions/new")
        created.raise_for_status()
        session = created.json()
        session_id = str(session.get("session_id") or session.get("id") or "")
        assert session_id.startswith("wise_")
        title = "WISE QA disposable session"
        updated = client.patch(f"/api/v2/sessions/{quote(session_id)}", json={"title": title})
        updated.raise_for_status()
        fetched = client.get(f"/api/v2/sessions/{quote(session_id)}")
        fetched.raise_for_status()
        persisted = fetched.json().get("metadata", {}).get("title") == title
        evidence["properties"].append({"name": "session_create_rename_reload", "ok": persisted})
        if not persisted:
            evidence["failures"].append("Session title did not persist after reload")
    except Exception as exc:
        evidence["failures"].append(f"Session lifecycle: {type(exc).__name__}: {exc}")
    finally:
        if session_id:
            try:
                client.delete(f"/api/v2/sessions/{quote(session_id)}")
            except Exception:
                pass

    # Deterministic negative/property checks.  These are GET-only and never
    # invoke a model, tool, UAC prompt, scheduler, or destructive endpoint.
    @settings(
        max_examples=30,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(st.integers(min_value=-10_000, max_value=10_000))
    def session_limit_is_validated(value: int) -> None:
        response = client.get("/api/v2/sessions", params={"limit": value})
        if 1 <= value <= 250:
            assert response.status_code == 200
            assert isinstance(response.json(), list)
        else:
            assert response.status_code == 422

    try:
        session_limit_is_validated()
        evidence["properties"].append({"name": "session_limit_validation_30_examples", "ok": True})
    except Exception as exc:
        evidence["properties"].append({"name": "session_limit_validation_30_examples", "ok": False})
        evidence["failures"].append(f"Property failure: {type(exc).__name__}: {exc}")

    # OpenAPI must expose the product-critical contracts used by the desktop UI.
    try:
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        required = {
            "/api/v2/chat",
            "/api/v2/sessions",
            "/api/v2/models/external",
            "/api/v2/settings",
            "/api/v2/system-integration",
        }
        missing = sorted(required - set(paths))
        evidence["properties"].append({"name": "critical_openapi_contracts", "ok": not missing, "missing": missing})
        if missing:
            evidence["failures"].append(f"OpenAPI missing: {', '.join(missing)}")
    except Exception as exc:
        evidence["failures"].append(f"OpenAPI: {type(exc).__name__}: {exc}")

    client.close()
    evidence["passed"] = not evidence["failures"]
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "failures": evidence["failures"]}, ensure_ascii=False))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


"""Short read-only loopback reliability probe; not a long-duration soak test."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx


async def probe(base_url: str, count: int) -> dict:
    endpoints = ("/health", "/api/v2/sessions?limit=5", "/api/v2/tasks?limit=5", "/api/v2/models/external")
    durations, errors = [], []
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        async def check(index):
            async with semaphore:
                started = time.perf_counter()
                path = endpoints[index % len(endpoints)]
                try:
                    response = await client.get(path)
                    response.raise_for_status()
                    response.json()
                    durations.append((time.perf_counter() - started) * 1000)
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append({"endpoint": path, "error_type": type(exc).__name__})
        await asyncio.gather(*(check(index) for index in range(count)))
    durations.sort()
    return {"requests": count, "passed": not errors, "errors": errors,
            "p95_ms": round(durations[min(len(durations) - 1, int(len(durations) * .95))], 2) if durations else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8878")
    parser.add_argument("--count", type=int, default=40)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
        parser.error("This probe accepts only an unauthenticated IPv4 loopback base URL")
    if not 1 <= args.count <= 60:
        parser.error("count must be between 1 and 60")
    result = asyncio.run(probe(args.base_url, args.count))
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

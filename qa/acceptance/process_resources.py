"""Measure the owned backend tree, including Windows Python launcher children.

This excludes the user's application and the browser/UI/GPU. RSS is a sum of
per-process working sets (shared pages may be counted twice), not physical RAM.
"""
from __future__ import annotations

import psutil


class OwnedProcessSampler:
    def __init__(self, pid):
        self.root = psutil.Process(pid)
        self.root_created = self.root.create_time()
        self.known = {}

    def sample(self):
        if not self.root.is_running() or self.root.create_time() != self.root_created:
            raise RuntimeError("Owned backend launcher exited or PID was reused")
        members = [self.root, *self.root.children(recursive=True)]
        records, cpu_ready = [], True
        for current in members:
            try:
                identity = (current.pid, current.create_time())
                process = self.known.get(identity)
                primed = process is not None
                if not primed:
                    process = current
                    self.known[identity] = process
                rss = process.memory_info().rss
                cpu = process.cpu_percent(interval=None)
                cpu_ready &= primed
                records.append({"pid": current.pid, "created": identity[1],
                                "rss_bytes": rss, "cpu_percent": cpu if primed else None})
            except psutil.NoSuchProcess:
                continue
        if not records:
            raise RuntimeError("No owned backend process could be measured")
        present = {(row["pid"], row["created"]) for row in records}
        self.known = {key: value for key, value in self.known.items() if key in present}
        return {"rss_mb": round(sum(row["rss_bytes"] for row in records) / 1024**2, 2),
                "cpu_percent": round(sum(row["cpu_percent"] or 0 for row in records), 2) if cpu_ready else None,
                "scope": "OWNED_BACKEND_PROCESS_TREE", "processes": records,
                "cpu_primed": cpu_ready}

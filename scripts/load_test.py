"""Storage load test: does per-request cost stay flat as users grow?

Grows a real sharded ledger store to each checkpoint (default 1k, 10k, 100k
users) and, at each one, times what a request does against randomly chosen
users: opening a ledger in a fresh process (cold, nothing cached), opening it
again (warm), reading balances, and drafting a transfer through the full
pipeline. It then projects storage for 1,000,000 users from the measured
per-user size.

    uv run python scripts/load_test.py --root D:/dimon-load --checkpoints 1000 10000 100000

The store is left on disk for inspection; delete --root when done.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.dimon_live.model_adapter import ModelUnavailable  # noqa: E402
from apps.dimon_live.service import ActionService  # noqa: E402
from apps.dimon_live.session_manager import SessionManager  # noqa: E402


class NoModel:
    def extract(self, text):
        raise ModelUnavailable("the load test uses the rule-based path only")


def manager_for(root: Path) -> SessionManager:
    return SessionManager(
        owner_path=root / "owner.sqlite3",
        visitor_dir=root / "visitors",
        allowed_origins={"http://load.test"},
        visitor_ttl_seconds=10 * 365 * 86400,
        max_visitor_sessions=0,
    )


def timed(function, samples: int) -> dict[str, float]:
    durations = []
    for _ in range(samples):
        started = time.perf_counter()
        function()
        durations.append((time.perf_counter() - started) * 1000)
    durations.sort()
    return {
        "p50_ms": round(statistics.median(durations), 2),
        "p95_ms": round(durations[int(len(durations) * 0.95) - 1], 2),
        "max_ms": round(durations[-1], 2),
    }


def folder_bytes(path: Path) -> int:
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--checkpoints", type=int, nargs="+", default=[1_000, 10_000, 100_000])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    manager = manager_for(args.root)
    users: list[str] = []
    report = []
    for checkpoint in args.checkpoints:
        grow = checkpoint - len(users)
        started = time.perf_counter()
        with ThreadPoolExecutor(args.workers) as pool:
            users.extend(pool.map(lambda _: manager.create_visitor().session_id, range(grow)))
        growth_seconds = time.perf_counter() - started
        manager._ledgers.clear()   # keep memory bounded between checkpoints

        def headers(session_id: str, which: SessionManager):
            return which.resolve_headers(host="load.test", origin="http://load.test",
                                         cookie=session_id, access_assertion=None)

        cold = manager_for(args.root)   # a new process: empty cache
        sample = random.sample(users, min(args.samples, len(users)))
        cold_iter = iter(sample)
        warm_ids = sample[: min(50, len(sample))]
        for session_id in warm_ids:
            headers(session_id, manager)
        warm_iter = iter(warm_ids * (args.samples // len(warm_ids) + 1))

        results = {
            "users": len(users),
            "new_users_per_second": round(grow / growth_seconds, 1),
            "create_user": timed(lambda: manager.create_visitor(), 100),
            "open_ledger_cold": timed(lambda: headers(next(cold_iter), cold), len(sample)),
            "open_ledger_warm": timed(lambda: headers(next(warm_iter), manager), args.samples),
            "read_balances": timed(lambda: headers(random.choice(warm_ids), manager).database.get_state(), args.samples),
            "draft_transfer": timed(
                lambda: ActionService(headers(random.choice(warm_ids), manager).database, NoModel()).process(
                    str(uuid.uuid4()), "send John Smith $5 from savings"
                ),
                min(100, args.samples),
            ),
        }
        users.extend([])  # created_user samples are counted below
        results["users"] = manager.registry.count()
        disk = folder_bytes(args.root / "visitors")
        results["disk_mb"] = round(disk / 1e6, 1)
        results["bytes_per_user"] = int(disk / max(1, results["users"]))
        report.append(results)
        print(json.dumps(results), flush=True)

    per_user = report[-1]["bytes_per_user"]
    projection = {
        "projected_users": 1_000_000,
        "projected_disk_gb": round(per_user * 1_000_000 / 1e9, 1),
        "registry_rows": 1_000_000,
        "note": "per-request timings above should not grow with users; storage grows linearly",
    }
    print(json.dumps(projection), flush=True)
    if args.out:
        args.out.write_text(json.dumps({"checkpoints": report, "projection": projection}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

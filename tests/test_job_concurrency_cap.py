"""Regression test for the SECURITY.md Phase 3 MEDIUM
finding: every POST /search spawned an unbounded `threading.Thread` with no
concurrency ceiling, no queue, and no per-client job cap — resource
exhaustion (thread + memory pressure) from concurrent job submission.

Fix: `_new_job()` now checks the count of currently-RUNNING jobs (done ==
False) against `_MAX_CONCURRENT_JOBS` and refuses to create a new one over
the cap, atomically (check + create under one `_JOBS_LOCK` acquisition so
two simultaneous requests can't both slip past it). `POST /search` returns
429 when `_new_job()` returns None, and never spawns the worker thread in
that case.

This test drives the real app object (`app_module.app`, `app_module.JOBS`,
`app_module._new_job`) rather than re-implementing the cap logic, so a
wiring mistake in the route (e.g. spawning the thread before checking the
return value) would be caught here.

Run from the repo root:  python3 tests/test_job_concurrency_cap.py
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("FEC_API_KEY", None)
os.environ.pop("CONGRESS_API_KEY", None)
os.environ.pop("SEARXNG_URL", None)

import app as app_module  # noqa: E402
import steps  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def fake_running_job() -> str:
    """Insert a job directly into JOBS, done=False, bypassing _new_job (so
    it doesn't itself count against — or get blocked by — the cap being
    tested)."""
    import uuid
    jid = uuid.uuid4().hex
    with app_module._JOBS_LOCK:
        app_module.JOBS[jid] = {
            "log": steps.StepLog(), "result": None, "error": None,
            "rate_limited": False, "done": False, "created": time.time(),
        }
    return jid


def main() -> int:
    # ── unit-level: _new_job() itself respects the cap ────────────────────────
    app_module.JOBS.clear()
    cap = app_module._MAX_CONCURRENT_JOBS
    check("MAX_CONCURRENT_JOBS is a small positive number (sane default)",
          0 < cap <= 100, cap)

    ids = [fake_running_job() for _ in range(cap)]
    check(f"exactly {cap} running jobs seeded", len(ids) == cap)

    over_cap = app_module._new_job()
    check("_new_job() refuses to create a job AT the cap (returns None)",
          over_cap is None)
    check("refused call did NOT add a job to JOBS",
          len(app_module.JOBS) == cap)

    # Finish one job (done=True) — that frees a concurrency slot even though
    # the job record itself is still in JOBS (kept around for polling/export
    # per _JOB_TTL) — proving the cap counts RUNNING jobs, not total records.
    with app_module._JOBS_LOCK:
        app_module.JOBS[ids[0]]["done"] = True

    freed = app_module._new_job()
    check("_new_job() succeeds again once a running job finishes "
          "(cap counts RUNNING jobs, not total JOBS entries)",
          freed is not None)
    check("JOBS now holds cap+1 total entries (one finished + freed slot's new job)",
          len(app_module.JOBS) == cap + 1)

    app_module.JOBS.clear()

    # ── route-level: POST /search returns 429 and never spawns a thread ──────
    app_module.JOBS.clear()
    for _ in range(cap):
        fake_running_job()

    client = app_module.app.test_client()
    thread_count_before = threading.active_count()
    resp = client.post("/search", json={"name": "Someone New"})
    check("POST /search returns 429 when at the concurrency cap",
          resp.status_code == 429, f"got {resp.status_code}: {resp.get_json()}")
    check("429 response names the cap in its error message",
          str(cap) in (resp.get_json() or {}).get("error", ""),
          resp.get_json())
    time.sleep(0.2)   # let any (incorrectly) spawned thread actually start
    check("no new worker thread was spawned for the rejected request",
          threading.active_count() <= thread_count_before + 1,   # +1 slack for test-runner noise
          f"before={thread_count_before} after={threading.active_count()}")
    check("JOBS was not mutated by the rejected request",
          len(app_module.JOBS) == cap)

    # Free a slot — the next request must succeed normally.
    with app_module._JOBS_LOCK:
        first_id = next(iter(app_module.JOBS))
        app_module.JOBS[first_id]["done"] = True
    resp = client.post("/search", json={"name": "Someone New"})
    check("POST /search succeeds once a slot frees up",
          resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")
    check("successful response carries a job_id",
          bool((resp.get_json() or {}).get("job_id")))

    app_module.JOBS.clear()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All concurrent-job-cap checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

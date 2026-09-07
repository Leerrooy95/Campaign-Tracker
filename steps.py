"""
steps.py — the step-log spine.

WHAT THIS IS, IN ONE SENTENCE
-----------------------------
A small, thread-safe, JSON-serializable record of what the tool is doing,
where each entry is BOTH (a) the thing the UI shows the user live, and (b) the
checkpoint the tool gates its own progress on. "Visible process" and "can't
skip a step and emit bad info" are therefore the same object, not two features.

WHY IT EXISTS
-------------
The hard requirement is: never give the user a confident answer that quietly
skipped a step or swallowed a problem. The usual way that goes wrong is a
function fails or returns partial data, the caller doesn't notice, and the
final report looks finished. The defense is to make every meaningful operation
declare itself as a step with an explicit status, make downstream work REFUSE
to run unless its prerequisite steps actually succeeded, and carry the whole
log into the final report as an audit trail. The same structure feeds the live
display, so the user watches the self-check happen in real time.

HOW IT'S USED (the 30-second version)
-------------------------------------
    log = StepLog()
    log.plan([("resolve", "Resolve candidate"),
              ("sched_a", "Pull direct contributions (Schedule A)"),
              ("sched_e", "Pull outside spending (Schedule E)"),
              ("correlate", "Compare rhetoric vs. funding")])

    with log.step("resolve") as s:
        cand = resolve(name)
        s.ok(f"matched {cand['name']}")

    with log.step("sched_a") as s:
        records = fetch_schedule_a(cid, key, cyc, progress_cb=s.progress())
        s.ok(f"{len(records)} contributions")

    with log.step("sched_e") as s:
        out = outside_spending_for_candidate(cid, key, cyc)
        if out.incomplete:
            s.warn(out.incomplete_reason)        # recorded, NON-blocking
        else:
            s.ok(f"{len(out.spenders)} outside spenders")

    with log.step("correlate") as s:
        log.require("sched_a")                    # hard gate: abort if it didn't succeed
        # sched_e is allowed to be a warn here — correlation can run on a lower
        # bound as long as the report says so. require(..., allow_warn=True).
        log.require("sched_e", allow_warn=True)
        result = correlate(...)
        s.ok()

At any moment, log.to_dict() is the JSON the /status endpoint returns to the
browser AND the audit block embedded in the exported report.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It has no idea how the events reach the browser. Per the 2026 SSE guidance —
"emit structured events, keep the transport layer responsible for delivery" —
production (this file) is kept separate from delivery (polling now, SSE later).
Swapping the transport never touches this code.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# Status is an ordered ladder, lowest to highest "doneness". Keeping it ordered
# lets reached()/require() ask "did this step get at least this far?" with a
# simple comparison instead of a pile of if/elif.
_PENDING = "pending"   # declared in the plan, not started yet (UI shows it greyed)
_RUNNING = "running"   # in progress (UI shows a spinner)
_OK = "ok"             # finished cleanly
_WARN = "warn"         # finished, but with a disclosed caveat — NON-blocking,
                       # but the caveat rides along into the report
_FAIL = "fail"         # did not finish; anything depending on it must NOT run

_RANK = {_PENDING: 0, _RUNNING: 1, _WARN: 2, _OK: 3, _FAIL: -1}
# Note WARN ranks BELOW OK but is still a "successful enough to continue" state.
# FAIL is ranked -1 on purpose: it can never satisfy a gate, even one that
# accepts WARN.


class StepGateError(RuntimeError):
    """Raised by require() when a prerequisite step did not reach a high enough
    status. This is the mechanism that turns "don't skip steps" from a hope
    into an enforced invariant — the pipeline raises instead of silently
    producing a result built on a missing input."""


@dataclass
class Step:
    key: str                         # stable id used by gates and progress_cb
    label: str                       # human text shown in the UI
    status: str = _PENDING
    detail: str = ""                 # short human note ("84,342 contributions")
    data: Optional[dict] = None      # optional structured payload for the report
    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    # The handle methods below are what you call INSIDE a `with log.step(key)`
    # block. They're thin — they delegate to the parent log so the lock is held
    # in one place — but they read naturally at the call site (s.ok(...),
    # s.warn(...)).
    _log: Optional["StepLog"] = field(default=None, repr=False)

    def ok(self, detail: str = "", data: Optional[dict] = None) -> None:
        self._log._set(self.key, _OK, detail, data)

    def warn(self, detail: str, data: Optional[dict] = None) -> None:
        self._log._set(self.key, _WARN, detail, data)

    def fail(self, detail: str, data: Optional[dict] = None) -> None:
        self._log._set(self.key, _FAIL, detail, data)

    def note(self, detail: str) -> None:
        """Update the live detail WITHOUT changing status — used to narrate a
        long-running step ('page 12/64') while it's still RUNNING."""
        self._log._set(self.key, _RUNNING, detail, None)

    def progress(self) -> Callable[[int, Optional[int]], None]:
        """Returns a (page, total) callback shaped exactly like the progress_cb
        that fec.py's fetch_schedule_a / fetch_schedule_e already accept. This
        is the bridge: pass it straight into those functions and their existing
        pagination updates flow into this step's live detail — zero changes to
        fec.py.
            fetch_schedule_a(cid, key, cyc, progress_cb=s.progress())
        """
        def _cb(page: int, total: Optional[int]) -> None:
            self.note(f"page {page}/{total}" if total else f"page {page}")
        return _cb


class StepLog:
    """Ordered, thread-safe collection of Steps for ONE search/job.

    Thread-safety matters because the worker thread mutates the log while the
    request thread reads it (to answer /status polls). Every read and write
    goes through the same lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._order: list[str] = []          # keys, in declared/insertion order
        self._steps: dict[str, Step] = {}
        self.created_at = time.time()

    # ── declaring steps ────────────────────────────────────────────────────
    def plan(self, steps: list[tuple[str, str]]) -> None:
        """Declare the whole sequence up front as (key, label) pairs. Optional,
        but recommended: it lets the UI show every step greyed-out immediately,
        then light each one as it runs — calm progressive disclosure instead of
        rows popping into existence. Re-declaring a key is a no-op."""
        with self._lock:
            for key, label in steps:
                if key not in self._steps:
                    self._steps[key] = Step(key=key, label=label, _log=self)
                    self._order.append(key)

    def add(self, key: str, label: str) -> Step:
        """Declare a single step on the fly (for work you didn't know about up
        front). Returns the Step so you can drive it directly if you're not
        using the context manager."""
        with self._lock:
            if key not in self._steps:
                self._steps[key] = Step(key=key, label=label, _log=self)
                self._order.append(key)
            return self._steps[key]

    # ── the ergonomic path: `with log.step(key):` ──────────────────────────
    def step(self, key: str, label: Optional[str] = None) -> "_StepContext":
        """Context manager. On enter: marks the step RUNNING. On clean exit:
        marks it OK *unless* you already set a terminal status inside the block
        (so an explicit s.warn() or s.fail() wins). On an UNCAUGHT EXCEPTION:
        marks it FAIL with the exception text and re-raises — a crash can never
        leave a step looking unfinished-but-fine. This is what makes every
        wrapped block a self-reporting, self-checking unit with no boilerplate."""
        with self._lock:
            if key not in self._steps:
                self.add(key, label or key)
            elif label:
                self._steps[key].label = label
        return _StepContext(self, key)

    # ── status transitions (all funnel through _set) ───────────────────────
    def _set(self, key: str, status: str, detail: str = "",
             data: Optional[dict] = None) -> None:
        with self._lock:
            s = self._steps[key]
            s.status = status
            if detail:
                s.detail = detail
            if data is not None:
                s.data = data
            if status == _RUNNING and s.started_at is None:
                s.started_at = time.time()
            if status in (_OK, _WARN, _FAIL):
                s.ended_at = time.time()

    # ── gates: the "can't skip a step" enforcement ─────────────────────────
    def reached(self, key: str, *, allow_warn: bool = False) -> bool:
        """True if `key` finished successfully. By default only OK counts;
        allow_warn=True also accepts a WARN (finished with a disclosed caveat).
        FAIL and not-yet-run never count."""
        with self._lock:
            s = self._steps.get(key)
            if s is None:
                return False
            if s.status == _OK:
                return True
            if s.status == _WARN and allow_warn:
                return True
            return False

    def require(self, key: str, *, allow_warn: bool = False) -> None:
        """Hard gate. Raises StepGateError if `key` did not finish successfully.
        Call this at the top of any step that must not run on missing/partial
        input. This is the line that converts 'the process is visible' into 'the
        process is enforced'."""
        if not self.reached(key, allow_warn=allow_warn):
            with self._lock:
                s = self._steps.get(key)
                got = s.status if s else "never ran"
            raise StepGateError(
                f"step {key!r} must succeed before this one can run "
                f"(it is {got!r}{', warn not accepted here' if not allow_warn else ''})"
            )

    def any_failed(self) -> bool:
        with self._lock:
            return any(s.status == _FAIL for s in self._steps.values())

    def warnings(self) -> list[Step]:
        """All steps that finished with a caveat — this is what the report's
        'what to be careful about' audit section is built from."""
        with self._lock:
            return [s for s in self._steps.values() if s.status == _WARN]

    # ── serialization: same dict for /status AND the report ────────────────
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "created_at": self.created_at,
                "any_failed": any(s.status == _FAIL for s in self._steps.values()),
                "steps": [{
                    "key": s.key,
                    "label": s.label,
                    "status": s.status,
                    "detail": s.detail,
                    "data": s.data,
                    "started_at": s.started_at,
                    "ended_at": s.ended_at,
                    "elapsed": (round(s.ended_at - s.started_at, 2)
                                if s.started_at and s.ended_at else None),
                } for s in (self._steps[k] for k in self._order)],
            }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class _StepContext:
    """Returned by StepLog.step(). Handles the auto-RUNNING / auto-OK /
    auto-FAIL lifecycle and hands the inner block a Step to call .ok()/.warn()/
    .note()/.progress() on."""

    def __init__(self, log: StepLog, key: str) -> None:
        self._log = log
        self._key = key

    def __enter__(self) -> Step:
        self._log._set(self._key, _RUNNING)
        return self._log._steps[self._key]

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # A crash inside the block: record FAIL with the reason, then let
            # the exception propagate (return False). The step can never be
            # left looking fine after a crash.
            self._log._set(self._key, _FAIL, f"{exc_type.__name__}: {exc}")
            return False
        # Clean exit: only auto-OK if the block didn't already set a terminal
        # status (an explicit warn()/fail()/ok() inside the block wins).
        with self._log._lock:
            if self._log._steps[self._key].status == _RUNNING:
                self._log._set(self._key, _OK)
        return False


# ════════════════════════════════════════════════════════════════════════════
# RUNNABLE DEMO — `python steps.py`
# Network-free. Simulates a candidate search so you can watch the log evolve,
# see a WARN ride through non-blocking, see a gate enforce a prerequisite, and
# see the final JSON that would feed both the UI and the report.
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    def show(log: StepLog, header: str) -> None:
        print(f"\n=== {header} ===", file=sys.stderr)
        for s in log.to_dict()["steps"]:
            mark = {"pending": "·", "running": "→", "ok": "✓",
                    "warn": "!", "fail": "✗"}[s["status"]]
            extra = f"  — {s['detail']}" if s["detail"] else ""
            print(f"  {mark} {s['label']:<42} [{s['status']}]{extra}", file=sys.stderr)

    log = StepLog()
    log.plan([
        ("resolve",   "Resolve candidate (FEC candidate_id)"),
        ("sched_a",   "Pull direct contributions (Schedule A)"),
        ("sched_e",   "Pull outside spending (Schedule E)"),
        ("rhetoric",  "Score public statements (rhetoric track)"),
        ("correlate", "Compare rhetoric vs. funding over time"),
    ])
    show(log, "PLANNED (all pending, UI shows them greyed)")

    with log.step("resolve") as s:
        time.sleep(0.05)
        s.ok("matched OSSOFF, T. JONATHAN  (S8GA00180)")

    with log.step("sched_a") as s:
        # simulate fec.py's progress_cb driving the live detail
        cb = s.progress()
        for page in range(1, 4):
            cb(page, 3)
            time.sleep(0.03)
        s.ok("84,342 contributions, deduped by sub_id")
    show(log, "AFTER SCHEDULE A")

    with log.step("sched_e") as s:
        time.sleep(0.05)
        # the issue-#3396 shortfall: finished, but a disclosed lower bound.
        s.warn("returned 312 of 487 reported rows — openFEC pagination bug "
               "(#3396); outside-spending totals are a lower bound",
               data={"reported": 487, "got": 312})

    with log.step("rhetoric") as s:
        time.sleep(0.05)
        s.ok("17 statements scored against money-in-politics rubric")
    show(log, "BEFORE CORRELATE (note sched_e is a WARN, not OK)")

    with log.step("correlate") as s:
        # Hard gate: Schedule A is mandatory and must be OK.
        log.require("sched_a")
        # Schedule E is allowed to be a WARN — we can correlate on a lower bound
        # AS LONG AS the report discloses it. This is the deliberate, visible
        # decision, not a silent one.
        log.require("sched_e", allow_warn=True)
        time.sleep(0.05)
        s.ok("r computed over 2017–2026; sched_e carried as lower bound")
    show(log, "FINAL")

    # The report's audit section is built straight from the warnings.
    print("\n=== REPORT AUDIT BLOCK (from log.warnings()) ===", file=sys.stderr)
    for w in log.warnings():
        print(f"  ⚠ {w.label}: {w.detail}", file=sys.stderr)

    # Demonstrate the gate ACTUALLY blocking — try to correlate in a fresh log
    # where Schedule A failed.
    print("\n=== GATE ENFORCEMENT (Schedule A failed → correlate refuses) ===",
          file=sys.stderr)
    bad = StepLog()
    bad.plan([("sched_a", "Pull Schedule A"), ("correlate", "Correlate")])
    with bad.step("sched_a") as s:
        s.fail("FEC key rejected (HTTP 403)")
    try:
        with bad.step("correlate") as s:
            bad.require("sched_a")          # <-- raises before any bad result is built
            s.ok("(this line never runs)")
    except StepGateError as e:
        print(f"  blocked as intended: {e}", file=sys.stderr)
    show(bad, "BAD-RUN FINAL (correlate is FAIL, never produced output)")

    # The machine-readable payload the /status endpoint would return:
    print("\n=== /status JSON (first run) ===")
    print(log.to_json())

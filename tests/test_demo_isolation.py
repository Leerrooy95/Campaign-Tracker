"""Regression test for the demo/live contamination found by the 2026-09-07
regression audit — demo mode ran REAL web searches, and said it hadn't.

BUG (two halves, both live-reproduced against a real instance):

  1. `statements.collect_statements` resolved its backend with
     `base = (searxng_url or os.getenv("SEARXNG_URL", ""))`. An empty string
     is falsy, so app.py's demo branch passing `searxng_url=""` to force
     fixtures fell THROUGH to the environment. On any machine with
     `SEARXNG_URL` configured — which is the state this repo's own quick
     start puts an operator in — a demo run made live network calls and
     returned one real, search-derived statements track sitting inside an
     otherwise fabricated result, with no marker distinguishing them.

  2. The `statements` step then unconditionally logged "N statements gathered
     (offline fixtures — set SEARXNG_URL for live)" whenever `demo` was set,
     because the message was written from the BRANCH'S INTENT rather than
     from what the collector returned. So in exactly the state where the
     disclosure mattered, the visible step log asserted the opposite of what
     had happened. The StepLog is the tool's self-check (Integrity Rule 6);
     a log that can describe an intention is not a check at all.

  3. Marking was inconsistent: `track_a_direct`/`track_a_outside` carried
     `_demo`, composition/record/votes/statements carried nothing, and
     `/status` returned no `demo` field at all — so an exported JSON or CSV,
     once it had left the running app, had no uniform machine-checkable way
     to say whether it was synthetic.

Pinned here:
  1. `FORCE_OFFLINE` beats a configured `SEARXNG_URL`; `None` still consults
     the environment; an explicit URL still runs live. Three distinct cases.
  2. A demo run makes ZERO calls into the search transport (the fixture path
     is proven, not assumed).
  3. The statements step's message is derived from `ss.backend`.
  4. Every demo track carries `_demo`, `result["demo"]` is True, and
     `/status` reports `demo`.

Offline: the SearXNG transport is monkeypatched to fail loudly if a demo run
ever reaches it. No network, no keys.

Run from the repo root:  python3 tests/test_demo_isolation.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import statements  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


class _SearchSpy:
    """Stands in for statements._searxng_search. Records every call so 'no
    network' can be PROVEN rather than inferred from a backend string."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, base, per_query):
        self.calls.append((query, base))
        return []


# ─────────────────────────────────────────────────────────────────────────
# 1. The three-way fixtures/live decision.
# ─────────────────────────────────────────────────────────────────────────
def test_force_offline_beats_env():
    spy = _SearchSpy()
    orig_search, orig_env = statements._searxng_search, os.environ.get("SEARXNG_URL")
    statements._searxng_search = spy
    os.environ["SEARXNG_URL"] = "http://localhost:8080"
    try:
        ss = statements.collect_statements("Some Candidate",
                                           searxng_url=statements.FORCE_OFFLINE,
                                           office="H")
        check("FORCE_OFFLINE returns the fixtures backend even with SEARXNG_URL set",
              ss.backend == "fixtures", f"backend={ss.backend!r}")
        check("FORCE_OFFLINE makes no search calls at all",
              spy.calls == [], f"{len(spy.calls)} call(s) reached the transport")
        check("FORCE_OFFLINE is the empty string (the value app.py used to pass bare)",
              statements.FORCE_OFFLINE == "")

        # The pre-fix behavior, spelled out: `"" or os.getenv(...)` would have
        # produced the env URL here. Assert the new branch does not.
        legacy = ("" or os.environ.get("SEARXNG_URL", "")).strip()
        check("the old falsy-string idiom would have gone live (bug is real, not theoretical)",
              legacy == "http://localhost:8080", legacy)
    finally:
        statements._searxng_search = orig_search
        if orig_env is None:
            os.environ.pop("SEARXNG_URL", None)
        else:
            os.environ["SEARXNG_URL"] = orig_env


def test_none_consults_env():
    spy = _SearchSpy()
    orig_search, orig_env = statements._searxng_search, os.environ.get("SEARXNG_URL")
    orig_fetch = statements._page_fetch_pass
    statements._searxng_search = spy
    statements._page_fetch_pass = lambda sts: (0, 0)
    os.environ["SEARXNG_URL"] = "http://localhost:8080"
    try:
        ss = statements.collect_statements("Some Candidate", office="H")
        check("searxng_url=None (default) still consults SEARXNG_URL",
              ss.backend == "searxng", f"backend={ss.backend!r}")
        check("the env URL is the one actually queried",
              bool(spy.calls) and all(c[1] == "http://localhost:8080" for c in spy.calls),
              str(spy.calls[:1]))

        os.environ.pop("SEARXNG_URL", None)
        spy.calls.clear()
        ss2 = statements.collect_statements("Some Candidate", office="H")
        check("searxng_url=None with no SEARXNG_URL falls back to fixtures",
              ss2.backend == "fixtures", f"backend={ss2.backend!r}")
        check("that fallback makes no search calls", spy.calls == [])
    finally:
        statements._searxng_search = orig_search
        statements._page_fetch_pass = orig_fetch
        if orig_env is None:
            os.environ.pop("SEARXNG_URL", None)
        else:
            os.environ["SEARXNG_URL"] = orig_env


def test_explicit_url_wins():
    spy = _SearchSpy()
    orig_search, orig_env = statements._searxng_search, os.environ.get("SEARXNG_URL")
    orig_fetch = statements._page_fetch_pass
    statements._searxng_search = spy
    statements._page_fetch_pass = lambda sts: (0, 0)
    os.environ["SEARXNG_URL"] = "http://env-instance:8080"
    try:
        ss = statements.collect_statements("Some Candidate",
                                           searxng_url="http://explicit:9999", office="S")
        check("an explicit URL is used, not the environment's",
              ss.backend == "searxng" and bool(spy.calls)
              and all(c[1] == "http://explicit:9999" for c in spy.calls),
              str(spy.calls[:1]))
    finally:
        statements._searxng_search = orig_search
        statements._page_fetch_pass = orig_fetch
        if orig_env is None:
            os.environ.pop("SEARXNG_URL", None)
        else:
            os.environ["SEARXNG_URL"] = orig_env


# ─────────────────────────────────────────────────────────────────────────
# 2. The disclosure is derived from the backend that ran.
# ─────────────────────────────────────────────────────────────────────────
def test_backend_note_is_derived():
    import app  # noqa: E402
    fixtures_note = app._statements_backend_note("fixtures")
    live_note = app._statements_backend_note("searxng")
    check("fixtures note says fixtures", "fixtures" in fixtures_note.lower())
    check("fixtures note claims no network", "no network" in fixtures_note.lower())
    check("live note says LIVE", "live" in live_note.lower())
    check("live note does NOT claim offline fixtures",
          "offline fixtures" not in live_note.lower(), live_note)
    check("the two notes are different (a hardcoded string would collapse them)",
          fixtures_note != live_note)


def test_mark_demo_stamps_dicts_and_lists():
    import app  # noqa: E402
    d = app._mark_demo({"a": 1})
    check("_mark_demo stamps a dict", d.get("_demo") is True)
    rows = app._mark_demo([{"cycle": 2026}, {"cycle": 2024}])
    check("_mark_demo stamps every element of a list",
          all(r.get("_demo") is True for r in rows))
    check("_mark_demo leaves the rest of the payload intact", d["a"] == 1)


# ─────────────────────────────────────────────────────────────────────────
# 3. End to end: a demo run with SEARXNG_URL configured must stay synthetic,
#    say so, and mark every track.
# ─────────────────────────────────────────────────────────────────────────
def test_demo_run_is_offline_and_labeled():
    import app  # noqa: E402
    from steps import StepLog

    def explode(query, base, per_query):
        raise AssertionError(
            f"demo run reached the live search transport: {query!r} -> {base!r}")

    orig_search, orig_env = statements._searxng_search, os.environ.get("SEARXNG_URL")
    statements._searxng_search = explode
    app.statements._searxng_search = explode
    os.environ["SEARXNG_URL"] = "http://localhost:8080"   # the contaminating state

    job_id = "test-demo-isolation"
    app.JOBS[job_id] = {"log": StepLog(), "error": None, "result": {},
                        "done": False, "rate_limited": False, "demo": True,
                        "created": 0.0}
    try:
        app._run_search(job_id, "Thomas Massie", "", True, anthropic_key="")
        job = app.JOBS[job_id]
    finally:
        statements._searxng_search = orig_search
        app.statements._searxng_search = orig_search
        if orig_env is None:
            os.environ.pop("SEARXNG_URL", None)
        else:
            os.environ["SEARXNG_URL"] = orig_env

    check("demo run completed without touching the network", job["error"] is None,
          str(job["error"]))
    result = job["result"] or {}

    check("result carries the authoritative demo flag", result.get("demo") is True)
    stmts = result.get("track_b_statements") or {}
    check("statements track used the fixtures backend",
          stmts.get("backend") == "fixtures", f"backend={stmts.get('backend')!r}")

    for key in ("track_a_direct", "track_a_outside", "track_b_record",
                "track_b_votes", "track_b_statements"):
        check(f"{key} carries a row-level _demo marker",
              (result.get(key) or {}).get("_demo") is True)
    comp = result.get("track_a_composition") or []
    check("every composition cycle carries _demo",
          bool(comp) and all(c.get("_demo") is True for c in comp))

    steps = {s["key"]: s for s in job["log"].to_dict()["steps"]}
    detail = (steps["statements"].get("detail") or "")
    check("statements step discloses the fixtures backend", "fixtures" in detail.lower(),
          detail)
    check("statements step does not claim the run touched the network",
          "touched the network" not in detail.lower() and "no network" in detail.lower(),
          detail)


def test_status_reports_demo():
    import app  # noqa: E402
    from steps import StepLog

    client = app.app.test_client()
    job_id = "test-demo-status"
    app.JOBS[job_id] = {"log": StepLog(), "error": None, "result": None,
                        "done": True, "rate_limited": False, "demo": True,
                        "created": 0.0}
    try:
        body = client.get(f"/status/{job_id}").get_json()
    finally:
        app.JOBS.pop(job_id, None)
    check("/status reports demo before a result exists", body.get("demo") is True,
          str(body)[:120])


def main() -> int:
    test_force_offline_beats_env()
    test_none_consults_env()
    test_explicit_url_wins()
    test_backend_note_is_derived()
    test_mark_demo_stamps_dicts_and_lists()
    test_demo_run_is_offline_and_labeled()
    test_status_reports_demo()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all demo-isolation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

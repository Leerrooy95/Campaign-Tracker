"""Regression test for the SECURITY.md Phase 2 HIGH findings:
`candidate_id` and `state` reached fec.py / congress.py's unencoded URL
interpolation with zero format validation.

  - `app.py:655` -> `fec.py:1258`/`fec.py:1304` — `candidate_id` was
    interpolated raw into `/candidate/{candidate_id}/totals/`. Verified
    injectable shapes included `../../../v1/candidates/search`,
    `X?per_page=1&`, and `A#`.
  - `app.py:658` -> `congress.py:303` — `state` was interpolated raw (after
    only `.upper()`) into `/member/{state}?...`.

Both are now validated at the POST /search boundary in app.py, against the
real upstream grammars (FEC's own `^[HSP][0-9A-Z]{8}$` candidate_id format;
a two-letter USPS state/territory code), BEFORE the job thread is even
spawned — so a malformed value never reaches fec.py or congress.py at all.

This test drives the real Flask app through its test client (not just the
regex in isolation), so a wiring mistake — e.g. the check applied to the
wrong field, or applied after the thread is already spawned — would be
caught here even if the regex itself were correct. It also proves the
"Did You Mean?" happy path (a real FEC id + state, exactly as static/app.js
sends them back from /candidates) still works, since that's the only
legitimate caller of these two fields.

Run from the repo root:  python3 tests/test_input_validation.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# No FEC_API_KEY -> demo mode. Keeps this test offline: a request that PASSES
# validation still never reaches a real upstream API, it just enters the demo
# fixture path in _run_search's worker thread.
os.environ.pop("FEC_API_KEY", None)
os.environ.pop("CONGRESS_API_KEY", None)
os.environ.pop("SEARXNG_URL", None)

import app as app_module  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    client = app_module.app.test_client()

    # ── the exact injection shapes the audit demonstrated ────────────────────
    injected_candidate_ids = [
        "../../../v1/candidates/search",
        "X?per_page=1&",
        "A#",
        "S8GA00180/../../etc",
        "'; DROP TABLE x;--",
        "s8ga00180",          # lowercase — doesn't match FEC's own grammar either
    ]
    for bad_id in injected_candidate_ids:
        resp = client.post("/search", json={"name": "Some Candidate",
                                            "candidate_id": bad_id})
        check(f"candidate_id {bad_id!r} rejected with 400",
              resp.status_code == 400, f"got {resp.status_code}")
        check(f"candidate_id {bad_id!r}: no job_id leaked in the rejection",
              "job_id" not in (resp.get_json() or {}))

    injected_states = [
        "../member", "GA/../HI", "GA?x=1", "G", "GAA", "ga1", "<script>",
    ]
    for bad_state in injected_states:
        resp = client.post("/search", json={"name": "Some Candidate",
                                            "candidate_id": "S8GA00180",
                                            "state": bad_state})
        check(f"state {bad_state!r} rejected with 400",
              resp.status_code == 400, f"got {resp.status_code}")

    # ── the legitimate "Did You Mean?" flow must still work ──────────────────
    # Exactly what /candidates returns for the demo candidate, and exactly
    # what static/app.js posts back to /search — must NOT be rejected.
    resp = client.post("/candidates", json={"name": "Ossoff"})
    check("/candidates (demo, no key) returns 200", resp.status_code == 200,
          f"got {resp.status_code}")
    demo_cand = (resp.get_json() or {}).get("candidates", [{}])[0]
    check("demo candidate carries a well-formed candidate_id",
          bool(demo_cand.get("candidate_id")), demo_cand)

    resp = client.post("/search", json={
        "name": "Ossoff",
        "candidate_id": demo_cand.get("candidate_id"),
        "candidate_name": demo_cand.get("name"),
        "office": demo_cand.get("office"),
        "state": demo_cand.get("state"),
    })
    check("legitimate Did-You-Mean payload is accepted (200, job spawned)",
          resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")
    check("accepted response carries a job_id",
          bool((resp.get_json() or {}).get("job_id")))

    # ── empty candidate_id/state (bare-name search) must still work ──────────
    resp = client.post("/search", json={"name": "Ossoff"})
    check("bare-name search (no candidate_id/state) is accepted",
          resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")

    # ── a well-formed but nonexistent candidate_id passes validation ─────────
    # (validation checks SHAPE, not existence — that's the resolve step's job,
    # and it must not be conflated with format rejection).
    resp = client.post("/search", json={"name": "Nobody",
                                        "candidate_id": "H9ZZ99999"})
    check("well-formed-but-unknown candidate_id passes the format check",
          resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All input-validation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

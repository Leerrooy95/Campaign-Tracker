"""Regression test for the Schedule A pagination bug found by the
2026-09-07 five-candidate live audit.

BUG — `fetch_schedule_a` hard-coded the keyset-pagination cursor's secondary
field as `last_contribution_receipt_date`. That's only the right field name
when the query is DATE-sorted. The capped "top donors" pull (max_pages set)
and the refund-netting pull (smallest_first) both sort by
contribution_receipt_amount, whose cursor field is actually
`last_contribution_receipt_amount` — so `last_indexes.get(
"last_contribution_receipt_date")` returned None, `str(None)` produced the
literal string "None", and every second-page request sent
`last_contribution_receipt_date=None`, which FEC rejected with a 422 —
silently truncating every capped pull to page 1, on every candidate, with no
disclosure (a later page failing after retries sets `truncated=True`, but
`app.py`'s `_summarize_direct` dropped that flag, and the `sched_a` step
always called `s.ok(...)` unconditionally).

Three things pinned here:
  1. `fetch_schedule_a` reads the cursor generically (whatever keys
     `last_indexes` actually contains) instead of hard-coding a date field —
     an amount-sorted second page must carry
     `last_contribution_receipt_amount`, never a stray `...date=None`.
  2. `app._summarize_direct` aggregates `truncated`/`truncated_reason` across
     committees instead of dropping them.
  3. The `sched_a` step in `app._run_search` warns (not oks) when the
     aggregated pull came back truncated — mirroring the `sched_e` step's
     existing incomplete check.

Run from the repo root:  python3 tests/test_schedule_a_pagination.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fec  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ─────────────────────────────────────────────────────────────────────────
# 1. fetch_schedule_a: an amount-sorted pull must send the AMOUNT cursor key
#    on page 2, never a literal "...date=None".
# ─────────────────────────────────────────────────────────────────────────

def _row(sub_id, amount):
    return {
        "contributor_name": "DONOR, SOME", "contribution_receipt_amount": amount,
        "contribution_receipt_date": "2025-08-22", "is_individual": True,
        "committee_id": "C00000000", "sub_id": sub_id, "memo_code": None,
    }


def test_amount_sort_cursor():
    requested_urls = []

    def fake_http(url):
        requested_urls.append(url)
        if len(requested_urls) == 1:
            # Page 1: FEC's own real response shape for an amount-sorted
            # query — last_indexes carries the AMOUNT key, no date key at all
            # (this is the exact live shape that audit captured).
            body = json.dumps({
                "results": [_row("A1", 3500.0)],
                "pagination": {
                    "pages": 2,
                    "last_indexes": {"last_index": "4051220261510089026",
                                     "last_contribution_receipt_amount": "3500.00"},
                },
            })
            return 200, body
        # Page 2: nothing left — pull ends cleanly.
        body = json.dumps({
            "results": [_row("A2", 1000.0)],
            "pagination": {"pages": 2, "last_indexes": {}},
        })
        return 200, body

    orig = fec._http_get_with_retry
    fec._http_get_with_retry = fake_http
    try:
        records, truncated, stop_reason = fec.fetch_schedule_a(
            "C00000000", "k", 2026, max_pages=10)
    finally:
        fec._http_get_with_retry = orig

    check("two pages fetched", len(requested_urls) == 2, f"got {len(requested_urls)}")
    check("pull not truncated", not truncated, stop_reason)
    check("both rows returned", len(records) == 2)
    second_url = requested_urls[1]
    check("page 2 carries the AMOUNT cursor key",
          "last_contribution_receipt_amount=3500.00" in second_url, second_url)
    check("page 2 never sends a bare 'None' date cursor",
          "last_contribution_receipt_date=None" not in second_url, second_url)
    check("page 2 never invents a date cursor key at all "
          "(none was in last_indexes)",
          "last_contribution_receipt_date" not in second_url, second_url)


# ─────────────────────────────────────────────────────────────────────────
# 1b. Hop-2 tracing (individuals_only=False) must actually omit the
#     is_individual filter — a real pre-existing bug (flagged by a GitHub
#     Copilot review comment on this PR, verified against the code and
#     fixed here): the base params dict used to set is_individual=true
#     unconditionally, before the `if individuals_only:` branch, so
#     individuals_only=False never took the filter back off. That silently
#     kept trace_spender_donors (Hop 2 — tracing who funds a super PAC)
#     individuals-only, missing exactly the PAC-to-PAC transfers it exists
#     to find; CLAUDE.md documents this filter as load-bearing ONLY for the
#     direct-donor view (Pipe 1), explicitly OFF for Hop 2.
# ─────────────────────────────────────────────────────────────────────────

def test_individuals_only_false_omits_filter():
    requested_urls = []

    def fake_http(url):
        requested_urls.append(url)
        body = json.dumps({
            "results": [_row("A1", 5000.0)],
            "pagination": {"pages": 1, "last_indexes": {}},
        })
        return 200, body

    orig = fec._http_get_with_retry
    fec._http_get_with_retry = fake_http
    try:
        fec.fetch_schedule_a("C00000000", "k", 2026, individuals_only=False)
    finally:
        fec._http_get_with_retry = orig

    url = requested_urls[0]
    # NOTE: "is_individual" also appears inside the `fields=` list (it's one
    # of the requested response columns) — check for the QUERY PARAM form
    # ("is_individual=") specifically, not a bare substring match.
    check("individuals_only=False omits the is_individual query param",
          "is_individual=" not in url, url)

    # And the default (individuals_only=True) still sends it — Pipe 1's
    # direct-donor view must keep filtering out committee transfers.
    requested_urls.clear()
    fec._http_get_with_retry = fake_http
    try:
        fec.fetch_schedule_a("C00000000", "k", 2026)
    finally:
        fec._http_get_with_retry = orig
    check("individuals_only default (True) still sends is_individual=true",
          "is_individual=true" in requested_urls[0], requested_urls[0])


# ─────────────────────────────────────────────────────────────────────────
# 2. A later page that keeps failing after retries must surface as
#    truncated=True all the way through search_fec_candidate → to_jsonable
#    → app._summarize_direct (not silently dropped).
# ─────────────────────────────────────────────────────────────────────────

CAND = [{
    "candidate_id": "H1AA00000", "name": "SOMEONE, A CANDIDATE", "office": "H",
    "office_full": "House", "state": "AA", "cycles": [2026],
    "candidate_status": "C", "has_raised_funds": True,
    "principal_committees": [{"committee_id": "C00000000", "cycles": [2026]}],
}]


def test_truncation_propagates_to_summarize_direct():
    def fake_sched_a(committee_id, key, cycle, cb=None, individuals_only=True,
                     max_pages=None, smallest_first=False):
        # Simulate a later page failing after retries — exactly what
        # fetch_schedule_a itself returns when this happens for real.
        return ([_row("A1", 5000.0)], True,
                "stopped at page 2/10: HTTP 422 after 5 attempts")

    o_search, o_fetch = fec.search_fec_candidates, fec.fetch_schedule_a
    fec.search_fec_candidates = lambda q, k, per_page=10: CAND
    fec.fetch_schedule_a = fake_sched_a
    try:
        groups = fec.search_fec_candidate("SOMEONE, A CANDIDATE", "k", max_pages=10,
                                          only_candidate_id="H1AA00000")
    finally:
        fec.search_fec_candidates, fec.fetch_schedule_a = o_search, o_fetch

    check("one group returned", len(groups) == 1)
    g = groups[0]
    check("RecipientGroup.truncated set", g.truncated is True)
    check("RecipientGroup.truncated_reason set", bool(g.truncated_reason), g.truncated_reason)

    jsonable = fec.to_jsonable(groups)
    check("to_jsonable serializes truncated", jsonable[0]["truncated"] is True)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app  # noqa: E402
    summary = app._summarize_direct(jsonable)
    check("_summarize_direct aggregates truncated=True", summary.get("truncated") is True)
    check("_summarize_direct aggregates truncated_reason",
          "C00000000" in (summary.get("truncated_reason") or "")
          or bool(summary.get("truncated_reason")),
          summary.get("truncated_reason"))

    # A clean (non-truncated) group must not be flagged.
    def fake_sched_a_clean(committee_id, key, cycle, cb=None, individuals_only=True,
                           max_pages=None, smallest_first=False):
        return ([_row("B1", 5000.0)], False, "")

    fec.search_fec_candidates = lambda q, k, per_page=10: CAND
    fec.fetch_schedule_a = fake_sched_a_clean
    try:
        clean_groups = fec.search_fec_candidate("SOMEONE, A CANDIDATE", "k", max_pages=10,
                                                 only_candidate_id="H1AA00000")
    finally:
        fec.search_fec_candidates, fec.fetch_schedule_a = o_search, o_fetch
    clean_summary = app._summarize_direct(fec.to_jsonable(clean_groups))
    check("clean pull is NOT flagged truncated", clean_summary.get("truncated") is False)
    check("clean pull has empty truncated_reason", clean_summary.get("truncated_reason") == "")


# ─────────────────────────────────────────────────────────────────────────
# 3. The sched_a step in _run_search must warn, not ok, when the aggregated
#    pull came back truncated — end-to-end through the real worker function,
#    with every other network-touching stage stubbed or skipped via missing
#    optional keys (CONGRESS_API_KEY / SEARXNG_URL unset, no Anthropic key,
#    office="H" so the votes step takes its no-key skip path).
# ─────────────────────────────────────────────────────────────────────────

def test_sched_a_step_warns_on_truncation():
    import os
    os.environ.pop("CONGRESS_API_KEY", None)
    os.environ.pop("SEARXNG_URL", None)

    import app  # noqa: E402
    from steps import StepLog

    def fake_search_fec_candidate(name, key, progress_cb=None, max_pages=None,
                                  only_candidate_id=None):
        return [fec.RecipientGroup(
            recipient_name="SOMEONE, A CANDIDATE",
            donors=[fec.DonorTotal(name="DONOR, SOME", bucket="individual", total=5000.0, count=1)],
            transactions=[], total_raised=5000.0,
            latest_record_year=2025, office="H", candidate_id="H1AA00000",
            truncated=True,
            truncated_reason="C00000000: stopped at page 2/10: HTTP 422 after 5 attempts",
        )]

    def fake_outside_spending_for_candidate(candidate_id, key, cycle,
                                            candidate_name="", progress_cb=None):
        return fec.OutsideSpending(candidate_id=candidate_id)

    def fake_funding_by_cycle(candidate_id, key, cycles, include_outside=True,
                              current_cycle=None, current_outside=None, progress_cb=None):
        return []

    o_search_cand = fec.search_fec_candidate
    o_outside = fec.outside_spending_for_candidate
    o_funding = fec.funding_by_cycle
    fec.search_fec_candidate = fake_search_fec_candidate
    fec.outside_spending_for_candidate = fake_outside_spending_for_candidate
    fec.funding_by_cycle = fake_funding_by_cycle
    app.fec.search_fec_candidate = fake_search_fec_candidate
    app.fec.outside_spending_for_candidate = fake_outside_spending_for_candidate
    app.fec.funding_by_cycle = fake_funding_by_cycle

    job_id = "test-sched-a-truncation"
    app.JOBS[job_id] = {"log": StepLog(), "error": None, "result": {}}
    try:
        app._run_search(job_id, "SOMEONE, A CANDIDATE", "fake-fec-key", False,
                        anthropic_key="", candidate_id="H1AA00000",
                        candidate_name="SOMEONE, A CANDIDATE", office="H", state="AA")
    finally:
        fec.search_fec_candidate = o_search_cand
        fec.outside_spending_for_candidate = o_outside
        fec.funding_by_cycle = o_funding
        job = app.JOBS.pop(job_id, None)

    steps = job["log"].to_dict()["steps"]
    sched_a = next(s for s in steps if s["key"] == "sched_a")
    check("sched_a step status is warn, not ok", sched_a["status"] == "warn",
          f"got {sched_a['status']!r}: {sched_a.get('detail')}")
    check("sched_a warn detail mentions the stop reason",
          "stopped early" in (sched_a.get("detail") or ""), sched_a.get("detail"))


def main() -> int:
    test_amount_sort_cursor()
    test_individuals_only_false_omits_filter()
    test_truncation_propagates_to_summarize_direct()
    test_sched_a_step_warns_on_truncation()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all schedule_a pagination checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

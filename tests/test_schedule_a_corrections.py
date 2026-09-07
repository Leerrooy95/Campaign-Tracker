"""Regression test for the Schedule A corrections found in Run 16 (Collins 2026).
Offline — the rows below mirror the real FEC data that exposed both bugs.

BUG 1 — memo re-itemizations counted as new money. `memo_code="X"` marks a
later report restating a receipt already counted. sub_id dedup can't catch it
(the restatement gets a NEW sub_id). Live data: Brent Scarbrough showed $21,000
when the true figure is $10,500, and 39 of 100 rows in a real pull were memos.

BUG 2 — refunds never netted. Refunds are NEGATIVE rows; because the capped
pull sorts largest-first they sit at the far end and are never reached. Live
data: Guy Millner showed $14,000 gross against a ~$7,000 net after a
2025-11-26 refund. Fixed with one extra ascending page.

Run from the repo root:  python3 tests/test_schedule_a_corrections.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fec  # noqa: E402

CAND = [{
    "candidate_id": "S6GA00390", "name": "COLLINS, MICHAEL A JR", "office": "S",
    "office_full": "Senate", "state": "GA", "cycles": [2026],
    "candidate_status": "C", "has_raised_funds": True,
    "principal_committees": [{"committee_id": "C00544684", "cycles": [2026]}],
}]

# Descending (largest-first) page — what the normal capped pull sees.
DESC = [
    # Scarbrough: one real receipt + one MEMO restatement of the same money.
    {"contributor_name": "SCARBROUGH, BRENT", "contribution_receipt_amount": 10500.0,
     "contribution_receipt_date": "2025-08-22", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "A1", "memo_code": None},
    {"contributor_name": "SCARBROUGH, BRENT", "contribution_receipt_amount": 10500.0,
     "contribution_receipt_date": "2025-08-22", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "A2", "memo_code": "X"},
    # Millner: two real receipts, later partly refunded (refund is in ASC page).
    {"contributor_name": "MILLNER, GUY", "contribution_receipt_amount": 7000.0,
     "contribution_receipt_date": "2025-11-12", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "B1", "memo_code": None},
    {"contributor_name": "MILLNER, GUY", "contribution_receipt_amount": 7000.0,
     "contribution_receipt_date": "2025-11-12", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "B2", "memo_code": None},
    # Clean donor, untouched by either correction.
    {"contributor_name": "KING JR, JAMES E", "contribution_receipt_amount": 14000.0,
     "contribution_receipt_date": "2025-08-12", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "C1", "memo_code": None},
    # Fully refunded donor — should disappear rather than show a negative total.
    {"contributor_name": "STRICK, JOY", "contribution_receipt_amount": 4570.0,
     "contribution_receipt_date": "2025-09-20", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "D1", "memo_code": None},
]

# Ascending (smallest-first) page — where refunds actually live.
ASC = [
    {"contributor_name": "MILLNER, GUY", "contribution_receipt_amount": -7000.0,
     "contribution_receipt_date": "2025-11-26", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "B3", "memo_code": None},
    {"contributor_name": "STRICK, JOY", "contribution_receipt_amount": -4570.0,
     "contribution_receipt_date": "2025-09-30", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "D2", "memo_code": None},
    # memo-coded negative: half of a redesignation pair that already nets to
    # zero — must NOT be applied again.
    {"contributor_name": "KING JR, JAMES E", "contribution_receipt_amount": -3500.0,
     "contribution_receipt_date": "2025-08-12", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "C2", "memo_code": "X"},
    # positives beyond the negatives — the netting loop must stop here
    {"contributor_name": "SOMEONE, SMALL", "contribution_receipt_amount": 5.0,
     "contribution_receipt_date": "2025-05-01", "is_individual": True,
     "committee_id": "C00544684", "sub_id": "E1", "memo_code": None},
]


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    def fake_sched_a(committee_id, key, cycle, cb=None, individuals_only=True,
                     max_pages=None, smallest_first=False):
        return ([dict(r) for r in (ASC if smallest_first else DESC)], False, "")

    o_search, o_fetch = fec.search_fec_candidates, fec.fetch_schedule_a
    fec.search_fec_candidates = lambda q, k, per_page=10: CAND
    fec.fetch_schedule_a = fake_sched_a
    try:
        groups = fec.search_fec_candidate("COLLINS, MICHAEL A JR", "k",
                                          max_pages=2, only_candidate_id="S6GA00390")
    finally:
        fec.search_fec_candidates, fec.fetch_schedule_a = o_search, o_fetch

    check("one group returned", len(groups) == 1)
    g = groups[0]
    by = {d.name.upper(): d for d in g.donors}

    # BUG 1 — memo rows dropped
    check("memo restatement dropped (Scarbrough $10,500, not $21,000)",
          "SCARBROUGH, BRENT" in by and by["SCARBROUGH, BRENT"].total == 10500.0,
          f"got {by.get('SCARBROUGH, BRENT') and by['SCARBROUGH, BRENT'].total}")
    check("memo count disclosed", g.memo_skipped == 1, f"memo_skipped={g.memo_skipped}")
    check("memo row absent from the transaction export too",
          not any(t["sub_id"] == "A2" for t in g.transactions))

    # BUG 2 — refunds netted
    check("refund netted (Millner $7,000, not $14,000)",
          "MILLNER, GUY" in by and by["MILLNER, GUY"].total == 7000.0,
          f"got {by.get('MILLNER, GUY') and by['MILLNER, GUY'].total}")
    check("fully-refunded donor removed, not shown negative",
          "STRICK, JOY" not in by)
    check("memo-coded negative NOT double-applied (King stays $14,000)",
          by["KING JR, JAMES E"].total == 14000.0,
          f"got {by['KING JR, JAMES E'].total}")
    check("refund disclosure counts", g.refunds_applied == 2 and g.refund_total == -11570.0,
          f"applied={g.refunds_applied} total={g.refund_total}")

    # totals + serialization
    check("total_raised reflects both corrections",
          g.total_raised == round(10500 + 7000 + 14000, 2), f"got {g.total_raised}")
    j = fec.to_jsonable(groups)[0]
    check("adjustments serialized",
          j["memo_skipped"] == 1 and j["refunds_applied"] == 2
          and j["refund_total"] == -11570.0)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all schedule_a correction checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

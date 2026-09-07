"""Regression test for fec._dedup_schedule_e — the notice/report double-count
fix. Offline: runs against a frozen snapshot of real openFEC rows (candidate
S8GA00180, cycle 2026, fetched 2026-07-12).

THE INVARIANT THIS PROTECTS: summing raw Schedule E rows double-counts every
expenditure that was filed both as a 24/48-hour notice (F24 / F5 notice) and on
the regular report (F3X / F5) — a real run overstated oppose spending ~2x
($934,835.78 vs the true $480,063.37). After dedup, our totals must reconcile
TO THE CENT against FEC's own /schedules/schedule_e/totals/by_candidate/
aggregate: (our total) − (disclosed notice-only sum) == (FEC aggregate).

Run from the repo root:  python3 tests/test_schedule_e_dedup.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fec  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_e_ossoff_2026_raw.json"
# FEC's own by_candidate aggregate for this candidate/cycle, fetched the same
# day as the fixture. FEC's aggregate excludes notice-only rows entirely.
FEC_AGGREGATE_OPPOSE = 462_124.63
FEC_AGGREGATE_SUPPORT = 23_791.26


def load_rows():
    return json.loads(FIXTURE.read_text())["results"]


def side_totals(rows):
    t = {"S": 0.0, "O": 0.0}
    for r in rows:
        so = (r.get("support_oppose_indicator") or "").strip().upper()
        if so in t:
            t[so] += r.get("expenditure_amount") or 0.0
    return {k: round(v, 2) for k, v in t.items()}


def main() -> int:
    rows = load_rows()
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # 0. The raw sum is the bug: confirm the fixture still reproduces it, so
    #    this test is actually exercising the failure mode.
    raw = side_totals(rows)
    check("fixture reproduces the raw double-count",
          raw == {"S": 66_522.65, "O": 934_835.78}, f"raw={raw}")

    kept, stats = fec._dedup_schedule_e([dict(r) for r in rows])
    ded = side_totals(kept)

    # 1. The headline invariant: reconcile to the cent against FEC's aggregate.
    check("oppose reconciles to the cent vs FEC aggregate",
          round(ded["O"] - stats["notice_only_oppose"], 2) == FEC_AGGREGATE_OPPOSE,
          f"{ded['O']} − {stats['notice_only_oppose']} vs {FEC_AGGREGATE_OPPOSE}")
    check("support reconciles to the cent vs FEC aggregate",
          round(ded["S"] - stats["notice_only_support"], 2) == FEC_AGGREGATE_SUPPORT,
          f"{ded['S']} − {stats['notice_only_support']} vs {FEC_AGGREGATE_SUPPORT}")

    # 2. Known drop counts for this snapshot.
    check("drop accounting",
          stats["raw_rows"] == 89 and stats["memo_dropped"] == 7
          and stats["superseded_dropped"] == 0 and stats["notice_deduped"] == 7
          and stats["notice_only_kept"] == 5, f"stats={stats}")

    # 3. Idempotence: deduping already-deduped rows changes nothing.
    kept2, stats2 = fec._dedup_schedule_e([dict(r) for r in kept])
    check("idempotent",
          len(kept2) == len(kept) and stats2["notice_deduped"] == 0
          and stats2["memo_dropped"] == 0 and stats2["superseded_dropped"] == 0)

    # 4. Per-spender corrections through the real aggregation path (HTTP stubbed).
    orig = fec.fetch_schedule_e
    fec.fetch_schedule_e = lambda *a, **k: ([dict(r) for r in rows], False, "", 89)
    try:
        out = fec.outside_spending_for_candidate("S8GA00180", "unused", 2026, "OSSOFF")
    finally:
        fec.fetch_schedule_e = orig
    by_id = {s.committee_id: s for s in out.spenders}
    check("SLF PAC halves to the true total",
          round(by_id["C00571703"].oppose_total, 2) == 313_599.98
          and by_id["C00571703"].count == 5)
    check("AMERICA ONE halves", round(by_id["C00728667"].oppose_total, 2) == 87_500.00)
    check("HARDWORKING AMERICANS halves",
          round(by_id["C00828608"].oppose_total, 2) == 53_672.43)
    check("CatholicVote F5 flagged notice-only",
          round(by_id["C90011800"].notice_only_total, 2) == 17_938.74)
    check("Forward Blue is entirely notice-only",
          round(by_id["C00835041"].notice_only_total, 2) == 28_800.00
          and round(by_id["C00835041"].support_total, 2) == 28_800.00)
    check("record_count stays RAW for the #3396 pagination check",
          out.record_count == 89)
    check("dedup stats ride the result", out.dedup == stats)

    # 5. Superseded-amendment rule (synthetic — the snapshot has none).
    synth = [
        {"committee_id": "C1", "support_oppose_indicator": "O",
         "expenditure_amount": 100.0, "expenditure_date": "2026-01-01",
         "is_notice": False, "most_recent": False, "memo_code": None},
        {"committee_id": "C1", "support_oppose_indicator": "O",
         "expenditure_amount": 150.0, "expenditure_date": "2026-01-01",
         "is_notice": False, "most_recent": True, "memo_code": None},
        # most_recent=None must be KEPT (older processed data lacks the flag)
        {"committee_id": "C2", "support_oppose_indicator": "S",
         "expenditure_amount": 50.0, "expenditure_date": "2026-01-02",
         "is_notice": False, "most_recent": None, "memo_code": None},
    ]
    k3, s3 = fec._dedup_schedule_e(synth)
    check("superseded amendment dropped, None kept",
          s3["superseded_dropped"] == 1 and side_totals(k3) == {"S": 50.0, "O": 150.0})

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all schedule_e dedup checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

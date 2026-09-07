"""Offline test for funding_by_cycle's two-source outside-spending design
(no network). Verifies:
  * current cycle reuses the passed-in per-spender OutsideSpending (NO second
    row pull), keeps notice-only dollars, and computes traceable/dark over BOTH
    support and oppose (the old code was support-only);
  * prior cycles take support/oppose from the FEC aggregate and leave
    traceable/dark null;
  * outside_spending_for_candidate is never called when current_outside is
    supplied (no double-pull of the current cycle).

Run from the repo root:  python3 tests/test_composition_sources.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fec  # noqa: E402

CID = "S8GA00180"
CURRENT = 2026


def make_current_outside():
    """A stand-in for the current-cycle sched_e pull: two traceable support
    spenders, one traceable oppose spender, one DARK oppose spender, plus a
    notice-only support dollar baked into the deduped totals."""
    spenders = [
        fec.OutsideSpender(committee_id="C_S1", support_total=28_800.0, oppose_total=0.0,
                           traceable=True),
        fec.OutsideSpender(committee_id="C_S2", support_total=22_100.89, oppose_total=0.0,
                           traceable=True),
        fec.OutsideSpender(committee_id="C_O1", support_total=0.0, oppose_total=462_124.63,
                           traceable=True),
        fec.OutsideSpender(committee_id="C_DARK", support_total=0.0, oppose_total=17_938.74,
                           traceable=False),   # dark money, OPPOSE side
    ]
    # deduped totals INCLUDE disclosed notice-only (support carries an extra
    # $13,782.43 of notice-only beyond the two spenders' listed support)
    return fec.OutsideSpending(
        candidate_id=CID, cycle=CURRENT,
        support_total=64_684.32, oppose_total=480_063.37,
        spenders=spenders, incomplete=False, incomplete_reason="",
        dedup={"notice_only_support": 13_782.43, "notice_only_oppose": 17_938.74},
    )


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ── stub the three network functions ────────────────────────────────────
    calls = {"agg": [], "rowpull": 0}

    def fake_totals(cid, key, cyc):
        # candidate active in all four cycles; receipts vary, splits fixed
        return {"receipts": 1000.0 + cyc,
                "individual_itemized_contributions": 400.0,
                "individual_unitemized_contributions": 600.0,
                "other_political_committee_contributions": 50.0}

    def fake_aggregate(cid, key, cyc):
        calls["agg"].append(cyc)
        # authoritative prior-cycle numbers (what FEC returns)
        table = {2020: {"support": 18_216_333.17, "oppose": 136_098_062.14},
                 2022: {"support": 6_009_383.24,  "oppose": 68_000.00},
                 2024: {"support": 0.0, "oppose": 0.0}}
        return table.get(cyc, {"support": 0.0, "oppose": 0.0})

    def fake_rowpull(*a, **k):
        calls["rowpull"] += 1
        raise AssertionError("outside_spending_for_candidate must NOT be called "
                             "when current_outside is supplied")

    orig = (fec.candidate_totals, fec.outside_totals_aggregate,
            fec.outside_spending_for_candidate)
    fec.candidate_totals = fake_totals
    fec.outside_totals_aggregate = fake_aggregate
    fec.outside_spending_for_candidate = fake_rowpull
    try:
        fcs = fec.funding_by_cycle(
            CID, "unused", [2020, 2022, 2024, 2026], include_outside=True,
            current_cycle=CURRENT, current_outside=make_current_outside())
    finally:
        (fec.candidate_totals, fec.outside_totals_aggregate,
         fec.outside_spending_for_candidate) = orig

    by_cyc = {fc.cycle: fc for fc in fcs}

    # 1. No double-pull of the current cycle.
    check("current cycle did NOT trigger a second row pull", calls["rowpull"] == 0)
    check("aggregate called only for the three prior cycles",
          sorted(calls["agg"]) == [2020, 2022, 2024], f"agg calls={calls['agg']}")

    # 2. Current cycle: totals reused verbatim, incl. notice-only.
    cur = by_cyc[2026]
    check("current support reused (with notice-only)", cur.outside_support == 64_684.32)
    check("current oppose reused (with notice-only)", cur.outside_oppose == 480_063.37)
    check("current source tagged 'spenders'", cur.outside_totals_source == "spenders")

    # 3. traceable/dark now cover BOTH sides. Dark = the OPPOSE-side dark spender
    #    ($17,938.74) — which the old support-only code would have MISSED entirely.
    check("dark covers oppose-side dark money", cur.outside_dark == 17_938.74,
          f"got {cur.outside_dark}")
    #    Traceable = the two support spenders + the traceable oppose spender.
    check("traceable covers both sides",
          cur.outside_traceable == round(28_800.0 + 22_100.89 + 462_124.63, 2),
          f"got {cur.outside_traceable}")

    # 4. Prior cycles: authoritative aggregate, null split.
    c20 = by_cyc[2020]
    check("2020 support from aggregate", c20.outside_support == 18_216_333.17)
    check("2020 oppose from aggregate", c20.outside_oppose == 136_098_062.14)
    check("2020 traceable/dark are null (not computed)",
          c20.outside_traceable is None and c20.outside_dark is None)
    check("2020 source tagged 'fec_aggregate'", c20.outside_totals_source == "fec_aggregate")
    check("2020 not marked incomplete (aggregate is authoritative)", c20.incomplete is False)

    # 5. jsonable emits nulls without error.
    j = fec.funding_cycle_to_jsonable(c20)
    check("jsonable emits null traceable/dark for prior cycle",
          j["outside_traceable"] is None and j["outside_dark"] is None
          and j["outside_totals_source"] == "fec_aggregate")

    # 6. Fallback: no current_outside supplied -> every cycle uses the aggregate,
    #    split omitted everywhere, still no row pull.
    calls["agg"].clear()
    fec.candidate_totals = fake_totals
    fec.outside_totals_aggregate = fake_aggregate
    fec.outside_spending_for_candidate = fake_rowpull
    try:
        fcs2 = fec.funding_by_cycle(CID, "unused", [2020, 2026], include_outside=True)
    finally:
        (fec.candidate_totals, fec.outside_totals_aggregate,
         fec.outside_spending_for_candidate) = orig
    check("fallback: all cycles via aggregate, split null",
          all(fc.outside_traceable is None for fc in fcs2)
          and sorted(calls["agg"]) == [2020, 2026])

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all composition-source checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

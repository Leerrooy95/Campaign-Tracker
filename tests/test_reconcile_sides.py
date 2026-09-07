"""Regression test for reconcile.check_spender_sides — the Run-13 false-positive
fix. The real Ossoff-2026 report is correct on every spender's side, yet v1
emitted five false "wrong side" highs: a "… are marked traceable" enumeration
listed the support spenders with no side word of its own and inherited the
"opposing" cue from the preceding CATHOLICVOTE sentence via a running-cue
fallback. The fix reads a side only where the prose states it next to the
spender (own segment cue, bound to that spender). This test pins:
  1. the real report now yields ZERO spender_side flags, and
  2. a genuinely corrupted report (one spender's inline side flipped) is STILL
     caught — the fix tightened precision without going blind.

Run from the repo root:  python3 tests/test_reconcile_sides.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reconcile  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "reconcile_run13.json"


def main() -> int:
    fx = json.loads(FIXTURE.read_text())
    report = fx["report"]
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # 1. The real, correct report must now produce NO spender_side flags.
    warns = reconcile.check_spender_sides(fx, report)
    flagged = sorted(w["message"].split('"')[1] for w in warns)
    check("real report yields zero spender_side flags", not warns,
          f"still flagged: {flagged}" if warns else "")

    # sanity: v1 would have flagged these five (documents what we fixed)
    check("fixture documents the five v1 false positives",
          fx["reconcile_v1_false_flags"] ==
          ["ACTIVATE AMERICA", "FIELD TEAM 6, INC.", "FORWARD BLUE",
           "GIVEGREEN UNITED ACTION", "INDIGO PAC"])

    # 2. Genuine flip must STILL be caught. FORWARD BLUE is support ($28,800);
    #    rewrite its timeline line to claim opposition, next to its own amount.
    corrupted = report.replace(
        "FORWARD BLUE (C00835041) recorded its last transaction in a run totaling "
        "$28,800 in support",
        "FORWARD BLUE (C00835041) recorded its last transaction in a run totaling "
        "$28,800 in opposition")
    check("corruption actually applied", corrupted != report)
    cwarns = reconcile.check_spender_sides(fx, corrupted)
    cflagged = [w["message"].split('"')[1] for w in cwarns]
    check("genuine inline flip on FORWARD BLUE is caught",
          "FORWARD BLUE" in cflagged, f"flagged: {cflagged}")
    check("only the corrupted spender is flagged (no collateral)",
          cflagged == ["FORWARD BLUE"], f"flagged: {cflagged}")

    # 3. An oppose spender flipped to support inline is also caught (symmetry).
    #    SLF PAC is oppose ($313,599.98).
    corrupted2 = report.replace(
        "SLF PAC (C00571703) recorded its last transaction in a run totaling "
        "$313,599.98 in opposition",
        "SLF PAC (C00571703) recorded its last transaction in a run totaling "
        "$313,599.98 in support")
    c2 = [w["message"].split('"')[1] for w in reconcile.check_spender_sides(fx, corrupted2)]
    check("genuine inline flip on SLF PAC is caught", c2 == ["SLF PAC"], f"flagged: {c2}")

    # 4. Full reconcile() over the real report: no HIGH spender_side severity.
    full = reconcile.reconcile(fx, report)
    high_side = [w for w in full["warnings"]
                 if w["check"] == "spender_side" and w["severity"] == "high"]
    check("full reconcile: no high spender_side warnings on the real report",
          not high_side, f"{len(high_side)} remain")

    # 5. The "for <candidate>" support cue generalizes to ANY candidate — it
    #    used to be hardcoded to 'jon|ossoff', which silently failed for everyone
    #    else. Verify it now keys off the candidate under analysis.
    bt = reconcile._candidate_name_terms(
        {"candidate_name": "BOEBERT, LAUREN", "candidate": "Lauren Boebert"})
    check("'for Boebert' reads support for Boebert",
          reconcile._side_signal("a PAC spent $5,000 for Boebert", bt) == "support")
    check("'against Boebert' reads oppose",
          reconcile._side_signal("spent $5,000 against Boebert", bt) == "oppose")
    check("wrong candidate's name doesn't trigger",
          reconcile._side_signal("spent for Ossoff", bt) is None)
    ot = reconcile._candidate_name_terms({"candidate_name": "OSSOFF, T. JONATHAN"})
    check("still works for Ossoff (backward compatible)",
          reconcile._side_signal("spent for Ossoff", ot) == "support")

    # 6. oppose_attribution — the Run-17 miss. The per-spender check passed
    #    because timeline entries were individually right; only the SUMMARY
    #    sentence handed the opposition money to "his opponent".
    res = {"track_a_outside": {"oppose_total": 7_006_560.55, "support_total": 5_378_133.28,
                               "spenders": []}}
    bad = ("**Outside spending** for the 2026 cycle:\n"
           "- Supporting Gallrein: **$5,378,133.28**\n"
           "- Opposing (presumably his opponent, though the data does not specify "
           "the target): **$7,006,560.55**")
    ow = reconcile.check_oppose_attribution(res, bad)
    check("misattributed opposition spending is caught", len(ow) == 1 and ow[0]["severity"] == "high",
          f"{len(ow)} flagged")
    good = ("Outside groups spent $7,006,560.55 opposing Gallrein and $5,378,133.28 "
            "supporting him. His opponent also drew outside money.")
    check("correct prose mentioning an opponent is NOT flagged",
          reconcile.check_oppose_attribution(res, good) == [])
    check("'target is not specified' phrasing caught",
          len(reconcile.check_oppose_attribution(
              res, "Outside opposition spending of $7,006,560.55 — the target is not specified.")) == 1)
    check("no oppose money → check is a no-op",
          reconcile.check_oppose_attribution(
              {"track_a_outside": {"oppose_total": 0}}, bad) == [])
    check("oppose_attribution runs inside reconcile()",
          reconcile.reconcile(res, bad)["ok"] is False)

    # 7. Committee-name masking — the Run-20 false positive. "DEFEATING
    #    COMMUNISM PAC" ($822,500 SUPPORT) was flagged high because "DEFEATING"
    #    in its own name substring-matched the "defeat" oppose cue: the segment
    #    naming the PAC read as an oppose claim made by the PAC's own name.
    #    Names are masked out before the cue read; a real verb still triggers.
    dcp = {"candidate_name": "MASSIE, THOMAS", "candidate": "Thomas Massie",
           "track_a_outside": {"oppose_total": 0, "support_total": 822_500.0,
                               "spenders": [{"committee_name": "DEFEATING COMMUNISM PAC",
                                             "committee_id": "C00999999",
                                             "support_total": 822_500.0,
                                             "oppose_total": 0}]}}
    benign = ("Among the support spenders, DEFEATING COMMUNISM PAC recorded "
              "$822,500 across the cycle.")
    check("cue word inside a committee name is not read as a side (Run 20)",
          reconcile.check_spender_sides(dcp, benign) == [])
    nameless = ("The largest was DEFEATING COMMUNISM PAC, at $822,500.")
    check("name-only segment with no verb yields no flag",
          reconcile.check_spender_sides(dcp, nameless) == [])
    flipped = ("DEFEATING COMMUNISM PAC spent $822,500 opposing Massie.")
    fl = [w["message"].split('"')[1]
          for w in reconcile.check_spender_sides(dcp, flipped)]
    check("masking doesn't blind the check — a real inline flip is still caught",
          fl == ["DEFEATING COMMUNISM PAC"], f"flagged: {fl}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all reconcile spender-side checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

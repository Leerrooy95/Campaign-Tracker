"""Offline tests for the deterministic spender ledger (no network, no API).

The structural fix behind reconcile.py's one known gap: the per-spender
support/oppose list is now rendered IN CODE from track_a_outside and swapped in
for a placeholder the model writes, so the model never restates a spender's
side at all. Verifies:
  * ledger content — sides exactly as filed, largest-total-first order, dark
    money flagged, split spenders show both sides, notice-only dollars
    disclosed, totals line present, zero-dollar spenders omitted;
  * placeholder surgery — replaced in place, duplicate placeholders removed,
    missing placeholder → ledger lands at the end of the Money Picture section
    (before ## Legislative Record) or is appended, no-spender results strip
    stray placeholders;
  * reconcile interplay — a report whose per-spender section IS the generated
    ledger produces zero side-contradiction findings (correct by construction).

Run from the repo root:  python3 tests/test_spender_ledger.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reconcile  # noqa: E402
import synthesis  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


RESULT = {
    "candidate_name": "OSSOFF, T. JONATHAN",
    "track_a_outside": {
        "cycle": 2026,
        "support_total": 64_684.32,
        "oppose_total": 645_138.70,
        "spenders": [
            {"committee_id": "C00571703", "committee_name": "SLF PAC",
             "support_total": 0, "oppose_total": 627_199.96, "count": 10,
             "first_date": "2025-08-20", "last_date": "2026-02-02",
             "traceable": True, "notice_only_total": 0},
            {"committee_id": "C90011800", "committee_name": "CATHOLICVOTE.ORG",
             "support_total": 0, "oppose_total": 17_938.74, "count": 1,
             "first_date": "2026-05-18", "last_date": "2026-05-18",
             "traceable": False, "notice_only_total": 0},
            {"committee_id": "C00835041", "committee_name": "FORWARD BLUE",
             "support_total": 20_100.0, "oppose_total": 0, "count": 1,
             "first_date": "2026-06-20", "last_date": "2026-06-20",
             "traceable": True, "notice_only_total": 13_782.43},
            {"committee_id": "C00000042", "committee_name": "SPLIT GROUP",
             "support_total": 30_801.89, "oppose_total": 1_000.0, "count": 4,
             "first_date": "2025-11-01", "last_date": "2026-03-15",
             "traceable": True, "notice_only_total": 0},
            {"committee_id": "C00000099", "committee_name": "ZERO DOLLARS INC",
             "support_total": 0, "oppose_total": 0, "count": 0,
             "traceable": True, "notice_only_total": 0},
        ],
    },
}

# ── 1. ledger content ────────────────────────────────────────────────────────
ledger = synthesis.build_spender_ledger(RESULT)
lines = [l for l in ledger.split("\n") if l.startswith("- ")]

check("one line per spender with money; zero-dollar spender omitted",
      len(lines) == 4 and "ZERO DOLLARS" not in ledger)
check("sorted largest total first",
      lines[0].startswith("- **SLF PAC**") and "SPLIT GROUP" in lines[1])
check("oppose side stated exactly as filed",
      "$627,199.96 spent opposing the candidate" in lines[0]
      and "10 transactions" in lines[0] and "2025-08-20 to 2026-02-02" in lines[0])
check("dark money flagged on its line",
      any("CATHOLICVOTE" in l and "NOT traceable (dark money" in l for l in lines))
check("split spender shows BOTH sides on one line",
      any("SPLIT GROUP" in l and "spent supporting the candidate" in l
          and "spent opposing the candidate" in l for l in lines))
check("notice-only dollars disclosed per spender",
      any("FORWARD BLUE" in l and "$13,782.43" in l and "24/48-hour" in l
          for l in lines))
check("totals line restates cycle for/against",
      "Ledger totals (2026 cycle): $64,684.32 supporting / $645,138.70 opposing"
      in ledger)
check("ledger is labeled machine-generated",
      "machine-generated" in ledger)

# ── 2. placeholder surgery ───────────────────────────────────────────────────
PH = synthesis.SPENDER_LEDGER_PLACEHOLDER
rep = f"## Money Picture\nTotals here.\n{PH}\nDonors here.\n## Legislative Record\nBills."
out, how = synthesis.insert_spender_ledger(rep, ledger)
check("placeholder replaced in place",
      how == "replaced" and PH not in out
      and out.index("SLF PAC") < out.index("Donors here"))

rep2 = f"## Money Picture\n{PH}\nmid\n{PH}\n## Legislative Record\nBills."
out2, how2 = synthesis.insert_spender_ledger(rep2, ledger)
check("duplicate placeholders: one ledger, extras removed",
      how2 == "replaced" and PH not in out2 and out2.count("SLF PAC") == 1)

rep3 = "## Money Picture\nTotals only, model forgot the placeholder.\n## Legislative Record\nBills."
out3, how3 = synthesis.insert_spender_ledger(rep3, ledger)
check("missing placeholder: ledger inserted before ## Legislative Record",
      how3 == "appended"
      and out3.index("SLF PAC") < out3.index("## Legislative Record"))

out4, how4 = synthesis.insert_spender_ledger("no sections at all", ledger)
check("missing placeholder + no marker: ledger appended at end",
      how4 == "appended" and out4.rstrip().endswith(ledger.split("\n")[-1]))

out5, how5 = synthesis.insert_spender_ledger(f"text\n{PH}\nmore", "")
check("no spenders: empty ledger, stray placeholder stripped",
      how5 == "none" and PH not in out5 and "text" in out5 and "more" in out5)

check("no spenders → empty ledger string",
      synthesis.build_spender_ledger({}) == ""
      and synthesis.build_spender_ledger({"track_a_outside": {"spenders": []}}) == "")

# ── 3. reconcile interplay: the generated ledger is correct by construction ──
report = ("## Money Picture\n"
          "In the 2026 cycle, outside groups spent $64,684.32 supporting "
          "Ossoff and $645,138.70 opposing Ossoff.\n\n"
          + ledger +
          "\n\n## Gaps and Caveats\nNone beyond the data's own flags.\n")
rec = reconcile.reconcile(RESULT, report)
side_flags = [w for w in rec.get("warnings", [])
              if w.get("check") in ("spender_side", "oppose_attribution")]
check("reconcile: zero side-contradiction findings on the generated ledger",
      not side_flags, str(side_flags))
check("reconcile: no high-severity findings at all (ok=True)",
      rec.get("ok") is True,
      str([w for w in rec.get("warnings", []) if w.get("severity") == "high"]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")

"""Offline regression test for the 'Did You Mean?' candidate resolution
(no network). Covers the two real failures it fixes:
  * a nickname FEC can't match ('Mike' → zero hits) broadens to the last name
    and floats the right person to the top by first-name similarity — with NO
    alias table; and
  * a picked candidate_id resolves to EXACTLY that candidate's committees, so a
    person with several runs (House vs Senate) doesn't get aggregated.

Run from the repo root:  python3 tests/test_candidate_resolve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fec  # noqa: E402

# Canned FEC candidate rows (trimmed to fields the resolver reads), mirroring
# the real 'collins' result set: two GA Michael-A-Jr Collins (Senate + House,
# both current, 2026) plus noise.
COLLINS = [
    {"candidate_id": "S6GA00390", "name": "COLLINS, MICHAEL A JR", "office": "S",
     "office_full": "Senate", "state": "GA", "party_full": "REPUBLICAN PARTY",
     "cycles": [2026], "candidate_status": "C", "has_raised_funds": True,
     "principal_committees": [{"committee_id": "C_SEN", "cycles": [2026]}]},
    {"candidate_id": "H4GA10071", "name": "COLLINS, MICHAEL A JR", "office": "H",
     "office_full": "House", "state": "GA", "party_full": "REPUBLICAN PARTY",
     "cycles": [2022, 2024, 2026], "candidate_status": "C", "has_raised_funds": True,
     "district": "10", "principal_committees": [{"committee_id": "C_HSE", "cycles": [2026]}]},
    {"candidate_id": "S6ME00159", "name": "COLLINS, SUSAN M.", "office": "S",
     "office_full": "Senate", "state": "ME", "party_full": "REPUBLICAN PARTY",
     "cycles": [2020, 2026], "candidate_status": "C", "has_raised_funds": True,
     "principal_committees": [{"committee_id": "C_SUS", "cycles": [2026]}]},
    {"candidate_id": "H2GA03070", "name": "COLLINS, MICHAEL ALLEN", "office": "H",
     "office_full": "House", "state": "GA", "party_full": "REPUBLICAN PARTY",
     "cycles": [2006, 2008], "candidate_status": "P", "has_raised_funds": True,
     "principal_committees": []},
]
BOEBERT = [
    {"candidate_id": "H0CO03165", "name": "BOEBERT, LAUREN", "office": "H",
     "office_full": "House", "state": "CO", "party_full": "REPUBLICAN PARTY",
     "cycles": [2022, 2024, 2026], "candidate_status": "C", "has_raised_funds": True,
     "principal_committees": [{"committee_id": "C_BO", "cycles": [2026]}]},
]


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    orig = fec.search_fec_candidates

    # 1. Nickname miss → broaden → Michael Collins on top, both GA runs surfaced.
    def fake_search_mike(q, key, per_page=10):
        return [] if q.lower() == "mike collins" else COLLINS
    fec.search_fec_candidates = fake_search_mike
    try:
        out = fec.resolve_candidate_options("mike collins", "k", limit=10)
    finally:
        fec.search_fec_candidates = orig
    ids = [c["candidate_id"] for c in out["candidates"]]
    check("nickname triggered broaden", out["broadened"] is True)
    check("both GA Mike Collins runs present (Senate + House)",
          "S6GA00390" in ids and "H4GA10071" in ids, f"ids={ids}")
    check("a Michael-A-Jr Collins ranks first (not Susan)",
          out["candidates"][0]["candidate_id"] in ("S6GA00390", "H4GA10071"),
          f"top={out['candidates'][0]['name']}")
    check("Senate run carries its office/cycle for the picker",
          any(c["candidate_id"] == "S6GA00390" and c["office_full"] == "Senate"
              and c["last_cycle"] == 2026 for c in out["candidates"]))

    # 2. Clean single match — no broaden, one option (always-confirm still shows it).
    fec.search_fec_candidates = lambda q, key, per_page=10: BOEBERT
    try:
        b = fec.resolve_candidate_options("lauren boebert", "k")
    finally:
        fec.search_fec_candidates = orig
    check("clean name: no broaden, single option",
          b["broadened"] is False and len(b["candidates"]) == 1
          and b["candidates"][0]["candidate_id"] == "H0CO03165")

    # 3. only_candidate_id resolves to EXACTLY that candidate's committees.
    seen_committees = []

    def fake_sched_a(committee_id, key, cycle, cb=None, max_pages=None, individuals_only=True):
        seen_committees.append(committee_id)
        return ([{"contributor_name": "X", "contribution_receipt_amount": 100,
                  "contribution_receipt_date": "2026-01-01", "is_individual": True,
                  "committee_id": committee_id, "sub_id": "1"}], False, "")

    fec.search_fec_candidates = lambda q, key, per_page=10: COLLINS
    orig_sched = fec.fetch_schedule_a
    fec.fetch_schedule_a = fake_sched_a
    try:
        groups = fec.search_fec_candidate("COLLINS, MICHAEL A JR", "k",
                                          only_candidate_id="S6GA00390")
    finally:
        fec.search_fec_candidates = orig
        fec.fetch_schedule_a = orig_sched
    check("only the picked candidate's committee is pulled",
          seen_committees == ["C_SEN"], f"pulled={seen_committees}")
    check("exactly one group, for the picked id",
          len(groups) == 1 and groups[0].candidate_id == "S6GA00390")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all candidate-resolution checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

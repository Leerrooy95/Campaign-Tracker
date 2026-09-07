"""
test_digest_trim.py — the model's view of the legislative record is narrowed to
money-related items; the audit trail and every count stay whole.

The digest was shipping every legislative action to the model. On the Massie run
that was 966 items — 63% of a 605KB digest, ~95,000 tokens the report never
used, since the Legislative Record section asks only for the money-related bills
and the per-year counts.

These tests pin the three things that must remain true after trimming:
  1. every count survives (so the per-year line is unchanged AND the content
     hash stays sensitive to the full record),
  2. the money-related items survive,
  3. the untrimmed record survives in the result for export.

Plain Python, no framework. Run from the repo root:
    python3 tests/test_digest_trim.py
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import synthesis as S    # noqa: E402

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILURES.append(label)


def _item(cite, money, terms=()):
    return {"citation": cite, "date": "2025-05-22", "role": "sponsored",
            "title": f"title for {cite}", "latest_action": "Referred.",
            "money_related": money, "money_terms": list(terms)}


RESULT = {
    "candidate": "Test Member",
    "candidate_name": "MEMBER, TEST",
    "candidate_id": "H0XX00000",
    "cycle": 2026,
    "office": "H",
    "state": "XX",
    "track_b_record": {
        "bioguide_id": "M000001",
        "member_name": "MEMBER, TEST",
        "total_actions": 6,
        "money_related_count": 2,
        "incomplete": False,
        "incomplete_reason": "",
        "money_related": [_item("HR 1 (119th Congress)", True, ["disclose act"])],
        "by_year": {
            "2025": {"sponsored": 2, "cosponsored": 2, "money_related": 1,
                     "items": [_item("HR 1 (119th Congress)", True, ["disclose act"]),
                               _item("HR 2 (119th Congress)", False),
                               _item("HR 3 (119th Congress)", False),
                               _item("HR 4 (119th Congress)", False)]},
            "2026": {"sponsored": 1, "cosponsored": 1, "money_related": 1,
                     "items": [_item("HR 5 (119th Congress)", True, ["super pac"]),
                               _item("HR 6 (119th Congress)", False)]},
        },
    },
}

print("-- trim: counts survive --")
trimmed = S._trim_record_for_digest(RESULT["track_b_record"])
check("total_actions preserved", trimmed["total_actions"], 6)
check("money_related_count preserved", trimmed["money_related_count"], 2)
check("top-level money_related list preserved",
      len(trimmed["money_related"]), 1)
for year, sp, co, mr in (("2025", 2, 2, 1), ("2026", 1, 1, 1)):
    slot = trimmed["by_year"][year]
    check(f"{year} sponsored count", slot["sponsored"], sp)
    check(f"{year} cosponsored count", slot["cosponsored"], co)
    check(f"{year} money_related count", slot["money_related"], mr)
check("incomplete flag preserved", trimmed["incomplete"], False)

print("\n-- trim: money items kept, others dropped --")
kept = [i["citation"] for s in trimmed["by_year"].values() for i in s["items"]]
check("only money-related items remain",
      sorted(kept), ["HR 1 (119th Congress)", "HR 5 (119th Congress)"])
check("money terms ride along",
      trimmed["by_year"]["2026"]["items"][0]["money_terms"], ["super pac"])

print("\n-- trim: the filtering is disclosed to the model --")
check("digest_note present when items were dropped",
      "digest_note" in trimmed, True)
check("note says counts are complete",
      "COMPLETE" in trimmed.get("digest_note", ""), True)
check("note warns against reading items as the whole record",
      "only the items listed here" in trimmed.get("digest_note", ""), True)

# A record with nothing to drop should not carry a note (nothing was hidden).
clean = {"total_actions": 1, "by_year": {"2025": {"sponsored": 1, "cosponsored": 0,
         "money_related": 1, "items": [_item("HR 1 (119th Congress)", True)]}}}
check("no note when nothing was dropped",
      "digest_note" in S._trim_record_for_digest(clean), False)

print("\n-- trim: non-mutating, audit trail intact --")
before = copy.deepcopy(RESULT)
S.build_digest(RESULT)
check("result['track_b_record'] untouched by build_digest",
      RESULT == before, True)
check("all 6 items still in the result for export",
      sum(len(s["items"]) for s in RESULT["track_b_record"]["by_year"].values()), 6)

print("\n-- digest: the trim actually lands --")
digest = S.build_digest(RESULT)
check("non-money title absent from digest", "title for HR 3" in digest, False)
check("money title present in digest", "title for HR 1" in digest, True)
check("per-year counts present in digest", '"sponsored": 2' in digest, True)

print("\n-- hash: still sensitive to the FULL record --")
# A non-money bill moving the counts must move the hash, or a stale cached
# report would be served after the record genuinely changed.
h_before = S.content_hash(S.build_digest(RESULT))
grown = copy.deepcopy(RESULT)
grown["track_b_record"]["by_year"]["2026"]["items"].append(
    _item("HR 7 (119th Congress)", False))
grown["track_b_record"]["by_year"]["2026"]["sponsored"] += 1
grown["track_b_record"]["total_actions"] += 1
check("adding a non-money action changes the hash",
      S.content_hash(S.build_digest(grown)) != h_before, True)

# And an unchanged result must hash identically twice (cache still works).
check("identical input hashes identically",
      S.content_hash(S.build_digest(RESULT)) == h_before, True)

print("\n-- degenerate inputs --")
check("missing by_year is a no-op",
      S._trim_record_for_digest({"total_actions": 0}), {"total_actions": 0})
check("non-dict record passes through", S._trim_record_for_digest(None), None)
check("empty result digests without error",
      isinstance(S.build_digest({}), str), True)
check("errored record survives",
      S._trim_record_for_digest({"error": "no member matched"}),
      {"error": "no member matched"})

print("\n" + ("ALL PASS" if not FAILURES
              else f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES)))
sys.exit(1 if FAILURES else 0)

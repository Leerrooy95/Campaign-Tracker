"""
test_run19_fixes.py — regression tests for the defects found reviewing Run 19.

Each test pins a specific failure that shipped and went undetected, so it can't
come back quietly. Plain Python, no framework, runnable from the repo root:

    python3 tests/test_run19_fixes.py

Every case is derived from the real Run-19 output, not invented.
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import congress                      # noqa: E402
import reconcile as R                # noqa: E402
import synthesis as S                # noqa: E402
import votes as V                    # noqa: E402

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILURES.append(label)


# ── 1. reconcile: magnitude suffix must be a whole word ──────────────────────
# Run 19 parsed "$12,093.06 known only from a 24/48-hour notice" as $12,093,060
# because the K alternative had no trailing boundary. It then reported that
# figure as unsourced — the only visible symptom of a bug that was ALSO
# silently corrupting two other figures which matched unrelated sources by
# coincidence through the old 5% tolerance.
print("\n-- reconcile: money regex --")
for text, want in [
    ("includes $12,093.06 known only from a notice", 12093.06),
    ("includes $28,800 known only from a notice", 28800.0),
    ("includes $17,938.74 known only from a notice", 17938.74),
    ("$500 billionaire donors", 500.0),
    ("$1,000 millennial voters", 1000.0),
]:
    vals = R._find_dollar_values(text)
    check(f"suffix not stolen from next word: {text[:38]!r}",
          vals[0][1] if vals else None, want)

# Real suffixes must still work.
for text, want in [("$488K against", 488000.0), ("$136.1M opposing", 136100000.0),
                   ("$1.2 million spent", 1200000.0), ("$3B total", 3e9)]:
    vals = R._find_dollar_values(text)
    check(f"real suffix still parsed: {text!r}",
          vals[0][1] if vals else None, want)

# ── 2. reconcile: rounding accepted, coincidence rejected ────────────────────
# The old blanket 5% band was wide enough to hide six-figure errors at cycle
# scale. Rounded renderings must still pass; near-misses must not.
print("\n-- reconcile: provenance tolerance --")
for label, report_val, source_val, want in [
    ("legend $488K vs $487,563.37", 488000.0, 487563.37, True),
    ("legend $136.1M vs $136,098,062.14", 136100000.0, 136098062.14, True),
    ("legend $65K vs $64,684.32", 65000.0, 64684.32, True),
    ("legend $156.1M vs $156,146,537.53", 156100000.0, 156146537.53, True),
    ("exact cents", 487563.37, 487563.37, True),
    ("COINCIDENCE $28.8M vs $28,654,210", 28800000.0, 28654210.06, False),
    ("COINCIDENCE $17.9M vs $18,216,333", 17938740.0, 18216333.17, False),
    ("plain wrong: $90k vs $53,672", 90000.0, 53672.43, False),
]:
    check(label, R._matches_a_source(report_val, {source_val}), want)


# ── 3. votes: parse failure is NOT "absent from the roll" ────────────────────
# Returning None for both meant a malformed payload was published as a factual
# claim about the candidate. The two cases must stay distinguishable.
print("\n-- votes: Senate parse failure vs absence --")
GOOD_XML = b"""<?xml version="1.0"?><roll_call_vote>
  <members>
    <member><last_name>Ossoff</last_name><state>GA</state>
            <vote_cast>Yea</vote_cast></member>
    <member><last_name>Warnock</last_name><state>GA</state>
            <vote_cast>Nay</vote_cast></member>
  </members></roll_call_vote>"""

check("member on the roll returns their position",
      V._senate_member_position(GOOD_XML, "Ossoff", "GA"), "Yea")
check("member genuinely absent returns None",
      V._senate_member_position(GOOD_XML, "Ossoff", "AZ"), None)
check("unreadable XML returns PARSE_FAILED, not None",
      V._senate_member_position(b"<roll_call_vote><members>", "Ossoff", "GA"),
      V.PARSE_FAILED)
check("PARSE_FAILED is distinguishable from None",
      V.PARSE_FAILED is None, False)


# ── 4. money terms: bare "corruption" gone, anti-corruption kept ─────────────
# Bare "corruption" put a Haiti post-disaster-recovery bill in the money ledger.
# Removing "anti-corruption" too would have cost S. 1 and S. 2093 — the two
# votes a campaign-finance tool most needs — so it stays.
print("\n-- money terms --")
HAITI = ("Senate Concurs in the House Amendment to the Senate Amendment to "
         "H.R. 2471; A bill to measure the progress of post-disaster recovery "
         "and efforts to address corruption, governance, rule of law, and "
         "media freedoms in Haiti.")
S1 = ("Motion to Discharge S. 1; A bill to expand Americans' access to the "
      "ballot box, reduce the influence of big money in politics, strengthen "
      "ethics rules for public servants, and implement other anti-corruption "
      "measures for the purpose of fortifying our democracy.")
SUPERPAC = ("Motion to Waive All Applicable Budgetary Discipline Re: Sanders "
            "Amdt. No. 5451; To place reasonable limits on contributions to "
            "Super PACs which make independent expenditures.")

check("Haiti governance bill no longer money-tagged",
      congress._money_match(HAITI), [])
check("S. 1 still money-tagged via anti-corruption",
      "anti-corruption" in congress._money_match(S1), True)
check("Super PAC amendment still tagged",
      sorted(congress._money_match(SUPERPAC)), ["pac", "super pac"])

# ── 4b. plurals: opt-in only ─────────────────────────────────────────────────
# \b...\b blocks the plural — "campaign contributions" never matched
# \bcampaign contribution\b. Countable nouns now pluralize; field/statute terms
# deliberately do not, because blanket pluralization put a voter-ID amendment
# in the money ledger via "Federal elections".
print("\n-- money terms: plurals --")
for label, title, want_term in [
    ("campaign contributions", "A bill to limit campaign contributions from foreign nationals", "campaign contribution"),
    ("contribution limits", "A bill to lower contribution limits for federal candidates", "contribution limit"),
    ("lobbyists", "To require lobbyists to disclose bundled contributions", "lobbyist"),
    ("political action committees", "Political action committees disclosure reform", "political action committee"),
    ("conflicts of interest", "Conflicts of interest in the executive branch", "conflict of interest"),
    ("financial disclosures", "Financial disclosures by Members of Congress", "financial disclosure"),
    ("campaign donors", "Campaign donors and foreign influence", "campaign donor"),
    ("corporate PACs", "Ban Corporate PACs Act", "corporate pac"),
]:
    check(f"plural matches: {label}", want_term in congress._money_match(title), True)

# Terms naming a field, statute, or uncountable must NOT pluralize.
FED_ELECTIONS = ("Motion to Waive All Applicable Budgetary Discipline Re: Kennedy "
                 "Amdt. No. 5414; To provide reconciliation instructions ... "
                 "establishing photo identification requirements for voting in "
                 "elections for Federal office, and election day and the counting "
                 "of ballots in Federal elections.")
check("voter-ID amendment NOT tagged via 'Federal elections'",
      congress._money_match(FED_ELECTIONS), [])
check("FEC nomination still tagged (singular form is the real usage)",
      congress._money_match("Confirmation: Dara Lindenbaum, of Virginia, to be a "
                            "Member of the Federal Election Commission"),
      ["federal election"])

# Bare-word false positives from earlier rounds must stay dead.
for title in ["Living Donor Protection Act", "Courthouse Ethics and Transparency Act",
              "Foster Care Placement Transparency Act",
              "a public awareness campaign for COVID-19 vaccine administration",
              "provide a health savings account contribution to certain enrollees"]:
    check(f"stays unflagged: {title[:44]!r}", congress._money_match(title), [])

# Display terms come from the list itself, never reverse-engineered from regex.
check("display term is the listed term, not a regex fragment",
      all(t in congress._MONEY_TERMS
          for t in congress._money_match("Ban Corporate PACs Act")), True)
check("no term leaks regex syntax into display",
      any("?" in t or t.endswith("s?") for t in congress._MONEY_TERMS), False)


# ── 5. synthesis: per-cycle figures are machine-built ────────────────────────
# Run 18 stated itemized/unitemized dollars for all four cycles; Run 19, same
# data, gave only percentages and dropped eight figures with no warning. The
# block is now rendered in code, so the numbers cannot go missing.
print("\n-- synthesis: deterministic money picture --")
RESULT = {
    "candidate": "Jon Ossoff",
    "candidate_name": "OSSOFF, T. JONATHAN",
    "cycle": 2026,
    "track_a_composition": [
        {"cycle": 2020, "receipts": 156146537.53,
         "individual_itemized": 75460307.89, "individual_unitemized": 69694694.49,
         "large_share": 0.5199, "small_share": 0.4801,
         "pac_contributions": 863565.06,
         "outside_support": 18216333.17, "outside_oppose": 136098062.14,
         "outside_traceable": None, "outside_dark": None,
         "outside_totals_source": "fec_aggregate", "incomplete": False},
        {"cycle": 2026, "receipts": 77279766.48,
         "individual_itemized": 28654210.06, "individual_unitemized": 39110515.04,
         "large_share": 0.4228, "small_share": 0.5772,
         "pac_contributions": 920315.95,
         "outside_support": 64684.32, "outside_oppose": 487563.37,
         "outside_traceable": 534308.95, "outside_dark": 17938.74,
         "outside_totals_source": "spenders", "incomplete": False},
    ],
}
block = S.build_money_picture(RESULT)

for figure in ["$156,146,537.53", "$75,460,307.89", "$69,694,694.49",
               "$863,565.06", "$77,279,766.48", "$28,654,210.06",
               "$39,110,515.04", "$920,315.95", "$487,563.37", "$17,938.74"]:
    check(f"figure present: {figure}", figure in block, True)

check("shares still stated", "51.99%" in block and "42.28%" in block, True)
check("readable name used, not FEC filing format",
      "Jon Ossoff" in block and "OSSOFF, T. JONATHAN" not in block, True)
check("aggregate cycle discloses the missing dark split",
      "does not break spending down by spender" in block, True)
check("spender-detail cycle reports the real split",
      "$534,308.95 was traceable" in block, True)
check("empty composition yields empty block",
      S.build_money_picture({"track_a_composition": []}), "")

# The swap must be pure string surgery, in both placeholder and fallback modes.
rep, how = S.insert_money_picture(
    "## Money Picture\n\n[[MONEY_PICTURE]]\n\n## Legislative Record\n", block)
check("placeholder replaced", how, "replaced")
check("no stray placeholder left", "[[MONEY_PICTURE]]" in rep, False)

rep2, how2 = S.insert_money_picture(
    "## Money Picture\n\nSome prose.\n\n## Legislative Record\n", block)
check("missing placeholder falls back to appended", how2, "appended")
check("fallback lands inside Money Picture, before Legislative Record",
      rep2.index(block) < rep2.index("## Legislative Record"), True)

check("no block to insert cleans stray placeholders",
      S.insert_money_picture("a\n[[MONEY_PICTURE]]\nb", "")[0], "a\nb")

# The machine-built block must survive its own guard.
warns = R.check_dollar_provenance(RESULT, "## Money Picture\n" + block)
check("machine-built block is self-consistent under reconcile", warns, [])


# ── 6. statements: raw search log is exported ────────────────────────────────
print("\n-- statements: raw search log --")
import statements as ST                                    # noqa: E402

ss = ST.StatementSet(
    candidate="Jon Ossoff", backend="searxng", searched_at="2026-07-23T12:00:00+00:00",
    search_log=[
        {"angle": "dark money", "query": '"Jon Ossoff" dark money', "rank": 1,
         "url": "https://example.org/a", "title": "T", "excerpt": "E",
         "published_date": "", "engine": "google", "disposition": "kept",
         "statement_id": "s1", "detail": "classified by_candidate (rhetoric_verb)"},
        {"angle": "dark money", "query": '"Jon Ossoff" dark money', "rank": 2,
         "url": "https://example.org/a", "title": "T", "excerpt": "E",
         "published_date": "", "engine": "google",
         "disposition": "dropped_duplicate_url", "statement_id": "", "detail": ""},
    ])
js = ST.statements_to_jsonable(ss)
check("search_log rides the jsonable", len(js.get("search_log") or []), 2)
check("searched_at recorded", js.get("searched_at"), "2026-07-23T12:00:00+00:00")
check("dropped rows keep their reason",
      js["search_log"][1]["disposition"], "dropped_duplicate_url")
check("kept rows link to their statement", js["search_log"][0]["statement_id"], "s1")
check("note documents the dispositions",
      "dropped_duplicate_url" in js.get("search_log_note", ""), True)
check("offline backend still serializes cleanly",
      ST.statements_to_jsonable(ST._offline("X")).get("search_log"), [])


print("\n" + ("ALL PASS" if not FAILURES
              else f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES)))
sys.exit(1 if FAILURES else 0)

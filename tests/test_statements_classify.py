"""Regression test for the candidate-generalized statement classifier. Before
this, `_OFFICIAL_DOMAINS` and `_RHETORIC_RE` were Ossoff-only, so ANY other
candidate's run reported "no statements in their own voice" as a finding when
it was really a classifier blind spot. build_candidate_context now derives the
official domain (<lastname>.senate.gov/.house.gov) and name/pronoun rhetoric
cues from the resolved name + chamber — no per-candidate table.

Run from the repo root:  python3 tests/test_statements_classify.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import statements as st  # noqa: E402


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ── domain derivation from name + chamber ────────────────────────────────
    ho = st.build_candidate_context("Lauren Boebert", "H")
    se = st.build_candidate_context("Jon Ossoff", "S")
    check("House official domain derived", ho["official_domains"] == ("boebert.house.gov",))
    check("Senate official domain derived", se["official_domains"] == ("ossoff.senate.gov",))
    check("unknown office → both chambers tried",
          set(st.build_candidate_context("Jane Smith")["official_domains"])
          == {"smith.senate.gov", "smith.house.gov"})

    # ── name parsing (FEC 'LAST, FIRST' and 'First Last', suffix-aware) ──────
    check("split 'First Last'", st._split_person_name("Lauren Boebert") == ("Lauren", "Boebert"))
    check("split 'LAST, FIRST'", st._split_person_name("BOEBERT, LAUREN") == ("LAUREN", "BOEBERT"))
    check("split ignores suffix for last name",
          st._split_person_name("Michael Collins Jr")[1] == "Collins")

    # ── classification for a NON-Ossoff candidate (the actual bug) ───────────
    b = ho
    def cls(excerpt, src): return st.classify_statement(excerpt, src, b)[0]
    check("name + rhetoric verb → by_candidate",
          cls("Boebert introduced a bill to ban corporate PACs", "thehill.com") == "by_candidate")
    check("derived official domain → by_candidate",
          cls("A statement on money in politics", "boebert.house.gov") == "by_candidate")
    check("subdomain of official domain still matches",
          cls("A statement", "press.boebert.house.gov") == "by_candidate")
    check("data page, no rhetoric → boilerplate",
          cls("Boebert for Congress campaign finance summary", "opensecrets.org") == "boilerplate")
    check("donation CTA → boilerplate",
          cls("Donate now to help us win", "actblue.com") == "boilerplate")

    # the false-positive guard: a bare pronoun with NO candidate name present is
    # NOT the candidate's voice (e.g. an opponent quoted in a Boebert article).
    check("pronoun without candidate name → about_candidate (not her voice)",
          cls("Her opponent said he would repeal the measure", "news.com") == "about_candidate")
    # …but a pronoun WITH the candidate name present does count.
    check("pronoun with candidate name present → by_candidate",
          cls("Boebert spoke Tuesday; she later said she would push for reform", "news.com")
          == "by_candidate")

    # ── Ossoff still classifies correctly under the dynamic context ──────────
    def clso(excerpt, src): return st.classify_statement(excerpt, src, se)[0]
    check("Ossoff name + verb → by_candidate",
          clso("Ossoff urged the Senate to act", "x.com") == "by_candidate")
    check("ossoff.senate.gov → by_candidate", clso("A release", "ossoff.senate.gov") == "by_candidate")
    check("wrong candidate's name doesn't count for Ossoff ctx",
          clso("Boebert introduced a bill", "x.com") == "about_candidate")

    # ── ctx=None keeps the exact legacy behavior (standalone/tests) ─────────
    check("legacy ctx=None still classifies Ossoff rhetoric",
          st.classify_statement("Ossoff introduced a bill", "x.com")[0] == "by_candidate")

    # ── enumeration / adjectival-participle false positives (Run 16) ────────
    # quiverquant.com's tracker blurb matched name+"proposed" and was reported
    # as the candidate's own voice. "proposed" there is an adjective on
    # "legislation" inside a comma-separated topic list, not a claim.
    c = st.build_candidate_context("COLLINS, MICHAEL A JR", "S")
    blurb = ("Track Michael A Jr Collins' stock trades, net worth, portfolio, "
             "corporate donors, proposed legislation and more")
    check("tracker blurb is NOT the candidate's voice",
          st.classify_statement(blurb, "quiverquant.com", c)[0] == "boilerplate")
    check("same blurb on an unknown domain still isn't his voice",
          st.classify_statement(blurb, "randomsite.com", c)[0] != "by_candidate")
    check("genuine 'proposed legislation to …' still counts",
          st.classify_statement("Collins proposed legislation to ban insider trading",
                                "thehill.com", c)[0] == "by_candidate")
    check("third-party trackers treated as data pages",
          "quiverquant.com" in st._DATA_DOMAINS and "ballotpedia.org" in st._DATA_DOMAINS)

    # ── zero-own-voice must be explained as a METHOD limit, not a finding ────
    gal = st.build_candidate_context("GALLREIN, ED", "H")
    note = st._own_voice_note({"about_candidate": 47, "boilerplate": 3},
                              ["facebook.com", "instagram.com", "kentuckylantern.com"], gal)
    check("zero own-voice produces an explanatory note", bool(note))
    check("note says it's a search limit, not candidate silence",
          "limit of the search" in note and "NOT as evidence" in note)
    check("note names the missing official domain (challenger case)",
          "gallrein.house.gov" in note)
    check("note flags login-walled social sources", "social platforms" in note)
    check("note is EMPTY when own voice was found",
          st._own_voice_note({"by_candidate": 4}, ["ossoff.senate.gov"],
                             st.build_candidate_context("OSSOFF, T. JONATHAN", "S")) == "")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all statement-classifier checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

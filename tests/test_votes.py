"""Offline tests for votes.py — House + Senate roll-call votes (no network).

Verifies:
  * money tagging is the SAME whole-word matcher as congress.py (identity, plus
    the known regression pair: "Living Donor Protection Act" NOT tagged,
    "Ban Corporate PACs Act" tagged);
  * Senate LIS parsing: menu day-month dates resolve against <congress_year>,
    the per-vote XML's full date wins, en-bloc entries without a title don't
    crash, the member is matched by last name + state, a member missing from a
    roll is counted (not_in_roll) rather than invented, and the detail-fetch
    cap sets incomplete + a disclosed reason;
  * House beta API: bill titles are looked up (memoized) because the vote list
    has none, member position comes from the .../members endpoint by bioguideId,
    member fetches are spent ONLY on money-relevant votes, the title-lookup cap
    discloses truncation, an uncovered congress 404s quietly, and pre-118th
    requests fail with a clear coverage error;
  * chamber dispatch by FEC office code, jsonable citations/URLs, and the
    timeline integration (vote events placed, cited, sort invariant holds).

Run from the repo root:  python3 tests/test_votes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import congress  # noqa: E402
import timeline  # noqa: E402
import votes  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ── 1. tagging parity with congress.py ───────────────────────────────────────
check("matcher is congress.py's own (identity)",
      votes._money_match is congress._money_match)
check("'Ban Corporate PACs Act' tagged", bool(votes._money_match("Ban Corporate PACs Act")))
check("'Living Donor Protection Act' NOT tagged (regression pair)",
      not votes._money_match("Living Donor Protection Act"))
check("'Motion to Invoke Cloture: DISCLOSE Act of 2025' tagged",
      bool(votes._money_match("Motion to Invoke Cloture: DISCLOSE Act of 2025")))

# ── 2. congress arithmetic + LIS date parsing ────────────────────────────────
check("congress_of_year 2025/2026 → 119",
      votes.congress_of_year(2025) == 119 and votes.congress_of_year(2026) == 119)
check("first_year_of 119 → 2025", votes.first_year_of(119) == 2025)
check("default_congresses(2026) newest-first",
      votes.default_congresses(2026) == [119, 118, 117])
check("menu date '18-Dec' + 2025 → 2025-12-18",
      votes._parse_lis_date("18-Dec", 2025) == "2025-12-18")
check("full date 'December 4, 2025,  05:31 PM' → 2025-12-04",
      votes._parse_lis_date("December 4, 2025,  05:31 PM", 0) == "2025-12-04")
check("unparseable date → '' (never guessed)",
      votes._parse_lis_date("someday", 2025) == "" and votes._parse_lis_date("18-Dec", 0) == "")

# ── 3. Senate record via fixture XML ─────────────────────────────────────────
MENU = (FIX / "senate_vote_menu_119_1.xml").read_bytes()
V612 = (FIX / "senate_vote_119_1_00612.xml").read_bytes()
V300 = (FIX / "senate_vote_119_1_00300.xml").read_bytes()

fetched_urls = []


def fake_fetch(url, **kw):
    fetched_urls.append(url)
    if "vote_menu_119_1" in url:
        return MENU
    if "vote_119_1_00612" in url:
        return V612
    if "vote_119_1_00300" in url:
        return V300
    raise votes.VotesAPIError(f"unexpected fetch in test: {url}")


orig_fetch = votes._fetch_bytes
votes._fetch_bytes = fake_fetch
try:
    rec = votes.senate_vote_record("Ossoff", "GA", congresses=[119],
                                   current_year=2025)
    check("senate: 5 menu votes scanned (incl. en-bloc)", rec.total_votes_scanned == 5,
          f"scanned={rec.total_votes_scanned}")
    check("senate: 2 money-relevant seen (DISCLOSE + Corporate PACs)",
          rec.money_related_seen == 2, f"seen={rec.money_related_seen}")
    check("senate: 'Living Donor' + nomination + en-bloc NOT fetched per-vote",
          not any("00500" in u or "00250" in u or "00450" in u for u in fetched_urls))
    check("senate: only session 1 menu pulled (session 2 of 119 not started in 2025)",
          sum("vote_menu" in u for u in fetched_urls) == 1)
    check("senate: member position matched by last name + state",
          len(rec.votes) == 1 and rec.votes[0].position == "Yea")
    check("senate: not-on-roll counted, not invented", rec.not_in_roll == 1,
          f"not_in_roll={rec.not_in_roll}")
    v = rec.votes[0]
    check("senate: per-vote XML full date wins over menu day-month",
          v.date == "2025-12-04", v.date)
    check("senate: citation + primary-source URL",
          v.citation == "Senate Roll Call 612 (119th Congress, Session 1)"
          and v.url.endswith("/vote1191/vote_119_1_00612.htm"), v.url)
    check("senate: clean run is not incomplete", not rec.incomplete,
          rec.incomplete_reason)

    # cap: only the NEWEST money vote's detail is fetched; disclosed
    fetched_urls.clear()
    rec2 = votes.senate_vote_record("Ossoff", "GA", congresses=[119],
                                    current_year=2025, max_detail_fetches=1)
    check("senate cap: one detail fetch, newest first",
          sum("roll_call_votes/" in u for u in fetched_urls) == 1
          and any("00612" in u for u in fetched_urls))
    check("senate cap: incomplete + disclosed",
          rec2.incomplete and "newest 1" in rec2.incomplete_reason,
          rec2.incomplete_reason)

    # a whole menu failing is disclosed; the rest of the data still returns
    def flaky_fetch(url, **kw):
        if "vote_menu_118" in url:
            raise votes.VotesAPIError("HTTP 500")
        return fake_fetch(url)
    votes._fetch_bytes = flaky_fetch
    rec3 = votes.senate_vote_record("Ossoff", "GA", congresses=[119, 118],
                                    current_year=2025)
    check("senate: one bad menu disclosed, good congress still returned",
          rec3.incomplete and "menus unavailable" in rec3.incomplete_reason
          and rec3.total_votes_scanned == 5, rec3.incomplete_reason)

    # ALL menus failing is an error, not an empty result
    def dead_fetch(url, **kw):
        raise votes.VotesAPIError("HTTP 500")
    votes._fetch_bytes = dead_fetch
    try:
        votes.senate_vote_record("Ossoff", "GA", congresses=[119], current_year=2025)
        check("senate: all menus down raises (never silent-empty)", False)
    except votes.VotesAPIError:
        check("senate: all menus down raises (never silent-empty)", True)
finally:
    votes._fetch_bytes = orig_fetch

# ── 4. House record via stubbed Congress.gov JSON ────────────────────────────
HOUSE_LIST = [
    {"congress": 119, "sessionNumber": 2, "rollCallNumber": 3,
     "startDate": "2026-01-10T12:00:00-05:00", "result": "Passed",
     "legislationType": "HR", "legislationNumber": "9500"},
    {"congress": 119, "sessionNumber": 1, "rollCallNumber": 25,
     "startDate": "2025-03-05T18:57:00-05:00", "result": "Failed",
     "amendmentType": "HAMDT", "amendmentNumber": "6",
     "amendmentAuthor": "Tlaib of Michigan Amendment No. 6"},
    {"congress": 119, "sessionNumber": 1, "rollCallNumber": 20,
     "startDate": "2025-02-01T10:00:00-05:00", "result": "Passed",
     "legislationType": "HR", "legislationNumber": "100"},
    {"congress": 119, "sessionNumber": 1, "rollCallNumber": 17,
     "startDate": "2025-01-16T14:00:00-05:00", "result": "Passed",
     "legislationType": "HR", "legislationNumber": "9500"},
]

json_calls = {"bill_9500": 0, "bill_100": 0, "amdt_6": 0, "members": []}
# The amendment's purpose text is swappable so one stub serves both the
# "not money-relevant" default and the money-relevant amendment sub-test.
amdt_purpose = {"text": "In the nature of a substitute."}


def fake_get_json(url, **kw):
    if "/house-vote/119/" in url and url.rstrip("?").split("?")[0].endswith("/members"):
        json_calls["members"].append(url)
        if "/119/2/3/members" in url:
            results = [{"bioguideId": "Z000999", "voteCast": "Nay"}]   # member absent
        else:
            results = [{"bioguideId": "Z000999", "voteCast": "Nay"},
                       {"bioguideID": "B000001", "voteCast": "Aye"}]
        return {"houseRollCallVoteMemberVotes": {
            "voteQuestion": "On Passage", "results": results}}
    if "/house-vote/119?" in url:
        return {"houseRollCallVotes": HOUSE_LIST}
    if "/house-vote/118?" in url:
        raise congress.CongressAPIError("Congress.gov HTTP 404", status_code=404)
    if "/bill/119/hr/9500?" in url:
        json_calls["bill_9500"] += 1
        return {"bill": {"title": "DISCLOSE Act of 2025"}}
    if "/bill/119/hr/100?" in url:
        json_calls["bill_100"] += 1
        return {"bill": {"title": "Living Donor Protection Act"}}
    if "/amendment/119/hamdt/6?" in url:
        json_calls["amdt_6"] += 1
        return {"amendment": {"purpose": amdt_purpose["text"],
                              "description": ""}}
    raise AssertionError(f"unexpected URL in test: {url}")


orig_json = votes._get_json
votes._get_json = fake_get_json
try:
    rec = votes.house_vote_record("B000001", "test-key", congresses=[119, 118],
                                  current_year=2026)
    check("house: 4 list votes scanned; 118th 404 skipped quietly",
          rec.total_votes_scanned == 4, f"scanned={rec.total_votes_scanned}")
    check("house: bill titles looked up once per distinct bill (memoized)",
          json_calls["bill_9500"] == 1 and json_calls["bill_100"] == 1,
          str(json_calls))
    check("house: amendment purpose looked up once (not money-relevant here)",
          json_calls["amdt_6"] == 1)
    check("house: 2 money-relevant (both HR 9500 votes); HR 100 + amendment not",
          rec.money_related_seen == 2, f"seen={rec.money_related_seen}")
    check("house: member fetches spent ONLY on money-relevant votes",
          len(json_calls["members"]) == 2
          and all(("/3/members" in u or "/17/members" in u) for u in json_calls["members"]))
    check("house: absent-from-roll counted, not invented", rec.not_in_roll == 1)
    check("house: position by bioguideId (either key casing)",
          len(rec.votes) == 1 and rec.votes[0].position == "Aye")
    v = rec.votes[0]
    check("house: date from startDate, citation + Clerk URL",
          v.date == "2025-01-16"
          and v.citation == "House Roll Call 17 (119th Congress, Session 1)"
          and v.url == "https://clerk.house.gov/Votes/202517", v.url)
    check("house: question carried from members endpoint", v.question == "On Passage")
    check("house: clean run not incomplete", not rec.incomplete, rec.incomplete_reason)

    # title-lookup cap: newest votes first; older distinct bills left untagged,
    # disclosed. Cache hits (roll 17's HR 9500) still tag without a new lookup.
    json_calls["bill_9500"] = json_calls["bill_100"] = 0
    json_calls["members"].clear()
    rec2 = votes.house_vote_record("B000001", "test-key", congresses=[119],
                                   current_year=2026, max_title_lookups=1)
    check("house title cap: 1 lookup spent on the newest vote's bill",
          json_calls["bill_9500"] == 1 and json_calls["bill_100"] == 0)
    check("house title cap: cached bill still tags both HR 9500 votes",
          rec2.money_related_seen == 2)
    check("house title cap: truncation disclosed",
          rec2.incomplete and "capped at 1" in rec2.incomplete_reason,
          rec2.incomplete_reason)

    # detail cap
    json_calls["members"].clear()
    rec3 = votes.house_vote_record("B000001", "test-key", congresses=[119],
                                   current_year=2026, max_detail_fetches=1)
    check("house detail cap: one member fetch, incomplete disclosed",
          len(json_calls["members"]) == 1 and rec3.incomplete
          and "newest 1" in rec3.incomplete_reason, rec3.incomplete_reason)

    # pre-coverage congresses: clear error, not a silent empty
    try:
        votes.house_vote_record("B000001", "test-key", congresses=[117],
                                current_year=2022)
        check("house: pre-118th request raises with coverage reason", False)
    except votes.VotesAPIError as e:
        check("house: pre-118th request raises with coverage reason",
              "118th" in str(e), str(e))

    # amendment votes tag on the amendment's PURPOSE text (where money riders
    # actually live), not just the author string
    amdt_purpose["text"] = ("To require disclosure of dark money contributions "
                            "to independent expenditure committees")
    json_calls["members"].clear()
    rec4 = votes.house_vote_record("B000001", "test-key", congresses=[119],
                                   current_year=2026)
    check("house amendment: money-relevant purpose tagged (3 relevant now)",
          rec4.money_related_seen == 3, f"seen={rec4.money_related_seen}")
    amdt_votes = [v for v in rec4.votes if v.legislation.startswith("HAMDT")]
    check("house amendment: member position fetched, purpose kept as title",
          len(amdt_votes) == 1 and amdt_votes[0].position == "Aye"
          and "dark money" in amdt_votes[0].title
          and "dark money" in amdt_votes[0].money_terms,
          amdt_votes[0].title if amdt_votes else "none")
    amdt_purpose["text"] = "In the nature of a substitute."

    # a /members fetch FAILURE is a source problem, disclosed — never counted
    # as "not on the roll" (Copilot review regression)
    def failing_members(url, **kw):
        if "/119/1/17/members" in url:
            raise congress.CongressAPIError("Congress.gov HTTP 500", status_code=500)
        return fake_get_json(url, **kw)
    votes._get_json = failing_members
    rec6 = votes.house_vote_record("B000001", "test-key", congresses=[119],
                                   current_year=2026)
    check("house: /members failure NOT counted as not_in_roll",
          rec6.not_in_roll == 1,   # only roll 3's genuine absence
          f"not_in_roll={rec6.not_in_roll}")
    check("house: /members failure disclosed as a fetch miss, run incomplete",
          rec6.incomplete and "member-position fetch(es) failed" in rec6.incomplete_reason
          and "not absences from the roll" in rec6.incomplete_reason,
          rec6.incomplete_reason)

    # a bill-title LOOKUP failure is disclosed as incomplete — never silently
    # passed off as "not money-related" (Copilot review regression)
    def failing_bill(url, **kw):
        if "/bill/119/hr/9500?" in url:
            raise congress.CongressAPIError("Congress.gov HTTP 500", status_code=500)
        return fake_get_json(url, **kw)
    votes._get_json = failing_bill
    rec7 = votes.house_vote_record("B000001", "test-key", congresses=[119],
                                   current_year=2026)
    check("house: failed bill lookup -> vote untagged BUT disclosed as lower bound",
          rec7.incomplete and "lookup(s) failed" in rec7.incomplete_reason,
          rec7.incomplete_reason)
    check("house: failed lookup counted once per distinct measure (memoized)",
          "1 bill/amendment lookup(s) failed" in rec7.incomplete_reason,
          rec7.incomplete_reason)
finally:
    votes._get_json = orig_json

# ── 4b. votes ↔ record join (factual link, exact match only) ─────────────────
check("norm: LIS dotted forms and House forms align",
      votes._norm_legislation("S. 512") == "S512"
      and votes._norm_legislation("S.J.Res. 5") == "SJRES5"
      and votes._norm_legislation("HR 9500") == "HR9500")

votes_json = {"money_related": [
    {"congress": 119, "legislation": "S. 512", "candidate_bill_role": ""},
    {"congress": 119, "legislation": "HR 9500", "candidate_bill_role": ""},
    {"congress": 118, "legislation": "S. 512", "candidate_bill_role": ""},   # wrong congress
    {"congress": 119, "legislation": "S.J.Res. 5", "candidate_bill_role": ""},
    {"congress": 119, "legislation": "PN373", "candidate_bill_role": ""},    # nomination
]}
record_json = {"by_year": {"2025": {"items": [
    {"citation": "S 512 (119th Congress)", "role": "cosponsored"},
    {"citation": "HR 9500 (119th Congress)", "role": "sponsored"},
    {"citation": "SJRES 5 (119th Congress)", "role": "cosponsored"},
    {"citation": "S 512 (118th Congress)", "role": "sponsored"},
]}}}
n = votes.link_votes_to_record(votes_json, record_json)
mr = votes_json["money_related"]
check("join: exact congress+bill matches linked with the right role",
      mr[0]["candidate_bill_role"] == "cosponsored"
      and mr[1]["candidate_bill_role"] == "sponsored"
      and mr[3]["candidate_bill_role"] == "cosponsored")
check("join: cross-congress match uses THAT congress's role (no bleed)",
      mr[2]["candidate_bill_role"] == "sponsored")
check("join: nomination (no bill) left unlinked; count is 4",
      mr[4]["candidate_bill_role"] == "" and n == 4)
check("join: safe on empty tracks",
      votes.link_votes_to_record({}, {}) == 0
      and votes.link_votes_to_record({"money_related": []}, None) == 0)

# demo → join → timeline: the linked vote's label says whose bill it was
demo_j = votes.record_to_jsonable(votes.demo_votes())
import congress as _c  # noqa: F401 (demo record comes from congress.py)
demo_rec = _c.record_to_jsonable(_c.demo_record())
n_demo = votes.link_votes_to_record(demo_j, demo_rec)
check("demo join: DISCLOSE Act vote links to the cosponsored demo bill",
      n_demo == 1 and any(v["candidate_bill_role"] == "cosponsored"
                          and v["legislation"] == "S. 1593"
                          for v in demo_j["money_related"]))
tl_j = timeline.build_timeline({"track_b_votes": demo_j})
check("timeline: linked vote label states 'a bill they cosponsored'",
      any("a bill they cosponsored" in e["label"] for e in tl_j["events"]))

# ── 4c. Senate default lookback is 5 congresses (~10 years) ──────────────────
_menu_urls = []


def counting_fetch(url, **kw):
    if "vote_menu_119_1" in url:
        _menu_urls.append(url)
        return MENU
    if "vote_menu" in url:
        _menu_urls.append(url)
        cong = url.split("vote_menu_")[1].split("_")[0]
        return (f'<?xml version="1.0" encoding="UTF-8"?><vote_summary>'
                f'<congress>{cong}</congress><session>1</session>'
                f'<congress_year>2020</congress_year><votes></votes>'
                f'</vote_summary>').encode()
    if "vote_119_1_00612" in url:
        return V612
    if "vote_119_1_00300" in url:
        return V300
    raise votes.VotesAPIError(f"unexpected fetch: {url}")


votes._fetch_bytes = counting_fetch
try:
    rec5 = votes.senate_vote_record("Ossoff", "GA", current_year=2025)
    congs_seen = sorted({u.split("vote_menu_")[1].split("_")[0] for u in _menu_urls})
    check("senate default: 5 congresses scanned (115th–119th)",
          congs_seen == ["115", "116", "117", "118", "119"], str(congs_seen))
    check("senate default: 9 congress-session menus (119-2 not started in 2025)",
          len(_menu_urls) == 9, f"menus={len(_menu_urls)}")
    check("senate default: coverage note states the span",
          "115th–119th" in rec5.coverage_note, rec5.coverage_note)
finally:
    votes._fetch_bytes = orig_fetch

# ── 5. chamber dispatch ──────────────────────────────────────────────────────
for office, kwargs, why in [
        ("P", {}, "no chamber"),
        ("H", {"api_key": "k"}, "needs bioguide"),
        ("H", {"bioguide_id": "B000001"}, "needs key"),
        ("S", {"last_name": "Ossoff"}, "needs state")]:
    try:
        votes.vote_record(office, **kwargs)
        check(f"dispatch: office {office!r} ({why}) raises", False)
    except votes.VotesAPIError:
        check(f"dispatch: office {office!r} ({why}) raises", True)

# ── 6. jsonable + timeline integration ───────────────────────────────────────
demo = votes.record_to_jsonable(votes.demo_votes())
check("jsonable: every money vote carries a citation and URL",
      demo["money_related"]
      and all(m["citation"] and m["url"] for m in demo["money_related"]))

result = {
    "track_b_votes": demo,
    "track_b_record": {"money_related": [
        {"date": "2026-03-04", "role": "cosponsored", "citation": "S 3991 (119th)",
         "title": "DISCLOSE Act of 2026", "money_terms": ["disclose act"]}]},
    "track_a_outside": {"spenders": [
        {"committee_name": "SLF PAC", "committee_id": "C00571703",
         "oppose_total": 627199.96, "support_total": 0, "count": 10,
         "first_date": "2025-08-20", "last_date": "2026-02-02", "traceable": True}]},
}
tl = timeline.build_timeline(result)
vote_events = [e for e in tl["events"] if e["label"].startswith("voted ")]
check("timeline: all demo votes placed on the record track",
      len(vote_events) == len(demo["money_related"])
      and all(e["track"] == "record" for e in vote_events))
check("timeline: vote labels carry position + citation",
      any("voted Yea on Senate Roll Call 512 (119th Congress, Session 1)" in e["label"]
          for e in vote_events))
check("timeline: vote events cite their source",
      all(e["source"] in ("Senate LIS roll call",
                          "House Clerk roll call (via Congress.gov API)")
          for e in vote_events))
check("timeline: sort invariant still holds with votes merged",
      timeline.verify_sorted(tl))
check("timeline: absent votes track contributes nothing (no crash)",
      not any(e["label"].startswith("voted ")
              for e in timeline.build_timeline({})["events"]))

# ── verdict ──────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")

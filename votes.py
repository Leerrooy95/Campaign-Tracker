"""
votes.py — Track B roll-call votes, as FACT, not score.

The other half of "what they actually DID": how the candidate voted on the
floor, chamber by chamber, with every vote dated and carrying its own citation
(a clerk.house.gov / senate.gov URL a reader can click and check). Same rules
as congress.py: facts only, money/finance relevance tagged by the SAME
whole-word regex term list, nothing scored, incompleteness disclosed.

TWO SOURCES (they don't overlap):
  - HOUSE — the Congress.gov **beta** house-vote API
    (/v3/house-vote/{congress}...). Coverage is honest and narrow: 118th
    Congress (2023) onward, and only votes associated with a piece of
    legislation (the API excludes e.g. Speaker elections for now). Member
    positions come from the .../members sub-endpoint, matched by bioguideId —
    the same id congress.py already resolved for the record stage.
  - SENATE — Senate LIS XML (www.senate.gov/legislative/LIS/...). No API key,
    no beta caveat: a per-session vote menu XML (numbers, dates, titles) plus a
    per-vote XML whose <members> block lists every senator's vote_cast. The
    member is matched by last name + state — the data the FEC resolve step
    already produced (there is no bioguideId in LIS).

SHAPE OF THE PULL (bounded on purpose — same philosophy as the capped
Schedule A pull): the vote LISTS are cheap and pulled in full for the covered
congresses; the EXPENSIVE per-vote detail (a member-position fetch per vote,
and for the House a bill-title lookup per distinct bill) is only spent on
money/finance-RELEVANT votes, newest first, under explicit caps. Hitting a cap
sets `incomplete` + a reason — disclosed, never papered over (Integrity Rule 4).
"""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET   # types (ET.Element) and ET.ParseError only — see below
from dataclasses import dataclass, field
from typing import Callable, Optional

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _parse_xml

import congress as _congress
from congress import CongressAPIError, _money_match   # SAME whole-word regex tagging

# SECURITY.md LOW: stdlib ElementTree.fromstring is
# documented-vulnerable to entity-expansion ("billion laughs") and quadratic
# blowup DoS (not XXE/file disclosure — ET never resolves external entities
# or fetches DTDs). All THREE parse sites below (Senate vote-menu XML,
# per-vote member-position XML, per-vote date-only re-parse) now go through
# defusedxml's fromstring instead, a drop-in replacement that rejects
# entity declarations, DTDs, and external references before expansion. A
# rejected payload raises `DefusedXmlException` (a `ValueError` subclass,
# NOT an `ET.ParseError` subclass) — every `except ET.ParseError` below is
# widened to `except (ET.ParseError, DefusedXmlException)` so a blocked
# malicious payload is disclosed the same way a malformed one already was
# (PARSE_FAILED / VotesAPIError), never an unhandled crash. Realistic
# exploitability was always low here (a fixed senate.gov host, no
# user-controlled path — see the audit) but the fix is a two-line change.

CONGRESS_BASE = _congress.CONGRESS_BASE
SENATE_LIS_BASE = "https://www.senate.gov/legislative/LIS"

ProgressCallback = Optional[Callable[[int, int], None]]

# Indirection points so offline tests can stub the network in one place each
# (tests assign votes._get_json / votes._fetch_bytes), same pattern as the fec
# tests stubbing module attributes.
_get_json = _congress._get


class VotesAPIError(RuntimeError):
    """A roll-call source failed in a way that isn't 'no data'. Carries enough
    text to be actionable; the app layer turns it into a disclosed warn — a
    broken votes stage must never take down the money side."""


# ── congress arithmetic ──────────────────────────────────────────────────────
# A Congress spans two calendar years: the Nth Congress starts in 1789+2(N-1).
# Session 1 is the odd (first) year, session 2 the even year.
_HOUSE_API_FLOOR = 118   # Congress.gov house-vote coverage starts here (2023)


def congress_of_year(year: int) -> int:
    return (year - 1789) // 2 + 1


def first_year_of(congress_num: int) -> int:
    return 1789 + 2 * (congress_num - 1)


def default_congresses(current_year: int, lookback: int = 3) -> list[int]:
    """The congresses a votes pull covers by default: the current one and up to
    `lookback - 1` before it. The House path additionally floors at the API's
    coverage start (118th) — asking for more would just 404."""
    cur = congress_of_year(current_year)
    return [c for c in range(cur, cur - lookback, -1) if c >= 1]


# ── data model ───────────────────────────────────────────────────────────────
@dataclass
class RollCallVote:
    date: str               # ISO YYYY-MM-DD
    year: int
    chamber: str            # "House" | "Senate"
    congress: int
    session: int
    number: int             # roll-call number within the session/congress
    question: str           # e.g. "On Passage", "On the Cloture Motion"
    title: str              # bill title (House) / vote title (Senate)
    result: str             # e.g. "Passed", "Agreed to"
    position: str           # the member's own vote: Yea/Nay/Aye/Present/Not Voting
    legislation: str = ""   # e.g. "HR 1", "S 51", "PN373"
    money_related: bool = False
    money_terms: list = field(default_factory=list)
    candidate_bill_role: str = ""   # "sponsored"/"cosponsored" when the vote is on
                                    # the candidate's OWN bill (set by the join below)

    @property
    def citation(self) -> str:
        return (f"{self.chamber} Roll Call {self.number} "
                f"({self.congress}th Congress, Session {self.session})")

    @property
    def url(self) -> str:
        # Primary-source pages: the House Clerk's vote page (year + roll number)
        # and the Senate LIS vote page. Both are the official record.
        if self.chamber == "House":
            return f"https://clerk.house.gov/Votes/{self.year}{self.number}"
        return (f"{SENATE_LIS_BASE}/roll_call_votes/"
                f"vote{self.congress}{self.session}/"
                f"vote_{self.congress}_{self.session}_{self.number:05d}.htm")


@dataclass
class VoteRecord:
    chamber: str
    member: str                       # bioguideId (House) or "Last (ST)" (Senate)
    votes: list = field(default_factory=list)   # money-relevant RollCallVotes WITH position
    total_votes_scanned: int = 0      # every roll call whose list entry we saw
    money_related_seen: int = 0       # tagged relevant (>= len(votes) if capped/absent)
    not_in_roll: int = 0              # relevant votes where the member wasn't on the roll
    incomplete: bool = False
    incomplete_reason: str = ""
    coverage_note: str = ""


# ── Senate LIS XML fetch/parse ───────────────────────────────────────────────
_BROWSER_UA = _congress._BROWSER_UA
_LIS_TIMEOUT = 15


def _fetch_bytes(url: str, retries: int = 1, timeout: int = _LIS_TIMEOUT) -> bytes:
    """GET raw bytes from senate.gov (no Cloudflare wall there — plain urllib
    works). Fails fast with one retry, raising VotesAPIError, never hanging."""
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(1.0); continue
            break
        except Exception as e:   # noqa: BLE001 — URLError, timeout, TLS
            last = str(e)
            if attempt < retries:
                time.sleep(1.0); continue
            break
    raise VotesAPIError(f"Senate LIS fetch failed ({last}): {url}")


_MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_FULL_DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_DAY_MON_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})$")


def _parse_lis_date(raw: str, congress_year: int) -> str:
    """LIS dates come in two shapes: the menu's '18-Dec' (year lives in the
    menu's <congress_year>) and the per-vote XML's 'December 18, 2025, 09:42 PM'.
    Returns ISO YYYY-MM-DD, or '' if unparseable — never a guessed date."""
    raw = (raw or "").strip()
    m = _FULL_DATE_RE.search(raw)
    if m:
        mon = _MONTH_NUM.get(m.group(1)[:3].lower())
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
    m = _DAY_MON_RE.match(raw)
    if m and congress_year:
        mon = _MONTH_NUM.get(m.group(2).lower())
        if mon:
            return f"{congress_year:04d}-{mon:02d}-{int(m.group(1)):02d}"
    return ""


def _menu_url(congress_num: int, session: int) -> str:
    return f"{SENATE_LIS_BASE}/roll_call_lists/vote_menu_{congress_num}_{session}.xml"


def _vote_xml_url(congress_num: int, session: int, number: int) -> str:
    return (f"{SENATE_LIS_BASE}/roll_call_votes/vote{congress_num}{session}/"
            f"vote_{congress_num}_{session}_{number:05d}.xml")


def _text(el: Optional[ET.Element], tag: str) -> str:
    child = el.find(tag) if el is not None else None
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_senate_menu(xml_bytes: bytes, congress_num: int, session: int) -> list[dict]:
    """Parse a vote_menu XML into plain dicts (number, date, title, question,
    result, issue). en-bloc votes lack their own top-level question/result —
    they still get title + date, which is what the tagging needs."""
    try:
        root = _parse_xml(xml_bytes)
    except (ET.ParseError, DefusedXmlException) as e:
        raise VotesAPIError(f"Senate vote menu {congress_num}-{session} unparseable: {e}")
    year = int(_text(root, "congress_year") or 0)
    out = []
    for v in root.iter("vote"):
        num_txt = _text(v, "vote_number")
        if not num_txt.isdigit():
            continue
        out.append({
            "congress": congress_num, "session": session,
            "number": int(num_txt),
            "date": _parse_lis_date(_text(v, "vote_date"), year),
            "issue": _text(v, "issue"),
            "question": re.sub(r"\s+", " ", _text(v, "question")),
            "result": _text(v, "result"),
            "title": _text(v, "title"),
        })
    return out


# Sentinel for "the XML could not be parsed", which is NOT the same claim as
# "the member was not on this roll call". The old code returned None for both,
# so a malformed payload was silently reported as an absence — a source failure
# published as a fact about the candidate, which is exactly what Integrity Rule
# 4 exists to prevent. The House path already separates its two cases; this
# brings the Senate path to the same contract.
PARSE_FAILED = object()


def _senate_member_position(xml_bytes: bytes, last_name: str, state: str):
    """Find the member's vote_cast in a per-vote XML by last name + state (LIS
    has no bioguideId). Returns:
      - the vote_cast string when the member is on the roll
      - None when the XML parsed fine and they simply aren't on it — a real
        outcome (not yet in office), not an error
      - PARSE_FAILED when the XML itself was unreadable — a source problem the
        caller must disclose, never a claim about the member."""
    try:
        root = _parse_xml(xml_bytes)
    except (ET.ParseError, DefusedXmlException):
        return PARSE_FAILED
    ln, st = last_name.strip().lower(), state.strip().upper()
    for m in root.iter("member"):
        if (_text(m, "last_name").lower() == ln
                and _text(m, "state").upper() == st):
            return _text(m, "vote_cast") or None
    return None


def senate_vote_record(last_name: str, state: str,
                       congresses: Optional[list[int]] = None,
                       current_year: Optional[int] = None,
                       max_detail_fetches: int = 60,
                       progress_cb: ProgressCallback = None) -> VoteRecord:
    """Senate roll calls for a member via LIS XML. Full menus are scanned and
    money-tagged (cheap: one XML per congress-session); the per-vote XML (for
    the member's own position) is fetched ONLY for money-relevant votes, newest
    first, capped at `max_detail_fetches` (cap hit ⇒ incomplete, disclosed)."""
    year = current_year or time.localtime().tm_year
    # Senate default looks back 5 congresses (~10 years) — matching the funding
    # composition's window — because LIS menus are cheap (one XML per
    # congress-session) and reach back decades; the expensive per-vote detail
    # stays bounded by max_detail_fetches regardless of how far the scan goes.
    congs = congresses or default_congresses(year, lookback=5)
    scanned: list[dict] = []
    menu_misses: list[str] = []
    for c in congs:
        for session in (1, 2):
            # Session 2 of the current congress may not exist yet — that's a
            # calendar fact, not incompleteness.
            if first_year_of(c) + (session - 1) > year:
                continue
            try:
                scanned.extend(_parse_senate_menu(
                    _fetch_bytes(_menu_url(c, session)), c, session))
            except VotesAPIError as e:
                menu_misses.append(f"{c}-{session} ({e})")
    if not scanned and menu_misses:
        raise VotesAPIError("no Senate vote menus reachable: " + "; ".join(menu_misses))

    # Tag on the menu title — the LIS equivalent of the bill title, and the
    # same field-class congress.py tags on. Newest first before capping.
    relevant = [v for v in scanned if _money_match(v["title"])]
    relevant.sort(key=lambda v: v["date"], reverse=True)

    votes: list[RollCallVote] = []
    not_in_roll = 0
    fetch_misses = 0
    capped = len(relevant) > max_detail_fetches
    to_fetch = relevant[:max_detail_fetches]
    for i, v in enumerate(to_fetch):
        if progress_cb:
            progress_cb(i + 1, len(to_fetch))
        try:
            xml_bytes = _fetch_bytes(_vote_xml_url(v["congress"], v["session"], v["number"]))
        except VotesAPIError:
            fetch_misses += 1
            continue
        pos = _senate_member_position(xml_bytes, last_name, state)
        if pos is PARSE_FAILED:
            fetch_misses += 1   # unreadable source — disclosed as a miss, NOT
            continue            # as "the member wasn't on this roll"
        if pos is None:
            not_in_roll += 1
            continue
        # Prefer the per-vote XML's full date (it carries the year explicitly).
        try:
            root = _parse_xml(xml_bytes)
            full = _parse_lis_date(_text(root, "vote_date"),
                                   int(_text(root, "congress_year") or 0))
        except (ET.ParseError, DefusedXmlException):
            full = ""
        date = full or v["date"]
        terms = _money_match(v["title"])
        votes.append(RollCallVote(
            date=date, year=int(date[:4]) if date[:4].isdigit() else 0,
            chamber="Senate", congress=v["congress"], session=v["session"],
            number=v["number"], question=v["question"], title=v["title"],
            result=v["result"], position=pos, legislation=v["issue"],
            money_related=True, money_terms=terms))

    reasons = []
    if capped:
        reasons.append(f"{len(relevant)} money-relevant Senate votes found; member "
                       f"positions fetched for the newest {max_detail_fetches} — lower bound")
    if fetch_misses:
        reasons.append(f"{fetch_misses} per-vote XML fetch(es) failed or were "
                       f"unreadable — those votes are missing from the list, "
                       f"not absences from the roll")
    if menu_misses:
        reasons.append("vote menus unavailable for " + ", ".join(m.split(" (")[0] for m in menu_misses))
    span = f"{min(congs)}th–{max(congs)}th" if len(congs) > 1 else f"{congs[0]}th"
    return VoteRecord(
        chamber="Senate", member=f"{last_name} ({state})", votes=votes,
        total_votes_scanned=len(scanned), money_related_seen=len(relevant),
        not_in_roll=not_in_roll,
        incomplete=bool(reasons), incomplete_reason="; ".join(reasons),
        coverage_note=(f"Senate LIS roll calls, {span} Congresses scanned; "
                       f"money-relevance tagged on the official vote title"))


# ── House — Congress.gov beta house-vote API ─────────────────────────────────
def _house_vote_pages(congress_num: int, api_key: str,
                      max_pages: int = 20) -> tuple[list[dict], bool]:
    """Page the vote list for one congress. Returns (items, hit_page_cap)."""
    items: list[dict] = []
    limit, offset = 250, 0
    for _ in range(max_pages):
        params = {"api_key": api_key, "format": "json",
                  "limit": str(limit), "offset": str(offset)}
        url = f"{CONGRESS_BASE}/house-vote/{congress_num}?{urllib.parse.urlencode(params)}"
        try:
            data = _get_json(url)
        except CongressAPIError as e:
            if getattr(e, "status_code", None) == 404 and not items:
                return [], False   # congress not covered by the beta API — not an error
            raise
        batch = data.get("houseRollCallVotes", [])
        items.extend(batch)
        if len(batch) < limit:
            return items, False
        offset += limit
    return items, True


def _house_bill_title(congress_num: int, leg_type: str, number: str,
                      api_key: str, cache: dict) -> Optional[str]:
    """One bill-title lookup, memoized — the vote list carries no titles, and
    the title is what the whole-word regex tags on. Returns None when the
    LOOKUP ITSELF failed (network/API), as opposed to "" for a bill with no
    title: the caller counts failures and discloses them as incomplete, so a
    transient API error can never silently pass as "not money-related"
    (Copilot review finding) — and a single flaky lookup doesn't abort the
    whole votes stage either."""
    key = (congress_num, leg_type.upper(), str(number))
    if key in cache:
        return cache[key]
    params = {"api_key": api_key, "format": "json"}
    url = (f"{CONGRESS_BASE}/bill/{congress_num}/{leg_type.lower()}/{number}"
           f"?{urllib.parse.urlencode(params)}")
    try:
        title = ((_get_json(url).get("bill") or {}).get("title") or "").strip()
    except CongressAPIError:
        title = None   # lookup FAILED — caller discloses; distinct from "no title"
    cache[key] = title
    return title


def _house_amendment_text(congress_num: int, number: str, api_key: str,
                          cache: dict) -> Optional[str]:
    """One amendment lookup, memoized — HAMDT votes carry no title at list
    level, only an author string, and money-relevant amendments (where
    campaign-finance riders actually live) hide in the purpose/description.
    Both fields are House-populated per the API docs; either may be empty.
    Returns None when the lookup itself failed (same disclosure contract as
    _house_bill_title)."""
    key = (congress_num, "HAMDT", str(number))
    if key in cache:
        return cache[key]
    params = {"api_key": api_key, "format": "json"}
    url = (f"{CONGRESS_BASE}/amendment/{congress_num}/hamdt/{number}"
           f"?{urllib.parse.urlencode(params)}")
    try:
        a = (_get_json(url).get("amendment") or {})
        text = " ".join(t for t in [(a.get("purpose") or "").strip(),
                                    (a.get("description") or "").strip()] if t)
    except CongressAPIError:
        text = None   # lookup FAILED — caller discloses; distinct from empty purpose
    cache[key] = text
    return text


def _iso_date(start: str) -> str:
    """'2024-03-05T18:57:00-05:00' → '2024-03-05'."""
    return (start or "")[:10]


def _member_position_from_results(data: dict, bioguide_id: str) -> Optional[str]:
    """Pull the member's voteCast out of a .../members response. The docs write
    the key as bioguideId; be tolerant of ID-cased variants."""
    container = data.get("houseRollCallVoteMemberVotes") or {}
    for item in container.get("results") or []:
        bid = item.get("bioguideId") or item.get("bioguideID") or ""
        if bid == bioguide_id:
            return (item.get("voteCast") or "").strip() or None
    return None


def house_vote_record(bioguide_id: str, api_key: str,
                      congresses: Optional[list[int]] = None,
                      current_year: Optional[int] = None,
                      max_title_lookups: int = 300,
                      max_detail_fetches: int = 60,
                      progress_cb: ProgressCallback = None) -> VoteRecord:
    """House roll calls for a member via the Congress.gov beta house-vote API.
    Vote lists are pulled in full for the covered congresses (118th+ only —
    earlier congresses aren't in the API, and that floor is disclosed, not
    hidden). Money-tagging needs bill titles, which the list omits, so titles
    are looked up newest-vote-first under `max_title_lookups`; member positions
    are then fetched only for money-relevant votes under `max_detail_fetches`.
    Either cap hitting ⇒ incomplete + reason."""
    year = current_year or time.localtime().tm_year
    congs = [c for c in (congresses or default_congresses(year))
             if c >= _HOUSE_API_FLOOR]
    floored = bool((congresses or default_congresses(year))) and not congs
    if floored:
        raise VotesAPIError(
            f"the Congress.gov house-vote API covers the {_HOUSE_API_FLOOR}th "
            f"Congress (2023) onward only — none of the requested congresses qualify")

    raw: list[dict] = []
    page_capped = False
    for c in congs:
        items, capped = _house_vote_pages(c, api_key)
        raw.extend(items)
        page_capped = page_capped or capped

    # Newest first, so the lookup caps bite the OLDEST votes, not the newest.
    raw.sort(key=lambda v: _iso_date(v.get("startDate", "")), reverse=True)

    # Tag: bill-title lookups for legislation votes; purpose/description
    # lookups for amendment (HAMDT) votes — both memoized and drawing from the
    # SAME lookup budget, newest votes first.
    title_cache: dict = {}
    lookups = 0
    tagging_truncated = False
    failed_lookups: set = set()   # distinct measures whose lookup errored — disclosed
    tagged: list[tuple[dict, str, list[str]]] = []
    for v in raw:
        leg_type = (v.get("legislationType") or "").strip()
        leg_num = str(v.get("legislationNumber") or "").strip()
        amdt_num = str(v.get("amendmentNumber") or "").strip()
        cong = int(v.get("congress") or 0)
        if leg_type and leg_num and leg_type.upper() != "HAMDT":
            key = (cong, leg_type.upper(), leg_num)
            if key not in title_cache:
                if lookups >= max_title_lookups:
                    tagging_truncated = True
                    continue
                lookups += 1
            title = _house_bill_title(cong, leg_type, leg_num, api_key, title_cache)
            if title is None:            # lookup FAILED (not "bill has no title"):
                failed_lookups.add(key)  # count + disclose, never silently untag
                title = ""
        elif amdt_num:
            key = (cong, "HAMDT", amdt_num)
            if key not in title_cache:
                if lookups >= max_title_lookups:
                    tagging_truncated = True
                    continue
                lookups += 1
            text = _house_amendment_text(cong, amdt_num, api_key, title_cache)
            if text is None:
                failed_lookups.add(key)
                text = ""
            title = text or (v.get("amendmentAuthor") or "").strip()
        else:
            title = (v.get("amendmentAuthor") or "").strip()
        terms = _money_match(title) if title else []
        if terms:
            tagged.append((v, title, terms))

    detail_capped = len(tagged) > max_detail_fetches
    to_fetch = tagged[:max_detail_fetches]
    votes: list[RollCallVote] = []
    not_in_roll = 0
    fetch_misses = 0   # /members fetch FAILURES — a source problem, disclosed
                       # separately, never conflated with "not on the roll"
                       # (Copilot review finding; matches the Senate path)
    for i, (v, title, terms) in enumerate(to_fetch):
        if progress_cb:
            progress_cb(i + 1, len(to_fetch))
        cong = int(v.get("congress") or 0)
        session = int(v.get("sessionNumber") or 0)
        roll = int(v.get("rollCallNumber") or 0)
        params = {"api_key": api_key, "format": "json"}
        url = (f"{CONGRESS_BASE}/house-vote/{cong}/{session}/{roll}/members"
               f"?{urllib.parse.urlencode(params)}")
        try:
            data = _get_json(url)
        except CongressAPIError:
            fetch_misses += 1
            continue
        pos = _member_position_from_results(data, bioguide_id)
        if pos is None:
            not_in_roll += 1
            continue
        container = data.get("houseRollCallVoteMemberVotes") or {}
        date = _iso_date(v.get("startDate", ""))
        leg_type = (v.get("legislationType") or v.get("amendmentType") or "").strip()
        leg_num = str(v.get("legislationNumber") or v.get("amendmentNumber") or "").strip()
        votes.append(RollCallVote(
            date=date, year=int(date[:4]) if date[:4].isdigit() else 0,
            chamber="House", congress=cong, session=session, number=roll,
            question=(container.get("voteQuestion") or "").strip(),
            title=title, result=(v.get("result") or "").strip(), position=pos,
            legislation=f"{leg_type} {leg_num}".strip(),
            money_related=True, money_terms=terms))

    reasons = []
    if page_capped:
        reasons.append("vote list page cap hit — scan may be a lower bound")
    if tagging_truncated:
        reasons.append(f"bill/amendment lookups capped at {max_title_lookups} distinct "
                       f"measures (newest votes first) — older votes untagged, lower bound")
    if failed_lookups:
        reasons.append(f"{len(failed_lookups)} bill/amendment lookup(s) failed "
                       f"(network/API) — votes on those measures could not be "
                       f"money-tagged, so the money-relevant list is a lower bound")
    if detail_capped:
        reasons.append(f"{len(tagged)} money-relevant House votes found; member "
                       f"positions fetched for the newest {max_detail_fetches} — lower bound")
    if fetch_misses:
        reasons.append(f"{fetch_misses} member-position fetch(es) failed — those "
                       f"votes are missing from the list, not absences from the roll")
    span = f"{min(congs)}th–{max(congs)}th" if len(congs) > 1 else f"{congs[0]}th"
    return VoteRecord(
        chamber="House", member=bioguide_id, votes=votes,
        total_votes_scanned=len(raw), money_related_seen=len(tagged),
        not_in_roll=not_in_roll,
        incomplete=bool(reasons), incomplete_reason="; ".join(reasons),
        coverage_note=(f"Congress.gov beta house-vote API, {span} Congresses — "
                       f"covers 2023+ legislation-linked roll calls only "
                       f"(the API's coverage, disclosed, not a choice); "
                       f"money-relevance tagged on the bill title or amendment "
                       f"purpose"))


# ── chamber dispatch (mirrors how app.py knows the candidate) ────────────────
def vote_record(office: str, *, bioguide_id: str = "", last_name: str = "",
                state: str = "", api_key: str = "",
                congresses: Optional[list[int]] = None,
                current_year: Optional[int] = None,
                progress_cb: ProgressCallback = None) -> VoteRecord:
    """Route by FEC office code: 'H' → House (needs bioguide_id + Congress.gov
    key), 'S' → Senate (needs last_name + state, no key). Anything else — a
    presidential candidate, say — has no chamber and raises with a clear why."""
    o = (office or "").upper()
    if o.startswith("S"):
        if not (last_name and state):
            raise VotesAPIError("Senate votes need the member's last name and state")
        return senate_vote_record(last_name, state, congresses=congresses,
                                  current_year=current_year, progress_cb=progress_cb)
    if o.startswith("H"):
        if not bioguide_id:
            raise VotesAPIError("House votes need a bioguideId (resolved by the record stage)")
        if not api_key:
            raise VotesAPIError("House votes need CONGRESS_API_KEY (the beta house-vote API)")
        return house_vote_record(bioguide_id, api_key, congresses=congresses,
                                 current_year=current_year, progress_cb=progress_cb)
    raise VotesAPIError(f"no roll-call chamber for office {office!r} — House and "
                        f"Senate members only")


# ── votes ↔ record join (factual, no scoring) ────────────────────────────────
# "Voted Yea on a bill they cosponsored" is a citable fact linking the two
# halves of Track B — a recorded relationship, not a judgment. The join is
# exact: same congress AND same normalized bill identity ("S. 512" from LIS,
# "HR 9500" from the House API, and "S 512 (119th Congress)" from the record's
# citation all normalize to comparable keys). No fuzzy matching — a miss means
# no claim.
_LEG_NORM_RE = re.compile(r"[^A-Z0-9]+")
_RECORD_CITATION_RE = re.compile(r"^([A-Z]+)\s*(\d+)\s*\((\d+)")


def _norm_legislation(s: str) -> str:
    """'S. 512' / 'S.J.Res. 5' / 'HR 9500' → 'S512' / 'SJRES5' / 'HR9500'."""
    return _LEG_NORM_RE.sub("", str(s or "").upper())


def link_votes_to_record(votes_json: dict, record_json: dict) -> int:
    """Annotate each money-relevant vote (jsonable dict) with
    candidate_bill_role = 'sponsored' | 'cosponsored' when the voted measure is
    a bill the candidate themselves sponsored/cosponsored (matched on congress +
    normalized bill identity from the record's citation). Mutates votes_json in
    place; returns how many votes were linked. Safe on empty/missing tracks."""
    votes = (votes_json or {}).get("money_related") or []
    roles: dict[tuple[int, str], str] = {}
    by_year = (record_json or {}).get("by_year") or {}
    for slot in by_year.values():
        for a in slot.get("items") or []:
            m = _RECORD_CITATION_RE.match(str(a.get("citation") or ""))
            if not m:
                continue
            key = (int(m.group(3)), _norm_legislation(m.group(1) + m.group(2)))
            # sponsored outranks cosponsored if the same bill somehow appears twice
            if roles.get(key) != "sponsored":
                roles[key] = a.get("role") or ""
    linked = 0
    for v in votes:
        leg = _norm_legislation(v.get("legislation") or "")
        if not leg:
            continue
        role = roles.get((int(v.get("congress") or 0), leg), "")
        if role:
            v["candidate_bill_role"] = role
            linked += 1
    return linked


# ── jsonable ─────────────────────────────────────────────────────────────────
def vote_to_jsonable(v: RollCallVote) -> dict:
    return {
        "date": v.date, "year": v.year, "chamber": v.chamber,
        "congress": v.congress, "session": v.session, "number": v.number,
        "citation": v.citation, "question": v.question, "title": v.title,
        "result": v.result, "position": v.position, "legislation": v.legislation,
        "money_related": v.money_related, "money_terms": v.money_terms,
        "candidate_bill_role": v.candidate_bill_role,
        "url": v.url,
    }


def record_to_jsonable(rec: VoteRecord) -> dict:
    return {
        "chamber": rec.chamber,
        "member": rec.member,
        "total_votes_scanned": rec.total_votes_scanned,
        "money_related_seen": rec.money_related_seen,
        "not_in_roll": rec.not_in_roll,
        "money_related": [vote_to_jsonable(v) for v in rec.votes],
        "incomplete": rec.incomplete,
        "incomplete_reason": rec.incomplete_reason,
        "coverage_note": rec.coverage_note,
    }


# ── demo fixtures (no key needed) — synthetic, clearly labeled ───────────────
def demo_votes() -> VoteRecord:
    raw = [
        ("2025-09-18", 119, 1, 512, "On the Cloture Motion",
         "Motion to Invoke Cloture: DISCLOSE Act of 2025", "Cloture Motion Rejected",
         "Yea", "S 512"),
        ("2024-07-11", 118, 2, 221, "On Passage",
         "Ban Corporate PACs Act", "Failed", "Yea", "S 2113"),
        ("2023-06-14", 118, 1, 154, "On Passage of the Bill",
         "DISCLOSE Act of 2023", "Failed", "Yea", "S. 1593"),
        ("2023-04-19", 118, 1, 88, "On the Motion to Proceed",
         "A bill to require donor disclosure for super PACs", "Agreed to",
         "Yea", "S 443"),
    ]
    votes = []
    for date, cong, sess, num, q, title, res, pos, leg in raw:
        terms = _money_match(title)
        votes.append(RollCallVote(
            date=date, year=int(date[:4]), chamber="Senate", congress=cong,
            session=sess, number=num, question=q, title=title, result=res,
            position=pos, legislation=leg, money_related=bool(terms),
            money_terms=terms))
    return VoteRecord(chamber="Senate", member="OSSOFF (GA) (demo)", votes=votes,
                      total_votes_scanned=1240, money_related_seen=4,
                      coverage_note="demo fixtures — synthetic votes, clearly labeled")


if __name__ == "__main__":
    import json, sys
    rec = demo_votes()
    print(f"{rec.member}: {len(rec.votes)} money-related votes of "
          f"{rec.total_votes_scanned} scanned", file=sys.stderr)
    print(json.dumps(record_to_jsonable(rec), indent=2))

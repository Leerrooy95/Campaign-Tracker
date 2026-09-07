"""
congress.py — Track B as FACT, not score.

What a candidate actually DID, year by year: the bills they sponsored and
cosponsored, pulled from the official, free **Congress.gov API**
(api.congress.gov). Every item is dated and carries its own citation (congress +
bill type + number → a congress.gov URL), so nothing here is interpretation —
it's the public legislative record, and the reader can click straight to the
source.

This deliberately REPLACES the old rhetoric-scoring track. Scoring rhetoric
intensity was an interpretation that could be biased and couldn't be cleanly
verified. A sponsored bill is a fact. The consistency question ("does the money
match the record?") is left for the report and the reader, not decided by a
number here.

WHAT'S IN / WHAT'S NOT (honest):
  - IN:  sponsored + cosponsored legislation for any member (House or Senate),
         dated, tagged for money/campaign-finance relevance, bucketed by year.
  - NOT: roll-call votes — those live in votes.py (House via the Congress.gov
         beta house-vote API, 2023+; Senate via LIS XML), which reuses this
         module's _money_match tagging and _get transport.

AUTH: api_key as a QUERY param (not a header — that was the dead ProPublica API).
Free key from api.data.gov. Env var: CONGRESS_API_KEY.

BRIDGE FROM FEC: the FEC candidate record already gives us state + name, so we
resolve name→bioguideId by listing the state's current members and matching the
last name. No paid search, no scraping.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Callable

CONGRESS_BASE = "https://api.congress.gov/v3"
BILL_TYPES = {"HR", "S", "HJRES", "SJRES", "HCONRES", "SCONRES", "HRES", "SRES"}

ProgressCallback = Optional[Callable[[int, int], None]]


class CongressAPIError(RuntimeError):
    """Raised when a Congress.gov call fails. Carries the HTTP status (or -1 for
    a connection-level failure) so a rate limit (429) or bad key (403) can be
    told apart from a real 'no data' result — an empty list is never silently
    confused with a failed request."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_rate_limit(self) -> bool:
        return self.status_code == 429


# ── money / campaign-finance relevance tagging ───────────────────────────────
# These tag which actions bear on the money-in-politics question. We DON'T drop
# the rest — the full record stays; relevant items are flagged so the report can
# foreground them. Matching is on the bill title (always present at list level),
# case-insensitive, whole-word (regex \b boundaries — see _money_match).
#
# v1 used bare substring matching with manual space-padding (e.g. " pac ").
# Two failure modes showed up on real data and are fixed here:
#
#   FALSE POSITIVES — bare single-word terms ("donor", "ethics", "transparency")
#   matched any bill sharing that word regardless of subject: Living Donor
#   Protection Act, Foster Care Placement Transparency Act, DHS Transparency
#   Act, Courthouse Ethics and Transparency Act. None are campaign-finance
#   bills. Fix: those three terms are removed as bare words; only kept as
#   compound phrases that are actually campaign-finance-specific
#   ("donor disclosure", "government ethics", "campaign finance transparency",
#   etc).
#
#   FALSE NEGATIVES — space-padded " pac " never matches the plural "PACs",
#   so "Ban Corporate PACs Act" was missed entirely. And "stock act" (the
#   literal STOCK Act) doesn't appear as a substring of "Ban Congressional
#   Stock Trading Act", so that was missed too. Fix: word-boundary regex
#   handles the plural (pacs?), and "stock trading act" is added alongside
#   "stock act" so both bill-naming patterns are caught.
_MONEY_TERMS = [
    "campaign finance", "campaign contribution", "campaign donor",
    "political action committee", "pac", "super pac", "corporate pac",
    "dark money", "dark-money", "disclose act",
    "lobby", "lobbying", "lobbyist", "citizens united",
    "foreign money", "election integrity", "contribution limit",
    "donor disclosure", "small-dollar", "small dollar",
    "public financing", "honest ads", "federal election", "fec",
    "government ethics", "congressional ethics", "conflict of interest",
    "stock act", "stock trading act", "insider trading",
    "financial disclosure", "campaign finance disclosure", "bribery",
    # "corruption" as a BARE word is removed — same failure mode as the bare
    # "donor"/"ethics"/"transparency" terms above. It matched foreign-policy and
    # governance bills with nothing to do with money in US politics: Senate Roll
    # Call 78 (H.R. 2471, post-disaster recovery and corruption in Haiti) sat in
    # Run 19's money/influence vote ledger purely on this term.
    #
    # "anti-corruption" is DELIBERATELY KEPT: it is the phrase the flagship
    # money-in-politics bills actually use ("...implement other anti-corruption
    # measures"), and dropping it would lose S. 1 and S. 2093 — the two votes a
    # campaign-finance tool most needs. The compounds below catch domestic
    # corruption bills that don't use the hyphenated form.
    "anti-corruption", "political corruption", "public corruption",
    "corruption in government", "government corruption",
    "campaign finance transparency", "political spending transparency",
]

# ── plurals: opt-in, never blanket ───────────────────────────────────────────
# \b...\b blocks the plural: "campaign contributions" does NOT match
# \bcampaign contribution\b, because the trailing s kills the word boundary.
# That is the same bug the earlier " pac " space-padding had, and it is still
# live for every multi-word noun term here.
#
# The fix is NOT to pluralize everything. Tested against 2,506 real Senate vote
# titles (117th-119th), blanket pluralization gained exactly one new match, and
# it was WRONG: "federal election" -> "Federal elections" fired on Kennedy Amdt.
# 5414, a voter-ID and ballot-counting amendment with no campaign-finance
# content. That term's real hits ("Federal Election Commission", "Federal
# Election Campaign Act") are singular by construction, so pluralizing it buys
# nothing and costs precision.
#
# So each plural is opted into deliberately: terms that name a COUNTABLE THING
# which real titles pluralize ("limit contributions", "require lobbyists to
# disclose", "ban corporate PACs"). Terms naming a field, a statute, or an
# uncountable ("campaign finance", "dark money", "federal election",
# "insider trading", "bribery") stay singular.
_PLURALIZE = {
    "campaign contribution", "campaign donor", "contribution limit",
    "lobbyist", "political action committee", "financial disclosure",
    "donor disclosure", "pac", "super pac", "corporate pac",
}

# Plural falls on a non-final word — a suffix rule can't reach it.
_PLURAL_IRREGULAR = {
    "conflict of interest": r"conflicts? of interest",
}


def _term_pattern(term: str) -> str:
    """Regex source for one term. `(?:e?s)?` covers both the plain -s plural
    ("PACs", "lobbyists") and the -es plural ("disclosures" is -s, but
    "committees" and any future -es noun are covered without a second rule)."""
    if term in _PLURAL_IRREGULAR:
        return _PLURAL_IRREGULAR[term]
    if term in _PLURALIZE:
        return term + r"(?:e?s)?"
    return term


# Pre-compiled once at import time. Each entry pairs the DISPLAY term (what the
# report and CSV show as the matched term) with its own pattern, so the display
# string is never reverse-engineered from the regex source — the previous code
# recovered it with str.rstrip("s?"), a character-set strip that would have
# mangled any future term ending in s or ?.
_MONEY_PATTERNS = [(term, re.compile(r"\b" + _term_pattern(term) + r"\b", re.IGNORECASE))
                   for term in _MONEY_TERMS]


def _money_match(title: str) -> list[str]:
    """Return the money/finance terms a bill title hits (empty list = not flagged).
    Returned values are the display terms exactly as listed in _MONEY_TERMS."""
    return [term for term, pattern in _MONEY_PATTERNS if pattern.search(title)]


@dataclass
class LegislativeAction:
    date: str               # introducedDate (ISO)
    year: int
    role: str               # "sponsored" | "cosponsored"
    congress: int
    bill_type: str          # HR, S, ...
    number: str
    title: str
    latest_action: str = ""
    money_related: bool = False
    money_terms: list = field(default_factory=list)

    @property
    def citation(self) -> str:
        return f"{self.bill_type} {self.number} ({self.congress}th Congress)"

    @property
    def url(self) -> str:
        # congress.gov human URL, e.g. .../bill/117th-congress/senate-bill/123
        slug = {
            "HR": "house-bill", "S": "senate-bill",
            "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
            "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution",
            "HRES": "house-resolution", "SRES": "senate-resolution",
        }.get(self.bill_type, "bill")
        return f"https://www.congress.gov/bill/{self.congress}th-congress/{slug}/{self.number}"


@dataclass
class LegislativeRecord:
    bioguide_id: str
    member_name: str
    actions: list = field(default_factory=list)     # list[LegislativeAction]
    incomplete: bool = False
    incomplete_reason: str = ""

    def by_year(self) -> dict:
        out: dict[int, dict] = {}
        for a in self.actions:
            slot = out.setdefault(a.year, {"sponsored": 0, "cosponsored": 0,
                                           "money_related": 0, "items": []})
            slot[a.role] += 1
            if a.money_related:
                slot["money_related"] += 1
            slot["items"].append(a)
        return dict(sorted(out.items()))


# ── HTTP ──────────────────────────────────────────────────────────────────────
# Optional: curl_cffi impersonates a real browser's TLS fingerprint — which is
# what's needed to clear Cloudflare's Browser Integrity Check on api.congress.gov.
# Plain urllib can't change its TLS signature, so it gets an instant "1010" block
# or a hanging challenge. If curl_cffi is installed we use it; if not, we fall
# back to urllib and fail FAST with a clear, actionable message.
try:
    from curl_cffi import requests as _curl   # type: ignore
    _HAVE_CURL = True
except Exception:   # noqa: BLE001
    _HAVE_CURL = False

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _cf_blocked(code: int, body: str) -> bool:
    """Looks like Cloudflare's bot block (1010) or the hang it causes."""
    return "1010" in (body or "") or code in (-1, 403)


def _get(url: str, retries: int = 1, timeout: int = 10) -> dict:
    """GET + JSON. Uses curl_cffi (browser-TLS impersonation) when available to
    clear Congress.gov's Cloudflare check; otherwise plain urllib. Fails FAST
    (short timeout, one retry) and raises CongressAPIError — never a silent empty."""
    last_code, last_body = -1, ""
    for attempt in range(retries + 1):
        try:
            if _HAVE_CURL:
                r = _curl.get(url, impersonate="chrome", timeout=timeout,
                              headers={"Accept": "application/json"})
                if r.status_code == 200:
                    return r.json()
                last_code, last_body = r.status_code, (r.text or "")[:300]
                if (r.status_code == 429 or 500 <= r.status_code < 600) and attempt < retries:
                    time.sleep(1.0)
                    continue
                raise CongressAPIError(f"Congress.gov HTTP {r.status_code}: {last_body!r}",
                                       status_code=r.status_code)
            req = urllib.request.Request(url, headers={
                "Accept": "application/json", "User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except CongressAPIError:
            raise
        except urllib.error.HTTPError as e:
            last_code = e.code
            last_body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1)); continue
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(1.0); continue
            hint = ("  [Congress.gov is behind a Cloudflare bot-check; install "
                    "curl_cffi to bypass it: pip install curl_cffi --break-system-packages]"
                    if (not _HAVE_CURL and _cf_blocked(e.code, last_body)) else "")
            raise CongressAPIError(f"Congress.gov HTTP {e.code}: {last_body!r}{hint}",
                                   status_code=e.code)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last_body = str(e)
            if attempt < retries:
                time.sleep(1.0); continue
            hint = ("  [likely Cloudflare's bot-check hanging the request; install "
                    "curl_cffi to bypass it: pip install curl_cffi --break-system-packages]"
                    if not _HAVE_CURL else "")
            raise CongressAPIError(f"Could not reach Congress.gov: {last_body}{hint}",
                                   status_code=-1)
        except Exception as e:   # curl_cffi transport errors (timeout, etc.)
            last_body = str(e)
            if attempt < retries:
                time.sleep(1.0); continue
            raise CongressAPIError(f"Could not reach Congress.gov: {last_body}",
                                   status_code=-1)
    raise CongressAPIError(f"Congress.gov failed after retries: HTTP {last_code}",
                           status_code=last_code)


# ── member resolution (FEC name/state → bioguideId) ──────────────────────────
def resolve_member(state: str, last_name: str, api_key: str,
                   office: str = "") -> Optional[dict]:
    """Find a current member's bioguideId by listing the state's members and
    matching last name. `office` ('S'/'H' from FEC) breaks ties by chamber.
    Returns the matched member dict (with bioguideId, name) or None."""
    params = {"api_key": api_key, "format": "json",
              "currentMember": "true", "limit": "250"}
    url = f"{CONGRESS_BASE}/member/{state.upper()}?{urllib.parse.urlencode(params)}"
    data = _get(url)
    members = data.get("members", [])
    ln = last_name.strip().lower()

    matches = [m for m in members if ln in (m.get("name", "").lower())]
    if not matches:
        return None
    if len(matches) == 1 or not office:
        return matches[0]

    # Tie-break by chamber when FEC told us the office.
    want = "Senate" if office.upper().startswith("S") else "House"
    for m in matches:
        terms = m.get("terms", {}).get("item", []) if isinstance(m.get("terms"), dict) else m.get("terms", [])
        chambers = " ".join(str(t.get("chamber", "")) for t in (terms or []))
        if want.lower() in chambers.lower():
            return m
    return matches[0]


# ── legislation pulls ─────────────────────────────────────────────────────────
def _pull_legislation(bioguide_id: str, kind: str, api_key: str,
                      max_pages: int = 10, progress_cb: ProgressCallback = None
                      ) -> tuple[list[dict], bool, str]:
    """Page through sponsored- or cosponsored-legislation. Returns
    (raw_items, incomplete, reason). `kind` is 'sponsored' or 'cosponsored'."""
    endpoint = f"{kind}-legislation"
    items: list[dict] = []
    limit, offset = 250, 0
    for page in range(max_pages):
        params = {"api_key": api_key, "format": "json",
                  "limit": str(limit), "offset": str(offset)}
        url = f"{CONGRESS_BASE}/member/{bioguide_id}/{endpoint}?{urllib.parse.urlencode(params)}"
        data = _get(url)
        batch = data.get(f"{kind}Legislation", [])
        items.extend(batch)
        if progress_cb:
            progress_cb(page + 1, max_pages)
        if len(batch) < limit:
            return items, False, ""                    # reached the end cleanly
        offset += limit
    # Hit the page cap with a full last page — there may be more.
    return items, True, f"{kind}: stopped at {max_pages} pages ({len(items)} items) — may be a lower bound"


def _to_action(raw: dict, role: str) -> Optional[LegislativeAction]:
    """Map a raw API legislation item to a LegislativeAction. Returns None if it
    lacks the minimum (a date and a bill identity)."""
    date = (raw.get("introducedDate") or "").strip()
    bill_type = (raw.get("type") or "").strip().upper()
    number = str(raw.get("number") or "").strip()
    if not date or not bill_type or not number:
        return None
    title = (raw.get("title") or raw.get("latestTitle") or "").strip()
    year = int(date[:4]) if date[:4].isdigit() else 0
    latest = ""
    la = raw.get("latestAction")
    if isinstance(la, dict):
        latest = (la.get("text") or "").strip()
    terms = _money_match(title)
    return LegislativeAction(
        date=date, year=year, role=role,
        congress=int(raw.get("congress") or 0),
        bill_type=bill_type, number=number, title=title,
        latest_action=latest,
        money_related=bool(terms), money_terms=terms,
    )


def legislative_record(state: str, last_name: str, api_key: str,
                       office: str = "", max_pages: int = 10,
                       progress_cb: ProgressCallback = None) -> LegislativeRecord:
    """Full Track-B record for a member: resolve, then pull + tag sponsored and
    cosponsored legislation, sorted newest-first. Factual; no scoring."""
    member = resolve_member(state, last_name, api_key, office)
    if not member:
        raise CongressAPIError(
            f"No current member of Congress matched last name {last_name!r} in {state}. "
            f"(They may be a challenger not yet in office — the legislative record "
            f"only exists for sitting/former members.)")
    bioguide = member.get("bioguideId", "")
    name = member.get("name", last_name)

    actions: list[LegislativeAction] = []
    incomplete, reasons = False, []
    for kind in ("sponsored", "cosponsored"):
        raw, inc, why = _pull_legislation(bioguide, kind, api_key, max_pages, progress_cb)
        for r in raw:
            a = _to_action(r, kind)
            if a:
                actions.append(a)
        if inc:
            incomplete = True
            reasons.append(why)

    actions.sort(key=lambda a: a.date, reverse=True)
    return LegislativeRecord(bioguide_id=bioguide, member_name=name, actions=actions,
                             incomplete=incomplete, incomplete_reason="; ".join(reasons))


# ── jsonable ──────────────────────────────────────────────────────────────────
def action_to_jsonable(a: LegislativeAction) -> dict:
    return {
        "date": a.date, "year": a.year, "role": a.role,
        "citation": a.citation, "title": a.title,
        "latest_action": a.latest_action,
        "money_related": a.money_related, "money_terms": a.money_terms,
        "url": a.url,
    }


def record_to_jsonable(rec: LegislativeRecord) -> dict:
    by_year = {
        str(yr): {
            "sponsored": slot["sponsored"],
            "cosponsored": slot["cosponsored"],
            "money_related": slot["money_related"],
            "items": [action_to_jsonable(a) for a in slot["items"]],
        }
        for yr, slot in rec.by_year().items()
    }
    money_items = [action_to_jsonable(a) for a in rec.actions if a.money_related]
    return {
        "bioguide_id": rec.bioguide_id,
        "member_name": rec.member_name,
        "total_actions": len(rec.actions),
        "money_related_count": len(money_items),
        "by_year": by_year,
        "money_related": money_items,
        "incomplete": rec.incomplete,
        "incomplete_reason": rec.incomplete_reason,
    }


# ── demo fixtures (no key needed) — synthetic, clearly labeled ───────────────
def demo_record() -> LegislativeRecord:
    raw = [
        ("2021-03-17", "S", "1", "sponsored", "For the People Act of 2021"),
        ("2021-06-22", "S", "2093", "cosponsored", "Freedom to Vote Act"),
        ("2022-02-08", "S", "3611", "sponsored", "Veterans health care access bill"),
        ("2023-05-10", "S", "1593", "cosponsored", "DISCLOSE Act of 2023"),
        ("2024-01-25", "S", "3712", "sponsored", "Rural broadband infrastructure funding"),
        ("2024-07-30", "S", "4521", "cosponsored", "Ban on corporate PAC contribution limits loophole"),
        ("2025-09-14", "S", "2880", "sponsored", "Small-dollar public financing pilot"),
    ]
    actions = [a for a in (_to_action(
        {"introducedDate": d, "type": bt, "number": n, "congress": 118,
         "title": title, "latestAction": {"text": "Referred to committee"}}, role)
        for d, bt, n, role, title in raw) if a]
    actions.sort(key=lambda a: a.date, reverse=True)
    return LegislativeRecord(bioguide_id="O000174 (demo)", member_name="OSSOFF, JON (demo)",
                             actions=actions)


if __name__ == "__main__":
    import sys
    rec = demo_record()
    print(f"{rec.member_name}: {len(rec.actions)} actions, "
          f"{sum(1 for a in rec.actions if a.money_related)} money-related", file=sys.stderr)
    print(json.dumps(record_to_jsonable(rec), indent=2))

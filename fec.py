"""
fec.py — federal candidate donor lookup via the openFEC API.

Mirrors search.py's output shape (RecipientGroup / DonorTotal) on purpose, so
app.py can treat "Federal (FEC)" as just another entry in the results dict
without new branching in the export/render code.

ARCHITECTURE NOTE (changed after live testing 2026-06-16): the original
version sorted by contribution amount and capped at 2,000 records per
committee. That broke silently for any donor who gave multiple times —
their separate contributions land at different points in an amount-sorted
ranking, and FEC's own engineering team has documented that their
amount-sorted pagination doesn't reliably tie-break, so which records a
capped pull catches varies run to run. Confirmed directly: two consecutive
runs against the same real committee gave one donor totals of $21,000 and
$10,500 for the exact same underlying 5 contributions.

The fix: pull EVERY record for a committee/cycle (no cap — FEC tells us the
total page count up front, so progress is knowable), and deduplicate by
`sub_id`, a true unique row identifier. This makes the result correct
regardless of any tie-breaking instability in their pagination.

This is now a slow operation for high-profile races (Talarico's committee
alone is 162k+ records / ~1,622 pages) and MUST be run as a background job,
not inline in a request — see app.py's /api/fec/search job endpoints.

Key difference from state search: FEC tells us individual-vs-not directly
via `is_individual` (their own methodology, not our name-heuristic), so
bucket_inferred is always False here.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

FEC_BASE = "https://api.open.fec.gov/v1"

# Explicit allowlist — we request ONLY these fields from schedule_a, never an
# address-shaped one, regardless of what else the API might return. This is
# the same data-minimization-at-the-request-level approach used in search.py:
# don't just hide fields in the UI, don't ask for them in the first place.
# sub_id is required for dedup, not for display.
SCHEDULE_A_FIELDS = [
    "contributor_name", "contributor_city", "contributor_state",
    "contributor_employer", "contributor_occupation",
    "contribution_receipt_amount", "contribution_receipt_date",
    "is_individual", "committee_id", "sub_id",
    # memo_code="X" marks a re-itemization of money already counted (a later
    # report restating an earlier receipt), NOT new money — FEC excludes these
    # from totals and so must we. Without it, a donor who appears on both the
    # original and a restating report is summed twice: real Collins-2026 case,
    # Brent Scarbrough showed $21,000 when the true figure is $10,500.
    "memo_code",
]

_PER_PAGE = 100
# Absolute safety valve against a runaway loop (e.g. an API bug that never
# returns last_index=None) — not a normal truncation point. At 100/page this
# is 2M records, far beyond any real committee.
_ABSOLUTE_MAX_PAGES = 20000

ProgressCallback = Optional[Callable[[int, Optional[int]], None]]  # (page_num, pages_total)


@dataclass
class DonorTotal:
    name: str
    bucket: str             # "individual" | "pac_or_org" — never inferred for FEC
    bucket_inferred: bool = False
    total: float = 0.0
    count: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    employer: Optional[str] = None   # FEC-specific extra context; city/state only, no employer for state data
    occupation: Optional[str] = None  # standard FEC disclosure field, shown alongside employer
    location: Optional[str] = None   # "City, ST" — never a street address


@dataclass
class RecipientGroup:
    state: str = "Federal (FEC)"
    recipient_name: str = ""
    donors: list = field(default_factory=list)
    transactions: list = field(default_factory=list)  # per-row Schedule A receipts
                                                       # (largest-first), for the
                                                       # transaction-level CSV export
    total_raised: float = 0.0
    memo_skipped: int = 0            # memo_code="X" re-itemizations dropped (not
                                     # new money — see SCHEDULE_A_FIELDS)
    refunds_applied: int = 0         # negative rows netted against donor totals
    refund_total: float = 0.0        # sum of those refunds (negative)
    latest_record_year: Optional[int] = None
    office: Optional[str] = None     # "H" / "S" / "P"
    candidate_id: Optional[str] = None
    truncated: bool = False          # True if data may be incomplete — either
                                      # the absolute safety valve fired, or a
                                      # later page kept failing even after
                                      # retries. total_raised is a lower bound
                                      # in either case, not an exact total.
    truncated_reason: str = ""       # human-readable why, when truncated is True


_REQUEST_TIMEOUT = 60  # seconds. Was 20 — confirmed too short by a live failure:
# the first page of a schedule_a query (no cursor) succeeds quickly, but the
# second page (which carries last_index/last_contribution_receipt_date —
# i.e. asks FEC to resume a cursor deep in an 84,000+ row sorted query)
# consistently read-timed-out at 20s across all 5 retry attempts. Retrying
# a too-short timeout doesn't help — it just times out again every time,
# regardless of backoff delay — so the actual fix is a longer timeout, not
# more retries.


def _http_get(url: str) -> tuple[int, str]:
    """Returns (status, body). On a genuine HTTP error response, status is
    the real status code and body is whatever error detail the server sent.
    On a connection-level failure (no response at all — DNS, timeout, TLS,
    etc.), status is -1 and body is the exception detail. -1 is a real
    sentinel, never a code a server would actually send, so callers can tell
    "we got an HTTP error" apart from "we never got a response."
    """
    try:
        with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body or str(e)
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


# ── Rate-limit pacing + retry ───────────────────────────────────────────────
# Process-wide, thread-safe pacing (not just per-call) — a real bug found by
# live testing: full-speed pagination with zero pacing blows past FEC's
# 120/min limit within seconds, gets 429'd, and a 429 on a later page used to
# be silently treated as "stop here, keep what we have" — which made a
# rate-limit failure look exactly like a small, complete, correct result.
# A live test against Talarico's committee (84,342 real records) returned a
# total of $3,911.59 this way — wrong by orders of magnitude, with no error.
_MIN_REQUEST_INTERVAL = 0.6   # ~100/min, safe margin under the 120/min cap
_rate_lock = threading.Lock()
_last_request_at = [0.0]      # mutable single-element container, shared across threads

_RETRYABLE_STATUS = {-1, 429, 500, 502, 503, 504}  # -1 = connection-level
# failure (DNS, timeout, TLS) — arguably the MOST worth retrying, since it's
# usually a momentary network blip rather than anything about the request
# itself. Omitting it here was a real bug: it meant a connection hiccup
# failed instantly with zero retries, while only HTTP error responses got
# the backoff treatment — exactly backwards from what's needed. Confirmed
# directly: a live run hit this exact case and failed on attempt 1 with no
# delay, instead of being retried.
_MAX_RETRIES = 4


def _paced_http_get(url: str) -> tuple[int, str]:
    with _rate_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.monotonic()
    return _http_get(url)


def _http_get_with_retry(url: str) -> tuple[int, str]:
    """Paced + retried with exponential backoff on transient status codes
    (429 rate limit, 5xx server errors). Non-retryable errors (bad key, bad
    params, etc.) return immediately on the first attempt — retrying those
    would just waste time producing the same failure."""
    delay = 1.0
    code, body = 0, ""
    for attempt in range(_MAX_RETRIES + 1):
        code, body = _paced_http_get(url)
        if code not in _RETRYABLE_STATUS:
            return code, body
        if attempt < _MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    return code, body  # retries exhausted — caller decides how to handle it


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


class FECAPIError(RuntimeError):
    """Raised when an FEC API call fails. Carries enough detail (status code,
    body snippet) to diagnose without guessing — a silent empty result here
    looks identical to "no data exists", which already cost real debugging
    time once (the missing two_year_transaction_period bug).

    `status_code` is the HTTP code (or -1 for connection-level failures, None if
    unknown) so callers can distinguish a rate limit (429) from a bad key (403)
    or a server fault (5xx) and message the user accordingly."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_rate_limit(self) -> bool:
        return self.status_code == 429


def search_fec_candidates(name: str, api_key: str, per_page: int = 10) -> list[dict]:
    """Look up federal candidates by name. Returns raw candidate dicts from FEC.

    Raises FECAPIError on any failure — does NOT silently return an empty
    list, because an empty list here is indistinguishable from "no such
    candidate" when the real cause might be a missing/bad key, a network
    error, or a rate limit.
    """
    params = {"q": name, "api_key": api_key, "sort": "-receipts",
              "per_page": str(per_page)}
    url = f"{FEC_BASE}/candidates/search/?{urllib.parse.urlencode(params)}"
    code, body = _http_get_with_retry(url)
    if code == -1:
        raise FECAPIError(f"Could not reach the FEC API (connection-level failure): {body}", status_code=-1)
    if code != 200:
        raise FECAPIError(f"FEC candidates/search returned HTTP {code}: {body[:300]!r}", status_code=code)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise FECAPIError(f"FEC candidates/search returned non-JSON response: {body[:300]!r}") from exc
    return data.get("results", [])


def _current_cycle() -> int:
    """FEC two-year cycles are always even years. Fallback for committees
    missing a `cycles` field — shouldn't normally be needed."""
    import datetime
    year = datetime.datetime.now().year
    return year if year % 2 == 0 else year + 1


def _committees_for_candidate(candidate: dict) -> list[dict]:
    """Returns the committee dicts themselves (not just IDs) — we need each
    committee's `cycles` list to query the right two_year_transaction_period."""
    return [c for c in (candidate.get("principal_committees") or []) if c.get("committee_id")]


def fetch_schedule_a(committee_id: str, api_key: str, cycle: int,
                      progress_cb: ProgressCallback = None,
                      individuals_only: bool = True,
                      max_pages: Optional[int] = None,
                      smallest_first: bool = False) -> tuple[list[dict], bool, str]:
    """Pull EVERY itemized contribution for one committee in one two-year
    cycle, deduplicated by sub_id.

    `cycle` (two_year_transaction_period) is REQUIRED in practice — omitting
    it returns zero results for committees that don't have data in whatever
    FEC defaults to, even when the committee has hundreds of thousands of
    real records under its actual cycle. Confirmed by direct testing:
    omitting it returned 0 results for a committee with 162,130 records
    under cycle=2026.

    Sorts by contribution_receipt_date (fewer exact ties than amount — most
    contribution amounts cluster hard around legal limits like $3,500/
    $7,000, dates much less so), and deduplicates every record by `sub_id`
    (a true unique row ID) as a defense against any remaining pagination
    instability rather than relying on perfect tie-breaking.

    Every request goes through _http_get_with_retry, which paces calls to
    stay under FEC's 120/min limit and retries 429/5xx with exponential
    backoff. This matters a lot here specifically: live testing found that
    unpaced, back-to-back requests across 800+ pages blow past the rate
    limit within seconds, and a single transient-looking failure on a later
    page can actually be sustained rate limiting — which, without retries,
    silently produces a "complete-looking" result that's wrong by orders of
    magnitude (a real run returned $3,911.59 against an 84,342-record
    committee this way).

    Returns (records, incomplete, stop_reason). incomplete is True if EITHER
    the absolute safety valve (_ABSOLUTE_MAX_PAGES) was hit, OR a later page
    kept failing even after all retries — in both cases, total_raised is a
    lower bound, not the real total, and that must be disclosed to the user
    rather than presented as a finished, trustworthy number. stop_reason is
    a human-readable explanation of why (empty string if not incomplete) —
    this exists because a previous version of this function set the
    incomplete flag with NO detail, which made debugging a real stuck-early
    run pure guesswork.

    progress_cb(page_num, pages_total), if given, is called after every page
    — pages_total comes from FEC's own response and is None only if the
    very first page fails.
    """
    seen_sub_ids: set = set()
    records: list[dict] = []
    last_index = None
    last_date = None
    pages_total: Optional[int] = None
    page_num = 0
    hit_safety_valve = False
    stop_reason = ""

    while True:
        params = {
            "api_key": api_key,
            "committee_id": committee_id,
            "two_year_transaction_period": str(cycle),
            # When capped (max_pages set), sort LARGEST-FIRST so the slice we
            # keep is the biggest donors. For an uncapped full pull, keep the
            # date sort (fewer cursor ties — see docstring).
            "sort": ("contribution_receipt_amount" if smallest_first
                     else ("-contribution_receipt_amount" if max_pages
                           else "contribution_receipt_date")),
            "per_page": str(_PER_PAGE),
            "is_individual": "true",  # KNOWN OPEN QUESTION, not yet resolved:
                                       # this was meant to exclude conduit/
                                       # committee-transfer noise, but every
                                       # raw record we've actually seen in
                                       # this dataset (filtered AND unfiltered
                                       # samples) carried line_number "11AI" —
                                       # literally "Contributions From
                                       # Individuals/Persons Other Than
                                       # Political Committees" — meaning this
                                       # filter may also be excluding real,
                                       # legitimate direct PAC-to-candidate
                                       # contributions, not just transfer
                                       # noise. The pac_or_org bucket in
                                       # DonorTotal can structurally never
                                       # fire while this filter is on. NOT
                                       # YET TESTED: pull a real sample
                                       # without this filter and check the
                                       # entity_type distribution before
                                       # changing it — removing it blind
                                       # risks reintroducing the conduit
                                       # double-counting this filter may
                                       # also have been protecting against.
            "fields": ",".join(SCHEDULE_A_FIELDS),
        }
        # is_individual=true excludes committee-to-committee transfers. That's
        # right for the candidate-donor view (Pipe 1), but WRONG for Hop 2:
        # tracing who funds a super PAC, PAC-to-PAC transfers INTO it are exactly
        # the routing path we want to see. So Hop 2 calls with
        # individuals_only=False. (See the long note above on why this filter is
        # load-bearing for the direct-donor view.)
        if individuals_only:
            params["is_individual"] = "true"
        if last_index is not None:
            params["last_index"] = str(last_index)
            params["last_contribution_receipt_date"] = str(last_date)

        url = f"{FEC_BASE}/schedules/schedule_a/?{urllib.parse.urlencode(params)}"
        code, body = _http_get_with_retry(url)
        page_num += 1
        if code != 200 or not body:
            if page_num == 1:
                # First request failing even after retries is almost always
                # a config/auth problem (bad key, bad params) — surface it
                # clearly rather than silently returning "zero records",
                # which looks identical to "this committee genuinely has no
                # data".
                if code == -1:
                    raise FECAPIError(
                        f"Could not reach the FEC API for committee {committee_id} "
                        f"(connection-level failure): {body}", status_code=-1
                    )
                raise FECAPIError(
                    f"FEC schedule_a returned HTTP {code} for committee {committee_id}: {body[:300]!r}",
                    status_code=code
                )
            # A later page still failing after _MAX_RETRIES exponential-backoff
            # attempts is NOT a quick blip — it's a real, sustained problem
            # (sustained rate limiting, an outage). Stop and keep whatever
            # real data we already have rather than raising and discarding
            # potentially many minutes of good progress, but — this is the
            # bug a live test caught — mark it incomplete. A previous version
            # of this code stopped silently here with no flag set, so a
            # rate-limited partial pull looked exactly like a small, complete,
            # correct total. It is not safe to assume a stop here means
            # "naturally reached the end of the data."
            hit_safety_valve = True
            stop_reason = (f"stopped at page {page_num}/{pages_total or '?'}: "
                            f"HTTP {code} after {_MAX_RETRIES + 1} attempts — {body[:200]!r}")
            break
        try:
            data = json.loads(body)
        except ValueError:
            if page_num == 1:
                raise FECAPIError(f"FEC schedule_a returned non-JSON response: {body[:300]!r}")
            hit_safety_valve = True
            stop_reason = f"stopped at page {page_num}/{pages_total or '?'}: non-JSON response — {body[:200]!r}"
            break

        pagination = data.get("pagination", {})
        if pages_total is None:
            pages_total = pagination.get("pages")
        if progress_cb:
            progress_cb(page_num, pages_total)

        page_results = data.get("results", [])
        if not page_results:
            break
        for r in page_results:
            sid = r.get("sub_id")
            if sid is not None:
                if sid in seen_sub_ids:
                    continue
                seen_sub_ids.add(sid)
            records.append(r)

        last_indexes = pagination.get("last_indexes") or {}
        last_index = last_indexes.get("last_index")
        last_date = last_indexes.get("last_contribution_receipt_date")
        if last_index is None:
            break
        # Deliberate cap: we only wanted the biggest donors, and (sorted
        # largest-first) we now have them. This is NOT a truncation — the
        # campaign total comes from /totals, not this list — so we stop
        # cleanly without setting the incomplete flag.
        if max_pages is not None and page_num >= max_pages:
            break
        if page_num >= _ABSOLUTE_MAX_PAGES:
            hit_safety_valve = True
            stop_reason = f"hit the absolute safety valve at page {page_num} (_ABSOLUTE_MAX_PAGES)"
            break

    return records, hit_safety_valve, stop_reason


def _schedule_a_txn(r: dict) -> dict:
    """Normalize one raw Schedule A receipt row into a flat, export-ready record.
    These are the transaction-level rows that get GROUPED into per-donor
    summaries; we keep them too so the export can offer donor detail down to the
    individual contribution. City/state only — never a street address."""
    city = r.get("contributor_city")
    st = r.get("contributor_state")
    return {
        "contributor_name": (r.get("contributor_name") or "").strip() or "(name not reported)",
        "employer": r.get("contributor_employer"),
        "occupation": r.get("contributor_occupation"),
        "city": city,
        "state": st,
        "date": (r.get("contribution_receipt_date") or "")[:10],
        "amount": round(_to_float(r.get("contribution_receipt_amount")), 2),
        "bucket": "individual" if r.get("is_individual") else "pac_or_org",
        "committee_id": r.get("committee_id"),
        "sub_id": r.get("sub_id"),
    }


def _split_fec_name(fec_name: str) -> tuple[str, str]:
    """'COLLINS, MICHAEL A JR' -> ('michael', 'collins'). FEC stores names
    LAST, FIRST MIDDLE [SUFFIX]; we only need first + last for matching."""
    parts = (fec_name or "").split(",", 1)
    last = parts[0].strip()
    first = ""
    if len(parts) > 1:
        toks = parts[1].strip().split()
        first = toks[0] if toks else ""
    return first, last


def _first_name_score(typed_first: str, cand_first: str) -> float:
    """How well a typed first name matches a FEC first name, WITHOUT a nickname
    table (the thing we explicitly don't want to maintain). Pure string
    similarity plus a shared-prefix bonus — enough to float 'Michael' up for
    'mike', 'David' for 'dave', 'Matthew' for 'matt', etc. Harder pairs
    (bill/William) still surface in the shortlist via the ratio, and the
    office/state/cycle shown in the picker lets the human make the final call."""
    if not typed_first or not cand_first:
        return 0.0
    tf, cf = typed_first.lower(), cand_first.lower()
    if tf == cf:
        return 2.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, tf, cf).ratio()
    p = 0
    while p < min(len(tf), len(cf)) and tf[p] == cf[p]:
        p += 1
    return ratio + min(p, 4) * 0.15   # prefix bonus rewards nickname→formal name


_OFFICE_LABEL = {"H": "U.S. House", "S": "U.S. Senate", "P": "President"}


def candidate_option(c: dict) -> dict:
    """Trim a FEC candidate dict to the fields the 'Did You Mean?' picker shows
    and the run needs. Everything here is public FEC metadata."""
    cycles = sorted(c.get("cycles") or [])
    return {
        "candidate_id": c.get("candidate_id"),
        "name": c.get("name"),
        "office": c.get("office"),
        "office_full": c.get("office_full") or _OFFICE_LABEL.get(c.get("office"), c.get("office")),
        "state": c.get("state"),
        "district": c.get("district") if c.get("office") == "H" else None,
        "party": c.get("party_full") or c.get("party"),
        "cycles": cycles,
        "last_cycle": cycles[-1] if cycles else None,
        "status": c.get("candidate_status"),          # C=current, P=prior, F/N=future/not-yet
        "inactive": bool(c.get("candidate_inactive")),
        "incumbent_challenge": c.get("incumbent_challenge_full"),
        "has_raised_funds": bool(c.get("has_raised_funds")),
    }


def resolve_candidate_options(name: str, api_key: str, limit: int = 15) -> dict:
    """Turn a typed name into a ranked shortlist of FEC candidates for the
    'Did You Mean?' picker — the fix for two real failures: nicknames FEC
    doesn't know ('Mike' ≠ 'Michael' → zero hits) and one person holding
    several candidate_ids across offices/cycles (House vs Senate runs).

    Strategy, no alias library:
      1. Search FEC by exactly what was typed.
      2. If that's empty (the nickname miss), broaden to the LAST name and pull
         a wide page, then rank by first-name similarity to what was typed.
      3. Dedupe by candidate_id; score by last-name match, first-name
         similarity, current-candidate status, whether they've raised funds,
         and cycle recency. Return the trimmed options for the picker.

    Returns {query, broadened, candidates:[option, …]}. Never auto-picks —
    the human chooses, so the exact office/cycle is always confirmed.
    """
    typed = (name or "").strip()
    tokens = [t for t in re.split(r"\s+", typed) if t]
    typed_first = tokens[0] if tokens else ""
    # Last-name token for the broaden step — skip trailing suffixes so
    # "Michael Collins Jr" broadens on "Collins", not "Jr".
    _SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "jr.", "sr."}
    core = [t for t in tokens if t.lower().strip(".") not in {s.strip(".") for s in _SUFFIXES}]
    typed_last = core[-1] if len(core) > 1 else ""

    raw = search_fec_candidates(typed, api_key)
    broadened = False
    if not raw and typed_last:
        raw = search_fec_candidates(typed_last, api_key, per_page=50)
        broadened = True

    seen: dict[str, dict] = {}
    for c in raw:
        cid = c.get("candidate_id")
        if cid and cid not in seen:
            seen[cid] = c

    scored: list[tuple[float, int, dict]] = []
    for c in seen.values():
        cf, cl = _split_fec_name(c.get("name", ""))
        fscore = _first_name_score(typed_first, cf) if typed_first else 0.0
        if typed_last:
            lmatch = 2.0 if typed_last.lower() in (cl or "").lower() else 0.0
        else:
            # single-token query — treat it as possibly a last name too
            lmatch = 1.0 if typed_first.lower() in (cl or "").lower() else 0.0
        active = 0.6 if c.get("candidate_status") == "C" else 0.0
        funds = 0.3 if c.get("has_raised_funds") else 0.0
        last_cycle = max(c.get("cycles") or [0]) if c.get("cycles") else 0
        score = lmatch + fscore + active + funds + last_cycle / 100000.0
        scored.append((score, last_cycle, candidate_option(c)))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return {
        "query": typed,
        "broadened": broadened,
        "candidates": [opt for _, __, opt in scored[:limit]],
    }


def search_fec_candidate(name: str, api_key: str,
                          progress_cb: ProgressCallback = None,
                          max_pages: Optional[int] = None,
                          only_candidate_id: Optional[str] = None) -> list[RecipientGroup]:
    """Search FEC for a candidate by name, return one RecipientGroup per
    matched candidate+committee (a person can have multiple candidate_ids
    across different federal offices/cycles).

    When `only_candidate_id` is set (the 'Did You Mean?' picker gives us an
    exact FEC id), the match is filtered to that one candidate — so a person
    with several runs (House vs Senate) resolves to exactly the seat chosen,
    not an aggregate across all of them. Without it, behaviour is unchanged.

    This can take minutes for high-profile races (hundreds of thousands of
    records) — always run via the async job pattern in app.py, never inline
    in a request handler."""
    candidates = search_fec_candidates(name, api_key)
    if only_candidate_id:
        match = [c for c in candidates if c.get("candidate_id") == only_candidate_id]
        if not match:
            # The name (possibly FEC "LAST, FIRST" form) didn't surface the id;
            # broaden to the last-name token and filter again. Robust to the
            # exact query not returning the target directly.
            last = (name.split(",")[0].strip() or name.split()[-1]) if name else ""
            if last:
                match = [c for c in search_fec_candidates(last, api_key, per_page=50)
                         if c.get("candidate_id") == only_candidate_id]
        candidates = match
    groups: list[RecipientGroup] = []

    for cand in candidates:
        committees = _committees_for_candidate(cand)
        if not committees:
            continue
        cand_name = cand.get("name") or name
        office = cand.get("office")

        donor_map: dict[str, DonorTotal] = {}
        txns: list[dict] = []            # per-row receipts, retained for export
        memo_skipped = 0                 # memo_code="X" re-itemizations dropped
        total_raised = 0.0
        latest_year = None
        any_truncated = False
        truncated_reasons: list[str] = []

        for committee in committees:
            committee_id = committee["committee_id"]
            cycles = committee.get("cycles") or []
            cycle = max(cycles) if cycles else _current_cycle()
            records, truncated, stop_reason = fetch_schedule_a(committee_id, api_key, cycle, progress_cb, max_pages=max_pages)
            any_truncated = any_truncated or truncated
            if stop_reason:
                truncated_reasons.append(f"{committee_id}: {stop_reason}")
            for r in records:
                # Skip memo re-itemizations (memo_code="X"): a later report
                # restating a receipt already counted on an earlier one. FEC
                # excludes them from totals; summing them double-counts the
                # donor (verified on live Collins-2026 data). Dropped BEFORE the
                # transaction list too, so the per-contribution CSV matches the
                # donor summaries.
                if (r.get("memo_code") or "").strip().upper() == "X":
                    memo_skipped += 1
                    continue
                txns.append(_schedule_a_txn(r))
                donor_name = (r.get("contributor_name") or "").strip() or "(name not reported)"
                amount = _to_float(r.get("contribution_receipt_amount"))
                rdate = (r.get("contribution_receipt_date") or "")[:10]
                is_individual = r.get("is_individual")
                bucket = "individual" if is_individual else "pac_or_org"
                city = r.get("contributor_city")
                st = r.get("contributor_state")
                location = ", ".join(p for p in [city, st] if p) or None

                key = donor_name.lower()
                if key not in donor_map:
                    donor_map[key] = DonorTotal(
                        name=donor_name, bucket=bucket,
                        employer=r.get("contributor_employer"),
                        occupation=r.get("contributor_occupation"), location=location,
                    )
                d = donor_map[key]
                d.total += amount
                d.count += 1
                total_raised += amount
                if rdate:
                    if not d.first_date or rdate < d.first_date:
                        d.first_date = rdate
                    if not d.last_date or rdate > d.last_date:
                        d.last_date = rdate
                    year = int(rdate[:4]) if rdate[:4].isdigit() else None
                    if year and (latest_year is None or year > latest_year):
                        latest_year = year

        if not donor_map:
            continue

        # ── Net out refunds ──────────────────────────────────────────────────
        # Refunds/redesignations are NEGATIVE Schedule A rows. Because the
        # capped pull sorts largest-first, negatives sit at the far end and are
        # never reached — so a donor who gave over the limit and was refunded
        # still shows the gross amount (real Collins-2026 case: Guy Millner
        # showed $14,000 when two $7,000 receipts were partly refunded on
        # 2025-11-26, netting ~$7,000). One extra ASCENDING page reaches the
        # most-negative rows cheaply and nets them against the donors we hold.
        refunds_applied = 0
        refund_total = 0.0
        if max_pages:                      # only meaningful for the capped pull
            try:
                neg_records, _, _ = fetch_schedule_a(
                    committee_id, api_key, cycle, None,
                    max_pages=1, smallest_first=True)
            except FECAPIError:
                neg_records = []           # refunds are a refinement, never fatal
            for r in neg_records:
                amt = _to_float(r.get("contribution_receipt_amount"))
                if amt >= 0:
                    break                  # ascending: past the negatives
                if (r.get("memo_code") or "").strip().upper() == "X":
                    continue               # memo pairs already net to zero
                k = ((r.get("contributor_name") or "").strip() or
                     "(name not reported)").lower()
                if k in donor_map:         # only net against donors we're showing
                    donor_map[k].total += amt      # amt is negative
                    total_raised += amt
                    refund_total += amt
                    refunds_applied += 1
                    txns.append(_schedule_a_txn(r))
            # a refund can push a donor to/below zero — drop those rather than
            # display a negative "donor"
            for k in [k for k, v in donor_map.items() if v.total <= 0]:
                donor_map.pop(k, None)

        donors = sorted(donor_map.values(), key=lambda d: d.total, reverse=True)
        txns.sort(key=lambda t: t.get("amount", 0) or 0, reverse=True)  # largest-first
        groups.append(RecipientGroup(
            recipient_name=cand_name, donors=donors, transactions=txns,
            total_raised=round(total_raised, 2),
            memo_skipped=memo_skipped, refunds_applied=refunds_applied,
            refund_total=round(refund_total, 2),
            latest_record_year=latest_year, office=office, candidate_id=cand.get("candidate_id"),
            truncated=any_truncated, truncated_reason="; ".join(truncated_reasons),
        ))

    groups.sort(key=lambda g: g.total_raised, reverse=True)
    return groups


def to_jsonable(groups: list[RecipientGroup]) -> list[dict]:
    return [{
        "recipient_name": g.recipient_name, "total_raised": g.total_raised,
        "latest_record_year": g.latest_record_year, "office": g.office,
        "candidate_id": g.candidate_id, "truncated": g.truncated,
        "truncated_reason": g.truncated_reason,
        "memo_skipped": g.memo_skipped,
        "refunds_applied": g.refunds_applied, "refund_total": g.refund_total,
        "donors": [{
            "name": d.name, "bucket": d.bucket, "bucket_inferred": d.bucket_inferred,
            "total": round(d.total, 2), "count": d.count,
            "first_date": d.first_date, "last_date": d.last_date,
            "employer": d.employer, "occupation": d.occupation, "location": d.location,
        } for d in g.donors],
        "transactions": g.transactions,   # per-row receipts (largest-first)
    } for g in groups]


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULE E — INDEPENDENT EXPENDITURES (outside spending / "Pipe 2")
# ════════════════════════════════════════════════════════════════════════════
#
# Schedule A (above) is money given TO a campaign — capped (~$3,500/election per
# person), so a billionaire's direct gift is the same ceiling as anyone's.
# Schedule E is money spent FOR or AGAINST a candidate by outside groups that
# legally can't coordinate with the campaign — UNCAPPED. This is where the
# material billionaire money actually lives, which is exactly why a donor-list
# view built only on Schedule A understates influence.
#
# THE TWO-HOP TRACE this enables:
#   Hop 1 (here): Schedule E → "Committee X spent $5M supporting Ossoff."
#   Hop 2 (reuses fetch_schedule_a): run Schedule A on Committee X itself →
#          "Soros gave $5M to Committee X." Now the chain is visible.
#   Dark-money dead-end: super PACs (type "O") disclose their donors so Hop 2
#          closes; 501(c)(4)s do not, so the trace stops at the opaque group.
#          We flag which is which rather than pretending the gap isn't there.
#
# KNOWN UPSTREAM BUG (openFEC issue #3396): schedule_e keyset pagination can
# return FAR fewer records than pagination.count claims (a documented case got
# ~9 pages of a claimed 80). This is the same silent-undercount failure mode
# fetch_schedule_a was hardened against. Defense here: capture pagination.count
# on page 1 and, after the pull, compare it to the unique-record count. Any
# shortfall sets incomplete=True with a reason — a partial IE pull must never
# look like a complete one, same rule as Schedule A.

# Explicit field allowlist, same discipline as SCHEDULE_A_FIELDS: request only
# what's needed. Schedule E has no contributor PII (the donors live one hop
# away, in the spender's OWN Schedule A), so there's no address surface to drop
# here — payee_name is a vendor (e.g. an ad firm), not a person's home.
SCHEDULE_E_FIELDS = [
    "candidate_id", "candidate_name",
    "committee_id", "committee_name",
    "support_oppose_indicator",          # "S" = supporting, "O" = opposing
    "expenditure_amount", "expenditure_date",
    "payee_name", "expenditure_description", "category_code",
    "sub_id",                            # unique row id — dedup key, not display
    # The four fields below exist to make totals CORRECT (see _dedup_schedule_e).
    # Without them the same expenditure is summed twice: once from its 24/48-hour
    # notice (Form 24/F5 notice) and again from the regular report (F3X/F5) that
    # later restates it — a real Ossoff 2026 run overstated oppose spending ~2x
    # ($934,835.78 vs FEC's official $462,124.63 + $17,938.74 notice-only).
    "is_notice",                         # True = 24/48-hour notice row
    "most_recent",                       # False = superseded by an amendment
    "memo_code",                         # "X" = memo itemization, not new money
    "filing_form",                       # F24/F3X/F5 — kept for display/debugging
]

# Committee designations that file their OWN Schedule A donor detail, so Hop 2
# can trace who funds them. This is a heuristic flag (marked, never asserted as
# fact — same spirit as DonorTotal.bucket_inferred), because the only fully
# reliable test is to actually attempt the Hop-2 pull and see if donors come
# back. Super PAC = type "O"; conventional PACs = "N"/"Q". A spender whose money
# originates in a 501(c)(4) will show here but Hop 2 will dead-end — that's the
# dark-money boundary, and it's intelligence in itself.
_TRACEABLE_COMMITTEE_TYPES = {"O", "N", "Q", "V", "W"}


@dataclass
class OutsideSpender:
    """One committee's independent-expenditure activity for/against a candidate."""
    committee_id: str
    committee_name: str = ""
    committee_type: Optional[str] = None   # one-letter FEC code, e.g. "O" (super PAC)
    committee_type_full: Optional[str] = None
    support_total: float = 0.0             # $ spent SUPPORTING this candidate
    oppose_total: float = 0.0              # $ spent OPPOSING this candidate
    count: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    traceable: bool = False                # heuristic: does it disclose its own donors?
    notice_only_total: float = 0.0         # $ counted only from 24/48-hour notices
                                           # (no regular report processed yet)


@dataclass
class OutsideSpending:
    """Hop-1 result: all independent expenditures targeting one candidate."""
    candidate_id: str
    candidate_name: str = ""
    cycle: Optional[int] = None
    support_total: float = 0.0             # sum of itemized supporting expenditures
    oppose_total: float = 0.0              # sum of itemized opposing expenditures
    spenders: list = field(default_factory=list)   # list[OutsideSpender]
    record_count: int = 0                  # unique IE rows actually pulled (RAW,
                                           # pre-dedup — comparable to reported_count)
    reported_count: Optional[int] = None   # pagination.count claimed by FEC (page 1)
    incomplete: bool = False               # True if pull < reported_count, or a page failed
    incomplete_reason: str = ""
    dedup: dict = field(default_factory=dict)   # notice/amendment collapse stats
                                           # (see _dedup_schedule_e) — how raw rows
                                           # became the totals, incl. notice-only sums
    transactions: list = field(default_factory=list)  # per-row deduped IEs, newest
                                           # -first, for the transaction-level CSV


def _schedule_e_txn(r: dict) -> dict:
    """Normalize one deduped Schedule E row into a flat, export-ready record —
    the transaction-level detail behind each per-spender summary. `notice_only`
    marks a row known only from a 24/48-hour notice (its regular report isn't
    processed yet); see _dedup_schedule_e."""
    return {
        "committee_id": r.get("committee_id"),
        "committee_name": (r.get("committee_name") or "").strip(),
        "support_oppose": (r.get("support_oppose_indicator") or "").strip().upper(),
        "amount": round(_to_float(r.get("expenditure_amount")), 2),
        "date": (r.get("expenditure_date") or "")[:10],
        "payee": r.get("payee_name"),
        "description": r.get("expenditure_description"),
        "is_notice": bool(r.get("is_notice")),
        "notice_only": bool(r.get("notice_only")),
        "filing_form": r.get("filing_form"),
        "sub_id": r.get("sub_id"),
    }


def _dedup_schedule_e(records: list[dict]) -> tuple[list[dict], dict]:
    """Collapse Schedule E rows to one row per actual expenditure. sub_id dedup
    alone is NOT enough: the same expenditure legally appears twice — once on a
    24/48-hour notice (Form 24, or an F5 notice) and again on the regular
    report (F3X/F5) that later restates it — under two different sub_ids.
    Summing raw rows double-counts every notice-then-reported expenditure.

    Rules (verified to the cent against FEC's own
    /schedules/schedule_e/totals/by_candidate/ aggregate on live Ossoff 2026
    data — filtered total minus notice-only rows matched FEC's $462,124.63
    oppose / $23,791.26 support exactly):
      1. Drop memo rows (memo_code == "X") — itemizations of money already
         counted, not new spending.
      2. Drop superseded amendment versions (most_recent is False; None is
         KEPT — older processed data doesn't populate the flag).
      3. Drop a notice row when a non-notice row matches on
         (committee_id, expenditure_date, expenditure_amount) — the regular
         report supersedes its own notice.
      4. KEEP notice rows with no matching regular row ("notice-only": the
         freshest spending, filed within 24/48 hours, whose regular report
         isn't processed yet — e.g. an F5 filer's latest buy). These are
         tagged notice_only=True on the row and disclosed, never silently
         included or dropped: FEC's own aggregate excludes them, so the
         tool's headline total = FEC aggregate + disclosed notice-only sum.

    Returns (kept_rows, stats). stats discloses every drop and the
    notice-only sums by side so a reader can reconcile our totals against
    fec.gov's to the cent.
    """
    live: list[dict] = []
    memo_dropped = superseded_dropped = 0
    for r in records:
        if (r.get("memo_code") or "").strip().upper() == "X":
            memo_dropped += 1
            continue
        if r.get("most_recent") is False:
            superseded_dropped += 1
            continue
        live.append(r)

    regular_keys = {
        (r.get("committee_id"), (r.get("expenditure_date") or "")[:10],
         _to_float(r.get("expenditure_amount")))
        for r in live if not r.get("is_notice")
    }
    kept: list[dict] = []
    notice_deduped = 0
    notice_only_support = notice_only_oppose = 0.0
    notice_only_kept = 0
    for r in live:
        if r.get("is_notice"):
            k = (r.get("committee_id"), (r.get("expenditure_date") or "")[:10],
                 _to_float(r.get("expenditure_amount")))
            if k in regular_keys:
                notice_deduped += 1
                continue
            r["notice_only"] = True
            notice_only_kept += 1
            amt = _to_float(r.get("expenditure_amount"))
            so = (r.get("support_oppose_indicator") or "").strip().upper()
            if so == "S":
                notice_only_support += amt
            elif so == "O":
                notice_only_oppose += amt
        kept.append(r)

    stats = {
        "raw_rows": len(records),
        "memo_dropped": memo_dropped,
        "superseded_dropped": superseded_dropped,
        "notice_deduped": notice_deduped,
        "notice_only_kept": notice_only_kept,
        "notice_only_support": round(notice_only_support, 2),
        "notice_only_oppose": round(notice_only_oppose, 2),
    }
    return kept, stats


def fetch_schedule_e(candidate_id: str, api_key: str, cycle: int,
                     progress_cb: ProgressCallback = None) -> tuple[list[dict], bool, str, Optional[int]]:
    """Pull every itemized independent expenditure targeting one candidate in
    one cycle, deduplicated by sub_id.

    Mirrors fetch_schedule_a's contract and hardening, with three deliberate
    differences forced by the endpoint:
      - filter param is `candidate_id` (who the spending targets), and the
        cycle param is `cycle`, NOT `two_year_transaction_period` — schedule_e
        uses a different name and passing the schedule_a one silently filters
        nothing.
      - sort key is `expenditure_date`; the resume cursor keys are read back
        generically from pagination.last_indexes (FEC's own param names), so we
        don't hard-code `last_expenditure_date` and break if it ever changes.
      - returns a 4th value, reported_count (pagination.count from page 1), so
        the caller can detect the issue-#3396 undercount by comparing it to the
        number of unique rows actually returned.

    Returns (records, incomplete, stop_reason, reported_count). `incomplete`
    here means a PAGE FAILED mid-pull (transport/JSON), same as schedule_a. The
    count-shortfall check is done one level up, in outside_spending_for_candidate,
    because only there do we know the final unique-row count.
    """
    seen_sub_ids: set = set()
    records: list[dict] = []
    cursor: dict = {}                 # last_indexes from the previous page
    reported_count: Optional[int] = None
    pages_total: Optional[int] = None
    page_num = 0
    incomplete = False
    stop_reason = ""

    while True:
        params = {
            "api_key": api_key,
            "candidate_id": candidate_id,
            "cycle": str(cycle),
            "sort": "expenditure_date",
            "per_page": str(_PER_PAGE),
            "fields": ",".join(SCHEDULE_E_FIELDS),
        }
        # Resume by echoing back whatever keyset cursor FEC handed us last page.
        # The keys in last_indexes ARE the query-param names (last_index,
        # last_expenditure_date), so this stays correct without hard-coding them.
        params.update({k: str(v) for k, v in cursor.items() if v is not None})

        url = f"{FEC_BASE}/schedules/schedule_e/?{urllib.parse.urlencode(params)}"
        code, body = _http_get_with_retry(url)
        page_num += 1

        if code != 200 or not body:
            if page_num == 1:
                if code == -1:
                    raise FECAPIError(
                        f"Could not reach the FEC API for schedule_e candidate "
                        f"{candidate_id} (connection-level failure): {body}", status_code=-1)
                raise FECAPIError(
                    f"FEC schedule_e returned HTTP {code} for candidate "
                    f"{candidate_id}: {body[:300]!r}", status_code=code)
            incomplete = True
            stop_reason = (f"schedule_e stopped at page {page_num}/{pages_total or '?'}: "
                           f"HTTP {code} after {_MAX_RETRIES + 1} attempts — {body[:200]!r}")
            break

        try:
            data = json.loads(body)
        except ValueError:
            if page_num == 1:
                raise FECAPIError(f"FEC schedule_e returned non-JSON: {body[:300]!r}")
            incomplete = True
            stop_reason = f"schedule_e stopped at page {page_num}: non-JSON — {body[:200]!r}"
            break

        pagination = data.get("pagination", {})
        if reported_count is None:
            reported_count = pagination.get("count")
            pages_total = pagination.get("pages")
        if progress_cb:
            progress_cb(page_num, pages_total)

        page_results = data.get("results", [])
        if not page_results:
            break
        for r in page_results:
            sid = r.get("sub_id")
            if sid is not None:
                if sid in seen_sub_ids:
                    continue
                seen_sub_ids.add(sid)
            records.append(r)

        cursor = pagination.get("last_indexes") or {}
        if not cursor or cursor.get("last_index") is None:
            break
        if page_num >= _ABSOLUTE_MAX_PAGES:
            incomplete = True
            stop_reason = f"schedule_e hit the absolute safety valve at page {page_num}"
            break

    return records, incomplete, stop_reason, reported_count


def outside_spending_for_candidate(candidate_id: str, api_key: str, cycle: int,
                                   candidate_name: str = "",
                                   progress_cb: ProgressCallback = None) -> OutsideSpending:
    """Hop 1: aggregate all independent expenditures targeting a candidate into
    per-spender support/oppose totals, with a completeness cross-check against
    FEC's own reported count (the issue-#3396 defense).

    Does NOT trace donors into the spenders — that's Hop 2 (trace_spender_donors),
    kept separate because it's expensive and often dead-ends at dark money, so
    the caller decides which spenders are worth chasing.
    """
    records, incomplete, stop_reason, reported_count = fetch_schedule_e(
        candidate_id, api_key, cycle, progress_cb)

    # Collapse notice/report double-filings BEFORE aggregating — raw rows count
    # the same expenditure twice (24/48-hour notice + regular report). The
    # #3396 pagination cross-check further down stays on the RAW row count,
    # because it measures pull completeness, not expenditure count.
    deduped, dedup_stats = _dedup_schedule_e(records)

    spenders: dict[str, OutsideSpender] = {}
    support_total = 0.0
    oppose_total = 0.0

    for r in deduped:
        cid = r.get("committee_id") or "(unknown committee)"
        amount = _to_float(r.get("expenditure_amount"))
        so = (r.get("support_oppose_indicator") or "").strip().upper()
        rdate = (r.get("expenditure_date") or "")[:10]

        sp = spenders.get(cid)
        if sp is None:
            # committee_type may arrive as a one-letter code at top level or
            # nested in a "committee" object depending on the response shape;
            # check both so the traceable flag is populated either way.
            ctype = r.get("committee_type")
            ctype_full = r.get("committee_type_full")
            nested = r.get("committee") if isinstance(r.get("committee"), dict) else {}
            ctype = ctype or nested.get("committee_type")
            ctype_full = ctype_full or nested.get("committee_type_full")
            sp = OutsideSpender(
                committee_id=cid,
                committee_name=(r.get("committee_name") or nested.get("name") or "").strip(),
                committee_type=ctype,
                committee_type_full=ctype_full,
                traceable=(ctype in _TRACEABLE_COMMITTEE_TYPES) if ctype else False,
            )
            spenders[cid] = sp

        if so == "O":
            sp.oppose_total += amount
            oppose_total += amount
        else:  # default unflagged rows to "support" is wrong — only count S as support
            if so == "S":
                sp.support_total += amount
                support_total += amount
            # rows with neither S nor O (rare/malformed) are counted toward
            # neither total but still increment count, so they're not invisible.
        sp.count += 1
        if r.get("notice_only"):
            sp.notice_only_total = round(sp.notice_only_total + amount, 2)
        if rdate:
            if not sp.first_date or rdate < sp.first_date:
                sp.first_date = rdate
            if not sp.last_date or rdate > sp.last_date:
                sp.last_date = rdate

    # Completeness cross-check — the core issue-#3396 defense. If FEC told us
    # there are N rows but keyset pagination only yielded fewer, the totals are
    # a LOWER BOUND, not the truth, and must be flagged exactly like a truncated
    # Schedule A pull.
    unique_count = len(records)
    if reported_count is not None and unique_count < reported_count:
        incomplete = True
        gap = reported_count - unique_count
        shortfall = (f"schedule_e returned {unique_count} of {reported_count} "
                     f"reported rows ({gap} missing) — likely openFEC keyset "
                     f"pagination bug (issue #3396); totals are a lower bound")
        stop_reason = f"{stop_reason}; {shortfall}" if stop_reason else shortfall

    ranked = sorted(spenders.values(),
                    key=lambda s: (s.support_total + s.oppose_total), reverse=True)
    txns = [_schedule_e_txn(r) for r in deduped]
    txns.sort(key=lambda t: (t.get("date") or "", t.get("amount", 0) or 0), reverse=True)
    return OutsideSpending(
        candidate_id=candidate_id, candidate_name=candidate_name, cycle=cycle,
        support_total=round(support_total, 2), oppose_total=round(oppose_total, 2),
        spenders=ranked, record_count=unique_count, reported_count=reported_count,
        incomplete=incomplete, incomplete_reason=stop_reason, dedup=dedup_stats,
        transactions=txns,
    )


def trace_spender_donors(committee_id: str, api_key: str, cycle: int,
                         progress_cb: ProgressCallback = None) -> tuple[list[DonorTotal], bool, str]:
    """Hop 2: who funds an outside-spending committee. Reuses fetch_schedule_a
    unchanged — a super PAC's donors are just its own Schedule A receipts.

    Returns (donors, incomplete, stop_reason). An EMPTY donor list from a
    committee that did real spending is itself a finding: it's the dark-money
    signature — the group spent but discloses no upstream donors here.

    Calls fetch_schedule_a with individuals_only=False ON PURPOSE: a super PAC is
    frequently funded by OTHER committees (PAC-to-PAC transfers), and the default
    is_individual=true filter would hide exactly those. So an empty Hop-2 result
    here now genuinely means "no disclosed funders of any kind" (the real
    dark-money signature), not merely "no individual funders."
    """
    records, incomplete, stop_reason = fetch_schedule_a(
        committee_id, api_key, cycle, progress_cb, individuals_only=False)
    donor_map: dict[str, DonorTotal] = {}
    for r in records:
        name = (r.get("contributor_name") or "").strip() or "(name not reported)"
        amount = _to_float(r.get("contribution_receipt_amount"))
        rdate = (r.get("contribution_receipt_date") or "")[:10]
        key = name.lower()
        d = donor_map.get(key)
        if d is None:
            d = DonorTotal(name=name,
                           bucket="individual" if r.get("is_individual") else "pac_or_org",
                           employer=r.get("contributor_employer"),
                           occupation=r.get("contributor_occupation"))
            donor_map[key] = d
        d.total += amount
        d.count += 1
        if rdate:
            if not d.first_date or rdate < d.first_date:
                d.first_date = rdate
            if not d.last_date or rdate > d.last_date:
                d.last_date = rdate
    donors = sorted(donor_map.values(), key=lambda d: d.total, reverse=True)
    return donors, incomplete, stop_reason


def outside_spending_to_jsonable(os_: OutsideSpending) -> dict:
    """JSON shape for app.py / the export layer. Flat and renderer-agnostic so
    the Friction_Breaker export functions can consume it without new branching."""
    return {
        "candidate_id": os_.candidate_id,
        "candidate_name": os_.candidate_name,
        "cycle": os_.cycle,
        "support_total": os_.support_total,
        "oppose_total": os_.oppose_total,
        "record_count": os_.record_count,
        "reported_count": os_.reported_count,
        "incomplete": os_.incomplete,
        "incomplete_reason": os_.incomplete_reason,
        "dedup": os_.dedup,
        "transactions": os_.transactions,   # per-row deduped IEs (newest-first)
        "spenders": [{
            "committee_id": s.committee_id,
            "committee_name": s.committee_name,
            "committee_type": s.committee_type,
            "committee_type_full": s.committee_type_full,
            "support_total": round(s.support_total, 2),
            "oppose_total": round(s.oppose_total, 2),
            "count": s.count,
            "first_date": s.first_date,
            "last_date": s.last_date,
            "traceable": s.traceable,
            "notice_only_total": round(s.notice_only_total, 2),
        } for s in os_.spenders],
    }


# ════════════════════════════════════════════════════════════════════════════
# PER-CYCLE FUNDING COMPOSITION (Track A for the overlay)
# ════════════════════════════════════════════════════════════════════════════
#
# correlate.py needs, per period, how RELIANT the candidate is on the
# "criticized" kind of money — big donors + outside spending — vs small-dollar.
# Campaign finance is reported in 2-YEAR CYCLES, and the small-dollar number
# (unitemized, <=$200 aggregate) only exists at cycle granularity on the /totals
# endpoint — it is NOT in Schedule A line items (sub-$200 gifts aren't itemized).
# So the overlay runs on cycles, which is the correct unit anyway.
#
# Per cycle we assemble:
#   Pipe 1 composition (from /candidate/{id}/totals/):
#     large_share = itemized / (itemized + unitemized individual contributions)
#     small_share = unitemized / (same)            → the small-dollar share
#   Pipe 2 (reusing outside_spending_for_candidate): support/oppose, split into
#     traceable (super-PAC, donors disclosed) vs dark (opaque) dollars.


def cycle_of(year: int) -> int:
    """Map any calendar year to its FEC two-year cycle (the even end-year):
    2025 → 2026, 2024 → 2024. Used to align per-year rhetoric to per-cycle
    funding so the two tracks join on the same key."""
    return year if year % 2 == 0 else year + 1


@dataclass
class FundingCycle:
    cycle: int
    receipts: float = 0.0
    individual_itemized: float = 0.0
    individual_unitemized: float = 0.0
    pac_contributions: float = 0.0
    large_share: float = 0.0        # itemized / (itemized + unitemized)
    small_share: float = 0.0        # unitemized / (itemized + unitemized)
    outside_support: float = 0.0
    outside_oppose: float = 0.0
    # traceable/dark now cover BOTH support and oppose (was support-only, a bug
    # that hid all opposition dark money). None means "not computed for this
    # cycle" — we only derive the split from full per-spender detail, which we
    # pull for the current cycle; prior cycles take authoritative support/oppose
    # totals from FEC's aggregate, which doesn't expose a per-spender split.
    outside_traceable: Optional[float] = None
    outside_dark: Optional[float] = None
    outside_totals_source: str = ""  # "spenders" (current, row-level) | "fec_aggregate" (prior)
    incomplete: bool = False        # True if the Schedule E side was a lower bound
    incomplete_reason: str = ""


def candidate_totals(candidate_id: str, api_key: str, cycle: int) -> dict:
    """One cycle of a candidate's aggregate totals from /candidate/{id}/totals/.
    Returns the matching result dict, or {} if the candidate has no totals for
    that cycle (a real, non-error state — e.g. a cycle they didn't run in).
    Raises FECAPIError only on transport/HTTP failure, never on 'no data'."""
    params = {"api_key": api_key, "cycle": str(cycle), "per_page": "100"}
    url = f"{FEC_BASE}/candidate/{candidate_id}/totals/?{urllib.parse.urlencode(params)}"
    code, body = _http_get_with_retry(url)
    if code == -1:
        raise FECAPIError(f"Could not reach FEC candidate totals for {candidate_id} "
                          f"(connection-level failure): {body}", status_code=-1)
    if code != 200:
        raise FECAPIError(f"FEC candidate totals returned HTTP {code} for "
                          f"{candidate_id}: {body[:300]!r}", status_code=code)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise FECAPIError(f"FEC candidate totals returned non-JSON: {body[:300]!r}") from exc
    # results may carry several rows (e.g. per election period); prefer the one
    # whose cycle matches, else take the first available.
    results = data.get("results", [])
    for r in results:
        if r.get("cycle") == cycle:
            return r
    return results[0] if results else {}


def outside_totals_aggregate(candidate_id: str, api_key: str, cycle: int) -> dict:
    """Authoritative per-cycle Schedule E support/oppose totals straight from
    FEC's own /schedules/schedule_e/totals/by_candidate/ aggregate.

    Why this exists: summing raw Schedule E rows ourselves and deduping by
    (committee, date, amount) is reliable on the small, clean current cycle
    (verified to the cent), but it does NOT scale to high-volume historical
    cycles — a 24/48-hour notice's *estimated* amount/date rarely matches its
    final regular report exactly, so brittle key-matching leaves both rows in
    and over-counts. On Ossoff's 2020 (Georgia-runoff) cycle that inflated
    oppose spending by ~$9.5M and support by ~$11M versus this endpoint, and
    mislabeled the surplus as "notice-only" — impossible for a closed cycle.
    FEC's aggregate is deduped by the filers' own transaction lineage, so it's
    the ground truth. We use it for prior cycles (and as the reconciliation
    target for the current one).

    `election_full=false` scopes the total to the single two-year cycle, which
    matches the `cycle=Y` row-pull semantics used everywhere else in this file
    (verified: 2026 aggregate = our deduped rows minus notice-only, to the cent).

    Returns {"support": float, "oppose": float}. Absent side => 0.0. Raises
    FECAPIError only on transport/HTTP/parse failure, never on 'no data'.
    """
    params = {"api_key": api_key, "candidate_id": candidate_id,
              "cycle": str(cycle), "election_full": "false", "per_page": "20"}
    url = f"{FEC_BASE}/schedules/schedule_e/totals/by_candidate/?{urllib.parse.urlencode(params)}"
    code, body = _http_get_with_retry(url)
    if code == -1:
        raise FECAPIError(f"Could not reach FEC Schedule E aggregate for {candidate_id} "
                          f"(connection-level failure): {body}", status_code=-1)
    if code != 200:
        raise FECAPIError(f"FEC Schedule E aggregate returned HTTP {code} for "
                          f"{candidate_id}: {body[:300]!r}", status_code=code)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise FECAPIError(f"FEC Schedule E aggregate returned non-JSON: {body[:300]!r}") from exc
    out = {"support": 0.0, "oppose": 0.0}
    for r in data.get("results", []):
        so = (r.get("support_oppose_indicator") or "").strip().upper()
        if so == "S":
            out["support"] = round(_to_float(r.get("total")), 2)
        elif so == "O":
            out["oppose"] = round(_to_float(r.get("total")), 2)
    return out


def funding_by_cycle(candidate_id: str, api_key: str, cycles: list[int],
                     include_outside: bool = True,
                     current_cycle: Optional[int] = None,
                     current_outside: Optional["OutsideSpending"] = None,
                     progress_cb: ProgressCallback = None) -> list[FundingCycle]:
    """Build the per-cycle Track-A composition the overlay consumes.

    For each cycle: pull the candidate's aggregate totals for the Pipe 1
    itemized/unitemized split, and (optionally) the Schedule E outside spending
    for Pipe 2. Cycles with no totals are skipped (not faked as zero), so the
    series reflects only cycles the candidate was actually active in.

    Outside-spending totals come from two sources by design:
      * Current cycle (== current_cycle, if `current_outside` is supplied):
        reuse the full per-spender OutsideSpending we already pulled in the
        sched_e step — no second pull — so composition matches the "by group"
        chart to the cent, INCLUDING disclosed notice-only dollars. The
        traceable/dark split is derived here from per-spender detail and now
        covers support AND oppose.
      * Prior cycles: take support/oppose from FEC's own by-candidate aggregate
        (`outside_totals_aggregate`) — authoritative, FEC-deduped, one call
        instead of paginating tens of thousands of rows. This both fixes the
        historical over-count (our row-level dedup doesn't scale to old
        high-volume filings) and removes the dominant runtime cost. No
        per-spender detail is available there, so traceable/dark stays None.

    If `current_outside` isn't supplied (e.g. standalone/CLI use), every cycle
    falls back to the aggregate: support/oppose stay authoritative, and the
    traceable/dark split is simply omitted everywhere rather than computed wrong.
    """
    out: list[FundingCycle] = []
    cyc_list = sorted(set(cycles))
    for i, cyc in enumerate(cyc_list):
        if progress_cb:
            progress_cb(i + 1, len(cyc_list))
        totals = candidate_totals(candidate_id, api_key, cyc)
        if not totals:
            continue  # candidate not active this cycle — omit rather than zero-fill

        itemized = _to_float(totals.get("individual_itemized_contributions"))
        unitemized = _to_float(totals.get("individual_unitemized_contributions"))
        pac = _to_float(totals.get("other_political_committee_contributions"))
        indiv = itemized + unitemized
        large_share = round(itemized / indiv, 4) if indiv > 0 else 0.0
        small_share = round(unitemized / indiv, 4) if indiv > 0 else 0.0

        fc = FundingCycle(
            cycle=cyc,
            receipts=_to_float(totals.get("receipts")),
            individual_itemized=itemized,
            individual_unitemized=unitemized,
            pac_contributions=pac,
            large_share=large_share,
            small_share=small_share,
        )

        if include_outside:
            if current_outside is not None and cyc == current_cycle:
                # Reuse the already-pulled current-cycle detail. Deduped totals
                # include disclosed notice-only dollars, matching the sched_e
                # step and the "by group" chart exactly.
                fc.outside_support = current_outside.support_total
                fc.outside_oppose = current_outside.oppose_total
                # traceable/dark over BOTH sides (was support-only).
                fc.outside_traceable = round(sum(
                    s.support_total + s.oppose_total
                    for s in current_outside.spenders if s.traceable), 2)
                fc.outside_dark = round(sum(
                    s.support_total + s.oppose_total
                    for s in current_outside.spenders if not s.traceable), 2)
                fc.outside_totals_source = "spenders"
                fc.incomplete = current_outside.incomplete
                fc.incomplete_reason = current_outside.incomplete_reason
            else:
                # Prior cycle (or no current detail supplied): authoritative
                # FEC aggregate. No per-spender split available.
                agg = outside_totals_aggregate(candidate_id, api_key, cyc)
                fc.outside_support = agg["support"]
                fc.outside_oppose = agg["oppose"]
                fc.outside_traceable = None
                fc.outside_dark = None
                fc.outside_totals_source = "fec_aggregate"
                # The aggregate is a single authoritative figure — no pagination
                # lower-bound risk, so it's never marked incomplete.

        out.append(fc)
    return out


def funding_cycle_to_jsonable(fc: FundingCycle) -> dict:
    return {
        "cycle": fc.cycle,
        "receipts": fc.receipts,
        "individual_itemized": fc.individual_itemized,
        "individual_unitemized": fc.individual_unitemized,
        "pac_contributions": fc.pac_contributions,
        "large_share": fc.large_share,
        "small_share": fc.small_share,
        "outside_support": fc.outside_support,
        "outside_oppose": fc.outside_oppose,
        "outside_traceable": fc.outside_traceable,   # null for prior cycles (not computed)
        "outside_dark": fc.outside_dark,             # null for prior cycles (not computed)
        "outside_totals_source": fc.outside_totals_source,
        "incomplete": fc.incomplete,
        "incomplete_reason": fc.incomplete_reason,
    }


if __name__ == "__main__":
    import os
    import sys
    key = os.environ.get("FEC_API_KEY")
    if not key:
        print("Set FEC_API_KEY in your environment first (it's a Codespaces secret —")
        print("if you just added it, stop and restart the Codespace for it to appear).")
        sys.exit(1)
    name = sys.argv[1] if len(sys.argv) > 1 else "Talarico"

    start = time.time()
    def _progress(page, total):
        elapsed = time.time() - start
        if total:
            print(f"\rPage {page}/{total} ({elapsed:.0f}s elapsed)", end="", file=sys.stderr)
        else:
            print(f"\rPage {page} ({elapsed:.0f}s elapsed)", end="", file=sys.stderr)

    groups = []
    try:
        groups = search_fec_candidate(name, key, progress_cb=_progress)
    except FECAPIError as exc:
        print(file=sys.stderr)
        print(f"FEC API error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(file=sys.stderr)  # newline after the progress line
    if not groups:
        print("No matching candidate found, or the candidate has no committee "
              "with itemized contributions in this cycle.", file=sys.stderr)
        sys.exit(0)
    for g in groups:
        if g.truncated:
            print(f"WARNING: data for {g.recipient_name!r} is INCOMPLETE. "
                  f"total_raised (${g.total_raised:,.2f}) is a lower bound, not the real total.",
                  file=sys.stderr)
            print(f"  Reason: {g.truncated_reason}", file=sys.stderr)
    print(json.dumps(to_jsonable(groups), indent=2)[:4000])

"""
statements.py — Track B data gathering: collect what a candidate SAID.

PURE FACTUAL COLLECTION. No scoring, no intensity, no framing, no judgment. This
module returns the actual statements — an excerpt, a date, a source, a URL — and
nothing else. The interpretive/"rhetoric" pass happens LATER, at the end, once
money + statements + bills are all assembled, and reads the whole picture for
nuance. Keeping collection dumb and factual is deliberate: collection bias and
interpretation bias can't hide inside each other if they're separate steps.

WORKS FOR ANYONE. It's web-search-backed, so it covers challengers, outsiders,
and sitting members alike — not just people who already have a public record.

BACKEND is pluggable (no paid search, ever):
  - SearXNG (self-hosted; set SEARXNG_URL) — the free, no-gatekeeper default. You
    run it; the tool just points at it.
  - Offline fixtures (no URL) — clearly labeled, so building and testing run with
    zero setup and zero network.
The `_search` seam is provider-shaped, so a free Tavily/Firecrawl tier could drop
in later without touching the rest.

REPRESENTATIVENESS GUARD (integrity, not optional). The collector pulls BROADLY —
several neutral angles on the candidate's money/corruption talk — and returns the
FULL dated set with a coverage report. It does NOT pre-filter to the damning
quotes; cherry-picking the sample is how you manufacture a verdict. Everything
found is kept; the synthesis layer and the report see all of it.

HONESTY ABOUT WHAT A RESULT IS. A search result gives an *excerpt* from a source
page, not a guaranteed verbatim quote, and web results often lack a reliable
date. So each item carries its source URL for verification, the excerpt is
labeled as such, and undated items are surfaced in the coverage report rather
than silently dropped. Precise-quote extraction (fetching the page) is the clean
next step; this layer gathers the sourced, dated leads.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
import datetime
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

# A publication date can't be in the future. Extracted years above this are
# rejected (that's what produced the phantom "2028" statement — a year lifted
# from a future-speculation headline, not a real publish date).
_CURRENT_YEAR = datetime.date.today().year

# Neutral, multi-angle queries. Topic-scoped (this IS the money/influence record
# we're laying against the funding) but NOT slanted — no "hypocrisy", no
# "scandal". We capture what they said across angles and keep all of it, so a
# consistent reformer and a consistent hypocrite both show up truthfully.
_QUERY_ANGLES = [
    'campaign finance', 'money in politics', 'dark money', 'corporate PAC',
    'special interests', 'lobbying', 'campaign donors', 'Citizens United',
    'corruption', 'getting money out of politics',
]


@dataclass
class CollectedStatement:
    id: str
    excerpt: str            # the source snippet — an excerpt, NOT a verified verbatim quote
    date: str               # ISO YYYY-MM-DD when a real date was found, else "" (never a placeholder)
    dated: bool             # True ONLY for a trustworthy full date; False = year-only or absent
    source: str             # domain of the source
    url: str
    query: str              # which angle surfaced it (transparency)
    year_hint: str = ""     # a bare year (YYYY) when that's all we have — kept SEPARATE from `date`
                            # so a year-only guess is never mistaken for a real Jan-1 timestamp
    date_source: str = ""   # where a real date came from: "search_engine" | "url_slug"
                            # | "page_metadata" | "" (undated) — provenance, disclosed
    classify: str = ""      # by_candidate | about_candidate | boilerplate — see classify_statement
    classify_reason: str = ""   # short tag for WHY, so every classification is auditable


@dataclass
class StatementSet:
    candidate: str
    statements: list = field(default_factory=list)   # list[CollectedStatement]
    backend: str = "fixtures"                          # "searxng" | "fixtures"
    coverage: dict = field(default_factory=dict)
    # Raw, unfiltered record of what the search engine actually returned — see
    # the note on _RESULT_DISPOSITIONS below. Empty for the offline backend.
    search_log: list = field(default_factory=list)
    searched_at: str = ""                              # ISO-8601 UTC, run start


def collect_statements(candidate: str, searxng_url: Optional[str] = None,
                       per_query: int = 8, office: Optional[str] = None) -> StatementSet:
    """Gather a candidate's public statements on money/influence. Returns a
    StatementSet (statements + coverage report). Factual only — no scoring.

    `office` ('S'/'H'/…) lets the classifier derive this candidate's official
    domain and name cues (see build_candidate_context) so 'their own voice' is
    detected for anyone, not just the hardcoded default.

    searxng_url: base URL of a SearXNG instance (or SEARXNG_URL env). If absent,
    returns labeled offline fixtures so the pipeline still runs."""
    base = (searxng_url or os.getenv("SEARXNG_URL", "")).strip()
    if not base:
        return _offline(candidate)

    ctx = build_candidate_context(candidate, office)
    seen_urls: set[str] = set()
    seen_text: list[str] = []
    statements: list[CollectedStatement] = []
    search_log: list[dict] = []
    searched_at = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    sid = 0
    for angle in _QUERY_ANGLES:
        query = f'"{candidate}" {angle}'
        try:
            results = _searxng_search(query, base, per_query)
        except Exception as e:   # noqa: BLE001 — one failed angle shouldn't kill the run
            # A failed angle is itself part of the audit trail: without this the
            # log would imply the query returned nothing, which is a different
            # claim from "the query never completed".
            search_log.append({
                "angle": angle, "query": query, "rank": 0, "url": "",
                "title": "", "excerpt": "", "published_date": "", "engine": "",
                "disposition": "query_failed", "statement_id": "",
                "detail": str(e)[:200],
            })
            continue
        for rank, r in enumerate(results, start=1):
            url = (r.get("url") or "").strip()
            excerpt = (r.get("content") or r.get("title") or "").strip()
            entry = {
                "angle": angle, "query": query, "rank": rank, "url": url,
                "title": (r.get("title") or "").strip(), "excerpt": excerpt,
                "published_date": (r.get("publishedDate") or "").strip(),
                "engine": str(r.get("engine") or ""),
                "disposition": "", "statement_id": "", "detail": "",
            }
            search_log.append(entry)
            if not url:
                entry["disposition"] = "dropped_no_url"
                continue
            if len(excerpt) < 40:
                entry["disposition"] = "dropped_excerpt_too_short"
                continue
            if url in seen_urls:
                entry["disposition"] = "dropped_duplicate_url"
                continue
            if _near_dup(excerpt, seen_text):
                entry["disposition"] = "dropped_near_duplicate_text"
                continue
            seen_urls.add(url)
            seen_text.append(excerpt)
            date, dated, year_hint = _extract_date(r)
            src_kind = ""
            if dated:
                src_kind = "search_engine" if (result_pd := (r.get("publishedDate") or "").strip()) and date in result_pd else "url_slug"
            sid += 1
            cls, reason = classify_statement(excerpt, _domain(url), ctx)
            statements.append(CollectedStatement(
                id=f"s{sid}", excerpt=excerpt, date=date, dated=dated,
                source=_domain(url), url=url, query=angle, year_hint=year_hint,
                date_source=src_kind, classify=cls, classify_reason=reason,
            ))
            entry["disposition"] = "kept"
            entry["statement_id"] = f"s{sid}"
            entry["detail"] = f"classified {cls} ({reason})"

    # Second dating tier: read publication dates from the pages themselves for
    # statements the search engine couldn't date. Capped + concurrent + non-fatal.
    attempted, dated_via_page = _page_fetch_pass(statements)

    s = StatementSet(candidate=candidate, statements=statements,
                     backend="searxng", search_log=search_log,
                     searched_at=searched_at)
    s.coverage = _coverage(statements, ctx)
    s.coverage["dated_via_page"] = dated_via_page
    s.coverage["page_fetch_attempted"] = attempted
    s.coverage["search_results_seen"] = sum(
        1 for e in search_log if e["disposition"] != "query_failed")
    s.coverage["search_queries_failed"] = sum(
        1 for e in search_log if e["disposition"] == "query_failed")
    return s


# -- SearXNG backend ----------------------------------------------------------
def _searxng_search(query: str, base_url: str, count: int) -> list[dict]:
    """Query a SearXNG instance for JSON results. (Enable `json` in the instance's
    settings.yml `search.formats` -- it's HTML-only by default.)"""
    params = {"q": query, "format": "json"}
    url = base_url.rstrip("/") + "/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "CampaignTracker/1.0 (research)",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("results") or [])[:count]


# -- helpers ------------------------------------------------------------------
def _near_dup(text: str, seen: list[str], threshold: float = 0.85) -> bool:
    for prev in seen:
        if SequenceMatcher(None, text, prev).ratio() >= threshold:
            return True
    return False


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except ValueError:
        return ""


def _extract_date(result: dict) -> tuple[str, bool, str]:
    """Best available date. Returns (iso_date, is_real, year_hint).

      - iso_date  : a full YYYY-MM-DD when we have a trustworthy one, else "".
                    NEVER a placeholder — a year-only guess does not go here.
      - is_real   : True only for a full date from `publishedDate` or a URL slug.
      - year_hint : a bare year (YYYY) when that's all we could find, kept SEPARATE
                    from iso_date so it can't be mistaken for a real Jan-1 date on
                    the timeline. "" when no year at all.

    Sources, most reliable first:
      1. `publishedDate` full date — the engine's own per-result date (general web
         search, unlike the `news` category, often lacks it — that's the main
         reason so many come back undated; a real gap in what search returns, not
         a parse bug).
      2. A full YYYY-MM-DD in the URL path — a deliberate permalink convention.
      3. Year only (last resort) — from the URL path first (a publishing
         convention), then `publishedDate`, then the title. Returned as a HINT,
         not a date.

    Future years are rejected everywhere (a publish date can't be later than this
    year); that's what kills the phantom "2028" lifted from a speculation headline.
    """
    def _ok(y: int) -> bool:
        return 1990 <= y <= _CURRENT_YEAR

    def _full(s: str) -> Optional[str]:
        m = re.search(r"((?:19|20)\d{2})[-/](\d{2})[-/](\d{2})", s or "")
        if m and _ok(int(m.group(1))) and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None

    pd = (result.get("publishedDate") or "").strip()
    url = result.get("url", "") or ""
    title = result.get("title", "") or ""

    real = _full(pd) or _full(url)
    if real:
        return real, True, real[:4]

    # Year-only fallbacks — hint only, prefer URL path, reject future years.
    for src in (url, pd, title):
        m = re.search(r"\b((?:19|20)\d{2})\b", src)
        if m and _ok(int(m.group(1))):
            return "", False, m.group(1)
    return "", False, ""


# -- page-fetch dating tier ----------------------------------------------------
# The primary lever on the undated rate. General web search rarely returns a
# publishedDate, but the PAGES themselves usually declare one in standard
# metadata. For statements still undated after _extract_date, fetch the page
# (capped, concurrent, short timeout, non-fatal) and read the publication date
# from, in order of preference:
#   1. <meta property="article:published_time" content="...">  (Open Graph news)
#   2. JSON-LD  "datePublished": "..."   (schema.org — also YouTube's uploadDate)
#   3. <meta name="date|pubdate|publish[-_]date|dc.date..." content="...">
#   4. <time datetime="...">
# "Modified/updated" variants are deliberately NOT used — we want first
# publication, not the last CMS touch. Domains that never yield to a plain GET
# (JS/login walls) are skipped instead of wasting the fetch budget.

# Domains skipped by the page-fetch tier: login/JS walls where a plain GET never
# yields a date, so fetching only wastes the budget. YouTube is deliberately NOT
# here — its watch pages expose a clean uploadDate in metadata, so we fetch it.
_FETCH_SKIP_DOMAINS = {
    "facebook.com", "m.facebook.com", "instagram.com", "x.com", "twitter.com",
    "threads.net", "tiktok.com", "linkedin.com",
}
_FETCH_MAX_PAGES = 40          # budget per run. Sized so ALL eligible undated
                               # statements get attempted at current scale (~48
                               # statements/run, ~30 eligible after the skip
                               # list) — Run 5 showed 25 was clipping the queue.
                               # 8 workers × 6s timeout keeps worst case bounded.
_FETCH_TIMEOUT = 6             # seconds per page; a slow page is a skipped page
_FETCH_MAX_BYTES = 262_144     # metadata lives in <head>; 256 KB is plenty
_FETCH_WORKERS = 8

_META_PUBLISHED_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:article:published_time|'
    r'og:article:published_time|parsely-pub-date|sailthru\.date)["\'][^>]*'
    r'content\s*=\s*["\']([^"\']+)["\']', re.I)
_META_PUBLISHED_RE2 = re.compile(  # content= before property= (attribute order varies)
    r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*'
    r'["\'](?:article:published_time|og:article:published_time|parsely-pub-date|'
    r'sailthru\.date)["\']', re.I)
_JSONLD_DATE_RE = re.compile(
    r'"(?:datePublished|uploadDate|publishDate)"\s*:\s*"([^"]+)"', re.I)
# itemprop meta (schema.org microdata) — YouTube and some CMSes use this instead
# of name=/property=. Handles either attribute order.
_META_ITEMPROP_DATE_RE = re.compile(
    r'<meta[^>]+itemprop\s*=\s*["\'](?:datePublished|uploadDate)["\'][^>]*'
    r'content\s*=\s*["\']([^"\']+)["\']', re.I)
_META_ITEMPROP_DATE_RE2 = re.compile(
    r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*'
    r'itemprop\s*=\s*["\'](?:datePublished|uploadDate)["\']', re.I)
_META_NAME_DATE_RE = re.compile(
    r'<meta[^>]+name\s*=\s*["\'](?:date|pubdate|publishdate|publish[-_]date|'
    r'dc\.date(?:\.issued)?|article\.published)["\'][^>]*'
    r'content\s*=\s*["\']([^"\']+)["\']', re.I)
_TIME_TAG_RE = re.compile(r'<time[^>]+datetime\s*=\s*["\']([^"\']+)["\']', re.I)
# canonical / og:url — the page's own declared "real" URL, for a single safe hop
# when a stub/aggregator page carries no date but its canonical target does.
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel\s*=\s*["\']canonical["\'][^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)
_OG_URL_RE = re.compile(
    r'<meta[^>]+property\s*=\s*["\']og:url["\'][^>]*content\s*=\s*["\']([^"\']+)["\']', re.I)
# WordPress/Elementor microdata pattern (e.g. ossoff.senate.gov): a bare
# human-readable <time>January 22, 2026</time> tied to itemprop="datePublished".
# Only read textual dates when anchored to a datePublished/posted-on marker —
# grabbing free-floating dates from body text would date articles by their
# CONTENT (exactly the class of error the future-year guard exists to prevent).
_MICRODATA_PUBLISHED_RE = re.compile(
    r'itemprop\s*=\s*["\']datePublished["\'][\s\S]{0,400}?<time[^>]*>([^<]{4,40})</time>',
    re.I)
_POSTED_ON_RE = re.compile(
    r'class\s*=\s*["\'][^"\']*(?:posted-on|entry-date|published)[^"\']*["\'][^>]*>'
    r'\s*(?:<[^>]+>\s*)*([A-Z][a-z]+ \d{1,2},? \d{4})', re.I)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _iso_from_textual(raw: str) -> Optional[str]:
    """'January 22, 2026' / 'Jan 22 2026' → '2026-01-22', same guards as _iso_from."""
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b", raw or "")
    if not m:
        return None
    mon = next((v for k, v in _MONTHS.items() if k.startswith(m.group(1).lower())), None)
    day, year = int(m.group(2)), int(m.group(3))
    if mon and 1 <= day <= 31 and 1990 <= year <= _CURRENT_YEAR:
        return f"{year}-{mon:02d}-{day:02d}"
    return None


def _iso_from(raw: str) -> Optional[str]:
    """Normalize a metadata date string to YYYY-MM-DD, with the same sanity and
    future-year guards as _extract_date. Rejects garbage rather than guessing."""
    m = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", raw or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1990 <= y <= _CURRENT_YEAR and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _parse_page_date(html: str) -> Optional[str]:
    """Pure parser: best publication date declared in a page's metadata, or None.
    Separated from the network so it's testable offline against fixture HTML."""
    for pattern in (_META_PUBLISHED_RE, _META_PUBLISHED_RE2, _JSONLD_DATE_RE,
                    _META_ITEMPROP_DATE_RE, _META_ITEMPROP_DATE_RE2,
                    _META_NAME_DATE_RE, _TIME_TAG_RE):
        for raw in pattern.findall(html or ""):
            iso = _iso_from(raw)
            if iso:
                return iso
    # Textual dates, only when anchored to an explicit published marker.
    for pattern in (_MICRODATA_PUBLISHED_RE, _POSTED_ON_RE):
        for raw in pattern.findall(html or ""):
            iso = _iso_from(raw) or _iso_from_textual(raw)
            if iso:
                return iso
    return None


# Browser-TLS impersonation for the page-fetch tier, same pattern as
# congress.py: many news CMSes 403 plain urllib on TLS fingerprint alone. If
# curl_cffi is installed (it already is, for Congress.gov) we use it; otherwise
# fall back to urllib and accept a lower hit rate. Either way: non-fatal.
try:
    from curl_cffi import requests as _curl   # type: ignore
    _HAVE_CURL = True
except Exception:   # noqa: BLE001
    _HAVE_CURL = False

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# SSRF guard. Page URLs here come from two attacker-reachable sources: raw
# SearXNG results (arbitrary indexed web pages) and a fetched page's OWN
# canonical/og:url (fully attacker-controlled HTML). Neither transport's
# default redirect-following can be trusted — a hostname check alone is
# bypassable by a 3xx to a private target — so every URL we're about to
# request, including each redirect hop, is resolved and checked here BEFORE
# the socket opens. Blocks loopback/private/link-local/multicast/reserved
# ranges (this covers the 169.254.169.254 cloud-metadata address) and
# IPv4-mapped-in-IPv6 tricks. Fails closed: anything unresolvable or
# ambiguous is treated as unsafe.
_MAX_REDIRECTS = 3


def _is_safe_url(url: str) -> bool:
    """True only for an http(s) URL whose host resolves EXCLUSIVELY to public,
    non-internal IPs. Used before every request/redirect hop in _http_get."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:   # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:   # noqa: BLE001 — unresolvable host is not safe to fetch
        return False
    if not infos:
        return False
    for info in infos:
        raw_ip = info[4][0].split("%")[0]   # strip IPv6 zone id
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        candidates = [ip]
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            candidates.append(mapped)
        for c in candidates:
            if (c.is_private or c.is_loopback or c.is_link_local or
                    c.is_multicast or c.is_reserved or c.is_unspecified):
                return False
    return True


class _RedirectBlocked(Exception):
    """Raised by _CapturingRedirectHandler to stop urllib from auto-following a
    redirect; the target is validated by the caller before it's ever fetched."""
    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


class _CapturingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D102
        raise _RedirectBlocked(newurl)


def _http_get_once(url: str) -> tuple[Optional[str], Optional[str]]:
    """One request, redirects NOT followed automatically. Returns
    (html_or_None, redirect_target_or_None) — exactly one is non-None on a
    handled response; both None on any failure."""
    try:
        if _HAVE_CURL:
            resp = _curl.get(url, impersonate="chrome", timeout=_FETCH_TIMEOUT,
                             headers={"Accept": "text/html"}, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                return None, urllib.parse.urljoin(url, loc) if loc else None
            if resp.status_code != 200:
                return None, None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                return None, None
            return resp.text[:_FETCH_MAX_BYTES], None
        req = urllib.request.Request(url, headers={
            "Accept": "text/html", "User-Agent": _BROWSER_UA})
        opener = urllib.request.build_opener(_CapturingRedirectHandler)
        with opener.open(req, timeout=_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "")
            if "html" not in ctype and "xml" not in ctype:
                return None, None
            return resp.read(_FETCH_MAX_BYTES).decode("utf-8", errors="replace"), None
    except _RedirectBlocked as e:
        return None, e.url
    except Exception:   # noqa: BLE001 — dating is best-effort, never fatal
        return None, None


def _http_get(url: str) -> Optional[str]:
    """GET one page, bounded read + short timeout, HTML only, following up to
    _MAX_REDIRECTS hops — each hop (including the first) re-validated against
    _is_safe_url before it is requested, so a 3xx into the operator's internal
    network is refused rather than silently followed. Never raises."""
    for _ in range(_MAX_REDIRECTS + 1):
        if not _is_safe_url(url):
            return None
        html, redirect_to = _http_get_once(url)
        if html is not None:
            return html
        if redirect_to is None:
            return None
        url = redirect_to
    return None


def _canonical_target(html: str, url: str) -> Optional[str]:
    """A single, safe redirect hop: the page's own declared canonical/og:url,
    when it differs from the URL we fetched. Handles stub/aggregator/AMP pages
    whose date lives on the canonical version. Returns None if same or absent.
    "Safe" here means dating-provenance-safe (the page's own declared target);
    network safety is enforced separately, in _http_get, when that target is
    actually fetched."""
    for pat in (_CANONICAL_RE, _OG_URL_RE):
        m = pat.search(html or "")
        if m:
            target = m.group(1).strip()
            if target.startswith("http") and target.split("#")[0] != url.split("#")[0]:
                return target
    return None


def _fetch_page_date(url: str) -> Optional[str]:
    """Fetch a page and parse its published date. If the page carries no date but
    declares a different canonical/og:url, follow that ONE hop and try again —
    bounded, non-guessing (we're reading the page's own declared target, not
    searching for a date). Any failure returns None; never raises."""
    html = _http_get(url)
    if not html:
        return None
    iso = _parse_page_date(html)
    if iso:
        return iso
    target = _canonical_target(html, url)
    if target:
        return _parse_page_date(_http_get(target) or "")
    return None


def _page_fetch_pass(statements: list[CollectedStatement]) -> tuple[int, int]:
    """Second dating tier: for undated statements, fetch pages concurrently and
    fill dates from page metadata. Mutates in place; returns (attempted, dated)
    so coverage can disclose whether the budget clipped the queue. Budgeted
    (_FETCH_MAX_PAGES) and skips domains that never yield."""
    from concurrent.futures import ThreadPoolExecutor

    todo = [s for s in statements
            if not s.dated and s.url.startswith("http")
            and s.source not in _FETCH_SKIP_DOMAINS][:_FETCH_MAX_PAGES]
    if not todo:
        return 0, 0
    dated = 0
    with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(todo))) as pool:
        for s, iso in zip(todo, pool.map(lambda t: _fetch_page_date(t.url), todo)):
            if iso:
                s.date, s.dated, s.year_hint = iso, True, iso[:4]
                s.date_source = "page_metadata"
                dated += 1
    return len(todo), dated


# -- statement classification --------------------------------------------------
# Split gathered statements into three buckets so the timeline can show the
# candidate's OWN voice and nothing else — WITHOUT deleting anything. Every
# statement keeps its class and a short reason, so a miscall is visible and easy
# to correct. The rhetoric-vs-funding comparison is only meaningful on his own
# words; coverage and boilerplate stay in the JSON, labeled, off the timeline.
#
#   by_candidate    — the candidate expressing a position/claim on money/influence:
#                     his own outlets (ossoff.senate.gov, electjon.com press/video),
#                     or reported rhetoric ("Ossoff urged/introduced/argued/decried…").
#   about_candidate — third-party facts or framing ABOUT him, not his words
#                     ("raised $X", "entering 2026 with an edge", opponents' claims,
#                     fact-checks, encyclopedic entries).
#   boilerplate     — not a statement at all: donation CTAs, contact pages, raw
#                     committee/data-dump pages.
#
# Heuristic and deliberately conservative about what counts as "his voice." Tune
# the patterns as real miscategorizations surface — that's what the reason tag is for.

_OFFICIAL_DOMAINS = ("ossoff.senate.gov",)  # official office — press releases ARE his voice
_CAMPAIGN_DOMAINS = ("electjon.com",)       # campaign site — ALSO hosts reposted coverage,
                                            # fundraising, and CTAs, so it needs a rhetoric cue
                                            # to count as his voice (see classify_statement)
_DATA_DOMAINS = ("fec.gov", "opensecrets.org", "secure.actblue.com",
                 # third-party trackers: profile/aggregation pages, never the
                 # candidate's own voice
                 "quiverquant.com", "followthemoney.org", "ballotpedia.org",
                 "votesmart.org", "legistorm.com")  # raw data / CTAs

_BOILERPLATE_RE = re.compile(
    r"\b(donate now|donate today|chip in|contribute (?:now|today)|paid for by|"
    r"contact information|for official senate purposes|see the details|"
    r"committee data|privacy policy|unsubscribe|actblue)\b", re.I)

# Candidate as the actor MAKING a claim / taking a position (his rhetoric),
# direct or reported. Fact-about verbs (raised, entering, holds, received,
# reported) are excluded — those describe him. Characterization verbs
# (centered/presents) were ALSO removed: "has centered his campaign on X" is a
# news outlet's framing, not his own words, and it was pulling coverage into the
# his-voice bucket.
# Verb set shared by the default regex and the per-candidate one.
_RHETORIC_VERBS = (
    r"said|says|argu(?:ed|es)|urg(?:ed|es)|call(?:ed|s)? for|decr(?:ied|ies)|"
    r"introduc(?:ed|es)|reintroduc(?:ed|es)|announc(?:ed|es)|vow(?:ed|s)|"
    r"pledg(?:ed|es)|demand(?:ed|s)|criticiz(?:ed|es)|accus(?:ed|es)|warn(?:ed|s)|"
    r"wrote|deliver(?:ed|s)|releas(?:ed|es)|propos(?:ed|es)|push(?:ed|es) for")

# Default actor — the ORIGINAL Ossoff-or-"he" behavior, kept exactly so the
# ctx=None path (standalone use / existing tests) is unchanged. Live runs pass a
# per-candidate ctx from build_candidate_context and never hit this.
_RHETORIC_RE = re.compile(
    r"\b(?:ossoff|he)\b[^.]{0,70}?\b(?:" + _RHETORIC_VERBS + r")\b", re.I)


def _split_person_name(candidate_name: str) -> tuple[str, str]:
    """(first, last) from either 'LAST, FIRST …' (FEC) or 'First Last' (typed)."""
    n = (candidate_name or "").strip()
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    if "," in n:
        last, _, rest = n.partition(",")
        first = (rest.strip().split() or [""])[0]
        return first.strip(), last.strip()
    toks = n.split()
    if not toks:
        return "", ""
    core = [t for t in toks if re.sub(r"[^a-z]", "", t.lower()) not in suffixes]
    last = core[-1] if len(core) > 1 else toks[-1]
    return toks[0], last


def build_candidate_context(candidate_name: str, office: Optional[str] = None) -> dict:
    """Per-candidate classifier signals from the resolved name + chamber — NO
    per-candidate config to maintain. The official domain follows the fixed
    congressional pattern (<lastname>.senate.gov / .house.gov); the rhetoric
    actor is the candidate's own name plus pronouns. This generalizes the
    formerly Ossoff-hardcoded classifier so a non-Ossoff run doesn't assert
    'no statements in their own voice' as a finding when it's a blind spot.

    Domain derivation is a heuristic (surname collisions can shift a member's
    real subdomain); if it's off we simply miss some official releases and fall
    back to rhetoric detection — conservative, never a false 'their voice'."""
    first, last = _split_person_name(candidate_name)
    def slug(x: str) -> str:
        return re.sub(r"[^a-z]", "", (x or "").lower())
    terms = {t for t in (slug(first), slug(last)) if len(t) >= 3}
    lastslug = slug(last)
    if office == "S":
        domains = (f"{lastslug}.senate.gov",) if lastslug else ()
    elif office == "H":
        domains = (f"{lastslug}.house.gov",) if lastslug else ()
    elif lastslug:
        domains = (f"{lastslug}.senate.gov", f"{lastslug}.house.gov")
    else:
        domains = ()
    name_alt = "|".join(sorted(re.escape(t) for t in terms)) or r"(?!x)x"  # never-match if no name
    verbs = _RHETORIC_VERBS
    return {
        "terms": frozenset(terms),
        "official_domains": domains,
        "name_re": re.compile(r"\b(?:" + name_alt + r")\b[^.]{0,70}?\b(?:" + verbs + r")\b", re.I),
        "pronoun_re": re.compile(r"\b(?:he|she|they)\b[^.]{0,70}?\b(?:" + verbs + r")\b", re.I),
        "name_present_re": re.compile(r"\b(?:" + name_alt + r")\b", re.I),
    }


def _rhetoric_hit(excerpt: str, ctx: Optional[dict]) -> bool:
    """Did the candidate MAKE a claim here? Name+verb always counts; a bare
    pronoun+verb counts only when the candidate's name is ALSO present in the
    excerpt — guards against a same-gender opponent's 'he/she said …' being
    misread as the candidate's own voice."""
    if ctx is None:
        return bool(_RHETORIC_RE.search(excerpt))
    m = ctx["name_re"].search(excerpt)
    if m and not _is_false_rhetoric(m.group(0)):
        return True
    pm = ctx["pronoun_re"].search(excerpt)
    if pm and ctx["name_present_re"].search(excerpt) and not _is_false_rhetoric(pm.group(0)):
        return True
    return False


# A "name … verb" span that is really a LIST of topics, not a sentence — e.g.
# quiverquant.com's "Track <name>'s stock trades, net worth, portfolio,
# corporate donors, proposed legislation and more", where "proposed" is an
# adjective on "legislation", not something the candidate said. Two general
# tells, no per-site table needed.
_ENUMERATION_RE = re.compile(r"(?:,[^,]{1,40}){3,}$", re.S)   # ≥3 commas in the span
_ADJECTIVAL_RE = re.compile(
    r"\b(?:propos(?:ed|es)|introduc(?:ed|es)|releas(?:ed|es))\s+"
    r"(?:legislation|bill|bills|rule|rules|budget|amendment|amendments|"
    r"measure|measures|report|reports)\b\s*(?:and more|,|$)", re.I)


def _is_false_rhetoric(span: str) -> bool:
    """True when a name+verb match is an enumeration or an adjectival
    participle rather than an actual claim by the candidate."""
    return bool(_ENUMERATION_RE.search(span) or _ADJECTIVAL_RE.search(span))


def _official_hit(src: str, official) -> bool:
    return any(src == d or src.endswith("." + d) for d in official)


def classify_statement(excerpt: str, source: str, ctx: Optional[dict] = None) -> tuple[str, str]:
    """Return (class, reason). class ∈ {by_candidate, about_candidate, boilerplate}.
    Order matters: non-statements first, then the candidate's own voice, then
    everything else is coverage. Pass a `ctx` from build_candidate_context to
    classify for a specific candidate; ctx=None keeps the original behavior."""
    e = excerpt or ""
    src = (source or "").lower()
    if _BOILERPLATE_RE.search(e):
        return "boilerplate", "cta_or_boilerplate_text"
    is_rhet = _rhetoric_hit(e, ctx)
    if src in _DATA_DOMAINS and not is_rhet:
        return "boilerplate", "reference_or_data_page"
    if is_rhet:
        return "by_candidate", "rhetoric_verb"
    official = ctx["official_domains"] if ctx else _OFFICIAL_DOMAINS
    if _official_hit(src, official):
        return "by_candidate", "official_release"
    # A campaign-site page with no rhetoric cue is almost always reposted
    # coverage or a fundraising item, not their own statement — treat as coverage.
    return "about_candidate", "third_party_framing"


def _coverage(statements: list[CollectedStatement],
              ctx: Optional[dict] = None) -> dict:
    """Per-year, per-source, per-angle counts + an undated count. The honesty
    layer: lets the report disclose a thin, skewed, or poorly-dated sample
    instead of treating it as solid."""
    from collections import Counter
    by_year, by_source, by_angle, by_class = Counter(), Counter(), Counter(), Counter()
    undated_by_class, year_only_by_class = Counter(), Counter()
    undated = 0
    year_only = 0
    for s in statements:
        yr = s.date[:4] if (s.dated and s.date[:4].isdigit()) else (s.year_hint or "")
        if yr:
            by_year[yr] += 1
        cls = s.classify or "unclassified"
        if not s.dated:
            undated += 1
            undated_by_class[cls] += 1
            if s.year_hint:
                year_only += 1
                year_only_by_class[cls] += 1
        by_source[s.source] += 1
        by_angle[s.query] += 1
        by_class[cls] += 1
    total = sum(by_source.values()) or 1
    dominant = (max(by_source.values()) / total) if by_source else 0.0
    own_voice_note = _own_voice_note(dict(by_class), list(by_source), ctx)
    return {
        "total": len(statements),
        "by_year": dict(sorted(by_year.items())),
        "by_source": dict(by_source),
        "by_angle": dict(by_angle),
        "undated": undated,                       # across ALL gathered statements
        "undated_year_only": year_only,           # of those, how many have a year_hint
        "undated_by_class": dict(undated_by_class),   # undated split by class — the by_candidate
        "year_only_by_class": dict(year_only_by_class),  # slice matches the timeline's own counts
        "by_class": dict(by_class),               # by_candidate / about_candidate / boilerplate
        "source_skew": round(dominant, 3),        # 1.0 = all one outlet
        "source_skewed": dominant > 0.6,
        "own_voice_note": own_voice_note,
    }


def _own_voice_note(by_class: dict, sources, ctx: Optional[dict]) -> str:
    """When ZERO statements land in the candidate's own voice, say WHY in
    method terms. Otherwise a reader takes "0 in his own voice" as a fact about
    the person ("he never talks about money") when it's usually a fact about
    what this method can see. Two structural blind spots dominate:

      * CHALLENGERS have no <lastname>.house/senate.gov, so the official-release
        path can never fire — their own voice lives on a campaign site we don't
        know and on social media whose search snippets are login walls.
      * Search snippets are short; a quote can be on the page but absent from
        the snippet the classifier sees.

    Returns "" when the candidate's own voice WAS found (no caveat needed)."""
    if by_class.get("by_candidate"):
        return ""
    official = tuple((ctx or {}).get("official_domains") or ())
    saw_official = any(
        s == d or str(s).endswith("." + d) for s in sources for d in official)
    bits = ["no statements in the candidate's own voice were identified — treat "
            "this as a limit of the search, NOT as evidence the candidate is silent "
            "on money"]
    if official and not saw_official:
        bits.append(f"nothing was gathered from the expected official domain "
                    f"({', '.join(official)}) — usual for a challenger not yet in "
                    f"office, whose own words sit on a campaign site/social media "
                    f"this method can't attribute")
    social = sum(1 for s in sources if str(s) in
                 ("facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com"))
    if social:
        bits.append(f"{social} result(s) came from social platforms whose snippets are "
                    f"often login walls rather than quotable text")
    return "; ".join(bits)


# -- offline fixtures (no URL needed) -- synthetic, clearly labeled -----------
# NOT real quotes. Spread across years, sources, and angles, and intentionally
# MIXED (reform talk AND ordinary policy talk) so a fair sample doesn't look
# pre-cooked toward any conclusion.
_FIXTURES = [
    ("2019-03-02", "ajc.com", "campaign finance",
     "[fixture] said the influence of big money in campaigns has to be confronted directly."),
    ("2020-09-30", "cnn.com", "dark money",
     "[fixture] called secret political spending a corrupting force that drowns out ordinary voters."),
    ("2021-06-22", "congress.gov", "getting money out of politics",
     "[fixture] introduced remarks supporting public financing of campaigns."),
    ("2022-08-05", "reuters.com", "corporate PAC",
     "[fixture] reiterated a pledge to refuse corporate PAC contributions."),
    ("2023-05-10", "wsj.com", "lobbying",
     "[fixture] discussed tightening lobbying disclosure rules in committee."),
    ("2024-01-25", "local-news.com", "money in politics",
     "[fixture] focused on lowering costs and infrastructure; no mention of campaign money."),
    ("2024-10-01", "youtube.com", "special interests",
     "[fixture] told a rally the billionaire class has bought too much influence in Washington."),
    ("", "podcast-host.com", "Citizens United",
     "[fixture, UNDATED] criticized the Citizens United decision in a long-form interview."),
]


def _offline(candidate: str) -> StatementSet:
    statements = [
        CollectedStatement(
            id=f"f{i+1}", excerpt=text, date=date, dated=bool(date),
            source=src, url=f"https://{src}/(offline-fixture)", query=angle,
            classify="by_candidate", classify_reason="fixture")
        for i, (date, src, angle, text) in enumerate(_FIXTURES)
    ]
    s = StatementSet(candidate=candidate, statements=statements, backend="fixtures")
    s.coverage = _coverage(statements)
    return s


def statement_to_jsonable(s: CollectedStatement) -> dict:
    return {"id": s.id, "excerpt": s.excerpt, "date": s.date, "dated": s.dated,
            "year_hint": s.year_hint, "date_source": s.date_source,
            "classify": s.classify, "classify_reason": s.classify_reason,
            "source": s.source, "url": s.url, "angle": s.query}


# WHY THE RAW LOG IS EXPORTED
# Live web search is not reproducible. Runs 18 and 19 were ~19 hours apart and
# shared only 40 of ~51 result URLs; the own-voice statement count moved 11 -> 6
# almost entirely from corpus churn rather than any code change. Without the raw
# result set there is no way, after the fact, to tell a classifier regression
# apart from the engine simply returning different pages — the one track in this
# tool that can't be re-derived from a stable public source.
#
# So persist what the engine actually returned, before dedup and filtering, with
# the disposition of every row. That makes the statements stage auditable in the
# same sense the FEC and Congress.gov stages already are: a reader can see what
# was offered, what was kept, and why each dropped row was dropped.
_RESULT_DISPOSITIONS = (
    "kept", "dropped_no_url", "dropped_excerpt_too_short",
    "dropped_duplicate_url", "dropped_near_duplicate_text", "query_failed",
)


def statements_to_jsonable(ss: StatementSet) -> dict:
    return {
        "candidate": ss.candidate,
        "backend": ss.backend,
        "total": len(ss.statements),
        "coverage": ss.coverage,
        "statements": [statement_to_jsonable(s) for s in ss.statements],
        "searched_at": ss.searched_at,
        "search_log": ss.search_log,
        "search_log_note": (
            "Raw SearXNG results as returned, before dedup/filtering, with the "
            "disposition of each row. Live search is not reproducible run to "
            "run; this is what makes the statements stage auditable after the "
            "fact. Dispositions: " + ", ".join(_RESULT_DISPOSITIONS) + "."),
    }


if __name__ == "__main__":
    import sys
    ss = collect_statements("SAMPLE CANDIDATE")
    print(f"backend: {ss.backend} | {len(ss.statements)} statements", file=sys.stderr)
    print(f"coverage: {json.dumps(ss.coverage, indent=2)}", file=sys.stderr)
    print(json.dumps(statements_to_jsonable(ss), indent=2))

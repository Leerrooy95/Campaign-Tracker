"""
timeline.py — deterministic Side-by-Side assembler.

The merged money/record/statements timeline is the tool's core method, and it
was previously assembled BY THE MODEL from raw track JSON — which produced sort
errors in real runs (2026-02-02 listed above 2026-02-03, twice, in two separate
reports). Ordering is not a judgment call; it should never have been the
model's job. This module builds the timeline in code:

  - MONEY events: outside spenders (one event per spender at their last
    recorded date, with the range noted; a range's start is a second event so
    a months-long spending run is visible as a span, not a point) and the top
    itemized donors (at their last contribution date).
  - RECORD events: money-related bills at their introduced date.
  - STATEMENT events: statements with a REAL date only.

Sorted newest-first with a total order (date, then track, then label — so ties
are stable across runs). Items with only a year_hint go in a separate
`year_only` bucket, sorted by year — never interleaved with real dates, so a
year-level guess can't masquerade as a dated event. Undated items are counted,
not placed.

The synthesis layer receives this pre-built timeline and is instructed to
restate it IN THE GIVEN ORDER. The model writes connective prose; the ordering
is ours. Facts, not vibes — same rule as everywhere else in this tool.
"""
from __future__ import annotations

from typing import Any

_TRACK_ORDER = {"statement": 0, "record": 1, "money": 2}   # tie-break only

_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _month_label(ym: str) -> str:
    """'2025-09' -> 'September 2025'. Falls back to the raw key if malformed."""
    try:
        y, m = ym.split("-")
        return f"{_MONTHS[int(m)]} {y}"
    except Exception:   # noqa: BLE001
        return ym


def _group_into_bands(events: list[dict]) -> list[dict]:
    """Group the already-sorted (newest-first) events into month bands so 27
    near-identical donor rows read as a handful of labeled clusters instead of a
    wall. Order is preserved (bands newest-first, events newest-first within).
    Each band carries per-track counts so the reader sees its shape at a glance."""
    from collections import Counter, OrderedDict
    grouped: "OrderedDict[str, list[dict]]" = OrderedDict()
    for e in events:
        grouped.setdefault(e["date"][:7], []).append(e)
    bands = []
    for ym, evs in grouped.items():
        counts = Counter(ev["track"] for ev in evs)
        bands.append({
            "band": ym,
            "label": _month_label(ym),
            "event_count": len(evs),
            "counts": dict(counts),
            "events": evs,
        })
    return bands


def _fmt_amount(v: float) -> str:
    return f"${v:,.2f}".rstrip("0").rstrip(".") if v else "$0"


def _spender_events(outside: dict) -> list[dict]:
    events: list[dict] = []
    for s in outside.get("spenders") or []:
        name = s.get("committee_name") or s.get("committee_id") or "unknown committee"
        cid = s.get("committee_id") or ""
        sup = float(s.get("support_total") or 0)
        opp = float(s.get("oppose_total") or 0)
        first, last = s.get("first_date"), s.get("last_date")
        if not (sup > 0 or opp > 0) or not last:
            continue
        # Describe each side it actually spent on (split spenders get both).
        parts = []
        if opp > 0:
            parts.append(f"{_fmt_amount(opp)} opposing")
        if sup > 0:
            parts.append(f"{_fmt_amount(sup)} supporting")
        side_txt = " and ".join(parts)
        dark = "" if s.get("traceable") else " — marked NOT traceable (dark money)"
        count = int(s.get("count") or 0)
        if first and first != last:
            events.append({
                "date": last, "track": "money",
                "label": (f"{name} ({cid}): last recorded transaction of a run — "
                          f"{side_txt} total across {count} transactions, "
                          f"{first} to {last}{dark}"),
                "source": "FEC Schedule E",
            })
            events.append({
                "date": first, "track": "money",
                "label": f"{name} ({cid}): first recorded transaction of the run above{dark}",
                "source": "FEC Schedule E",
            })
        else:
            events.append({
                "date": last, "track": "money",
                "label": f"{name} ({cid}): {side_txt}"
                         + (f" across {count} transactions" if count > 1 else "")
                         + dark,
                "source": "FEC Schedule E",
            })
    return events


def _donor_events(direct: dict) -> list[dict]:
    events: list[dict] = []
    for d in direct.get("top_donors") or []:
        last = d.get("last_date")
        if not last:
            continue
        name = d.get("name") or "unknown donor"
        total = _fmt_amount(float(d.get("total") or 0))
        count = int(d.get("count") or 0)
        first = d.get("first_date")
        span = (f", {first} to {last}" if first and first != last else "")
        events.append({
            "date": last, "track": "money",
            "label": (f"Top itemized donor {name}: {total}"
                      + (f" across {count} contributions{span}" if count > 1 else "")),
            "source": "FEC Schedule A (largest itemized contributions)",
        })
    return events


def _record_events(record: dict) -> list[dict]:
    events: list[dict] = []
    for a in record.get("money_related") or []:
        date = a.get("date")
        if not date:
            continue
        terms = ", ".join(a.get("money_terms") or [])
        events.append({
            "date": date, "track": "record",
            "label": (f'{a.get("role", "acted on")} {a.get("citation", "?")} — '
                      f'"{a.get("title", "")}"'
                      + (f" (matched: {terms})" if terms else "")),
            "source": "Congress.gov",
        })
    return events


def _vote_events(votes: dict) -> list[dict]:
    """Roll-call votes (votes.py) join the RECORD track: what the candidate did
    on the floor sits beside what they sponsored. Only money-relevant votes are
    in the jsonable's `money_related` list, each already carrying its own
    citation + primary-source URL — cited like every other timeline line."""
    events: list[dict] = []
    for v in votes.get("money_related") or []:
        date = v.get("date")
        if not date:
            continue
        terms = ", ".join(v.get("money_terms") or [])
        src = ("House Clerk roll call (via Congress.gov API)"
               if v.get("chamber") == "House" else "Senate LIS roll call")
        role = v.get("candidate_bill_role") or ""
        events.append({
            "date": date, "track": "record",
            "label": (f'voted {v.get("position", "?")} on {v.get("citation", "?")} — '
                      f'"{v.get("title", "")}"'
                      + (f' [{v.get("question")}]' if v.get("question") else "")
                      + (f" — a bill they {role}" if role else "")
                      + (f" (matched: {terms})" if terms else "")),
            "source": src,
        })
    return events


def _statement_events(stmts: dict) -> tuple[list[dict], list[dict], int, dict]:
    """Returns (dated_events, year_only_items, undated_count, excluded).
    Only the candidate's OWN voice (classify == 'by_candidate') is placed on the
    timeline. Coverage-about-him and boilerplate are counted in `excluded` and
    kept in the JSON, just off the timeline — the rhetoric-vs-funding comparison
    is only meaningful on his own words. Statements with no class (older data)
    default to by_candidate so nothing silently vanishes."""
    events: list[dict] = []
    year_only: list[dict] = []
    undated = 0
    excluded: dict = {}
    for s in stmts.get("statements") or []:
        cls = s.get("classify") or "by_candidate"
        if cls != "by_candidate":
            excluded[cls] = excluded.get(cls, 0) + 1
            continue
        excerpt = (s.get("excerpt") or "").strip()
        short = excerpt if len(excerpt) <= 160 else excerpt[:157] + "…"
        if s.get("dated") and s.get("date"):
            events.append({
                "date": s["date"], "track": "statement",
                "label": f'statement ({s.get("id", "?")}): "{short}"',
                "source": s.get("source") or "",
            })
        elif s.get("year_hint"):
            year_only.append({
                "year": s["year_hint"],
                "label": f'statement ({s.get("id", "?")}), year only: "{short}"',
                "source": s.get("source") or "",
            })
        else:
            undated += 1
    return events, year_only, undated, excluded


def build_timeline(result: dict) -> dict:
    """Assemble the Side-by-Side timeline deterministically from the result.
    Pure function of the tracks; safe on partial results (skipped stages just
    contribute nothing)."""
    events: list[dict] = []
    events += _spender_events(result.get("track_a_outside") or {})
    events += _donor_events(result.get("track_a_direct") or {})
    events += _record_events(result.get("track_b_record") or {})
    events += _vote_events(result.get("track_b_votes") or {})
    st_events, year_only, undated, st_excluded = _statement_events(
        result.get("track_b_statements") or {})
    events += st_events

    # Newest first; total order so output is stable run to run.
    events.sort(key=lambda e: (e["date"], -_TRACK_ORDER.get(e["track"], 9),
                               e["label"]), reverse=True)
    year_only.sort(key=lambda e: (e["year"], e["label"]), reverse=True)

    note = ("Pre-sorted newest-first, built deterministically in code from the "
            "tracks above and grouped into month bands (see 'bands') for "
            "readability. Dated events only; year-only statements are in "
            "year_only (never interleaved); undated items are counted, not placed. "
            "Statements are filtered to the candidate's own voice — coverage about "
            "him and boilerplate are held off the timeline (see statements_excluded "
            "and coverage.by_class), not deleted.")
    return {
        "note": note,
        "events": events,                     # flat, newest-first (kept for anything that needs it)
        "bands": _group_into_bands(events),   # same events grouped into month bands, newest-first
        "year_only": year_only,
        "undated_count": undated,
        "event_count": len(events),
        "statements_excluded": st_excluded,   # {about_candidate: n, boilerplate: n} kept off-timeline
    }


def verify_sorted(timeline: dict) -> bool:
    """True if events are non-increasing by date — the invariant the model was
    violating. Cheap self-check; used by tests and available to the audit."""
    dates = [e["date"] for e in timeline.get("events", [])]
    return all(a >= b for a, b in zip(dates, dates[1:]))


if __name__ == "__main__":
    # Offline self-test on shapes mirroring the real result JSON, including the
    # exact adjacent dates the model mis-sorted in Runs 3 and 4.
    result = {
        "track_a_outside": {"spenders": [
            {"committee_name": "SLF PAC", "committee_id": "C00571703",
             "oppose_total": 627199.96, "support_total": 0, "count": 10,
             "first_date": "2025-08-20", "last_date": "2026-02-02", "traceable": True},
            {"committee_name": "FORWARD BLUE", "committee_id": "C00835041",
             "support_total": 20100.0, "oppose_total": 0, "count": 1,
             "first_date": "2026-06-20", "last_date": "2026-06-20", "traceable": True},
            {"committee_name": "CATHOLICVOTE.ORG", "committee_id": "C90011800",
             "oppose_total": 17938.74, "support_total": 0, "count": 1,
             "first_date": "2026-05-18", "last_date": "2026-05-18", "traceable": False},
        ]},
        "track_a_direct": {"top_donors": [
            {"name": "Tim Disney", "total": 14000.0, "count": 3,
             "first_date": "2025-08-25", "last_date": "2026-04-17"},
        ]},
        "track_b_record": {"money_related": [
            {"date": "2026-03-04", "role": "cosponsored", "citation": "S 3991 (119th)",
             "title": "DISCLOSE Act of 2026", "money_terms": ["disclose act"]},
        ]},
        "track_b_statements": {"statements": [
            {"id": "s1", "date": "2026-06-28", "dated": True, "year_hint": "2026",
             "excerpt": "Ossoff urged the Senate to act on money in politics…",
             "source": "youtube.com", "classify": "by_candidate"},
            {"id": "s2", "date": "2026-02-03", "dated": True, "year_hint": "2026",
             "excerpt": "Ossoff enters 2026 with a fundraising edge…",
             "source": "georgiarecorder.com", "classify": "about_candidate"},
            {"id": "s3", "date": "", "dated": False, "year_hint": "2020",
             "excerpt": "FactCheck on the 2020 runoff dark-money claim…",
             "source": "factcheck.org", "classify": "about_candidate"},
            {"id": "s4", "date": "2026-05-01", "dated": True, "year_hint": "2026",
             "excerpt": "Donate now — chip in to the campaign",
             "source": "electjon.com", "classify": "boilerplate"},
        ]},
    }
    tl = build_timeline(result)
    print(f"events: {tl['event_count']} | year_only: {len(tl['year_only'])} | "
          f"undated: {tl['undated_count']} | excluded: {tl['statements_excluded']}")
    for e in tl["events"]:
        print(f"  {e['date']}  [{e['track']:<9}] {e['label'][:78]}")
    print("year_only:")
    for e in tl["year_only"]:
        print(f"  {e['year']}        {e['label'][:70]}")
    print("\nsorted newest-first invariant:", verify_sorted(tl))
    # Only the by_candidate statement (s1) should appear; s2/s3/s4 held off-timeline.
    labels = " ".join(e["label"] for e in tl["events"])
    print("by_candidate statement placed:", "Ossoff urged" in labels)
    print("about/boilerplate excluded:", "fundraising edge" not in labels and "Donate now" not in labels)
    print("excluded counts:", tl["statements_excluded"])
    # Ordering invariant already checked above via verify_sorted().

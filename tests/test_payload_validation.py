"""Standalone Pydantic validation of the Campaign Tracker result payload.

This does NOT replace the plain-python offline tests elsewhere in tests/
(see CLAUDE.md's testing conventions) — it's a schema-shaped validation pass
over the *complete* JSON a `/status/<job_id>` response (or a full JSON export)
carries, plus five payload-wide integrity invariants pulled straight out of
CLAUDE.md's Integrity Rules:

  1. `track_a_direct.largest_contributions_total` (the capped, largest-first
     itemized-donor sum) can never exceed the current cycle's actual
     `receipts` in `track_a_composition` — the capped list is a subset of
     real receipts, never a bigger number than the total it's drawn from
     (CLAUDE.md, fec.py specifics: "the campaign's real receipts total comes
     from /totals ... never from summing this capped list").
  2. `outside_dark` (the traceable/dark money split) is only ever computed
     for the CURRENT cycle (`cycle == candidate.cycle`, here 2026) — every
     other, prior cycle must carry `null` rather than a fabricated split
     (CLAUDE.md: "computed only for the current cycle ... null for prior
     cycles ... never fabricate it for a prior cycle").
  3. Every timeline event and every money-related legislative/vote record
     carries a non-empty primary-source citation field — Integrity Rule 2,
     "Every line cites a primary source... If it can't be cited, it doesn't
     ship."
  4. The `demo` flag and the per-track `_demo` marker agree everywhere: if
     `demo` is True every track must carry `_demo`, and if `demo` is False
     no track may carry it at all (CLAUDE.md: "`result["demo"]`... every
     demo track carries `_demo`" / `_mark_demo`).
  5. No date field holds a fabricated `YYYY-01-01` placeholder. This is
     scoped precisely to where CLAUDE.md documents the actual historical bug
     and its fix:
       - the merged `timeline` (events + bands + year_only) — "an early bug
         put fake Jan-1 dates on the timeline" (timeline.py / statements.py
         specifics) — and the official-record tracks (bills, roll-call
         votes), whose dates come straight from Congress.gov/Clerk/LIS and
         are never guessed;
       - AND, inside raw `track_b_statements.statements`, the module's own
         contract (`statements.py`: "date: ISO YYYY-MM-DD when a real date
         was found, else '' (never a placeholder)"): a statement with
         `dated=False` may never carry a non-empty `date`.
     It deliberately does NOT blanket-reject a `YYYY-01-01` value inside a
     *dated=True, page_metadata-sourced* raw statement date — a source page
     genuinely declaring that date (e.g. a data page's generic template
     metadata) is real, disclosed data, not the invented-guess bug the rule
     exists to catch; only `by_candidate` statements ever reach the timeline
     (timeline.py), and the timeline/record/vote check above still catches
     any such date that actually lands there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

_PLACEHOLDER_DATE_RE = re.compile(r"^\d{4}-01-01$")


def _is_placeholder_date(value: str) -> bool:
    return bool(value) and bool(_PLACEHOLDER_DATE_RE.match(value))


# ---------------------------------------------------------------------------
# Shared small models
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    detail: str


class ReportMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    cached: bool
    content_hash: str
    model: str
    readability_grade: int


class ReconciliationCounts(BaseModel):
    model_config = ConfigDict(extra="allow")

    high: int
    review: int
    warnings: int


class ReportReconciliation(BaseModel):
    model_config = ConfigDict(extra="allow")

    counts: ReconciliationCounts
    ok: bool
    warnings: list[Any] = []


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    label: str
    source: str
    track: str


class TimelineBand(BaseModel):
    model_config = ConfigDict(extra="allow")

    band: str
    counts: dict[str, int]
    event_count: int
    events: list[TimelineEvent]
    label: str


class YearOnlyItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    year: str
    label: str
    source: str


class Timeline(BaseModel):
    model_config = ConfigDict(extra="allow")

    bands: list[TimelineBand]
    event_count: int
    events: list[TimelineEvent]
    note: str
    statements_excluded: dict[str, int]
    undated_count: int
    year_only: list[YearOnlyItem]


# ---------------------------------------------------------------------------
# Track A — money
# ---------------------------------------------------------------------------


class CompositionCycle(BaseModel):
    model_config = ConfigDict(extra="allow")

    cycle: int
    incomplete: bool
    incomplete_reason: str
    individual_itemized: float
    individual_unitemized: float
    large_share: float
    small_share: float
    pac_contributions: float
    receipts: float
    outside_support: float
    outside_oppose: float
    outside_dark: Optional[float] = None
    outside_traceable: Optional[float] = None
    outside_totals_source: str


class DirectDonor(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    bucket: str
    bucket_inferred: bool
    count: int
    total: float
    first_date: str
    last_date: str
    employer: Optional[str] = None
    occupation: Optional[str] = None
    location: str


class DirectTransaction(BaseModel):
    model_config = ConfigDict(extra="allow")

    sub_id: str
    contributor_name: str
    amount: float
    date: str
    committee_id: str
    bucket: str
    city: str
    state: str
    employer: Optional[str] = None
    occupation: Optional[str] = None


class TrackADirect(BaseModel):
    model_config = ConfigDict(extra="allow")

    donor_count: int
    largest_contributions_total: float
    memo_skipped: int
    note: str
    pulled_donors: list[DirectDonor]
    refund_total: float
    refunds_applied: int
    top_donors: list[DirectDonor]
    transaction_count: int
    transactions: list[DirectTransaction]
    transactions_capped: bool
    truncated: bool
    truncated_reason: str


class OutsideSpender(BaseModel):
    model_config = ConfigDict(extra="allow")

    committee_id: str
    committee_name: str
    committee_type: str
    committee_type_full: str
    count: int
    first_date: str
    last_date: str
    notice_only_total: float
    oppose_total: float
    support_total: float
    traceable: bool


class OutsideTransaction(BaseModel):
    model_config = ConfigDict(extra="allow")

    sub_id: str
    committee_id: str
    committee_name: str
    amount: float
    date: str
    description: str
    filing_form: str
    is_notice: bool
    notice_only: bool
    payee: str
    support_oppose: str


class OutsideDedup(BaseModel):
    model_config = ConfigDict(extra="allow")

    memo_dropped: int
    notice_deduped: int
    notice_only_kept: int
    notice_only_oppose: float
    notice_only_support: float
    raw_rows: int
    superseded_dropped: int


class TrackAOutside(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_id: str
    candidate_name: str
    cycle: int
    dedup: OutsideDedup
    incomplete: bool
    incomplete_reason: str
    oppose_total: float
    support_total: float
    record_count: int
    reported_count: int
    spenders: list[OutsideSpender]
    transactions: list[OutsideTransaction]


# ---------------------------------------------------------------------------
# Track B — record, votes, statements
# ---------------------------------------------------------------------------


class LegislativeAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    citation: str
    date: str
    latest_action: str
    money_related: bool
    money_terms: list[str]
    role: str
    title: str
    url: str
    year: int


class RecordYearBucket(BaseModel):
    model_config = ConfigDict(extra="allow")

    cosponsored: Optional[int] = None
    sponsored: Optional[int] = None
    items: list[LegislativeAction]


class TrackBRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    bioguide_id: str
    by_year: dict[str, RecordYearBucket]
    incomplete: bool
    incomplete_reason: str
    member_name: str
    money_related: list[LegislativeAction]
    money_related_count: int
    total_actions: int


class VoteRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_bill_role: str
    chamber: str
    citation: str
    congress: int
    date: str
    legislation: str
    money_related: bool
    money_terms: list[str]
    number: int
    position: str
    question: str
    result: str
    title: str
    url: str
    year: int


class TrackBVotes(BaseModel):
    model_config = ConfigDict(extra="allow")

    chamber: str
    coverage_note: str
    incomplete: bool
    incomplete_reason: str
    member: str
    money_related: list[VoteRecord]
    money_related_seen: int
    not_in_roll: int
    total_votes_scanned: int


class Statement(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    angle: str
    classify: str
    classify_reason: str
    date: str
    date_source: str
    dated: bool
    excerpt: str
    source: str
    url: str
    year_hint: str

    @model_validator(mode="after")
    def _no_fabricated_date_when_not_dated(self) -> "Statement":
        # statements.py's own contract: `date` is a real ISO date when
        # `dated` is True, else "" — never a guessed/fabricated value.
        if not self.dated and self.date:
            raise ValueError(
                f"statement {self.id!r} has dated=False but a non-empty "
                f"date {self.date!r} — a date must never be fabricated "
                f"for an undated statement"
            )
        return self


class StatementsCoverage(BaseModel):
    model_config = ConfigDict(extra="allow")

    total: int
    by_class: dict[str, int]
    by_angle: dict[str, int]
    by_source: dict[str, int]
    by_year: dict[str, int]
    dated_via_page: int
    page_fetch_attempted: int
    search_queries_failed: int
    search_results_seen: int
    undated: int
    undated_by_class: dict[str, int]
    undated_year_only: int
    year_only_by_class: dict[str, int]


class SearchLogEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    angle: str
    detail: str
    disposition: str
    engine: str
    query: str
    rank: int
    statement_id: str
    title: str
    url: str


class TrackBStatements(BaseModel):
    model_config = ConfigDict(extra="allow")

    backend: str
    candidate: str
    coverage: StatementsCoverage
    search_log: list[SearchLogEntry]
    search_log_note: str
    searched_at: str
    statements: list[Statement]
    total: int


# ---------------------------------------------------------------------------
# Top-level payload
# ---------------------------------------------------------------------------


class CampaignTrackerResult(BaseModel):
    """The complete `/status/<job_id>` result payload."""

    model_config = ConfigDict(extra="allow")

    audit: list[AuditEntry]
    candidate: str
    candidate_id: str
    candidate_name: str
    cycle: int
    demo: bool
    office: str
    report: str
    report_meta: ReportMeta
    report_reconciliation: ReportReconciliation
    state: str
    timeline: Timeline
    track_a_composition: list[CompositionCycle]
    track_a_direct: TrackADirect
    track_a_outside: TrackAOutside
    track_b_record: TrackBRecord
    track_b_statements: TrackBStatements
    track_b_votes: TrackBVotes

    # -- Rule 1 ------------------------------------------------------------
    @model_validator(mode="after")
    def _largest_contributions_within_current_cycle_receipts(
        self,
    ) -> "CampaignTrackerResult":
        current = next(
            (c for c in self.track_a_composition if c.cycle == self.cycle),
            None,
        )
        if current is None:
            raise ValueError(
                f"no track_a_composition entry for the current cycle "
                f"({self.cycle}) — cannot check largest_contributions_total "
                f"against real receipts"
            )
        capped_total = self.track_a_direct.largest_contributions_total
        if capped_total > current.receipts:
            raise ValueError(
                "track_a_direct.largest_contributions_total "
                f"({capped_total}) exceeds the {self.cycle} cycle's real "
                f"receipts ({current.receipts}) in track_a_composition — "
                "the capped largest-first donor list must be a subset of "
                "the campaign's actual receipts, never larger than them "
                "(CLAUDE.md: the headline total always comes from /totals, "
                "never from summing the capped list)"
            )
        return self

    # -- Rule 2 --------------------------------------------------------------
    @model_validator(mode="after")
    def _outside_dark_only_on_current_cycle(self) -> "CampaignTrackerResult":
        for entry in self.track_a_composition:
            if entry.cycle == self.cycle:
                if entry.outside_dark is None:
                    raise ValueError(
                        f"cycle {entry.cycle} is the current cycle but "
                        "outside_dark is null — the traceable/dark split "
                        "must be a real number for the current cycle "
                        "(per-spender detail exists to compute it)"
                    )
            else:
                if entry.outside_dark is not None:
                    raise ValueError(
                        f"cycle {entry.cycle} is a prior cycle but "
                        f"outside_dark = {entry.outside_dark!r} — prior "
                        "cycles have no per-spender detail and must carry "
                        "null, never a fabricated split (CLAUDE.md: "
                        "'never fabricate it for a prior cycle')"
                    )
        return self

    # -- Rule 3 --------------------------------------------------------------
    @model_validator(mode="after")
    def _every_event_and_action_has_a_citation(self) -> "CampaignTrackerResult":
        problems: list[str] = []

        for i, ev in enumerate(self.timeline.events):
            if not ev.source.strip():
                problems.append(f"timeline.events[{i}] ({ev.label!r}) has an empty source")
        for bi, band in enumerate(self.timeline.bands):
            for ei, ev in enumerate(band.events):
                if not ev.source.strip():
                    problems.append(
                        f"timeline.bands[{bi}].events[{ei}] ({ev.label!r}) has an empty source"
                    )
        for yi, yo in enumerate(self.timeline.year_only):
            if not yo.source.strip():
                problems.append(f"timeline.year_only[{yi}] ({yo.label!r}) has an empty source")

        for year, bucket in self.track_b_record.by_year.items():
            for i, item in enumerate(bucket.items):
                if not item.citation.strip():
                    problems.append(
                        f"track_b_record.by_year[{year!r}].items[{i}] "
                        f"({item.title!r}) has an empty citation"
                    )
        for i, item in enumerate(self.track_b_record.money_related):
            if not item.citation.strip():
                problems.append(
                    f"track_b_record.money_related[{i}] ({item.title!r}) has an empty citation"
                )

        for i, vote in enumerate(self.track_b_votes.money_related):
            if not vote.citation.strip():
                problems.append(
                    f"track_b_votes.money_related[{i}] ({vote.title!r}) has an empty citation"
                )

        if problems:
            raise ValueError(
                "missing primary-source citation on "
                f"{len(problems)} object(s): " + "; ".join(problems[:10])
                + (" ..." if len(problems) > 10 else "")
            )
        return self

    # -- Rule 4 --------------------------------------------------------------
    @model_validator(mode="after")
    def _demo_marker_matches_top_level_flag(self) -> "CampaignTrackerResult":
        track_names = (
            "track_a_direct",
            "track_a_outside",
            "track_b_record",
            "track_b_statements",
            "track_b_votes",
        )
        missing_demo: list[str] = []
        unexpected_demo: list[str] = []
        for name in track_names:
            track = getattr(self, name)
            extras = getattr(track, "model_extra", None) or {}
            has_marker = "_demo" in extras
            if self.demo and not has_marker:
                missing_demo.append(name)
            if not self.demo and has_marker:
                unexpected_demo.append(name)

        if missing_demo:
            raise ValueError(
                f"demo=True but these tracks are missing the _demo marker: "
                f"{', '.join(missing_demo)}"
            )
        if unexpected_demo:
            raise ValueError(
                f"demo=False but these tracks carry a _demo marker anyway: "
                f"{', '.join(unexpected_demo)}"
            )
        return self

    # -- Rule 5 --------------------------------------------------------------
    @model_validator(mode="after")
    def _no_placeholder_dates(self) -> "CampaignTrackerResult":
        problems: list[str] = []

        for i, ev in enumerate(self.timeline.events):
            if _is_placeholder_date(ev.date):
                problems.append(f"timeline.events[{i}] date={ev.date!r} ({ev.label!r})")
        for bi, band in enumerate(self.timeline.bands):
            for ei, ev in enumerate(band.events):
                if _is_placeholder_date(ev.date):
                    problems.append(
                        f"timeline.bands[{bi}].events[{ei}] date={ev.date!r} ({ev.label!r})"
                    )

        for year, bucket in self.track_b_record.by_year.items():
            for i, item in enumerate(bucket.items):
                if _is_placeholder_date(item.date):
                    problems.append(
                        f"track_b_record.by_year[{year!r}].items[{i}] "
                        f"date={item.date!r} ({item.title!r})"
                    )

        for i, vote in enumerate(self.track_b_votes.money_related):
            if _is_placeholder_date(vote.date):
                problems.append(
                    f"track_b_votes.money_related[{i}] date={vote.date!r} ({vote.title!r})"
                )

        if problems:
            raise ValueError(
                f"{len(problems)} fabricated YYYY-01-01 placeholder date(s) found: "
                + "; ".join(problems[:10])
                + (" ..." if len(problems) > 10 else "")
            )
        return self


# ---------------------------------------------------------------------------
# Dynamic fixture discovery
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The required (no-default) top-level fields of the master model — used to
# sniff which *.json files under fixtures/ are actually full result payloads.
# tests/fixtures/ is shared by the whole suite (reconcile.py's Run-13 replay,
# raw Schedule E rows, Senate LIS XML, ...) and most of what lives there is
# NOT a full payload; attempting to force those through this schema would
# fail for reasons this script has nothing to do with, so they're skipped
# rather than treated as a validation failure (CLAUDE.md's own rule —
# "disclose incompleteness, never fake" — applies to this script's own
# coverage too: a fixture that isn't a full payload is disclosed as skipped,
# not silently ignored and not falsely failed).
_REQUIRED_TOP_LEVEL_FIELDS = {
    name
    for name, info in CampaignTrackerResult.model_fields.items()
    if info.is_required()
}


def _is_full_payload(data: Any) -> bool:
    return isinstance(data, dict) and _REQUIRED_TOP_LEVEL_FIELDS.issubset(data.keys())


def _discover_full_payload_fixtures() -> list[Any]:
    """Scan tests/fixtures/*.json once at collection time.

    Each matching file is parsed exactly once here and carried as the
    parametrize value itself (rather than a path re-read inside the test
    body) so a large fixture — the real run is >1MB — is never read or
    `json.loads`-ed twice per test session.
    """
    cases = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if _is_full_payload(data):
            cases.append(pytest.param(data, id=path.name))
    return cases


FULL_PAYLOAD_FIXTURE_CASES = _discover_full_payload_fixtures()


# ---------------------------------------------------------------------------
# pytest entry points
# ---------------------------------------------------------------------------


def test_fixture_discovery_found_full_payloads():
    """Guard against the dynamic scan silently finding nothing.

    An empty parametrize list collects zero tests and pytest reports that as
    a pass — exactly the kind of quiet, unearned green the rest of this repo
    is built to avoid (Integrity Rule 4/6). This fails loudly instead if
    tests/fixtures/ stops containing at least one full-payload JSON file.
    """
    assert FULL_PAYLOAD_FIXTURE_CASES, (
        f"no full-payload JSON fixtures found under {FIXTURES_DIR} — "
        "expected at least massie_thomas_h_2026_full.json and "
        "demo_synthetic_run_2026_full.json"
    )


@pytest.mark.parametrize("payload", FULL_PAYLOAD_FIXTURE_CASES)
def test_fixture_payload_validates(payload: dict[str, Any]):
    """Every full-payload fixture under tests/fixtures/ parses cleanly.

    A pass means, for that fixture: the capped donor total never exceeds
    real receipts, the traceable/dark split is present only for the current
    cycle, every timeline/legislative/vote object is cited, the demo flag
    and the per-track _demo markers agree, and no date field carries a
    fabricated YYYY-01-01 placeholder.
    """
    result = CampaignTrackerResult.model_validate(payload)
    assert result.candidate_id


def test_demo_marker_enforcement():
    """Prove the demo/_demo check actually enforces, using the synthetic
    demo:true fixture — not just that a correctly-marked run passes (that
    alone wouldn't distinguish a real check from no check at all), but that
    breaking the invariant in either direction is rejected.
    """
    demo_path = FIXTURES_DIR / "demo_synthetic_run_2026_full.json"
    raw = json.loads(demo_path.read_text())

    # Positive: a genuinely demo:true run with _demo on every track validates.
    result = CampaignTrackerResult.model_validate(raw)
    assert result.demo is True

    # Negative: demo:true but one track is missing its _demo marker.
    missing_marker = json.loads(demo_path.read_text())
    del missing_marker["track_b_votes"]["_demo"]
    with pytest.raises(ValidationError, match="_demo marker"):
        CampaignTrackerResult.model_validate(missing_marker)

    # Negative: demo:false but a _demo marker was left behind on a track.
    stale_marker = json.loads(demo_path.read_text())
    stale_marker["demo"] = False
    with pytest.raises(ValidationError, match="_demo marker"):
        CampaignTrackerResult.model_validate(stale_marker)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

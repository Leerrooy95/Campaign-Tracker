# Claude Recommendations — 5-candidate live rate-limit / completeness audit

Run date: 2026-09-07. Five candidates run one at a time through the real
two-phase flow (`POST /candidates` → picker → `POST /search` with the
resolved `candidate_id`/`office`/`state`): **Thomas Massie, Ed Gallrein,
Steve Womack, Lauren Boebert, Mike Collins**. Full JSON output for each is
saved under `tests/rate_limit_run/<n>_<name>/`. The raw per-call FEC HTTP log
(path, status, `X-RateLimit-Remaining`/`-Limit`, and the error body on any
non-200) is at `tests/rate_limit_run/fec_http_log.jsonl`.

**Headline result: all 5 candidates completed with no unhandled crash and
no undisclosed step.** Every non-optional stage showed `ok`; `votes`,
`statements`, and `synthesize` showed disclosed `warn`s exactly where the
README says they're optional/capped. **However, this run surfaced a real,
100%-reproducible silent-data-loss bug in Schedule A pagination — not a
rate-limit problem — that the task asked to be recorded here rather than
fixed live.** Per-instruction, it has NOT been fixed.

---

## Rate limiting: no 429s occurred; lowest `X-RateLimit-Remaining` seen was **100** (of 120)

113 FEC calls were logged across all 5 runs (including two runs of Steve
Womack — one attempt was killed client-side by my own test harness timeout,
but had already completed server-side; I re-fetched its result rather than
re-running it, which added extra calls to the log). Status breakdown:
`200` × 106, `422` × 7, **`429` × 0**.

`X-RateLimit-Remaining` never dropped below **100/120** at any point in the
whole run — the existing pacing (`_MIN_REQUEST_INTERVAL = 0.6s` in
`fec.py`) kept every run comfortably under FEC's cap. **Because no 429 was
ever returned, I could not observe the app's retry-and-recover behavior
live** — I can't respond to the "did it retry and recover, or silently
drop data" question for 429 specifically, since it never fired. The retry
path (`_http_get_with_retry`, `_RETRYABLE_STATUS = {-1, 429, 500, 502, 503,
504}`, exponential backoff, `_MAX_RETRIES = 4`) is present in the code and
covers 429 by design, but this run is not a live demonstration of it.

## The real finding: Schedule A silently truncates after page 1, on every candidate, and is never disclosed

This is **not rate-limiting** — `X-RateLimit-Remaining` was always ≥100 when
it happened. It's a request-construction bug that makes FEC reject page 2
of the capped "top donors" pull with **HTTP 422**, deterministically, for
**every one of the 5 candidates** whose committee had more than one page
(100 rows) of individually-itemized Schedule A receipts in the cycle.

**Root cause** — `fec.py`, `fetch_schedule_a()`:

```python
last_indexes = pagination.get("last_indexes") or {}
last_index = last_indexes.get("last_index")
last_date = last_indexes.get("last_contribution_receipt_date")   # ← line 455
...
if last_index is not None:
    params["last_index"] = str(last_index)
    params["last_contribution_receipt_date"] = str(last_date)    # ← line 392
```

This assumes FEC's pagination cursor always names its secondary field
`last_contribution_receipt_date`. That's only true when the query is
**date-sorted**. The capped "top donors" pull
(`search_fec_candidate(..., max_pages=10)`) and the refund-netting pull
(`smallest_first=True`) both sort by **amount**
(`sort=-contribution_receipt_amount` / `sort=contribution_receipt_amount`),
and I confirmed directly against the live FEC API that an amount-sorted
response's cursor field is actually named
**`last_contribution_receipt_amount`**, not
`last_contribution_receipt_date`:

```json
"last_indexes": {
  "last_contribution_receipt_amount": "3500.00",
  "last_index": "4051220261510089026"
}
```

So `last_indexes.get("last_contribution_receipt_date")` returns Python
`None`, `str(None)` produces the literal string `"None"`, and that gets sent
to FEC as a real query parameter on every second-page request:

```
last_contribution_receipt_date=None
```

FEC correctly rejects this with:

```json
{"message":"Invalid date. Date must be formatted as MM/DD/YYYY or YYYY-MM-DD.","status":422}
```

**422 is (correctly, in general) not in `_RETRYABLE_STATUS`** — retrying an
identically malformed request would just get 422 forever, so the no-retry
behavior is *right* for this status code in general. The bug is upstream of
retry logic entirely: the request itself is malformed on every single
2nd-page attempt of an amount-sorted pull.

**Confirmed live in all 5 runs**, one 422 per candidate (Womack shows twice
because it ran twice, both times with the identical error):

| Candidate | Committee | 422 confirmed |
|---|---|---|
| Thomas Massie | (H2KY04121's committee) | yes |
| Ed Gallrein | C00923995 | yes (reproduced twice, byte-identical error) |
| Steve Womack | C00477745 | yes (both runs) |
| Lauren Boebert | C00728238 | yes |
| Mike Collins | C00544684 | yes |

**Impact:** because 422 hits on page 2 (not page 1), `fetch_schedule_a`
takes the "later page failing" branch, which stops pagination and returns
whatever page 1 alone contained (up to 100 raw rows) instead of continuing
toward the intended `max_pages=10` (up to ~1,000 rows). Concretely, for
every candidate here, the "top N of M donors" figure and
`largest_contributions_total` reflect **only the single largest page of
itemized contributions**, not the fuller largest-donor picture the
`max_pages=10` cap was designed to build. (Sorted largest-first, page 1 does
still contain the single largest individual contributions system-wide, so
the very top of the list is likely still correct — but any donor whose
total sits between what fits on page 1 and what would have fit across 10
pages is silently missing, and the app has no way to know or say so.)

Scope, precisely — **this bug does NOT affect**:
- Schedule E (outside spending) — its pagination is already written
  generically (`cursor = pagination.get("last_indexes") or {}`, keys used
  as-is), which is exactly the pattern that avoids this bug class. Verified
  clean across all 5 runs: `sum(spender.count) == len(transactions)` and
  `incomplete=False` for every candidate, i.e. Schedule E was fully
  complete and correctly self-consistent every time.
- Hop-2 tracing (`trace_spender_donors`) — it calls `fetch_schedule_a`
  uncapped (`max_pages=None`), which uses date-sort, so its cursor field
  really is `last_contribution_receipt_date` and works correctly.
- The refund-netting pull — it's capped at `max_pages=1`, so it never
  requests a second page and never trips this bug, even though it's also
  amount-sorted.

**This bug DOES affect**: the primary "top donors" capped pull used by
every live candidate search — i.e., it's universal, not an edge case.

## A second, compounding bug: even a correctly-flagged truncation never reaches the user

Independent of the above — `fetch_schedule_a` *does* correctly return
`(records, truncated, stop_reason)` on this kind of failure, and
`RecipientGroup.truncated` / `truncated_reason` are populated and even
serialized by `fec.to_jsonable()`. But `app.py`'s `_summarize_direct()`
(the function that turns `to_jsonable()` output into `track_a_direct`)
**never reads `truncated` or `truncated_reason` at all** — they're dropped
before the dict is built. And the `sched_a` step in `_run_search`
unconditionally calls `s.ok(...)`; it never checks whether the pull came
back truncated, unlike the `sched_e` step just below it, which does check
`outspend.incomplete` and calls `s.warn(...)` when set.

Confirmed directly: none of the 5 saved `track_a_direct` JSON blocks contain
a `truncated` key anywhere, despite the underlying pull being truncated in
every case.

**Why this matters beyond the specific 422 bug above:** even after that
cursor bug is fixed, *any* future Schedule A failure that legitimately
exhausts retries on a later page (a real, sustained 429 or 5xx run, which
this specific audit never triggered but which `fec.py`'s own comments say
has happened before — see the Talarico $3,911.59-of-$millions incident
described in `fec.py`'s docstring) would **still** report a clean `ok` on
the `sched_a` step, because the disclosure pathway itself is broken, not
just this one trigger. This is the more architecturally important half of
the finding: the plumbing that's supposed to carry "this is incomplete" all
the way to the user has a gap in it specifically for Schedule A (Schedule E
does not have this gap).

## Recommended fix (for a future session — not applied here per instructions)

1. In `fetch_schedule_a`, don't hardcode `last_contribution_receipt_date`.
   Read the cursor generically the way `outside_spending_for_candidate`'s
   Schedule E pagination already does (`cursor = pagination.get(
   "last_indexes") or {}`, pass its keys straight back as params) — or at
   minimum, branch on the actual `sort` value in use and read/send
   `last_contribution_receipt_amount` when sorted by amount.
2. In `_summarize_direct` (app.py), aggregate `truncated`/`truncated_reason`
   across groups (an `any_truncated` + joined reasons, mirroring what
   `search_fec_candidate` already does across committees) and surface both
   in the returned dict.
3. In the `sched_a` step (`_run_search`), check that aggregated
   `truncated` flag and call `s.warn(...)` instead of `s.ok(...)` when set —
   mirroring the `sched_e` step's existing `if outspend.incomplete: s.warn(...)
   else: s.ok(...)` pattern exactly.
4. Add a regression test (offline fixture: a page-1 response whose
   `last_indexes` only contains an amount key, no date key) asserting the
   correct cursor param is sent on the next request and that a forced later
   -page failure surfaces as `truncated=True` all the way through
   `_summarize_direct` and into a `warn` step, not an `ok`.

## Per-candidate step summary (all 5 — no crashes, no silent drops beyond the Schedule A issue above)

| Candidate | resolve | sched_a | sched_e | composition | record | votes | statements | synthesize |
|---|---|---|---|---|---|---|---|---|
| Thomas Massie | ok | ok* | ok | ok | ok | warn (disclosed cap) | warn (disclosed) | warn (no key) |
| Ed Gallrein | ok | ok* | ok | ok | warn (no bioguide — challenger) | warn (needs record) | warn (disclosed) | warn (no key) |
| Steve Womack | ok | ok* | ok | ok | ok | warn (disclosed cap) | warn (disclosed) | warn (no key) |
| Lauren Boebert | ok | ok* | ok | ok | ok | warn (disclosed cap) | warn (disclosed) | warn (no key) |
| Mike Collins | ok | ok* | ok | ok | ok | ok | warn (disclosed) | warn (no key) |

`ok*` = reported as a clean `ok`, but per the finding above, was actually a
silently truncated pull (this is the bug — it should have been a `warn`).
Every other `ok`/`warn` in this table is genuine and correctly disclosed.

## Money/donor completeness check (beyond the truncation bug itself)

For everything that *did* make it out of `fetch_schedule_a`, nothing was
dropped further downstream: `donor_count == len(pulled_donors)` for all 5
candidates, and for Schedule E, `sum(spender.count) == len(transactions)`
for all 5, with `incomplete=False` in every case (the openFEC #3396
pagination cross-check passed cleanly every time). Memo re-itemizations and
refund netting were disclosed on every candidate that had any (Gallrein,
Boebert, Collins) exactly as `CLAUDE.md` describes.

## Note on this audit's own instrumentation

To actually observe `X-RateLimit-Remaining` (the app discarded all response
headers before this audit — `fec.py`'s `_http_get` only ever returned
`(status, body)`), I temporarily added an opt-in logging hook to `fec.py`,
gated entirely behind an unset-by-default env var (`FEC_HTTP_LOG`): zero
effect on normal operation, no behavior change, just an optional
append-only JSONL write of header/status/query (API key redacted) when that
env var was set. It has since been **fully reverted** — `fec.py` is back to
its pre-audit state, verified by diff. The evidence it captured lives on in
`tests/rate_limit_run/fec_http_log.jsonl` (also API-key-redacted) and in
this file; nothing about the hook itself remains in the codebase.

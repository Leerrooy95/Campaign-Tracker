# Changelog

All notable changes to Campaign Tracker are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/) conventions: newest
release first, changes grouped under **Added / Changed / Fixed**, and every
entry stated as a fact a reader can verify against the code or a run.

## [1.2.1] — 2026-09-07

Driven by `Claude_Recommendations.md` — a 5-candidate live rate-limit/
completeness audit (Thomas Massie, Ed Gallrein, Steve Womack, Lauren
Boebert, Mike Collins) run one at a time through the real two-phase
resolve flow. No 429s occurred (`X-RateLimit-Remaining` never dropped
below 100/120), but the audit surfaced a 100%-reproducible silent-data-loss
bug in Schedule A pagination — not a rate-limit problem — plus a gap in how
a truncated pull gets disclosed.

### Fixed

- **`fec.py` — Schedule A's capped "top donors" pull silently truncated to
  page 1 on every candidate with more than 100 rows of itemized
  contributions.** `fetch_schedule_a`'s pagination cursor hard-coded the
  keyset's secondary field as `last_contribution_receipt_date`, which is
  only the right field name for a date-sorted query. The capped pull (and
  the `smallest_first` refund pull, once it grows past one page) sorts by
  `contribution_receipt_amount`, whose actual cursor field is
  `last_contribution_receipt_amount` — so the hard-coded lookup returned
  `None`, `str(None)` produced the literal string `"None"`, and every
  second-page request sent FEC `last_contribution_receipt_date=None`, which
  FEC correctly rejected with a 422. Because 422 hits on page 2 (not page
  1), the pull silently stopped and returned only page 1 — up to 100 raw
  rows — instead of continuing toward the intended `max_pages=10` (~1,000
  rows), with no error surfaced anywhere. Confirmed live, one 422 per
  candidate, in all 5 audit runs. Fixed by reading the cursor generically
  from `last_indexes` (whatever keys FEC actually returns) and re-sending
  them as-is — the same pattern `fetch_schedule_a`'s Schedule E sibling
  (`outside_spending_for_candidate`) already used, which is exactly why
  Schedule E was unaffected. Hop-2 tracing (uncapped, date-sorted) and the
  refund pull (capped at 1 page) were also unaffected by the bug itself.
- **`app.py` — a truncated Schedule A pull never reached the user.**
  `fetch_schedule_a` already returned `(records, truncated, stop_reason)`
  correctly, and `RecipientGroup.truncated`/`truncated_reason` were
  populated and serialized, but `_summarize_direct` dropped both fields
  before building `track_a_direct`, and the `sched_a` step unconditionally
  called `s.ok(...)` — unlike the `sched_e` step just below it, which
  already checks `outspend.incomplete`. So even a *legitimate* future
  Schedule A failure (a sustained 429/5xx run exhausting retries on a later
  page — the class of incident described in `fec.py`'s own Talarico
  $3,911.59-of-$millions docstring note) would still report a clean `ok`.
  `_summarize_direct` now aggregates `truncated`/`truncated_reason` across
  a candidate's committees, and the `sched_a` step warns (mirroring
  `sched_e`'s existing `if outspend.incomplete: s.warn(...)` pattern)
  instead of always reporting `ok`. The `composition` step's gate on
  `sched_a` now accepts a warn (`log.require("sched_a", allow_warn=True)`)
  so a disclosed-but-partial donor pull doesn't halt the
  record/votes/statements/synthesis stages downstream — composition
  doesn't consume the capped donor list itself (it re-pulls totals from
  FEC's own `/totals`), so there was nothing for that gate to actually
  protect, and `sched_e`'s incomplete flag was never allowed to block
  anything either.
- **`fec.py` — Hop-2 dark-money tracing (`trace_spender_donors`) silently
  stayed individuals-only, missing PAC-to-PAC transfers into a super PAC.**
  Flagged by a GitHub Copilot review comment on the PR for the fixes above,
  verified against the code and fixed the same day. `fetch_schedule_a`'s
  base request-params dict set `is_individual: "true"` unconditionally,
  ahead of the `if individuals_only:` branch a few lines down — so calling
  with `individuals_only=False` (exactly what Hop 2 does, on purpose, per
  its own docstring) never actually removed the filter. An empty Hop-2
  result was supposed to mean "this super PAC's money traces to dark
  money (a 501(c)(4))"; instead it could also just mean "this super PAC's
  incoming money happened to arrive from other committees, which the
  filter was hiding." Fixed by only setting the param inside the
  conditional, so it's omitted from the request entirely when
  `individuals_only=False`.
- Regression: `tests/test_schedule_a_pagination.py` — an amount-sorted
  fixture pins the correct cursor key on page 2 (and that a bare
  `...date=None` is never sent); a fixture confirms `individuals_only=False`
  omits the `is_individual` query param while the default still sends it;
  and an end-to-end run through `app._run_search` confirms a forced
  later-page failure surfaces as a `warn` on the `sched_a` step, not an
  `ok`.

### Changed

- Removed `_psycache_/` — a stray committed Python bytecode cache (9 `.pyc`
  files plus a leftover `template` placeholder) that had no business under
  version control; tracked in error the same way `User_Runs/` was in 1.2.0.
  Added `.gitignore` (previously absent from this repo) covering
  `__pycache__/`, `*.pyc`, and `.synthesis_cache/` so neither can creep
  back in.

## [1.2.0] — 2026-08-08

Prepared for open-source release. No changes to the factual pipeline or the
synthesis/reconciliation layer in this release — this is packaging and
hardening for people other than the operator to clone and run it.

### Added
- `docker/searxng/` — a ready-to-run, single-container docker-compose setup
  for the SearXNG instance `statements.py` needs. Previously there was no
  bundled way to stand one up, and a stock SearXNG install doesn't enable the
  JSON output format the tool requires either way, so a `settings.yml`
  override ships alongside it. Deliberately minimal (no reverse proxy, no
  Valkey, bound to `127.0.0.1`) — this instance only ever answers this
  project's own local queries, not public traffic.
- `LICENSE` (MIT).

### Changed
- `app.py` — the dev server's `debug` flag now defaults to `False`. Werkzeug's
  debug mode ships an interactive in-browser debugger that allows arbitrary
  code execution to anyone who can reach it — not a safe default for a tool
  other people will clone and run themselves. Opt in locally with
  `FLASK_DEBUG=1`.

### Removed
- `User_Runs/` — 62 files (~24MB) of the operator's personal test-run
  PDFs/PNGs/ZIPs against real candidates, tracked in error. Added to
  `.gitignore` so it can't creep back in, along with `synthesis.py`'s
  generated `.synthesis_cache/`.
- Dormant "opponent / race comparison" frontend code (`static/app.js` and
  `static/app.css`: an unused `#opponent` field lookup, the race-panel
  renderer and its collapse toggle, and their CSS). This was committed while
  the feature was being prototyped in a separate track and never got a
  working backend — `opponent.py` doesn't exist and `app.py` never read an
  `opponent_name` from the request, so the code was unreachable behind a
  `<div>` that was never added to `templates/index.html` either. Nothing
  observable changes by removing it: verified with a real headless-browser
  run through the full pipeline (zero JS exceptions, every other panel
  renders as before). It may come back properly wired up in a future release;
  until then the repo shouldn't carry code implying a capability that doesn't
  exist.

**How to update this file:** when you change the tool, add your changes under
an `## [Unreleased]` heading at the top as you go; when you cut a release,
rename that heading to the new version + date. Cite the run number when a
change was driven by a real run's output (the repo's convention — e.g.
"Run 19" — so every fix traces to the evidence that motivated it).

---

## [1.1.0] — 2026-07-23

Two targeted improvements driven by Run 20 — the first run against a House
member (Thomas Massie, KY-04) and the largest legislative record the tool has
processed (966 actions). Contributed as an Opus 4.8 patch set
(`Updates/`), reviewed, verified against the real Run-20 export, and
integrated; the design message rides in
`archive_superseded/Opus_4.8_Message.md`.

### Fixed

- **reconcile.py — a committee's own name can no longer fake a side cue
  (Run 20).** DEFEATING COMMUNISM PAC spent $822,500 **supporting** the
  candidate, but "DEFEATING" in the PAC's own name matched the "defeat"
  oppose cue, so a segment that merely *named* the PAC read as an oppose
  claim and the guard raised a false high-severity "wrong side" flag on a
  correct report. Committee names are political slogans and routinely carry
  cue words ("Stop …", "Defend …", "… Victory Fund"); a cue inside a proper
  noun is never a directional claim. `check_spender_sides` now masks every
  spender's name out of a segment before reading its side cue (longest
  variant first, so nothing is left behind) — name lookup still runs on the
  unmasked text, and a real "opposing X" verb outside the name still
  triggers. Re-running the guard over the actual Run-20 report: 1 high → 0;
  a genuine inline flip on the same spender is still caught. Regression:
  `tests/test_reconcile_sides.py` (§7, verified to fail on the pre-fix
  code).

### Changed

- **synthesis.py — the digest no longer ships the full legislative record to
  the model (`_trim_record_for_digest`).** The report's Legislative Record
  section uses exactly two things: the money-related bills and the per-year
  counts. But the digest carried *every* action's title — on Run 20 that was
  966 items, 381,182 of the record block's 381,369 characters, ~95,000
  tokens the model read and discarded (63% of a 605KB digest; the wrong
  shape of input for a model with documented figure-dropping under load,
  Run 18 vs 19). Non-money items are now dropped from the **model's view
  only**: every count survives byte-identical (so the per-year line is
  unchanged and the content hash stays sensitive to the full record — any
  new bill still busts the cache), every money-related item survives, a
  `digest_note` discloses the filtering in-band so the model can't misread
  the short list as the whole record, and the untrimmed record stays in the
  result JSON and every CSV export. Measured on the real runs: Massie
  605,228 → 161,843 chars (−73%), Ossoff 499,058 → 109,178 (−78%).
  Existing `.synthesis_cache` entries predate the digest change and simply
  miss (new hash); clear the directory to reclaim the space.

### Added

- `tests/test_digest_trim.py` — 28 assertions pinning the trim's three
  invariants: counts survive, money items survive, the result is never
  mutated (audit trail intact); plus hash sensitivity and degenerate inputs.
- `tests/test_reconcile_sides.py` §7 — the Run-20 committee-name-cue pin
  (added at integration; the patch set shipped without one).

---

## [1.0.0] — 2026-07-23

The first version validated end to end against real data: nineteen live runs
against a sitting U.S. senator's actual FEC, Congress.gov, and Senate LIS
records, with every defect found along the way pinned by an offline regression
test (~240 Python checks + ~47 export checks, all green at tag time). v1.0 is
the point where the operator is confident the tool returns good, legitimate
data.

### What the tool is at v1.0

A federal-first, facts-not-scores tracker that lays a candidate's **money**
(FEC) next to their **record** (Congress.gov bills + House/Senate roll-call
votes) and their **words** (web-searched public statements), on one
deterministic timeline, with every line citing a primary source. There is no
scoring, no verdict, and no correlation number — the reader judges. One
optional interpretive step (a plain-language report) is machine-checked
against the data before it is shown.

### The pipeline (eight gated steps)

`resolve → sched_a → sched_e → composition → record → votes → statements →
synthesize`, driven by a visible StepLog: each step is a checkpoint later
stages gate on, failures halt visibly, and every caveat rides into the result
as a disclosed warning. Demo mode runs the whole path on synthetic fixtures
with no keys and no network.

### Track A — the money (FEC)

- **Pipe 1, direct money:** per-cycle itemized (big-donor) vs unitemized
  (small-dollar) split plus PAC contributions, from `/totals`. The biggest
  individual donors come from a capped, largest-first Schedule A pull.
  Donor figures are **memo-filtered** (`memo_code="X"` re-itemizations are
  restatements of money already counted — 39% of rows in one real pull — and
  are dropped) and **refund-netted** (negative rows netted against donor
  totals); both corrections disclosed.
- **Pipe 2, outside spending:** Schedule E independent expenditures for and
  against the candidate, **notice/amendment-deduplicated** (the same
  expenditure legally appears twice — 24/48-hour notice + regular report;
  raw sums ran ~2× high on real data) and reconciled to the cent against
  FEC's own by-candidate aggregate. Notice-only dollars are kept, tagged,
  and disclosed. A **two-hop trace** follows super-PAC money to its donors
  and flags the dead-end at dark money — the dead-end itself is a finding.
- **Per-cycle composition** is two-sourced: the current cycle reuses the
  deduped per-spender detail (matches the chart to the cent); prior cycles
  take for/against from FEC's authoritative aggregate (row-level dedup does
  not scale to old high-volume filings). The traceable/dark split covers
  both support and oppose, and is `null` — never fabricated — where
  per-spender detail doesn't exist.

### Track B — the record

- **Bills** (`congress.py`): sponsored + cosponsored legislation, dated,
  cited, money-tagged by a whole-word regex term list with **opt-in
  plurals** — tuned against 2,506 real Senate vote titles and 863 real bill
  titles. Bare `donor`/`ethics`/`transparency`/`corruption` are gone (each
  removed after a documented real false positive; `anti-corruption` and
  domestic compounds deliberately kept so S. 1 / S. 2093-class bills still
  tag). Patterns are `(display_term, regex)` pairs; one matcher object is
  shared with the votes module so the two can never drift.
- **Roll-call votes** (`votes.py`): House via the Congress.gov beta
  house-vote API (118th Congress/2023+, legislation-linked votes only —
  the API's coverage, disclosed in a `coverage_note`), member position by
  bioguideId; Senate via LIS XML (no key), member matched by last name +
  state, default lookback 5 congresses (~10 years). Bounded on purpose:
  vote lists are scanned in full; expensive per-vote detail (bill-title /
  amendment-purpose lookups, member-position fetches) is spent newest-first
  on money-relevant votes under explicit caps, any cap hit disclosed as a
  lower bound. Failure honesty throughout: a failed lookup or fetch is its
  own disclosed count, an unreadable Senate XML is a fetch miss
  (`PARSE_FAILED`), and neither is ever conflated with "not on the roll" or
  silently passed off as "not money-related." Votes on the candidate's own
  sponsored/cosponsored bills carry `candidate_bill_role` — an exact-match
  join, a recorded relationship, not a judgment. Every vote cites its
  primary source (clerk.house.gov / senate.gov LIS).
- **Statements** (`statements.py`): SearXNG-backed collection across neutral
  query angles, full dated set, no cherry-picking. Real dates only (engine
  date, URL slug, or a capped page-fetch tier reading the page's own
  metadata); year-only hints kept separate; nothing guessed. Every
  statement is classified — the candidate's own voice vs coverage about
  them vs boilerplate — with a per-call reason, candidate-generalized (no
  hardcoded names/domains), and only own-voice statements reach the
  timeline. The **raw search log is persisted**: every engine result with a
  kept/dropped disposition, because live search isn't reproducible (two
  runs 19h apart shared only 40 of ~51 URLs) and the log is what makes a
  classifier regression distinguishable from corpus churn after the fact.

### The timeline

Merged money/record/votes/statements events, assembled **in code**
(`timeline.py`) — sorted newest-first with a total order, grouped into month
bands, verified by an invariant check. Ordering was once the model's job and
it mis-sorted adjacent dates in two real runs; it is not a judgment call and
is no longer entrusted to one.

### The report layer (optional, verified rather than trusted)

One Claude Sonnet 5 call restates the assembled facts in plain language —
if and only if the user pastes an Anthropic key into the UI (transient:
request-scoped, never stored, logged, or echoed; blank field = the tool is
byte-for-byte the factual version). Three structural safeguards:

1. **Machine-rendered number blocks.** The per-cycle Money Picture and the
   per-spender ledger are built deterministically from the JSON and swapped
   in for placeholders the model writes — the model states no per-cycle
   figures and no spender sides at all, so a dropped figure (Run 18 vs 19)
   or a flipped side (Run 13) is impossible rather than merely detectable.
2. **Reconciliation guard** (`reconcile.py`): deterministic post-check of
   the remaining prose against the JSON — spender sides, oppose-direction
   attribution, cycle totals restated, dollar provenance (rounded-variant
   matching: 1–6 significant figures + $1, not a percentage band that
   scales into a hole), caveat preservation. High-severity findings ride
   into the caveats; the guard is non-fatal and can only add disclosure.
3. **Bounded HTTP**: the API call runs under a per-attempt wall-clock
   deadline in a daemon thread (covers DNS stalls and slow-drip bodies),
   one retry on transient classes only. A hung network can no longer hang
   a run.

### The frontend

Single page, no framework. Live step trace; "Did You Mean?" candidate picker
(exact FEC id confirmed before the heavy run; nicknames broaden by last
name); four inline-SVG charts (funding composition, per-spender outside
spending with dark money hatched, cross-cycle outside trend, timeline
swimlane with vote diamonds vs bill circles, honest "⋯" gap compression,
opening scrolled to the newest end — and print CSS that scales it onto the
page instead of clipping the newest months off the PDF); a roll-call votes
ledger with position badges, "their bill" tags, and primary-source links;
client-side data export — **ten CSV tables** (donors, donor transactions,
outside spenders, spender transactions, composition, legislative record,
roll-call votes, statements, statement search log, timeline) plus a
dependency-free ZIP (CSVs + full result JSON), built entirely in the browser
with no new server routes; the report as rendered markdown; the raw JSON
collapsed behind a toggle and excluded from print.

### Testing

Offline, plain-Python (plus one Node/ZIP round-trip), no framework, no
network, no keys: `python3 tests/<file>.py`. Eleven test files covering the
Schedule E dedup (to-the-cent against FEC's aggregate), Schedule A
memo/refund corrections, composition two-sourcing, candidate resolution,
statement classification, bounded synthesis HTTP (mock server), roll-call
votes (fixtures modeled byte-for-byte on real senate.gov XML), the
deterministic spender ledger, reconcile's side checks against a real run's
report, the full export path (CSV quoting, all ten tables, ZIP byte-verified
by Python's stdlib), and the Run-19 defect pins.

### Known limits (disclosed in-product, listed here for the record)

- House votes: 118th Congress (2023)+ and legislation-linked only — the
  beta API's coverage. Pre-2023 House votes would need the Clerk's EVS XML
  (deliberately deferred until the format can be verified live).
- Non-legislation House votes (e.g. Speaker elections) aren't in the API.
- Per-vote detail is capped (newest-first) and discloses when a cap bites.
- The traceable/dark split exists only where per-spender detail does
  (current cycle); prior cycles carry for/against totals only.
- openFEC issue #3396 (Schedule E pagination under-return) is cross-checked
  and surfaces as a disclosed lower bound when it bites.
- Statements depend on a self-hosted SearXNG; the search corpus itself
  churns run to run — which is exactly why the raw search log is persisted.
- `export.py` (server-rendered report artifact) and collapsible report
  sections are not built.

### Provenance of this release

Defects found in real runs and fixed on the way to v1.0 — each with a
regression test — include: Schedule E notice double-counting (~2× oppose
totals), Schedule A memo re-itemization (a donor shown at twice his true
total) and un-netted refunds, prior-cycle outside-spending over-counts,
model-side timeline mis-sorting, a spender narrated on the wrong side, an
oppose total attributed to "presumably the opponent," dropped Money Picture
figures between identical-data runs, a reconcile regex that corrupted
figures 1000× by eating the first letter of the next word, a 5% provenance
band wide enough to hide six-figure errors, a Haiti disaster-recovery vote
tagged as money-relevant by a bare "corruption" term, plural forms the word
boundary silently missed, API-failure states conflated with "member not on
the roll," and a raw search log that would have leaked deliberately-dropped
content into the model's prompt while silently defeating the report cache.
Every one of those is now a pinned test.

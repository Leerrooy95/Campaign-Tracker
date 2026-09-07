# CLAUDE.md — Campaign Tracker

Context for AI assistants (and humans) working on this codebase. Read this
before touching anything. *Campaign Tracker* is a working name.

---

## What this is

A federal-first tool that lays a candidate's **money** next to their
**legislative record**, factually, so a reader can judge whether the two line up.

It is **not** a "who's bought" detector and **not** the 50-state donor portal
(different repo). The question:

> Is a candidate's stance on money in politics a **principle** (it shows up in
> what they do) or a **position** (they object to who's winning, not the money)?

**The tool does not answer that — it assembles the evidence and the reader
decides.** There is no scoring, no correlation coefficient, no verdict. This is a
deliberate design choice (see Integrity Rules): a score is an interpretation that
can be biased and can't be verified; a sourced fact can be clicked and checked.

---

## If someone just asked you to help them set this up

This is the fast path — for "help me get this running," not "help me change
how X works." (For the latter, read on; the rest of this file is exactly
that context.)

1. **Check for `flask` and the other deps**: `pip install -r requirements.txt`
   (add `--break-system-packages` on Debian/Crostini — `python`/`pip` aren't
   symlinked there, use `python3`/`pip3` throughout).
2. **Offer the zero-setup path first**: `python3 app.py` with no keys set at
   all runs in **demo mode** on synthetic fixtures — no signup, no network,
   good enough to confirm the install works and show them what the tool
   does before they invest in real keys.
3. **For real data, two free keys, both instant self-service signups**:
   `FEC_API_KEY` (https://api.open.fec.gov/developers/) and
   `CONGRESS_API_KEY` (https://api.data.gov/signup/, same key system covers
   Congress.gov). Put them in `run.sh` (it has clearly marked slots) and run
   `bash run.sh` — or `export` them by hand and run `python3 app.py`.
4. **Statements (what the candidate said) need SearXNG — optional.** If they
   want that track, walk them through `docker/searxng/` (a ready-to-run
   compose file — `cd docker/searxng && cp .env.example .env`, paste a real
   `openssl rand -hex 32` secret into `.env`, `docker compose up -d`). If they
   don't want to bother with Docker, tell them it's fine to skip: that one
   stage is disclosed-and-skipped, money + legislative record + votes still
   run complete.
5. **The Anthropic key (the plain-language report) is UI-only**, pasted into
   a web field per search, never an env var, never stored — see "The
   translation layer is isolated by design" below before you tell them
   anything else about it.
6. If something fails, the **step log tells you exactly which stage and
   why** (`/status/<job_id>` in the API, or the live trace in the browser) —
   read the failed step's detail message before guessing; steps.py's whole
   design point is that a failure is never silent or ambiguous.

---

## The two-track model (both FACTUAL)

**Track A — the money (FEC).** Two distinct pipes, never conflated:

- **Pipe 1 — direct money (per cycle, from /totals).** Itemized (big-donor) vs
  unitemized (small-dollar) individual contributions, PLUS PAC money — folded in
  because "I refuse corporate PAC money" is often the exact claim under test.
  `fec.py: funding_by_cycle`.
- **Pipe 2 — outside spending (Schedule E).** Uncapped independent expenditures
  by super PACs, spent for or against the candidate. `fec.py:
  outside_spending_for_candidate` plus the **two-hop trace**
  (`trace_spender_donors`): Schedule E says "Committee X spent $5M for the
  candidate," then Schedule A on X says "Donor Y funded X." Closes for super PACs;
  **dead-ends at dark money** (501c4s) — that dead-end is itself a finding.

**Track B — the record (Congress.gov).** What the candidate actually *did*: bills
**sponsored and cosponsored**, dated, tagged for money/campaign-finance
relevance, bucketed by year (`congress.py`). Every item carries a citation
(congress + bill type + number → congress.gov URL). Pulled from the free official
Congress.gov API. **Facts, not scores.**

**Track B also includes STATEMENTS (`statements.py`):** what the candidate said
on money/influence, gathered by web search (SearXNG, free/self-hosted) across
neutral angles, returned as a full dated set — never pre-filtered to the damning
quotes. Statements and bills are both *factual data*. The interpretive "rhetoric"
pass (a model reading the whole assembled picture for nuance) is a **final
synthesis layer**, built last, against real data — NOT a collection-time score.

There is intentionally **no overlay/correlation step** at collection time. The
tracks are gathered; the synthesis/report layer (synthesis.py, now built)
restates them side by side in plain language; the reader concludes.

---

## Integrity Rules (the part that matters most)

1. **Facts, not scores.** Never assign intensity, sentiment, framing, or a
   consistency grade. The tool's job is to assemble sourced facts. The moment you
   reach for a number that represents a judgment, stop — that's the bias-prone
   path this design exists to avoid.
2. **Every line cites a primary source.** An FEC `sub_id` for money, a
   congress.gov bill number for an action. If it can't be cited, it doesn't ship.
   This is also how the tool is tested for bias: click through and verify.
3. **Pipe 1 ≠ Pipe 2.** A capped direct gift and an uncapped super-PAC buy are
   different sizes; never collapse them. Keep provenance separate ("gave to
   candidate" vs "spent independently" vs "did X in office" are distinct claims).
4. **Disclose incompleteness; never fake.** A pagination shortfall (#3396), a
   skipped stage (no Congress key), or a challenger with no record is surfaced as
   a warn — never papered over with a zero or an invented value.
5. **The report restates, never embellishes — and is now machine-checked.** The
   report layer only restates provided, cited facts (the Political_Translator
   constraint: all facts and numbers preserved from source). This is no longer
   trust-the-prompt alone: `reconcile.py` runs after synthesis and checks the
   prose back against the JSON — spender support/oppose side, cycle totals,
   dollar provenance, caveat preservation — and rides any contradiction into the
   caveats. It is a backstop, not a license to stop spot-checking by hand. AI as
   translator of cited facts — never as judge.
6. **Visible process = self-check. Never bypass a gate.**

---

## File map

| File | State | Notes |
|------|-------|-------|
| `fec.py` | **done** | Track A. Schedule A (Pipe 1) + Schedule E (Pipe 2) + two-hop trace + per-cycle composition (`funding_by_cycle`, itemized/unitemized/PAC from /totals). Deduped by `sub_id`, paced/retried under FEC's 120/min cap, #3396 cross-check. **Schedule E totals are notice/amendment-deduped (`_dedup_schedule_e` — read its docstring before touching Pipe 2):** raw rows list the same expenditure twice (24/48-hour notice + regular report, different `sub_id`s), which overstated a real Ossoff 2026 run's oppose total ~2x ($934,835.78 vs the true $480,063.37). Dedup drops memo rows, superseded amendments, and notice rows matched by a regular row; notice-ONLY rows (freshest spending, regular report not yet processed) are kept, tagged `notice_only`, and disclosed — verified to the cent against FEC's own `/schedules/schedule_e/totals/by_candidate/` aggregate (`tests/test_schedule_e_dedup.py`). `funding_by_cycle` inherits the fix (it reuses `outside_spending_for_candidate`), so ALL cycles' outside numbers corrected, incl. 2020's. **Per-cycle composition totals are two-sourced (post-Run-12):** the current cycle reuses the already-pulled per-spender detail (deduped, includes disclosed notice-only, matches the "by group" chart to the cent), while prior cycles take support/oppose from FEC's authoritative `outside_totals_aggregate` (`/schedules/schedule_e/totals/by_candidate/`, `election_full=false`) — one call each instead of paginating tens of thousands of rows. This fixes a historical over-count (the row-level exact-match dedup doesn't scale to old high-volume filings — 2020 was ~$9.5M oppose / ~$11M support too high vs FEC) AND removes the ~260s composition bottleneck. The traceable/dark split now covers BOTH support and oppose (was support-only — it hid all opposition dark money) and is `Optional`: computed only for the current cycle (needs per-spender detail), `null` for prior cycles, tagged by `outside_totals_source` ("spenders" | "fec_aggregate"). **Transaction-level rows are retained for export:** `RecipientGroup.transactions` (per-contribution Schedule A receipts, via `_schedule_a_txn`) and `OutsideSpending.transactions` (per-expenditure deduped Schedule E rows, via `_schedule_e_txn`, newest-first with `notice_only` flags), both serialized so the CSV export reaches individual-transaction grain. **Schedule A totals are memo-filtered and refund-netted (post-Run-16):** `memo_code="X"` rows are re-itemizations of money already counted (a later report restating an earlier receipt) and are dropped — `sub_id` dedup can't catch them because the restatement gets a NEW sub_id (real Collins-2026 data: Brent Scarbrough showed $21,000 vs a true $10,500, and **39 of 100 rows in a live pull were memos**). Refunds are NEGATIVE rows that the largest-first capped pull never reaches, so one extra ascending page (`smallest_first=True`) nets them against donor totals (Guy Millner: $14,000 gross → $7,000 net); memo-coded negatives are skipped since redesignation pairs already net to zero, and a donor netted to ≤0 is dropped rather than shown negative. Both corrections are disclosed (`memo_skipped`, `refunds_applied`, `refund_total`) on the step line and in the JSON. Regression: `tests/test_schedule_a_corrections.py`. |
| `congress.py` | **done** | Track B *record*. Resolves name→bioguideId via FEC state + last name, pulls sponsored + cosponsored legislation, tags money/finance relevance by **whole-word regex** on the title, buckets by year. Factual; cites every bill. Honest about the votes gap. Uses **curl_cffi** to clear api.congress.gov's Cloudflare bot-check (fast-fail fallback). |
| `votes.py` | **done** | Track B *votes* — the other half of "what they did." House roll calls via the Congress.gov **beta** house-vote API (`/v3/house-vote/...`, 118th Congress/2023+ and legislation-linked votes only — the API's coverage, disclosed in `coverage_note`, not a choice), member position matched by **bioguideId** from the `.../members` sub-endpoint; Senate roll calls via **LIS XML** (menu per congress-session + per-vote XML, no key), member matched by **last name + state** (LIS has no bioguideId). Money/finance relevance is tagged by the SAME whole-word regex as congress.py (imports `_money_match` — one term list, no drift). The pull is bounded on purpose: full vote lists are scanned cheap, but per-vote detail (House bill-title lookups — the list has no titles — and member-position fetches) is spent newest-first on money-relevant votes under explicit caps; a hit cap sets `incomplete` + reason (Rule 4). A member absent from a roll is counted (`not_in_roll`), never invented; a FAILED lookup or member-position fetch is disclosed as its own incomplete reason (never conflated with not-on-roll, never silently passed off as "not money-related" — both chambers; an unreadable Senate per-vote XML returns the `PARSE_FAILED` sentinel and counts as a fetch miss, not an absence); unparseable LIS dates return `""`, never a guess. Each vote carries a citation + primary-source URL (clerk.house.gov / senate.gov). **House HAMDT votes tag on the amendment's purpose/description** (fetched via `/amendment/{congress}/hamdt/{n}`, memoized, same lookup budget as bill titles — money riders live in the purpose, not the author string). **Senate default lookback is 5 congresses (~10 years, matching the composition window)** — LIS menus are cheap; the detail caps still bound the expensive part. **`link_votes_to_record` (factual join, exact congress + normalized bill identity — "S. 512"/"S.J.Res. 5"/"HR 9500" vs record citations):** sets `candidate_bill_role` = sponsored/cosponsored when a vote is on the candidate's OWN bill — a recorded relationship, not a judgment; no fuzzy matching, a miss means no claim. Feeds `track_b_votes`; timeline.py places money-relevant votes on the record track (linked votes say "a bill they cosponsored"). Regression: `tests/test_votes.py` (offline fixtures). |
| `synthesis.py` | **done** | Translation layer (final stage). Single Claude Sonnet 5 call over the assembled result → plain-language cited report. Guardrails from Political_Translator (keep every number, preserve audit trail, no verdicts) + a provided-data-only rule. Restates the **pre-built `timeline`** band-by-band in order (never re-sorts) and builds the statements section from the **`by_candidate`** slice only. Hash-cache per candidate, optional readability grade. Isolated: only runs if a key is posted from the UI; failure warns and never touches the factual result — API errors are parsed to a clean one-line caveat (`_friendly_api_error`), no raw JSON/`request_id` blob. **The HTTP call is hard-bounded (post-Run-10):** each attempt runs in a daemon thread with a wall-clock deadline (`ATTEMPT_DEADLINE`, 130s) *on top of* the per-socket-op `HTTP_TIMEOUT` (120s) — the thread bound covers DNS/getaddrinfo stalls urllib's timeout can't see (the likely Run-10 hang), and the body is read via `read1()` chunks against the same deadline so a slow-drip response can't slide past a per-recv timeout (plain `read(n)` blocks until n bytes — a real bug caught in the mock tests). One retry on transient failures only (timeout / network / HTTP 429 · 500 · 504 · 529, the classes Anthropic's error docs mark retryable; `Retry-After` honored up to 30s); 400/401/403/413 fail immediately. Worst case ≈ 2×130s + backoff, vs. unbounded before. **Also runs the `reconcile.py` guard on its output** (via `_safe_reconcile`, even on cache hits) and returns a `reconciliation` block; the guard is wrapped so a guard failure can never break the report. **The prompt covers roll-call votes** (a required section restating each `track_b_votes` position VERBATIM — never translated into policy support/opposition or a consistency call — plus the coverage limits in the caveats), and `track_b_votes` is in `_TRACK_KEYS`, so the model actually sees the votes and the cache hash invalidates when only the votes change. **The per-cycle Money Picture is now DETERMINISTIC too (`build_money_picture` + `[[MONEY_PICTURE]]` placeholder, post-Run-19):** Runs 18 and 19 produced materially different Money Pictures from identical composition data (Run 19 dropped eight absolute figures for percentages; reconcile can't flag figures that VANISH), so the per-cycle breakdown — receipts, itemized/unitemized dollars AND shares, PAC, outside for/against, traceable/dark where real — is machine-rendered and swapped in like the ledger; the model writes one no-numbers lead-in sentence. `money_picture` meta key records the path. Cached pre-feature reports need `.synthesis_cache` cleared to regenerate. **The per-spender ledger is likewise DETERMINISTIC (`build_spender_ledger` + `insert_spender_ledger`):** the prompt forbids the model from writing per-spender support/oppose lines and has it drop a `[[SPENDER_LEDGER]]` placeholder instead, which `synthesize()` swaps for a machine-built markdown ledger (sides exactly as filed, largest-first, dark flagged, notice-only disclosed, totals line) BEFORE caching and reconciliation — the side-flip failure mode reconcile.py existed to catch is deleted at the source, not just patrolled (the guard still runs as backstop over the model's connective prose). Placeholder missing → ledger is inserted at the end of Money Picture (before `## Legislative Record`); no spenders → stray placeholders stripped. `report_meta`-adjacent `spender_ledger` key records which path ran. Regression: `tests/test_spender_ledger.py`. **The digest trims the legislative record to its money-related slice (`_trim_record_for_digest`, post-Run-20):** the report's Legislative Record section uses only the money-related bills and the per-year counts, but the digest shipped every action's title — 966 items / ~95K discarded tokens on the Massie run (63% of a 605KB digest; the wrong input shape for a model with documented figure-dropping under load, Run 18 vs 19). Non-money items are dropped from the MODEL'S VIEW ONLY: every count survives byte-identical (per-year line unchanged; content hash stays sensitive to the full record, so any new bill still busts the cache), money-related items survive, a `digest_note` discloses the filtering in-band, and the untrimmed record stays in `result` for the JSON/CSV exports (`build_digest` never mutates). Massie 605,228 → 161,843 chars. Regression: `tests/test_digest_trim.py`. |
| `reconcile.py` | **done** | Deterministic guard over the synthesis output — the machine check behind Integrity Rule 5. Four checks: spender support/oppose **side** vs FEC data (severity high), cycle **totals** restated (high), **dollar provenance** — every $ figure in the prose traces to a source number (review), **caveat preservation** (review). Pure stdlib `re`, no API, no model. Non-fatal: findings are caveats, never failures. Conservative — only flags clear contradictions. **Committee names are masked before a segment's side cue is read (`_mask_committee_names`, post-Run-20):** "DEFEATING COMMUNISM PAC" ($822,500 SUPPORT) got a false high because "DEFEATING" in its own name matched the "defeat" oppose cue — a cue inside a proper noun is never a directional claim, and masking every name also stops one spender's name injecting a cue that binds to a different spender in the same segment. Name lookup still uses the unmasked text; a real "opposing X" verb outside the name still triggers (pinned in `tests/test_reconcile_sides.py` §7). See specifics below. |
| `statements.py` | **done** | Track B *statements*. Web-search-backed (SearXNG via `SEARXNG_URL`) collection of what they SAID on money/influence — neutral multi-angle queries, full dated set + coverage report, no cherry-picking, no scoring. Works for anyone (not just members). Offline fixtures for tests. **Two things worth reading the specifics for: the date model (real-date-only + `year_hint` + page-fetch tier) and the his-voice classifier (`by_candidate`/`about_candidate`/`boilerplate`).** The classifier is **candidate-generalized** — `collect_statements(office=…)` builds a `build_candidate_context` (official domain derived as `<lastname>.senate.gov/.house.gov`; rhetoric actor = the candidate's name + pronouns, with a bare-pronoun match requiring the name also present so an opponent's "he/she said" isn't misread). No per-candidate table; `ctx=None` preserves the legacy Ossoff path. Rhetoric matches are guarded against **enumerations and adjectival participles** (`_is_false_rhetoric`) — a tracker blurb like "…corporate donors, proposed legislation and more" matched name+"proposed" and was reported as the candidate's own voice in Run 16; third-party trackers (quiverquant, ballotpedia, votesmart, followthemoney, legistorm) are now `_DATA_DOMAINS`. When ZERO statements land in the candidate's own voice, `_own_voice_note` explains WHY in method terms (challengers have no `<lastname>.house/senate.gov`, so the official-release path can never fire; social snippets are login walls) — otherwise "0 in his own voice" reads as a fact about the person rather than a limit of the search. It rides into the step caveats. **The raw search log is persisted (post-Run-19):** live search isn't reproducible (Runs 18/19, ~19h apart, shared only 40 of ~51 URLs — own-voice count moved 11→6 from corpus churn alone), so every engine result is recorded in `search_log` with a disposition (kept / dropped_no_url / dropped_excerpt_too_short / dropped_duplicate_url / dropped_near_duplicate_text / query_failed) + `searched_at`, serialized into the result and exported as its own CSV. **Deliberately EXCLUDED from the synthesis digest** (see `synthesis.build_digest`): the log contains rows the pipeline dropped, and showing them to the model would invite the report to quote excluded content; `searched_at` would bust the content-hash cache every run. (NOTE: `parked/statements.py` is the OLD scored version — different file.) |
| `timeline.py` | **done** | Deterministic Side-by-Side assembler. Merges money (spenders + top donors), record (money-bills), and *his-voice* statement events onto one timeline, **sorted newest-first in code and grouped into month bands** — ordering is not the model's job (it mis-sorted adjacent dates in real runs). Pure function of the tracks; safe on partial results. See specifics below. |
| `steps.py` | **done** | The StepLog spine — visible process = self-check. |
| `app.py` + frontend | **done** | Server, async job worker, live `/status` polling, demo mode. Builds the deterministic `timeline` into the result (before audit/synthesis) and surfaces the statements class split in the step line ("N his voice on timeline, M coverage/boilerplate held off"). Reads the synthesis `reconciliation` block and rides high-severity findings into the audit/caveats through the `synthesize` step's warn (review-level items are noted on the ok line); stores `report_reconciliation` for export. Frontend renders four inline-SVG charts straight from the result JSON: funding composition, outside spending (current cycle, per spender), a **cross-cycle outside-spending trend** (for/against paired bars per cycle from `track_a_composition` — prior cycles are FEC's own aggregate, current matches the per-spender chart; ⚠ marks disclosed lower bounds), and a **timeline swimlane** (`renderTimelinePanel`) — three lanes (money / record / statements) over the SAME month bands timeline.py built, a pure re-render of `timeline.bands` with no re-sorting: bills are circles, roll-call votes are diamonds, months with no events compress to a "⋯" gap marker (the axis never fakes continuous coverage), >8 events in a band-lane become a "+N" count (nothing silently dropped), and every marker's tooltip is the event's own label + source. The swimlane scroller opens at the NEWEST end (bands run oldest→newest; the scroll must happen AFTER `panel.hidden = false` — a display:none element reports zero scrollWidth, so scrolling while hidden is a silent no-op, caught in a real headless run), and print CSS lets it scale onto the page (`overflow` clipping in print cut the newest months off the Run-19 PDF — the empty Money lane read as "no outside money"). Plus the report as markdown, and the raw JSON collapsed behind a `<details>` toggle (dropped from print). The outside-spending chart lays out spender labels by **measured text width** (canvas 2D metrics) with a reserved value-lane, so the largest bar's name/amount don't collide and long names don't clip off the left edge. A **Data-export card** sits between the caveats and the first chart: per-table CSV buttons — donors, **donor transactions (per contribution)**, outside spenders, **spender transactions (per expenditure)**, composition, legislative record, **roll-call votes (money-relevant, with position + citation URL)**, statements, timeline — plus a **Download-all ZIP** (CSVs + the full `result` JSON). A **roll-call votes ledger panel** (`renderVotesPanel`) sits between the charts and the report: one row per money-relevant vote with a Yea/Nay position badge, the citation as a link to the primary source (clerk.house.gov / senate.gov LIS), and the coverage/caps caveats votes.py disclosed restated in the footnote — deterministic render from `track_b_votes`, same no-model rule as the charts. It's entirely client-side (`static/export.js`) — built from the `result` the page already has, so NO backend route, NO server file storage, NO key exposure. CSVs are RFC-4180 quoted with a UTF-8 BOM; the ZIP is a dependency-free store-only writer, cross-verified against Python's stdlib in `tests/`. `app.py`'s `_summarize_direct` adds `pulled_donors` (full ~70 capped-pull donors) and `transactions` (per-contribution receipts, largest-first, capped at 5000) so the export goes down to transaction grain, not just the ~25 shown on-page. **Candidate resolution is a "Did You Mean?" two-phase flow:** on Search the frontend first hits `POST /candidates` (fast, synchronous, creator's FEC key) → a ranked shortlist; the picker ALWAYS shows so the exact office/cycle is confirmed (a person can hold several candidate_ids — House vs Senate). Picking one starts the run via `POST /search` with an explicit `candidate_id`, which the resolve step trusts and `sched_a` scopes to via `only_candidate_id`. Nicknames FEC can't match ('Mike'→'Michael', zero hits) broaden to the last name and rank by first-name similarity — no alias table. The Anthropic key is read+cleared on Search and held in a closure var only until the run starts. |
| `tests/` | **started** | Plain-python, offline, no framework: `python3 tests/<file>.py` from the repo root. `test_schedule_e_dedup.py` — the notice/report double-count regression, frozen real Ossoff-2026 rows in `tests/fixtures/`, asserts to-the-cent reconciliation against FEC's own aggregate. `test_synthesis_http.py` — mock-server test of the bounded/retrying Anthropic call (success, 529-retry, 401-no-retry, hang, slow-drip); ~30s, no key, nothing leaves the machine. `test_composition_sources.py` — funding_by_cycle's two-source design: current-cycle reuse (no double-pull), both-sides traceable/dark, prior-cycle aggregate + null split, fallback path. `run_export_tests.sh` (→ `test_export_js.mjs` + `test_export_zip.py`) — the client-side CSV/ZIP export: CSV quoting edge cases, all-ten-tables build (incl. the votes table: position, citation, primary-source URL; and the statement search-log audit table with per-row dispositions), full-record (not just money) legislative rows, and a real ZIP unzipped + byte-verified by Python's stdlib. `test_run19_fixes.py` — regressions pinned from the real Run-19 output: the reconcile `$`-suffix word-boundary bug, rounded-variants provenance (rounding passes, coincidence rejected), bare-corruption removal + opt-in plurals (recall and precision cases), Senate `PARSE_FAILED` vs not-on-roll, the deterministic Money Picture (content + placeholder surgery), and the search-log serialization. `test_reconcile_sides.py` — the `spender_side` guard: real Run-13 report yields zero flags (kills the running-cue-bleed false positives) while a genuine inline side-flip is still caught both directions; §7 pins the Run-20 committee-name-cue fix (a "DEFEATING …" PAC name is not an oppose claim; masking doesn't blind the check to a real flip). `test_digest_trim.py` — the record trim in the model's digest: counts survive, money items survive, `result` never mutated, hash still sensitive to the full record, filtering disclosed via `digest_note`. `test_candidate_resolve.py` — the "Did You Mean?" resolver: nickname miss broadens to last name and floats the right person up (no alias table), a picked `candidate_id` scopes `sched_a` to exactly that candidate's committees. `test_schedule_a_corrections.py` — the memo-filter + refund-netting fixes (memo restatement dropped, refund netted, fully-refunded donor removed, memo-coded negative not double-applied). `test_statements_classify.py` — the candidate-generalized classifier: derived domains, name/pronoun rhetoric, the pronoun-needs-name guard, Ossoff parity, and the legacy `ctx=None` path. `test_votes.py` — roll-call votes offline (LIS XML fixtures modeled byte-for-byte on real senate.gov files + stubbed Congress.gov JSON): tagging parity with congress.py (same matcher object, the Living-Donor/Corporate-PACs regression pair), LIS date handling (menu day-month vs per-vote full date, unparseable → `""` never a guess), member matching both chambers, detail/title caps disclosed as incomplete, not-on-roll counted not invented, chamber dispatch errors, amendment-purpose tagging, the votes↔record join (exact-match roles, cross-congress no-bleed, empty-safe), the Senate 5-congress default lookback, and the timeline merge (placed, cited, sort invariant, linked-vote labels). `test_spender_ledger.py` — the deterministic spender ledger: content (sides as filed, largest-first, dark/notice-only disclosed, totals), placeholder surgery (replace / dedupe / fallback insert / strip), and reconcile running clean over the generated ledger. |
| `static/export.js` | **done, tested** | Client-side CSV + store-only ZIP export (see the app.py+frontend row). Pure/DOM-free → Node-testable. Tests: `tests/run_export_tests.sh` (Node builds a real ZIP, Python's `zipfile` opens + byte-verifies it). |
| `export.py` (server-side downloadable report) | **not built** | Optional — client-side CSV/ZIP now covers data export; this would be for a rendered PDF/report artifact. |
| `parked/rhetoric.py`, `parked/correlate.py`, `parked/statements.py` | **parked** | The old rhetoric-scoring + correlation track. Superseded by the factual design — kept for reference, not imported. |

---

## Architecture: the StepLog spine

Every search is one job running one `StepLog` (`steps.py`), the backbone of both
the live display and the correctness guarantee.

```
POST /search ──▶ worker thread: _run_search(job_id, name, key, demo)
                     │  log.plan([...])   declare all steps (UI greys them)
                     ▼
        with log.step("resolve"):     FEC candidate_id + state/office → ok
        with log.step("sched_a"):     Schedule A top donors (capped)  → ok
        with log.step("sched_e"):     Schedule E (Pipe 2)             → ok | warn(#3396)
        with log.step("composition"): require("sched_a"); funding_by_cycle → ok
        with log.step("record"):      require("resolve"); Congress.gov   → ok | warn(no key)
        with log.step("votes"):       require("resolve"); roll calls (House Clerk/Senate LIS) → ok | warn(no key/chamber/cap)
        with log.step("statements"):  require("resolve"); SearXNG        → ok | warn(no SEARXNG_URL)
        [timeline.build_timeline(result)]  deterministic Side-by-Side (sorted + month-banded, his-voice)
        with log.step("synthesize"):  posted UI key? report + reconcile guard → ok | warn(no key / failed / reconciliation flag)
                     │
GET /status/<id> ◀───┘  returns log.to_dict()  (frontend polls this)
```

A stage that fails its `require(...)` raises StepGateError, which the worker
catches and records — the offending step is left FAIL, so the UI shows exactly
where and why it stopped.

---

## fec.py specifics (read before changing it)

- **Schedule A is pulled CAPPED + LARGEST-FIRST for the donor list.**
  `fetch_schedule_a` / `search_fec_candidate` take `max_pages` (app.py passes
  10). When set, the sort flips to `-contribution_receipt_amount` and pagination
  stops after `max_pages`, so the result is the **biggest itemized donors**, not
  a candidate's full ~250k-row history (which made a real run 2,892 pages /
  1,000+ PDF pages). This is a *deliberate* stop, NOT a truncation — it does
  **not** set the incomplete flag. **The campaign's real receipts total comes
  from `/totals` (the composition stage), never from summing this capped list.**
  app.py's `_summarize_direct` exposes the capped sum only as
  `largest_contributions_total` so it can't be mistaken for the headline total.
  (Uncapped full pulls keep the date sort for cursor stability — see below.)
- **`is_individual=true`** on Schedule A is load-bearing for the direct-donor
  view (excludes committee transfers / conduit double-counting). `fetch_schedule_a`
  takes `individuals_only` (default True); **Hop 2 (`trace_spender_donors`) passes
  False on purpose** so PAC-to-PAC transfers into a super PAC are captured — an
  empty Hop-2 result then genuinely means dark money, not just "no individuals."
  **This was silently broken until the 2026-09-07 fix** (flagged by a GitHub
  Copilot review comment): the base request-params dict set
  `is_individual: "true"` unconditionally, ahead of the `if individuals_only:`
  branch, so `individuals_only=False` never actually removed it — Hop 2 stayed
  individuals-only the whole time, missing exactly the PAC-to-PAC transfers it
  exists to find. Fixed by only setting the param inside the conditional (it's
  omitted entirely when `individuals_only=False`). Regression:
  `tests/test_schedule_a_pagination.py`.
- **#3396**: openFEC Schedule E pagination under-returns; the code cross-checks
  count vs rows and flags `incomplete` rather than reporting a false total.
- **Cycles**: campaign finance is 2-year; the small-dollar (unitemized) share
  only exists per cycle on `/totals`, not in Schedule A line items. `cycle_of()`
  maps a calendar year to its cycle.
- **`FEC_API_KEY`** (note: the State Donors Portal repo uses `DATA_API_KEY` — a
  different name on purpose).

---

## congress.py specifics

- **Auth is a query param** (`api_key=`), NOT a header. The header-based
  ProPublica Congress API is **dead** (no new keys); Congress.gov's own API
  replaced it. `CONGRESS_API_KEY`, free from api.data.gov.
- **Name→bioguideId**: `/v3/member/{state}?currentMember=true`, match last name,
  tie-break by chamber using the FEC office (S/H). Uses data the FEC resolve step
  already produced.
- **Legislation**: `/v3/member/{bioguideId}/sponsored-legislation` and
  `/cosponsored-legislation`, paged (limit 250, offset), each item dated by
  `introducedDate`. Money relevance is tagged from the bill title via
  **whole-word regex** (`\b...\b`, pre-compiled) against a campaign-finance-
  specific term list. This was tightened after real data showed the old bare-
  substring match producing false positives (e.g. "Living Donor Protection Act" →
  organ donors; "Courthouse Ethics and Transparency Act") and false negatives
  (plural "Corporate PACs", "Stock Trading Act"). Bare `donor`/`ethics`/
  `transparency` are gone; they survive only as campaign-finance compounds
  ("donor disclosure", "government ethics"). Bare `corruption` is gone too
  (Run 19: a Haiti disaster-recovery vote landed in the money ledger on it);
  `anti-corruption` is deliberately KEPT (it's what S. 1 / S. 2093 actually
  say) plus domestic compounds ("political corruption", "public corruption").
  **Plurals are opt-in per term** (`_PLURALIZE` + `_term_pattern`), never
  blanket — tested against 2,506 real Senate vote titles, blanket
  pluralization's one new match was a WRONG one ("Federal elections" on a
  voter-ID amendment). `conflict of interest` pluralizes irregularly
  (`conflicts? of interest`). Patterns are `(display_term, regex)` pairs —
  the display string is never derived from the regex source. All Congress.gov
  param dicts send `format=json` explicitly (XML is the documented default;
  the API removed per-endpoint JSON inconsistencies in March 2026).
  Policy-area enrichment is a later option.
- **Cloudflare bot-check on api.congress.gov.** The endpoint sits behind a
  Cloudflare Browser Integrity Check that blocks plain `urllib` with `error
  code: 1010` — it fingerprints the **TLS handshake**, which urllib can't fake,
  so no User-Agent trick alone works. `_get` uses **curl_cffi** (browser-TLS
  impersonation) when it's installed (`pip install curl_cffi`) and falls back to
  urllib otherwise. Either path fails **fast** (short timeout, one retry) with a
  clear, actionable message — a blocked record stage never hangs the run or takes
  down the money side.
- **Votes are in votes.py now** (House via the Congress.gov beta house-vote API,
  Senate via LIS XML — see the votes.py row). The remaining honest gaps live
  there as disclosed `coverage_note`/`incomplete` text: the House API covers
  118th+ (2023) legislation-linked roll calls only, and per-vote detail is
  fetched under caps, newest-first.

---

## statements.py specifics (read before changing it)

Two subsystems here matter: **dating** and **classification**. Both are tuned
from real Ossoff runs and both are deliberately auditable.

**Dating — real dates only, guesses kept separate.** `_extract_date` returns
`(iso_date, is_real, year_hint)`:
- A real `YYYY-MM-DD` comes only from the search engine's `publishedDate` or a
  `YYYY-MM-DD` **URL slug**. That's it for `dated=True`.
- Year-only results return `year_hint` (a bare `YYYY`), NEVER a `YYYY-01-01`
  placeholder in the `date` field — an early bug put fake Jan-1 dates on the
  timeline. Kept separate so a guess can't masquerade as a real timestamp.
- **Future years are rejected everywhere** (a publish date can't be later than
  this year) — this killed a phantom "2028" lifted from a speculation headline.

**Dating tier 2 — page fetch (the main lever on the undated rate).** General web
search rarely returns a `publishedDate`, but the *pages* usually declare one.
For statements still undated, `_page_fetch_pass` fetches the page (concurrent,
capped at `_FETCH_MAX_PAGES=40`, 6s timeout, 256KB read, non-fatal) and
`_parse_page_date` reads the publication date from, in order: OG
`article:published_time`, JSON-LD `datePublished`/`uploadDate`/`publishDate`,
`itemprop` datePublished/uploadDate (YouTube), `meta name=date/pubdate/dc.date`,
`<time datetime>`, then WordPress/Elementor microdata and posted-on textual
dates (`January 22, 2026`). Textual dates are read **only when anchored to a
published marker**, never from free body text (that would date an article by its
content). "Modified/updated" is never used. Uses **curl_cffi** browser-TLS (news
CMSes 403 plain urllib); falls back to urllib. A single **canonical/og:url hop**
handles stub/AMP pages whose date lives on the canonical version — that's the
page's own declared target, not a search, so it doesn't reintroduce guessing.
`date_source` records where each real date came from. **Skip-list** =
login/JS-walled domains (facebook/instagram/x/tiktok/linkedin) where a GET never
yields a date; **YouTube is deliberately NOT skipped** — its watch pages expose a
clean `uploadDate`. Do NOT add a "search the web for a missing date" tier: that
re-queries and picks a *different* result's date, which silently mis-dates the
timeline — the one move that corrupts a correlation method. Cheap unambiguous
recovery (page metadata, canonical hop) = yes; date *invention* = no.

**Classification — his voice vs coverage vs boilerplate.** `classify_statement`
returns `(class, reason)`:
- `by_candidate` — his own outlet (`ossoff.senate.gov`, official — press
  releases ARE his voice) OR reported rhetoric (`_RHETORIC_RE`: Ossoff/he as the
  actor — said/urged/introduced/decried/…). Fact-about verbs (raised/entering/
  received) and characterization verbs (centered/presents) are **excluded** — a
  news outlet's "has centered his campaign on X" is framing, not his words.
- `about_candidate` — third-party coverage/framing. This is the default, and it's
  where the **campaign site** (`electjon.com`) lands *without* a rhetoric cue,
  because it also reposts coverage, fundraising, and CTAs. (Official domain
  defaults to his voice; campaign domain must show a cue.)
- `boilerplate` — donation CTAs / contact / raw data pages (`_BOILERPLATE_RE`,
  or a `_DATA_DOMAINS` page with no rhetoric).
- **Only `by_candidate` reaches the timeline** (see timeline.py); the rest stay
  in the JSON, labeled, disclosed in `coverage.by_class`. Every call carries a
  `classify_reason` so miscategorizations are visible and cheap to fix — tune the
  patterns against real excerpts, not paraphrases.
- **Candidate-specific:** the domains and the name in `_RHETORIC_RE` are hardcoded
  for Ossoff. Pointing the tool at another candidate needs these parameterized by
  resolved surname + domains — a contained change, not yet done.

**Coverage** reports `by_class`, plus `undated`/`undated_year_only` **and their
per-class splits** (`undated_by_class`, `year_only_by_class`) so the timeline's
his-voice undated count reconciles obviously against the all-statements count
(they're different scopes, not a discrepancy). Also `dated_via_page` /
`page_fetch_attempted` so a clipped fetch budget is visible.

---

## timeline.py specifics (read before changing it)

The Side-by-Side merged timeline, built **in code** — ordering was the model's
job once and it mis-sorted adjacent dates (`2026-02-02` above `2026-02-03`) in
two real runs. Sorting is not a judgment call.

- **Events:** money (each outside spender at its last-transaction date, plus a
  separate *first*-transaction event so a multi-month run shows as a span; top
  itemized donors at their last contribution date), record (money-bills at their
  date), and **only `by_candidate`** statements. Coverage/boilerplate statements
  are counted in `statements_excluded`, kept in the JSON, off the timeline.
- **Sorted newest-first with a total order** (date, then track, then label) so
  output is stable run to run; `verify_sorted` asserts the invariant.
- **Month bands.** `build_timeline` also returns `bands` (the same events grouped
  by `YYYY-MM`, newest-first, each with per-track counts) so a wall of ~27
  near-identical donor rows reads as labeled clusters. The flat `events` list is
  kept alongside for anything that needs it. The synthesis prompt renders
  band-by-band, in order.
- **Year-only** statements go in a separate `year_only` bucket (never interleaved
  with real dates); fully-undated `by_candidate` statements are counted in
  `undated_count`, not placed. These counts are the **his-voice slice** and match
  `coverage.*_by_class["by_candidate"]`.
- Pure function of `result`; skipped stages just contribute nothing. The
  "first transaction of the run above" lines intentionally omit a side word —
  reconcile.py must not infer a side for them (see its specifics).

---

## reconcile.py specifics (read before changing it)

The deterministic backstop behind Integrity Rule 5. It runs *after* the model
writes the report and asks one question the prompt can't guarantee on its own:
**does the prose actually match the JSON?** In testing it caught the model
narrating an outside spender (Senate Conservatives Fund) as "support" when the
FEC data had its dollars on the **oppose** side — the chart, drawn from JSON,
had it right; only the prose was wrong. This module makes that class of slip
catchable automatically.

- **Four checks, two severities.**
  - `oppose_attribution` (**high**) — outside "oppose" money is spent AGAINST
    THIS candidate; the target is known. Run 17 wrote it as "Opposing
    (presumably his opponent, though the data does not specify the target)",
    inverting the biggest fact in the race ($7.0M attacking the candidate).
    `spender_side` passed clean because the per-spender timeline lines were
    individually correct — only the SUMMARY sentence was wrong — so this needs
    its own check. The synthesis prompt now states the direction explicitly too.
  - `spender_side` (**high**) — each outside spender's support/oppose side in the
    prose must match the side its dollars sit on in `track_a_outside`. The
    headline check. **Reads a side ONLY where the prose states it next to the
    spender** — a segment that names the spender AND carries its own side word,
    bound to that spender (no other spender named, or the spender's own dollar
    figure present). It does *not* inherit a side from a running/section cue
    that bled in from a neighbouring sentence. That running-cue fallback caused
    the Run-13 false positives: a "… are marked traceable" enumeration listed
    the five SUPPORT spenders with no side word of its own and inherited the
    "opposing" cue from the preceding CATHOLICVOTE sentence, flagging all five
    though the report was correct. Regression: `tests/test_reconcile_sides.py`
    (real report → 0 flags; a genuine inline flip still caught, both directions;
    plus the "for <candidate>" support cue generalized off the old hardcoded
    `jon|ossoff` to any candidate via `_candidate_name_terms`).
  - `cycle_total` (**high**) — the for/against cycle totals must be restated
    somewhere in the report.
  - `dollar_provenance` (**review**) — every `$` figure in the prose must trace
    to a source number within tolerance; unmatched figures are listed to
    *verify* (that's where an invented number or a bad computed sum surfaces).
  - `caveat_carried` (**review**) — each audit caveat's distinctive figures must
    survive into the report (preserve-the-audit-trail).
  - **high** findings ride into the audit/caveats (the `synthesize` step warns);
    **review** findings are noted and stored in `report_reconciliation`, but
    don't mark the report unsound. `reconcile()` returns `{ok, warnings, counts}`;
    `ok` is False only when a high-severity item fired.
- **Non-fatal, always.** `synthesis._safe_reconcile` wraps it in try/except — a
  bug in the guard returns a clean/empty result rather than costing you the
  report. Same philosophy as the rest of the pipeline. It also runs on **cache
  hits**, so improving the checks re-reconciles old reports for free.
- **Feed it the FULL `result`, not the digest.** `check_dollar_provenance`
  collects its source amounts from the whole result (composition receipts, the
  donor list, every spender, plus `$`-figures embedded in statement excerpts).
  app.py passes the full `result` — do the same anywhere else, or the provenance
  check over-flags every figure it can't see as "to verify."
- **Side detection is sentence-scoped with a running header context, and it is
  deliberately conservative.** A spender's side is read from its own clause or
  the nearest governing list-header ("Support spending came from …") that
  precedes it, reset at markdown headings. Abbreviation periods (`Inc.`, `Corp.`,
  …) are **shielded before sentence-splitting** — without that, "Field Team 6,
  Inc. ($…)" splits mid-list and severs the following spenders from their header
  (a real bug that was fixed). It only flags an unambiguous contradiction; it
  **skips** spenders that are genuinely split (real money both sides, unless one
  side ≥5×) and spenders not named in the prose. Bare "for" is never a support
  cue (too noisy) — support is matched via "support/in favor/backing/…" or the
  `$X for` / `for <candidate|pronoun>` patterns.
- **Established-side rule (fixes a timeline interaction — don't regress it).** A
  spender's side is fixed once, from its first inline cue or governing header.
  After that, a **bare** later mention with no side word of its own must NOT
  re-derive a side from wherever the running context has drifted. This exists
  because the deterministic timeline emits "…first transaction of the run above"
  lines that carry no side word; the old logic inherited a side from the
  *preceding, unrelated* spender and false-flagged support spenders (ACTIVATE
  AMERICA, GIVEGREEN, INDIGO) as opposed in a real run. An explicit inline cue is
  always trusted, so a genuine wrong-side line is still caught — only sideless
  back-references are ignored once the side is known.
- **Committee names are masked out before a segment's cue is read
  (post-Run-20 — don't regress it).** PAC names are political slogans and
  routinely contain cue words ("DEFEATING COMMUNISM PAC" carries "defeat";
  "Stop …", "Defend …", "… Victory Fund" are all in the wild). A segment that
  merely names such a spender must not read its name as a side claim — that
  false-flagged a correct Run-20 report at high severity. `_mask_committee_names`
  blanks every spender's name variants (longest first, case-insensitive) from
  the copy `_side_signal` sees; `seg_low` (name lookup) stays unmasked, and an
  explicit verb outside the name still triggers, so genuine flips are still
  caught (`tests/test_reconcile_sides.py` §7).
- **Provenance matching is rounded-variants + $1 absolute, NOT a percentage
  band** (post-Run-19). The old blanket 5% scaled with the number — at
  2020-cycle magnitudes it was a ±$7.8M hole that waved a corrupted figure
  through. `_rounded_variants` generates the renderings a writer would
  plausibly produce (1–6 significant figures + exact) and matches those
  points only; legend-rounding still passes, six-figure coincidences don't.
  Also post-Run-19: the `$` regex requires magnitude suffixes to be whole
  words (`(?![A-Za-z])`) — the bare `K` alternative used to bite the first
  letter off "known", silently corrupting "$12,093.06 known only from a
  notice" into $12M. Regressions: `tests/test_run19_fixes.py`.
- **Known limitation — now structurally closed upstream:** a side implied *only*
  by a distant heading with no cue near the spender's name isn't caught by the
  side check; `cycle_total` + `dollar_provenance` are the backstop there. The
  clean fix landed in synthesis.py: the per-spender ledger is rendered
  deterministically from JSON (`build_spender_ledger`) and the model is
  forbidden from writing per-spender sides at all, so this guard now polices
  only the model's connective prose — keep it anyway (defense in depth, and it
  re-checks cached pre-ledger reports).

---

## Common gotchas

- **NEVER sum raw Schedule E rows.** The same expenditure legally appears twice
  in `/schedules/schedule_e/` — a 24/48-hour notice (F24, or an F5 notice) and
  the regular report (F3X/F5) that restates it — under different `sub_id`s, so
  `sub_id` dedup does not catch it. Raw sums ran ~2x high on real data. Any new
  code that touches Schedule E rows must go through `_dedup_schedule_e` (or the
  functions that already do). Ground truth for a cross-check is FEC's own
  `/schedules/schedule_e/totals/by_candidate/` — our deduped total minus the
  disclosed notice-only sum must equal it to the cent.
- **Don't sum raw Schedule E rows for PRIOR cycles either.** The row-level
  dedup is verified on the small current cycle but does NOT scale to old
  high-volume cycles (a notice's estimated amount/date rarely matches its final
  report exactly, so brittle key-matching keeps both). Prior-cycle composition
  totals must come from `outside_totals_aggregate` (FEC's own aggregate), not a
  row sum. A "notice-only" residual on a CLOSED cycle is the tell that something
  regressed to summing rows.
- **traceable/dark must cover both support AND oppose.** The split was once
  support-only, which silently dropped all opposition dark money. It's only
  meaningful where per-spender detail exists (current cycle); it's `null`
  otherwise — never fabricate it for a prior cycle.
- **Schedule A has the SAME class of trap as Schedule E.** Never sum raw
  Schedule A rows either: `memo_code="X"` rows are re-itemizations of money
  already counted (new sub_id, so sub_id dedup misses them — 39% of rows in a
  real pull), and refunds are negative rows the largest-first capped pull never
  reaches. Both corrections live in `search_fec_candidate`; any new Schedule A
  code path must apply them. A donor total above the federal per-election limit
  is the tell that something regressed.
- **Never hard-code which key names a keyset-pagination cursor.** FEC's
  `last_indexes` names its secondary field after whatever the query is sorted
  by (`last_contribution_receipt_date` when date-sorted,
  `last_contribution_receipt_amount` when amount-sorted, etc.) — `fetch_schedule_a`
  hard-coded the date name and broke every amount-sorted pull (the capped
  "top donors" pull, and the `smallest_first` refund pull once it grows past
  one page): `last_indexes.get("last_contribution_receipt_date")` silently
  returned `None`, and `str(None)` sent a literal `last_contribution_receipt_date=None`
  to FEC, which correctly 422'd and truncated the pull to page 1 — on every
  candidate, 100% reproducible, never disclosed (see `Claude_Recommendations.md`,
  the 5-candidate 2026-09-07 audit). Fixed by reading `last_indexes` back
  generically and re-sending its keys as-is, the same pattern `fetch_schedule_e`
  already used. `_summarize_direct` now aggregates `truncated`/`truncated_reason`
  across committees, and the `sched_a` step warns (not oks) when set — mirroring
  `sched_e`'s existing incomplete check; `composition`'s gate on `sched_a` now
  accepts a warn (`allow_warn=True`) so a disclosed-but-partial donor pull
  doesn't halt the record/votes/statements/synthesis stages downstream, same as
  `sched_e`'s incomplete flag never has. Regression: `tests/test_schedule_a_pagination.py`.
- **No paid search anywhere.** Brave and similar pay-to-browse APIs are banned by
  preference. Statement gathering uses **self-hosted SearXNG** (`SEARXNG_URL`,
  free, no gatekeeper); `ddgs` is a fallback. The tool calls the operator's own
  SearXNG instance — keep it locked down (Cloudflare Access / Tailscale) if it's
  cloud-hosted, and enable `json` in its `settings.yml`. A minimal, ready-to-run
  compose file lives at `docker/searxng/` — it's deliberately a single-container
  setup (no reverse proxy, no Valkey, bound to `127.0.0.1`) because this instance
  answers only this project's own JSON queries, not public traffic; see
  `docker/searxng/README.md`. If you instead run the official multi-container
  `searxng-docker` (reverse proxy + Valkey, needed once you turn the limiter
  back on for a public instance), its service is named `core`/`searxng-core`,
  not `searxng` — the in-container hostname differs by which compose file you
  used.
- **`threaded=True`** on `app.run` is required, or a running worker blocks
  `/status` polls.
- **In-process JOBS dict** is fine single-worker; move to Redis only if you scale
  out.
- **Launching:** `run.sh` is the launcher — it exports `FEC_API_KEY`,
  `CONGRESS_API_KEY`, `SEARXNG_URL`, then runs the app. It uses **`python3`** (not
  `python` — Debian/Crostini doesn't symlink `python`). Deps install with
  `pip install -r requirements.txt` (add `--break-system-packages` on
  Debian/Crostini). Typical local dev: edit in code-server, **run on the host**
  (SearXNG reachable at `http://localhost:8080`); only from *inside* a container
  on the same Docker network would you use the service name (`http://searxng-core:8080`).
- **Runtime modes:** DEMO (no FEC key, or `?demo=1`) runs the factual stages on
  synthetic fixtures (synthesis skips in demo). REAL needs `FEC_API_KEY`;
  `CONGRESS_API_KEY` and `SEARXNG_URL` are each optional (missing → that stage
  skipped + disclosed, everything upstream still runs). The Anthropic key is
  separate — UI-field only, not an env var — see below.
- **The translation layer is isolated by design.** It's the 7th stage and the
  ONLY interpretive step. Provide no Anthropic key in the UI field and the
  tool is byte-for-byte the validated six-stage factual version — the
  rollback is a blank field, not a code change.
- **The Anthropic key is secret + transient.** It arrives via the UI field
  only (no env var — this was removed on purpose so there's nothing left in
  `run.sh` to forget to scrub before sharing the folder), is used only for
  the single synthesis call, and is NEVER written to the job record, the
  result, logs, or any `/status` response, and is cleared from the page on
  submit. This is verified (posting a sentinel key shows it appears nowhere
  in the response). Do not add
  code that persists, logs, or echoes it. (The old FEC rate-limit UI field was
  repurposed for this — FEC is now env-only.)
- **Congress.gov needs `curl_cffi`** installed to clear its Cloudflare bot-check;
  without it the record stage fails fast and discloses, money side unaffected.

---

## What's next

1. **Refine the synthesis prompt against real reports.** The translation layer is
   built with a deterministic backstop (reconcile.py), a deterministic
   **timeline** (timeline.py), and now a deterministic **spender ledger**
   (synthesis.build_spender_ledger swaps in for a model-written placeholder —
   the model never restates per-spender sides, retiring reconcile's one known
   gap; the guard stays on as backstop). Open work is prompt tuning as real
   reports flow. The statement classifier is now
   candidate-generalized (`build_candidate_context` derives the official domain
   from `<lastname>.senate.gov/.house.gov` and the name/pronoun rhetoric cues
   from the resolved name + chamber; `ctx=None` keeps the legacy path;
   `tests/test_statements_classify.py`), so a non-Ossoff run no longer asserts
   "no statements in their own voice" as a finding. **Sonnet 5 API note:** it rejects a non-default
   `temperature`/`top_p`/`top_k` (400 error) and defaults to adaptive thinking at
   high effort — synthesis.py sets no sampling params and passes
   `thinking:{type:"disabled"}` for a rigid restatement task. If you ever want
   deeper reasoning, use the `effort` parameter instead of re-adding temperature.
2. **Roll-call votes — built** (votes.py + the `votes` step + timeline events;
   `tests/test_votes.py`). Still open inside it: non-legislation House votes
   (Speaker elections etc.) aren't in the beta API yet, and pre-2023 House
   votes would need the Clerk's own EVS XML — both disclosed, not faked.
3. **Policy-area enrichment** — per-bill `policyArea`/`subjects` for sharper
   money-relevance tagging than title keywords.
4. **Visualization polish** — four SVG charts are built and render from the
   result JSON: funding composition, outside spending per spender (dark-money
   flagging, measured label layout), the cross-cycle outside-spending trend,
   and the timeline swimlane (money/record/statements lanes, vote diamonds vs
   bill circles, honest "⋯" month-gap compression). Still open: collapsible
   report sections (`<details>`, Side-by-Side collapsed by default).
5. `export.py` (downloadable report), `tests/`.
6. State plugin (optional) — for state-office targets, reusing State Donors
   Portal code. Federal candidates never need it.

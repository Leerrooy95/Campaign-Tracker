# Post-Security-Update Regression Audit — Campaign Tracker

**Date:** 2026-09-07
**Scope:** Verify the 4-phase hardening pass documented in `Security_Recommendations.md`
(Phase 1 CRITICAL/SSRF, Phase 2 HIGH/input+transport+secrets, Phase 3
MEDIUM/defense-in-depth, Phase 4 LOW/cheap wins) broke nothing — functionality,
data accuracy, or documented behavior — plus a from-scratch trace of demo mode
per the instructions' Part 1 priority section.

**Environment prepared for this audit:** Node v20.19.2 + npm installed (were
absent), Playwright + Chromium installed, `flask`/`flask-limiter`/`curl_cffi`/
`defusedxml` installed. A local SearXNG instance (`campaign-tracker-searxng`,
already running, healthy) answers JSON queries at `http://localhost:8080`.
The repo's own `.env` (real `FEC_API_KEY`/`CONGRESS_API_KEY`/`SEARXNG_URL`) was
used as-is per instructions.

---

## STATUS: PASS (Phases 1–4 regression-free) — with one significant pre-existing integrity finding surfaced by the Part 1 trace

No functional or behavioral regression was found that traces to the Phase 1–4
security work itself: all 25 `tests/test_*.py` files pass, both Node/Playwright
export suites pass, two full live searches (Ossoff, Massie) completed
end-to-end with internally consistent data, and a real headless-browser pass
found zero console errors, zero CSP violations, and correct rendering
throughout.

However, Part 1's demo-mode trace (explicitly the priority section of this
audit) surfaced a **real, reproduced, pre-existing** data-integrity bug that
is orthogonal to the four security phases (not introduced or touched by any
of them) but strikes directly at the project's own stated Integrity Rules 4
and 6 in `CLAUDE.md` ("Disclose incompleteness; never fake" / "Visible
process = self-check. Never bypass a gate.") — see the Demo Mode Findings
section below. This is flagged prominently rather than folded silently into
"PASS" because it is genuinely unresolved and, in the tool's current
configuration in this very environment (`SEARXNG_URL` set in `.env`, matching
CLAUDE.md's own documented "independently optional" setup), it is not a
theoretical edge case — it fires by default.

---

## Demo Mode Findings (Part 1)

### What demo mode is, and how it's actually triggered

`app.py:784`:
```python
demo = (str(data.get("demo", "")).lower() in ("1", "true", "yes") or not fec_key)
```
`demo` is decided once, from a JSON POST-body field to `/search` (or simply
the absence of `FEC_API_KEY`) — **not** a URL query string. `app.py`'s own
module docstring and CLAUDE.md both describe the trigger as `?demo=1`; that
literal mechanism does not exist in the code (`grep -n "request.args" app.py`
= zero hits, confirmed live). `static/app.js` never reads `location.search`
and never sends a `demo` field, so the browser UI cannot hit this lever at
all, by accident or otherwise — forcing demo with real keys present requires
a hand-crafted API call. This is a **documentation/code mismatch**
(classification: `other`, not a security regression), separate from the
mechanism finding below.

### Mechanism: five of six data tracks branch on the shared flag; one doesn't

`fec.py`/`congress.py`/`votes.py` real-path functions have no fixture
fallback of their own — `app.py`'s demo branches for `resolve`, `sched_a`,
`sched_e`, `composition`, `record`, and `votes` call dedicated zero-argument
synthetic generators (`congress.demo_record()`, `votes.demo_votes()`, inline
dicts) and never touch the real modules at all when `demo=True`. This is
sound, shared-flag, all-or-nothing gating, verified by reading every real-path
function for a fixture branch (none exists) and by a live demo run (below).

`statements.py` is architecturally different: it owns its own
"fixtures-vs-live" decision internally, and that decision does not correctly
honor the caller's flag.

### Contamination — reproduced live, not just traced

`app.py:521,525,527`:
```python
search_name = name.strip() or cand_name          # the REAL name the caller typed
...
if demo:
    ss = statements.collect_statements(search_name, searxng_url="", office=office)  # fixtures
    ...
    s.warn(f"{len(ss.statements)} statements gathered (offline fixtures — "
           f"set SEARXNG_URL for live)")
```
`statements.py:105-107`:
```python
base = (searxng_url or os.getenv("SEARXNG_URL", "")).strip()
if not base:
    return _offline(candidate)
```
`app.py` passes `searxng_url=""` intending fixtures, but `"" or os.getenv(...)`
is Python's classic falsy-string fallthrough: whenever `SEARXNG_URL` is set in
the environment, `base` resolves to the **real** SearXNG URL and
`collect_statements` proceeds into a live network search instead of
`_offline()`.

**This was reproduced directly in this environment, not just inferred from
reading the code.** Steps taken:

1. Stopped the real server; started `python3 app.py` with **no `FEC_API_KEY`**
   (→ `demo=True` automatically) but `SEARXNG_URL=http://localhost:8080` set —
   exactly the state this repo's own `.env` puts an operator in.
2. `POST /search {"name": "Thomas Massie"}` → `{"demo": true, ...}`, confirming
   demo mode.
3. Result: `track_a_direct`/`track_a_outside`/`track_a_composition`/
   `track_b_record`/`track_b_votes` were all the hardcoded fictional
   `OSSOFF, T. JONATHAN` / `S8GA00180` GA-Senate fixture data — **while**
   `track_b_statements.backend == "searxng"` (not `"fixtures"`) and
   `track_b_statements.candidate == "Thomas Massie"` — the real typed name —
   with `own_voice_note` referencing `massie.senate.gov` (the classifier's
   real per-candidate domain derivation, which only runs on the live path).
   The live search returned 0 results this run because the SearXNG instance's
   upstream engines were transiently rate-limited/CAPTCHA'd at that moment
   (confirmed separately, see Part 3/Part 4 below) — **not** because it fell
   back to fixtures. A moment when the upstream engines are responsive (also
   observed live during this audit, see the Massie UI run in Part 3) would
   return real statement excerpts about Massie sitting in the same JSON
   object as five fabricated Ossoff tracks, with no data-layer marker
   distinguishing the two (see next finding).
4. **The disclosed step-log message is actively false in this state.** Step
   `statements` unconditionally warns `"N statements gathered (offline
   fixtures — set SEARXNG_URL for live)"` whenever `demo=True`, regardless of
   what `collect_statements` actually did. In the reproduction above, the
   *data* (`backend: "searxng"`) says live search ran; the *disclosed log
   line the operator is told to trust* says "offline fixtures." This is a
   more serious problem than a missing marker: `CLAUDE.md`'s Rule 6 is
   "Visible process = self-check. Never bypass a gate" and the whole StepLog
   architecture exists so "the thing you watched happen is provably the thing
   that ran" (README, "The visible process *is* the safety mechanism"). Here
   it is not — the log actively misreports which path ran.

### `.synthesis_cache` — confirmed untouched in demo mode, no finding

`app.py:578-583`: `if demo:` is checked first in the `synthesize` step and
unconditionally short-circuits before `synthesis.synthesize()` (the only
function touching `.synthesis_cache`) is ever called, regardless of whether
an Anthropic key was also supplied. Verified by reading the branch order and
by every live demo run performed in this audit: no `.synthesis_cache` writes
occurred during any demo run (checked `ls -la .synthesis_cache/` timestamps
before/after — unchanged). Secondary note: even if this were reachable, the
cache key (`content_hash` of the actual data digest, `synthesis.py:590-597`)
is not an explicit demo tag but would differ structurally between demo and
real data for the same name, so accidental cross-serving is not realistically
possible even hypothetically. Moot here since the path is unreachable.

### No `demo: true/false` field in the exported result data — confirmed missing

`GET /status/<job_id>` (`app.py:815-828`) returns `{done, error, rate_limited,
result, log}` — **no `demo` key anywhere**, confirmed by inspecting both live
saved exports (`tests/live_runs/{ossoff,massie}/result.json`) and a live demo
run's `/status` response. The only places a `demo` boolean exists are the
transient `/candidates` and `/search` job-creation responses, which
`static/app.js` uses once to paint a UI banner and which never get persisted
into `result`. Marking inside `result` itself is inconsistent: `track_a_direct`
/`track_a_outside` carry `"_demo": true`; `track_a_composition`,
`track_b_record`, and `track_b_votes` carry **no marker at all** (same
serializer function used for both real and demo data); `track_b_statements`
carries a `"backend"` field (`"searxng"`/`"fixtures"`) — the closest thing to
a real signal, but differently named, present on only one of six tracks, and
— per the finding above — can be **actively wrong-reading** relative to the
step log during a demo run. **Confirmed: an exported JSON/CSV, once it leaves
the running app (saved to disk, reopened later, piped into another script),
carries no reliable, uniform, machine-checkable way to distinguish synthetic
from real data.**

### `?demo=1`/forced demo reachability

Confirmed forceable with real keys configured (via the JSON body field, not a
query string — see mechanism above). **Not** reachable by accident through a
bookmarked/shared URL or browser autocomplete, since the literal `?demo=1`
trigger described in the docs doesn't exist in the code and the browser UI
never sends this field. It **is** trivially reachable by anyone driving the
(already-documented-as-unauthenticated) `/search` API directly with a crafted
JSON body.

### CLAUDE.md's stated rationale vs. what was found

CLAUDE.md frames demo mode as a bare, key-less, "no network," dev-onboarding
convenience ("confirm the install works... before they invest in real
keys"). For that *exact* scenario — nothing in `.env` at all — the trace and
a live run both confirm this holds precisely: `SEARXNG_URL` empty →
`_offline()` fires, zero network calls, fully synthetic (confirmed live: a
demo run against the unmodified `.env`-absent state returned `backend:
"fixtures"` and the message "8 statements gathered (offline fixtures — set
SEARXNG_URL for live)," truthfully this time). **The "no network" claim does
not hold in general**, though: the moment `SEARXNG_URL` is configured — a
state CLAUDE.md's own setup walkthrough explicitly anticipates as
independent/optional/separately-configurable, and the exact state this
repository's own `.env` is in right now — demo mode's statements stage makes
a live network call, and neither the exported data nor (worse) the disclosed
step log reliably says so.

### Verdict and recommendation

Blast radius: any demo run in an environment where `SEARXNG_URL` happens to
be configured (default for anyone following this repo's own quick-start once
they've set up SearXNG) silently mixes one live, real-search-derived track
into an otherwise fully fabricated result, and the audit trail that's
supposed to make this detectable currently says the opposite of what
happened. Recommend **hardening, not removal** — the zero-setup rationale is
legitimate and correctly implemented for the true bare-install case:

1. `statements.collect_statements` should not conflate "caller passed `""`
   to force fixtures" with "caller passed nothing, check env." Minimal fix:
   default `searxng_url: Optional[str] = None`, check `if searxng_url is
   None: base = os.getenv(...) else: base = searxng_url`, and have `app.py`'s
   demo branch pass an explicit sentinel that always means "force
   `_offline()`," never falling through to the environment.
2. Add a top-level `result["demo"] = demo` boolean in `_run_search`, threaded
   through into `/status` and therefore every export, so a downstream
   consumer has one authoritative field instead of three inconsistent
   per-module conventions.
3. Make the `statements` step's log message conditional on
   `ss.backend`/actual behavior, not hardcoded to the caller's intent — this
   is the more urgent half of the fix, since it's the one actively producing
   a false disclosure today, not just a missing one.

**Classification for items 1–3 above: pre-existing issue** (not a regression
introduced by Phases 1–4 — nothing in `Security_Recommendations.md` touches
`statements.py`'s fixtures/live branching or this code path at all) — but
**unresolved** and worth prioritizing given it undermines the project's own
core "visible process = self-check" guarantee.

---

## Part 2 — Regression Test Suite

All 25 `tests/test_*.py` files run individually (`python3 tests/<file>.py`):
**25/25 pass.** `bash tests/run_export_tests.sh` (Node + Python export suite,
34 checks): **all pass.**

**`test_export_zip.py`'s previously-noted Node-prerequisite failure: RESOLVED
in this environment.** Node wasn't installed before this audit; it is now
(v20.19.2), and `bash tests/run_export_tests.sh` — the documented entrypoint,
which runs `test_export_js.mjs` then `test_export_zip.py` in sequence —
passes clean end-to-end. Running `test_export_zip.py` **standalone** without
first running the `.mjs` step still exits 1 with "Node step didn't produce
the artifacts; run the .mjs first" — this is by the test's own design
(pinned in its docstring), not a bug, and not the same failure that was
previously noted as pre-existing.

All 15 named security regression tests individually confirmed passing, with
real (not just exit-code) evidence in each: `test_ssrf_guard.py` (transport
proven never invoked for unsafe hosts; redirect loop bounded exactly at
`_MAX_REDIRECTS`+1), `test_input_validation.py`, `test_bind_host_default.py`,
`test_run_sh_env.py`, `test_searxng_secret_rotation.py`,
`test_csv_formula_injection.mjs`, `test_prompt_injection_guard.py`,
`test_rate_limiting.py` (real 429 burst against the live Flask test client),
`test_job_concurrency_cap.py`, `test_page_fetch_streaming.py` (real slow-drip
server: 0.18s / 278KB sent vs. a 16MB body, socket-level cap proven),
`test_csrf_protection.py`, `test_security_headers.py`, `test_xxe_guard.py`
(real billion-laughs payload proven to expand under stdlib `ElementTree` and
rejected under `defusedxml`, returning `PARSE_FAILED` not `None`),
`test_debug_bind_coupling.py` (port never bound when both risky flags are
set together), `test_citation_link_scheme.mjs`.

Two harmless observations, not failures: `test_schedule_a_pagination.py`
emits a `flask_limiter` in-memory-storage `UserWarning` (expected in this
dev/test context); `test_bind_host_default.py`'s subprocess log includes the
normal Flask startup banner (cosmetic).

No test files were modified, weakened, or skipped.

---

## Part 3 — Live Functional Run

`run.sh` started via `nohup bash run.sh &`. Startup banner confirmed **Mode:
REAL (FEC_API_KEY found)**; `GET /health` returned exactly `{"ok": true}`
(the Phase 4 fix — no `has_fec_key` field), with all Phase 3 security headers
present (`Content-Security-Policy`, `Referrer-Policy`, `Permissions-Policy`;
no `Strict-Transport-Security` over plain HTTP, correctly gated).

### Jon Ossoff (Senate, `S8GA00180`, GA)

Resolver flow: `POST /candidates {"name":"Jon Ossoff"}` → 2 candidates
(current Senate seat + a prior House run) → `POST /search` with the picked
`candidate_id`/`candidate_name`/`office`/`state` → polled `/status` to
completion (~40s). Step log: `resolve` ok, `sched_a` ok (top 25 of 308 donors,
635 memo re-itemizations dropped), `sched_e` ok ($183,865 support /
$1,664,712 oppose, 17 spenders, notice-deduped), `composition` ok (4 cycles),
`record` ok (878 actions, 9 money-related), `votes` ok (8 money-related roll
calls of 3,849 scanned), `statements` warn (0 gathered — SearXNG upstream
engines transiently rate-limited at that moment, see Part 4), `synthesize`
warn (no Anthropic key — **fails the documented way**, factual result
untouched). Full JSON saved to `tests/live_runs/ossoff/result.json`.

Data-layer consistency checked directly against the raw JSON: current-cycle
(2026) composition `outside_support`/`outside_oppose` match the `sched_e`
step's disclosed totals to the cent ($183,865.02 / $1,664,711.87);
`itemized + unitemized + pac ≤ receipts` holds for all 4 cycles;
`outside_totals_source` correctly reads `"spenders"` for the current cycle
and `"fec_aggregate"` for the 3 prior cycles, matching the documented
two-source design; 25 timeline bands / 67 events populated; 8 votes each
carry a citation, position, and `clerk.house.gov`/`senate.gov` URL.

### Thomas Massie (House, `H2KY04121`, KY)

Same flow (1 candidate returned, no ambiguity). Step log: `resolve` ok,
`sched_a` ok (top 25 of 774 donors, 154 memos dropped), `sched_e` ok
($3,707,058 support / $10,138,865 oppose, 9 spenders), `composition` ok (5
cycles), `record` ok (968 actions, 0 money-related this pull), `votes` warn
(2 money-related of 1,898 scanned — bill/amendment detail lookups capped at
300, newest-first, disclosed as an incomplete reason, not faked), `statements`
warn (0 gathered, same transient upstream cause), `synthesize` warn (no key,
same documented fail-closed behavior). Full JSON saved to
`tests/live_runs/massie/result.json`.

Same consistency checks passed: current-cycle outside support/oppose match
`sched_e`'s disclosed totals to the cent ($3,707,058.45 / $10,138,865.35);
donor-total math holds; 11 timeline bands / 44 events; 2 votes with full
citations.

### Rendering-layer verification (real headless browser, Playwright + Chromium)

Wrote `pw_test.py` (scratchpad) driving the real UI end-to-end: type a name →
click Search → confirm via the "Did You Mean?" picker → poll the live step
trace → verify charts, votes panel, and ZIP export. Run against the live REAL
server for both candidates:

| Check | Ossoff | Massie |
|---|---|---|
| Console errors | 0 | 0 |
| Page (JS) errors | 0 | 0 |
| CSP violations | 0 | 0 |
| `#money-chart` SVG | rendered, 42 nodes | rendered, 50 nodes |
| `#outside-chart` SVG | rendered, 62 nodes | rendered, 47 nodes |
| `#trend-chart` SVG | rendered, 32 nodes | rendered, 45 nodes |
| `#timeline-chart` SVG | rendered, 180 nodes | rendered, 88 nodes |
| Votes panel citation links | 8, e.g. `senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00148.htm` | 2, e.g. `clerk.house.gov/Votes/2026280` |
| Download-all ZIP | downloaded, 9 files, integrity-verified (`zipfile.testzip()` → `None`) | downloaded, 11 files (statements/search_log included this run — see Part 4), integrity-verified |

All SVGs render with real visible content; all votes panel entries carry
working citation links routed through the Phase 4 `safeHref()` scheme
allowlist; both ZIPs are valid, complete archives matching the documented
per-table CSV set.

### Demo-mode fallback confirmation

Stopped the server, `mv .env .env.hidden`, restarted via `bash run.sh` —
startup banner correctly read **"No .env found... Running without one is
fine too: the app falls back to DEMO MODE"** and **Mode: DEMO (no
FEC_API_KEY — simulated data)**. Ran one search (`{"name":"anything"}`) →
`demo: true` in the response, all 8 steps completed with the documented
fixture behavior (Ossoff fixture data, statements step correctly reporting
`"8 statements gathered (offline fixtures — set SEARXNG_URL for live)"` with
`backend: "fixtures"` — accurate in this exact state, since `SEARXNG_URL` was
not set for this particular restart). Restored `.env` (`mv .env.hidden
.env`), restarted via `bash run.sh` — confirmed **Mode: REAL (FEC_API_KEY
found)** and `/health` → `{"ok": true}` again. No `.env.hidden` left behind;
repo restored to its original state.

*(Note: the reproduction of the demo/live statements contamination described
in the Demo Mode Findings section above was a separate, additional step — a
third server instance launched with `SEARXNG_URL` explicitly set but no
`FEC_API_KEY` — done specifically to verify Part 1's trace with live
evidence, not part of the baseline demo-mode confirmation the instructions
asked for. That instance was also stopped before restoring `.env`.)*

---

## Part 4 — Targeted Security-Patch Regression Checks

- **statements.py SSRF guard vs. legitimate fetches:** confirmed **not
  blocking legitimate fetches**. The Massie UI run (a later, separate
  invocation than the JSON-only run above, made once the SearXNG instance's
  upstream engines had recovered from transient rate-limiting) actually
  exercised the live page-fetch dating tier against real external hosts —
  `fec.gov`, `localcandidates.org`, `civdotiq.org`, `opensecrets.org`,
  `factually.co` — all fetched successfully, two with real
  `date_source: "page_metadata"` recovered dates. No legitimate fetch was
  refused. (The `sched_a`/`sched_e`/`record`/`votes` runs above separately
  confirm the SSRF/validation work doesn't interfere with FEC/Congress.gov
  API calls, which don't go through `_http_get` at all.)
- **fec.py/congress.py candidate_id/state validation vs. legitimate
  selections:** confirmed **not rejecting** real Did-You-Mean picks. Both
  live searches passed real FEC-grammar `candidate_id`s
  (`S8GA00180`/`H2KY04121`) and real 2-letter `state`s (`GA`/`KY`) through
  `/search` end-to-end with no 400s, exactly the picker flow the frontend
  drives. Also confirmed structurally by `test_input_validation.py` (Part 2).
- **run.sh/.env sourcing REAL vs. DEMO:** confirmed both directions live —
  see Part 3's demo-mode fallback confirmation above.
- **synthesis.py prompt-injection delimiters / reconcile.py's four checks
  against a real report:** **could not be exercised against a live report**
  in this audit — no `ANTHROPIC_API_KEY` is configured (per the instructions'
  setup note, this is expected, not a finding), and both live runs correctly
  showed the `synthesize` step warning and skipping rather than failing or
  fabricating a report. This is the documented "warns, never touches the
  factual result" behavior, confirmed. The delimiter/reconcile logic itself
  was verified structurally and offline in Part 2 via
  `test_prompt_injection_guard.py` and `test_reconcile_sides.py` — this is a
  genuine gap in *live* coverage, not a failure; see "Anything that could not
  be verified" below.
- **export.js CSV formula-injection neutralization vs. legitimate negative
  dollar figures:** confirmed **not mangled**, using real data from the
  actual live export. Massie's `spender_transactions.csv` contains a real
  Schedule E refund row: `2025-10-20,,C00799031,O,-1860.77,ONMESSAGE
  INC,MEDIA PLACEMENT REFUND,false,false,F3X,4022620261359579102` — the
  `-1860.77` amount is stored as a raw, unquoted, unmangled negative number
  (no neutralizing leading apostrophe), exactly the regression the Phase 3
  fix was designed to avoid while still neutralizing genuine formula-shaped
  cells.
- **votes.py defusedxml vs. normal XML parsing:** confirmed **not broken**.
  Ossoff's real run parsed real Senate LIS XML end-to-end (8 money-related
  roll calls out of 3,849 scanned, each with a working
  `senate.gov/legislative/LIS/...` citation); Massie's real run parsed real
  House Clerk data (2 of 1,898 scanned, working `clerk.house.gov` citations).
  No parse failures, no `PARSE_FAILED` sentinels, in either live run.
- **Security headers/CSP — console violations, broken rendering:** confirmed
  **clean** — see the Playwright table in Part 3 (0 console errors, 0 CSP
  violations, all 4 chart types rendered with content, votes panel and ZIP
  export both functional) for both candidates.

---

## 1. Tests/checks performed

- Read `CLAUDE.md`, `README.md`, `Security_Recommendations.md` in full before
  touching code.
- Traced the `demo` flag from origin through all six data-gathering modules
  by direct code reading (Part 1), then reproduced the contamination finding
  live against a real running instance (not just inferred from source).
- Ran all 25 `tests/test_*.py` files individually plus
  `tests/run_export_tests.sh` (Node + Python export suite).
- Started the real app via `run.sh` against the repo's actual `.env`; ran two
  full live searches (Ossoff, Massie) through the documented
  `/candidates` → `/search` → poll `/status` resolver flow; saved both raw
  JSON exports.
- Wrote and ran a Playwright headless-Chromium script driving the real UI
  end-to-end for both candidates (search → picker → live step trace → charts
  → votes panel → ZIP download), checking console/page errors, CSP
  violations, SVG content, citation links, and ZIP integrity.
- Verified real exported CSV data for the CSV-injection-neutralization
  regression against an actual negative dollar figure from live data.
- Stopped the server, renamed `.env` away, restarted, ran a search, confirmed
  demo-mode fallback; restored `.env`, restarted, confirmed REAL mode.
- Separately reproduced the Part 1 demo/statements contamination finding live
  by launching a third instance with `SEARXNG_URL` set but no `FEC_API_KEY`.

## 2. What passed

- All 25 `tests/test_*.py` files (including all 15 named security regression
  tests).
- `tests/run_export_tests.sh` (34 checks, Node + Python).
- Both live full searches: all steps either `ok` or a correctly-documented
  `warn` (no fails); data internally consistent (composition vs. sched_e
  totals to the cent, donor-total math, timeline bands/events populated,
  vote citations present).
- REAL/DEMO mode switching via `.env` presence/absence, in both directions,
  confirmed live via startup banner and `/health`.
- Full Playwright rendering-layer pass for both candidates: 0 console errors,
  0 CSP violations, all 4 SVG chart types rendered with real content, votes
  panel citation links functional, ZIP export downloaded and byte-verified.
- All 6 Part 4 targeted regression checks that could be exercised live
  (SSRF guard, candidate_id/state validation, run.sh/.env sourcing, CSV
  negative-figure preservation, defusedxml parsing, security headers/CSP).

## 3. What failed

Nothing from the Phase 1–4 security work regressed. The one real finding —
demo-mode statement contamination plus a false step-log disclosure in that
state — is a **pre-existing** issue orthogonal to the four phases (see Demo
Mode Findings; classification: pre-existing issue, currently unresolved).

## 4. Any behavior that changed

None observed relative to what `Security_Recommendations.md` and `CLAUDE.md`
document — every checked behavior (bind default, `/health` shape, rate
limiting, CSRF handling, CSP headers, XML parsing, CSV neutralization,
`run.sh`/`.env` sourcing) matched its documented post-hardening description
exactly.

## 5. Anything that could not be verified

- **Live synthesis + reconcile.py against a real Anthropic report:** no
  `ANTHROPIC_API_KEY` was available (expected per instructions), so the
  `synthesize` step's fail-closed behavior was confirmed live, but the
  prompt-injection delimiters and the four `reconcile.py` checks running
  against genuine model output were only verified offline/structurally
  (Part 2's `test_prompt_injection_guard.py`/`test_reconcile_sides.py`), not
  against a live report in this audit.
- **Live statements SSRF-guard exercise was intermittent**, not because of
  anything in this app: the local SearXNG instance's upstream general-search
  engines (brave/duckduckgo/startpage, per its default config) were
  transiently rate-limited/CAPTCHA'd for parts of this audit (confirmed
  directly via `curl`'s `unresponsive_engines` field), causing both initial
  live runs to return 0 statements. A later run (once the engines recovered)
  did exercise real page fetches successfully — see Part 4 — but this was
  opportunistic, not something this audit could force on demand. This is an
  **environment/upstream-service issue**, not a code defect.
- Non-legislation House votes and pre-2023 House votes (documented as an open
  gap in `CLAUDE.md`'s "What's next," unrelated to security hardening) were
  not and could not be exercised — outside this audit's scope.

## 6. Overall conclusion

The Phase 1–4 security hardening pass is **regression-free** by every check
this audit could run: the full offline test suite (Python + Node) passes
clean, two live real-data runs against real FEC/Congress.gov data completed
correctly with internally consistent output, and a real headless-browser
pass confirms the rendering layer, CSP, and CSV export all behave exactly as
`Security_Recommendations.md` describes post-fix — including verifying the
one regression risk the checklist itself flagged for the CSV fix (a real
negative dollar figure from live data stays a real negative number).

Separately — and this is the priority finding of this audit, per the
instructions — the demo-mode trace requested in Part 1 surfaced a genuine,
live-reproduced integrity gap: `statements.py`'s fixtures-vs-live decision
does not honor the shared `demo` flag when `SEARXNG_URL` is configured (the
exact state this repo's own `.env` is in), and in that state the disclosed
step-log message actively misreports a live search as "offline fixtures."
This predates and is unrelated to the four security phases, so it does not
change the PASS verdict on the regression check this audit was primarily
chartered to perform — but it is real, reproducible, and directly undermines
the "visible process = self-check" principle `CLAUDE.md` states as its most
important rule. Recommend treating it as a near-term priority fix (see the
three concrete recommendations in the Demo Mode Findings section), not
folding it away as a footnote.

## SYSTEM ROLE
You are performing a post-security-update regression audit of this repository
(Campaign Tracker). Security_Recommendations.md documents a 4-phase hardening
pass (Phase 1 CRITICAL/SSRF, Phase 2 HIGH/input+transport+secrets, Phase 3
MEDIUM/defense-in-depth, Phase 4 LOW/cheap wins), all marked DONE. Your job is
to verify none of that work broke existing functionality, data accuracy, or
behavior — not to re-audit security itself.

## SETUP
- Read CLAUDE.md and README.md fully before touching code.
- Read Security_Recommendations.md's checklist (Phases 1-4) to know exactly
  what changed and why, including the specific test files each phase added.
- You may start/configure local services (SearXNG, etc.) needed to run the
  app, but do not alter application behavior to make something pass.
- No `ANTHROPIC_API_KEY` is configured — synthesis will fail/skip. Treat
  that as expected, not a finding, but confirm the failure mode itself is
  still the disclosed "warns, never touches the factual result" behavior
  the docs describe.
- You may start/configure local services (SearXNG, etc.) needed to run the app, and you have permission to install testing libraries like Playwright (including running playwright install for the browser binaries) to fulfill the headless UI checks, but do not alter application behavior to make something pass.

## PART 1 — DEMO MODE: TRACE, DON'T TRUST THE DOCS
This is the priority section of your report. Docs describe demo mode as: no
`FEC_API_KEY` (or `?demo=1`) → all factual stages run on synthetic fixtures,
synthesis skips. Congress/SearXNG keys are described as independently
optional — missing → that stage skips and discloses, rather than falling
back to fixtures. Verify this is actually how the code behaves, don't just
restate it. Specifically:

1. Find every place the `demo` flag (or equivalent) is set and read — trace
   it from wherever it originates (env check / `?demo=1` param) through
   app.py into fec.py, congress.py, votes.py, statements.py, synthesis.py,
   timeline.py. For each module, determine: does it branch on the SAME
   shared `demo` flag, or does it independently decide "use fixtures" based
   on its own missing key? These are different designs with different risk
   profiles — a shared flag is all-or-nothing; independent per-module checks
   create the possibility of a mixed run.
2. Determine whether a single run can ever mix real and synthetic data —
   e.g., FEC_API_KEY present (so `demo=False`) but some other condition
   causes one module to silently substitute fixture data anyway. This is
   the main risk to rule in or out: a chart or report section built from
   fixture data while everything around it is real, with no flag catching
   the mismatch.
3. Check `synthesis.py`'s `.synthesis_cache` (and any other candidate-keyed
   cache): since synthesis is documented to skip entirely in demo mode,
   confirm demo runs never touch the cache at all. If you find they DO touch
   it in any way, that's itself a finding — and in that case, confirm the
   cache key is sensitive to demo vs. real so a demo run's output could never
   be served back for a real query on the same candidate name.
4. Check whether the exported JSON/CSV/result object carries an explicit
   `demo: true/false` field IN THE DATA ITSELF (not the UI) that a
   downstream consumer — another script, a saved file reopened later,
   anything ingesting the export without the browser session — could check
   to tell synthetic from real. If it's missing, exported synthetic data
   is indistinguishable from real data once it leaves the running app.
5. Confirm `?demo=1` can force demo mode even with real keys configured —
   verify this, and check whether it's reachable in a way that could be hit
   by accident (a bookmarked/shared URL, a browser autocomplete, etc.).
6. Read CLAUDE.md's own stated rationale for why demo mode was built
   ("offer the zero-setup path first... good enough to confirm the install
   works... before they invest in real keys") and report that context
   plainly — this looks like a dev-onboarding convenience, not a feature
   aimed at end-stage analysis use, but confirm nothing you find in 1-4
   contradicts that read.

Report on this in plain terms: what demo mode is, exactly what triggers it,
exactly what happens to the data when it's active, whether it can
contaminate or be mistaken for a real run's output, and — based only on what
you actually found in the code, not the docs' framing of it — whether it's
safe to leave as-is, needs hardening (e.g., an explicit data-layer marker),
or should be removed for a tool where accidental synthetic output in real
analysis is unacceptable.

## PART 2 — REGRESSION TEST SUITE
Run every existing test, not a subset you judge relevant:
- All `tests/test_*.py` (plain python, run individually: `python3 tests/<file>.py`)
- `tests/run_export_tests.sh` (Node-based CSV/ZIP export tests)
Specifically confirm these security-fix regression tests pass:
test_ssrf_guard.py, test_input_validation.py, test_bind_host_default.py,
test_run_sh_env.py, test_searxng_secret_rotation.py,
test_csv_formula_injection.mjs, test_prompt_injection_guard.py,
test_rate_limiting.py, test_job_concurrency_cap.py,
test_page_fetch_streaming.py, test_csrf_protection.py,
test_security_headers.py, test_xxe_guard.py, test_debug_bind_coupling.py,
test_citation_link_scheme.mjs.
Note: test_export_zip.py had a pre-existing Node-prerequisite failure
unrelated to the security work as of the last audit pass — confirm whether
that's still true or newly regressed, don't just assume it's fine.

## PART 3 — LIVE FUNCTIONAL RUN
A .env file with real FEC_API_KEY / CONGRESS_API_KEY already exists — use it
as-is, do not create or modify it. Start the app via run.sh in the
background, and confirm it comes up in REAL mode (check the startup console
banner and /health, which now returns only {"ok": true}).

Run full searches for two candidates via the local API/curl: Jon Ossoff and
Thomas Massie.
For each:
- Go through the resolver: POST /candidates, then POST /search with the
  chosen candidate_id, then poll /status/<job_id> until complete.
- Synthesis will fail closed — confirm it fails the documented way in the
  step log.
- Verification (data layer, from the raw JSON): check the final /status
  result to ensure funding composition totals, outside-spending rows, the
  votes list, and the timeline bands are fully populated and internally
  consistent.
- Verification (rendering layer, requires an actual browser): write and run
  a short headless-browser script (Playwright, matching how Phase 3's CSP
  work was originally verified per Security_Recommendations.md) that loads
  the page, runs a search through the UI, and confirms: no console errors,
  the composition/outside-spending/timeline SVGs actually render with
  visible content (not blank), the votes panel renders citation links, and
  the "Download ZIP" button produces a real file. This is the only reliable
  way to catch a rendering regression — a passing JSON result does not prove
  the frontend drew it correctly.
- Save each run's raw JSON export to tests/live_runs/<ossoff|massie>/result.json
  (create this directory if needed).

Finally, stop the server, temporarily rename .env to .env.hidden, start the
server again, and run one search to confirm demo mode still works as traced
in Part 1. Restore .env to its original name when done, and confirm the app
comes back up in REAL mode afterward — don't leave the repo in a state where
your real keys are sitting under a renamed, gitignored-by-accident file.


## PART 4 — TARGETED SECURITY-PATCH REGRESSION CHECKS
- statements.py: SSRF guard (Phase 1) doesn't block legitimate page fetches
  during the real runs above
- fec.py / congress.py: candidate_id/state validation (Phase 2) doesn't
  reject any legitimate "Did You Mean?" selection
- run.sh / .env: sourcing still correctly switches REAL vs DEMO
- synthesis.py: prompt-injection delimiters don't corrupt legitimate
  excerpts; reconcile.py's four checks run clean against a real report
- export.js: CSV formula-injection neutralization doesn't mangle legitimate
  negative dollar figures in the real export data
- votes.py: defusedxml swap doesn't break normal XML parsing
- Security headers / CSP: no console violations, no broken rendering

## RULES
- Do not weaken, remove, or modify tests to make them pass.
- For any failure, classify as: regression / pre-existing issue / expected
  limitation / environment issue / test limitation / unresolved / other.

## OUTPUT
STATUS: PASS / REGRESSION FOUND / UNRESOLVED

### Demo Mode Findings
(mechanism, trigger, blast radius, contamination risk, recommendation —
per Part 1)

For each other issue: [Classification | Severity | Location | Evidence |
Expected Behavior | Observed Behavior]

Then:
1. Tests/checks performed
2. What passed
3. What failed
4. Any behavior that changed
5. Anything that could not be verified
6. Overall conclusion

If there's a lot to report, write it to a markdown file at the project root:
Security_Update_Tests/<date>-regression-audit.md

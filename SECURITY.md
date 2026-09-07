# Security

Campaign Tracker is a **local-first research tool**: a single-user Flask app
that reads public data (FEC, Congress.gov, House Clerk / Senate LIS, a
self-hosted SearXNG) and renders it. It has **no authentication, no
authorization, and no TLS of its own**, and it is not built to be a
multi-tenant service. Everything below follows from that.

This file is the public record of the tool's security posture: what the
defaults are, what they protect, and what you have to add yourself before
putting it anywhere but your own machine. Code comments that cite a
`SECURITY.md HIGH/MEDIUM/LOW` finding refer to the corresponding section
here.

---

## Reporting a vulnerability

Open a GitHub issue for anything already public (a dependency advisory, a
crash, a docs mismatch). For something exploitable that isn't public yet,
use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability) instead of an issue, so it isn't
disclosed before there's a fix. There is no bug bounty and no SLA — this is
a personal research tool.

Please include the version/commit, the run mode (demo or real), and the
steps that reproduce it. A step log (`/status/<job_id>`, or the trace shown
in the browser) is usually the fastest way to show what actually ran.

---

## Deployment posture (read before you expose it)

**The safe default is the one you get.** A bare `python3 app.py` or
`bash run.sh` binds to `127.0.0.1` — this machine only.

| If you… | Then you must… |
|---|---|
| Run it on your own laptop | Nothing. The defaults are for you. |
| Set `HOST=0.0.0.0` or a LAN address | Put a reverse proxy doing **TLS and authentication** in front of it first. The app prints a warning when you do this, and does not add auth on your behalf. |
| Run behind that real reverse proxy | Also set `BEHIND_PROXY=1`, so rate limiting keys on the real client IP from `X-Forwarded-For` instead of the proxy's. **Never set it without a real proxy** — any client can then spoof that header and dodge the limiter entirely. |
| Want the interactive debugger (`FLASK_DEBUG=1`) | Keep `HOST` on loopback. Setting both is refused at startup (exit 1, no bind): Werkzeug's debugger is arbitrary code execution for anyone who can reach the port. |
| Expect several concurrent searches | Raise `MAX_CONCURRENT_JOBS` (default `8`) only as far as your FEC/Congress quota supports. It bounds running jobs, and `/search` returns 429 past the cap. |

Traffic to the app is **plaintext HTTP unless you terminate TLS in front of
it.** That includes the Anthropic key posted from the UI field — it is never
written to disk, logs, the job record, or any response, but "never stored"
is not "never interceptable." TLS is your job, not the app's.

If you host your SearXNG instance somewhere other than localhost, lock it
down (Cloudflare Access, Tailscale, or equivalent). `docker/searxng/` binds
to `127.0.0.1` for exactly this reason.

---

## Key and secret handling

- `FEC_API_KEY`, `CONGRESS_API_KEY`, `SEARXNG_URL` are **server-side**: put
  them in `.env` (gitignored; `cp .env.example .env`) or the shell
  environment. `run.sh` sources `.env` and carries no key values itself, so
  the tracked launcher never has to be scrubbed before you share the folder.
- The **Anthropic key is UI-only and request-scoped** — no environment
  variable, no config file. It lives in one local variable for the length of
  a single API call, is cleared from the page on submit, and is never
  written into the job record, the result, a log line, or any `/status`
  response. Leave the field blank and the tool is byte-for-byte the factual
  six-stage version.
- Nothing in `.env` or the UI field is ever echoed back by an endpoint.
  `/health` is a plain `{"ok": true}` — it deliberately does **not** report
  whether a key is configured.

---

## What is hardened, by severity

The tool went through a four-phase hardening pass before public release.
Each item below is closed in code and pinned by an offline regression test
in `tests/`; the severity labels are the ones the code comments cite.

**CRITICAL / HIGH**

- **SSRF guard on outbound fetches** (`statements.py`): the page-fetch dating
  tier follows URLs that come from web-search results, i.e. from outside the
  tool. Hosts are validated before the transport is ever invoked, redirects
  are bounded, and private/link-local/loopback targets are refused.
  (`tests/test_ssrf_guard.py`)
- **Input validation on values interpolated into upstream API paths**:
  `candidate_id` and `state` arrive from the "Did You Mean?" picker's POST
  body and are interpolated unencoded into FEC/Congress.gov paths, so both
  are matched against the real upstream grammar and rejected at the route
  otherwise. (`tests/test_input_validation.py`)
- **Safe bind default**: `127.0.0.1` unless `HOST` says otherwise, with a
  printed warning when it does. (`tests/test_bind_host_default.py`)
- **Credential handling in the tracked launcher**: `run.sh` sources `.env`
  rather than holding key values. (`tests/test_run_sh_env.py`)
- **SearXNG secret rotation actually rotates**: three files disagreed on the
  placeholder string, so the documented `sed` command matched nothing, exited
  0, and every clone that followed the setup kept running the same publicly
  known session secret with no signal that rotation had failed. One documented
  place for the command now, and the placeholder matches.
  (`tests/test_searxng_secret_rotation.py`)

**MEDIUM**

- **Rate limiting is a hard dependency.** `flask-limiter` used to degrade to
  a no-op decorator when missing — silently running with no limiting at all.
  It is a plain import now: a broken install fails loudly. `/search` is
  limited, and `/status` (previously unlimited, and polled every 750 ms) has
  its own ceiling well above what the UI generates.
  (`tests/test_rate_limiting.py`)
- **Concurrent job cap.** Every `/search` spawns a worker thread with its own
  page-fetch pool inside; `MAX_CONCURRENT_JOBS` bounds running jobs, checked
  and created under one lock. (`tests/test_job_concurrency_cap.py`)
- **CSRF / same-origin check** on both POST routes.
  (`tests/test_csrf_protection.py`)
- **Security headers**: CSP, Referrer-Policy, Permissions-Policy, plus the
  existing X-Content-Type-Options / X-Frame-Options. The page loads no
  external resources at all, so the CSP needs no `unsafe-inline` — the one
  remaining inline style was moved to a CSS class to keep it that way.
  (`tests/test_security_headers.py`)
- **Bounded page fetches**: socket-level read caps and deadlines, so a
  slow-drip or oversized response can't tie up a worker.
  (`tests/test_page_fetch_streaming.py`)
- **CSV/DDE formula-injection neutralization** in the client-side export:
  statement titles and FEC free-text fields are not sanitized upstream, so a
  cell beginning `=`, `+`, `-`, `@`, tab, or CR is prefixed with an
  apostrophe — **except** plain numbers, because this tool exports
  legitimate negative figures (refunds, net donor totals) that must stay
  numbers. (`tests/test_csv_formula_injection.mjs`)
- **Prompt-injection delimiters** around the data handed to the synthesis
  model, which includes third-party web excerpts.
  (`tests/test_prompt_injection_guard.py`)
- **`ProxyFix` is opt-in** (`BEHIND_PROXY=1`) — see the table above.

**LOW / informational**

- **`FLASK_DEBUG=1` + a non-loopback `HOST` is refused outright**, not merely
  warned about: individually both defaults are safe, and the combination is
  arbitrary code execution reachable from the network.
  (`tests/test_debug_bind_coupling.py`)
- **XXE / entity-expansion guard** (`votes.py`): all XML parsing goes through
  `defusedxml`, and every `except ET.ParseError` was widened to catch
  `DefusedXmlException` too — otherwise a *blocked* payload would crash the
  run instead of being disclosed, or worse, be misreported as "the member
  wasn't on this roll," which is exactly the fact-invention the integrity
  rules exist to prevent. (`tests/test_xxe_guard.py`)
- **Link-scheme allowlist on rendered citation links** (`safeHref` in
  `static/app.js`): latent rather than exploitable — citation URLs come from
  fixed clerk.house.gov/senate.gov templates today — but if they ever become
  data-derived, `http`/`https` only is enforced at the render site.
  (`tests/test_citation_link_scheme.mjs`)
- **`/health` no longer reports `has_fec_key`** — reconnaissance value for an
  unauthenticated caller (real deployment vs demo), with no consumer in the
  app. The operator who needs it gets it from the startup banner.
- **Python bytecode caches and personal run artifacts** are gitignored, after
  both were committed in error earlier in the project's life.

---

## Data-integrity guarantees that are also security properties

The tool's integrity rules are not separate from its security posture — a
tool that quietly reports synthetic or partial data as real is a
correctness *and* trust failure:

- **Demo mode is fully synthetic and makes no network calls**, whatever your
  environment holds. The statements stage is handed an explicit
  `FORCE_OFFLINE` sentinel rather than an empty string that could fall
  through to `SEARXNG_URL`.
- **Every run says what it is.** `result["demo"]` and `/status`'s `demo`
  flag it at the run level, each track carries `_demo`, and demo exports are
  named `demo_*` — so a file that has left the app still identifies itself.
- **The step log describes what ran, not what was intended.** Stage messages
  are derived from the collector's actual backend and the data's actual
  state. A disclosure that reports intent can be wrong; one derived from the
  run cannot.
- **Incompleteness is disclosed, never papered over** — pagination
  shortfalls, lookup caps, skipped stages, and failed fetches each surface as
  their own warning with a reason, and are never silently rendered as a zero.
- **The one interpretive step is machine-checked.** The plain-language report
  is reconciled against the source JSON (`reconcile.py`) before it is shown,
  and the two most error-prone number blocks are rendered deterministically
  rather than written by the model.

---

## Known limits (by design, not oversight)

- No authentication or authorization anywhere. Anyone who can reach the port
  can run searches with your configured keys.
- The in-process job registry (`JOBS`) is single-worker only. Run multiple
  gunicorn workers and `/status` polls will miss jobs; that needs Redis, not
  a config flag.
- The synthesis prompt includes third-party web excerpts. They are delimited
  and the output is reconciled against the JSON, but no prompt boundary is
  absolute — that is why the report can only *restate* cited facts and why
  the factual result stands on its own without it.
- Dependencies are pinned in `requirements.txt`; keeping them current is on
  the operator.

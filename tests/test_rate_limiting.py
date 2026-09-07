"""Regression test for two SECURITY.md Phase 3 MEDIUM
findings in app.py's rate limiting:

  1. flask-limiter degraded to a no-op decorator (returns the function
     unchanged) when the package was missing, with NO startup warning — so
     a broken/incomplete install silently ran with NO rate limiting at all.
     Fix: it's a hard import now (no try/except fallback); requirements.txt
     no longer calls it optional.
  2. `/status/<job_id>` was completely unlimited (only `/search` and
     `/candidates` carried a limit), and the limiter's `key_func` was
     `get_remote_address` with no `ProxyFix` — behind any reverse proxy
     every client collapses into one bucket keyed on the proxy's own IP,
     so one user exhausts the limit for everyone.
     Fix: `/status` now carries its own (much higher, since the frontend
     polls it every 750ms per running job) limit, and `ProxyFix` is applied
     — but ONLY when the operator opts in via `BEHIND_PROXY=1`, since
     applying it unconditionally would let any direct client spoof
     X-Forwarded-For and make the limiter key on a fake IP.

This test drives the real `app_module.create_app()` and a real Flask test
client issuing enough rapid requests to actually trip the configured limits
— not just asserting the rate strings appear as source text — so a wiring
mistake (limiter created but decorator not applied, wrong route decorated)
would be caught.

Run from the repo root:  python3 tests/test_rate_limiting.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("FEC_API_KEY", None)
os.environ.pop("CONGRESS_API_KEY", None)
os.environ.pop("SEARXNG_URL", None)
os.environ.pop("BEHIND_PROXY", None)

import app as app_module  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    # ── 1. hard dependency: no optional-degrade fallback left in the module ──
    check("app.py no longer defines the old _HAS_LIMITER optional-degrade flag",
          not hasattr(app_module, "_HAS_LIMITER"))
    check("flask_limiter.Limiter is a real hard import (not None/stubbed)",
          app_module.Limiter is not None and app_module.Limiter.__module__.startswith("flask_limiter"))
    req_txt = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    check("requirements.txt no longer describes flask-limiter as optional/"
          "degrades-gracefully (the old wording that matched the removed "
          "no-op fallback)",
          "degrades gracefully" not in req_txt.lower()
          and "optional — app" not in req_txt.lower())
    check("requirements.txt now calls it a hard dependency",
          "hard dependency" in req_txt.lower())

    # ── 2a. ProxyFix is opt-in, not automatic ─────────────────────────────────
    os.environ.pop("BEHIND_PROXY", None)
    app_no_proxy = app_module.create_app()
    check("wsgi_app is NOT wrapped in ProxyFix when BEHIND_PROXY is unset",
          "ProxyFix" not in type(app_no_proxy.wsgi_app).__name__,
          type(app_no_proxy.wsgi_app).__name__)

    os.environ["BEHIND_PROXY"] = "1"
    try:
        app_with_proxy = app_module.create_app()
        check("wsgi_app IS wrapped in ProxyFix when BEHIND_PROXY=1",
              type(app_with_proxy.wsgi_app).__name__ == "ProxyFix",
              type(app_with_proxy.wsgi_app).__name__)
    finally:
        os.environ.pop("BEHIND_PROXY", None)

    # ── 2b. /status now actually enforces a cap (was previously unlimited) ───
    # A burst well past 240/min, hitting an endpoint that does real work
    # (dict lookup + 404) so it's fast enough to run in-process.
    app_module.JOBS.clear()
    client = app_module.app.test_client()
    codes = [client.get("/status/does-not-exist").status_code for _ in range(260)]
    check("a 260-request /status burst is NOT all 404 (some hit the new limit)",
          429 in codes, f"unique codes seen: {sorted(set(codes))}")
    check("requests before the limit tripped still got a normal 404 "
          "(the limiter isn't rejecting everything)",
          codes[0] == 404, codes[0])

    # ── 2c. /search's existing 20/min limit still applies (unchanged, but
    #        confirm it's still wired after the app-factory refactor) ────────
    app_module.JOBS.clear()
    # Neutralize the separate concurrent-jobs cap (a different, lower-level
    # guard tested in test_job_concurrency_cap.py) so this burst exercises
    # ONLY the rate limiter, not job-slot exhaustion.
    orig_cap = app_module._MAX_CONCURRENT_JOBS
    app_module._MAX_CONCURRENT_JOBS = 10_000
    try:
        client2 = app_module.app.test_client()
        codes2 = []
        for _ in range(25):
            r = client2.post("/search", json={"name": "Rate Limit Test Candidate"})
            codes2.append(r.status_code)
        check("a 25-request /search burst trips the 20/min limit (429 appears)",
              429 in codes2, f"codes: {codes2}")
        check("requests before the limit tripped succeeded normally (200)",
              codes2[0] == 200, codes2[0])
    finally:
        app_module._MAX_CONCURRENT_JOBS = orig_cap
        app_module.JOBS.clear()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All rate-limiting checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

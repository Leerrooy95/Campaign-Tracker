"""Regression test for the SECURITY.md Phase 3 MEDIUM
finding: only X-Content-Type-Options and X-Frame-Options were set — no
Content-Security-Policy, Referrer-Policy, Permissions-Policy, or HSTS, and
templates/index.html had no CSP meta tag either. The audit called this a
missing defense-in-depth layer (every innerHTML sink in static/app.js was
separately reviewed and escapes correctly), not an exploitable hole on its
own — but the right backstop for a page that renders third-party web
excerpts and an optional model-written report.

Fix: app.py's `_headers` after_request hook now sets a real CSP (no
'unsafe-inline' anywhere — the page has no external resources and the one
remaining inline style="" usage, legend swatch background colors in
static/app.js, was converted to CSS classes in static/app.css's `.c-*`
utilities), Referrer-Policy, Permissions-Policy, and a conditional HSTS
(only when request.is_secure, which reflects X-Forwarded-Proto once
ProxyFix/BEHIND_PROXY is active — HSTS is meaningless, and browsers ignore
it, over plain HTTP).

This test checks the headers on real responses from the Flask test client.
It CANNOT prove the CSP doesn't break the page in a real browser (no DOM,
no CSS/JS execution) — that was verified separately, live, with headless
Chromium via Playwright: loaded the page, ran a full demo search through
the "Did You Mean?" picker, and confirmed (a) zero CSP violation console
messages, (b) all 9 legend swatches rendered with their real background
colors (proving the class-based swatch fix works under a style-src with no
unsafe-inline), and (c) all 3 chart SVGs rendered with real content. That
run isn't automated here (no Playwright dependency in requirements.txt) —
this test covers what a fast, dependency-free unit test can: the headers
are actually present, correctly shaped, and gated correctly.

Run from the repo root:  python3 tests/test_security_headers.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("FEC_API_KEY", None)
os.environ.pop("CONGRESS_API_KEY", None)
os.environ.pop("SEARXNG_URL", None)

import app as app_module  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    client = app_module.app.test_client()

    for path in ("/", "/health"):
        resp = client.get(path)
        h = resp.headers

        check(f"{path}: X-Content-Type-Options: nosniff (pre-existing, unchanged)",
              h.get("X-Content-Type-Options") == "nosniff")
        check(f"{path}: X-Frame-Options: DENY (pre-existing, unchanged)",
              h.get("X-Frame-Options") == "DENY")

        csp = h.get("Content-Security-Policy", "")
        check(f"{path}: Content-Security-Policy is present", bool(csp))
        for directive in ("default-src 'self'", "script-src 'self'",
                          "style-src 'self'", "object-src 'none'",
                          "frame-ancestors 'none'"):
            check(f"{path}: CSP includes {directive!r}", directive in csp)
        check(f"{path}: CSP does NOT permit 'unsafe-inline' anywhere "
              f"(the page needs none — see static/app.css's .c-* swatch classes)",
              "unsafe-inline" not in csp)
        check(f"{path}: CSP does NOT permit 'unsafe-eval'",
              "unsafe-eval" not in csp)

        check(f"{path}: Referrer-Policy is set",
              h.get("Referrer-Policy") == "no-referrer")
        check(f"{path}: Permissions-Policy is set and restricts sensitive APIs",
              bool(h.get("Permissions-Policy"))
              and "geolocation=()" in h.get("Permissions-Policy", ""))

        # HSTS must NOT be sent over plain HTTP — the Flask test client's
        # default request is not secure, so this proves the gate works, not
        # just that the header exists unconditionally.
        check(f"{path}: HSTS is NOT sent over a non-secure request "
              f"(would be meaningless/ignored by browsers over HTTP)",
              "Strict-Transport-Security" not in h)

    # ── HSTS DOES fire once the request looks secure (simulating what
    #    ProxyFix would produce from a real X-Forwarded-Proto: https) ────────
    with app_module.app.test_request_context("/", headers={"X-Forwarded-Proto": "https"}):
        pass   # (context alone doesn't run after_request; test via the client below)

    # Flask's test client can simulate an HTTPS request directly via base_url.
    https_client = app_module.app.test_client()
    resp = https_client.get("/", base_url="https://localhost")
    check("HSTS IS sent when the request is actually secure (https:// base_url)",
          "Strict-Transport-Security" in resp.headers,
          dict(resp.headers))
    if "Strict-Transport-Security" in resp.headers:
        check("HSTS value includes a max-age and includeSubDomains",
              "max-age=" in resp.headers["Strict-Transport-Security"]
              and "includeSubDomains" in resp.headers["Strict-Transport-Security"])

    # ── no remaining inline style="" attributes anywhere in the frontend ─────
    repo = Path(__file__).resolve().parent.parent
    for f in ("static/app.js", "static/export.js", "templates/index.html"):
        text = (repo / f).read_text()
        check(f"{f}: no inline style=\"...\" attributes remain "
              f"(would be blocked by the new style-src with no unsafe-inline)",
              'style="' not in text)

    # ── the swatch color utility classes actually exist in app.css ──────────
    css = (repo / "static" / "app.css").read_text()
    for cls in (".c-green", ".c-red", ".c-purple", ".c-blue", ".c-gold", ".c-orange"):
        check(f"static/app.css defines {cls}", cls in css)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All security-header checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

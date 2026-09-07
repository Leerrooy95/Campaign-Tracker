"""Regression test for the Security_Recommendations.md Phase 3 MEDIUM CSRF
finding: neither `POST /candidates` nor `POST /search` carried a CSRF token,
and both accepted `request.form` as a fallback when JSON parsing failed —
which made them reachable by a plain cross-origin HTML <form> POST as a CORS
"simple request" (no preflight required, no cooperation from the browser).
A visited malicious page could force jobs on any reachable instance, burning
the shared FEC/Congress quota.

Fix (app.py's `_is_same_origin` / `_json_body_or_none`, see the CSRF note
above them):
  1. `request.form` fallback removed — both routes now require a real
     `application/json` body via `_json_body_or_none`. A native HTML <form>
     POST can never send that Content-Type (it's not one of the three CORS
     "simple" content types), so a bare cross-site form submission is
     rejected outright.
  2. Defense in depth: `_is_same_origin` rejects a request whose Origin (or
     Referer, if Origin is absent) names a different host than the
     request's own Host header.

This test drives the real Flask app via its test client, simulating the two
concrete attack shapes the audit named (a classic HTML form's
x-www-form-urlencoded body, and a cross-origin JSON POST carrying a
mismatched Origin) alongside proving the legitimate same-origin JSON flow
(exactly what static/app.js sends) still works.

Run from the repo root:  python3 tests/test_csrf_protection.py
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

    for route in ("/candidates", "/search"):
        app_module.JOBS.clear()

        # ── attack shape 1: a classic <form method=post> submission ──────────
        # This is exactly what a cross-site attacker page can send with NO
        # JavaScript, no CORS cooperation, no preflight — the textbook CSRF
        # vector. Flask's test client with `data=` (not `json=`) sends
        # application/x-www-form-urlencoded, matching a real <form> post.
        resp = client.post(route, data={"name": "CSRF Attack Candidate"})
        check(f"{route}: classic form-urlencoded POST is rejected (not silently "
              f"parsed via the old request.form fallback)",
              resp.status_code == 400, f"got {resp.status_code}: {resp.get_json()}")
        check(f"{route}: form-POST rejection does not leak a job_id",
              "job_id" not in (resp.get_json() or {}))

        # ── attack shape 2: a cross-origin JSON POST with a mismatched Origin ──
        # Simulates what would reach the server if a browser's CORS
        # preflight somehow let a cross-origin fetch() through (e.g. a
        # future misconfiguration that adds permissive CORS headers) — the
        # Origin check is the defense-in-depth layer for exactly that case.
        resp = client.post(route, json={"name": "CSRF Attack Candidate"},
                           headers={"Origin": "http://attacker.example",
                                    "Host": "victim-instance.example"})
        check(f"{route}: cross-origin JSON POST (mismatched Origin) is rejected",
              resp.status_code == 403, f"got {resp.status_code}: {resp.get_json()}")

        # ── legitimate flow: same-origin JSON POST, exactly what static/app.js sends ──
        resp = client.post(route, json={"name": "Ossoff"},
                           headers={"Content-Type": "application/json"})
        check(f"{route}: legitimate same-origin JSON POST (no Origin header, "
              f"as the Flask test client and most non-browser callers send) "
              f"still succeeds",
              resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")

        # ── legitimate flow: same-origin JSON POST WITH a matching Origin ──────
        # (what an actual browser fetch() from the app's own page sends —
        # Origin always present, host always matches).
        resp = client.post(route, json={"name": "Ossoff"},
                           headers={"Origin": "http://localhost",
                                    "Host": "localhost"})
        check(f"{route}: same-origin JSON POST WITH a matching Origin header succeeds",
              resp.status_code == 200, f"got {resp.status_code}: {resp.get_json()}")

        app_module.JOBS.clear()

    # ── _is_same_origin unit checks (Referer fallback, malformed headers) ────
    class FakeReq:
        def __init__(self, headers):
            self.headers = headers

    check("Referer fallback: matching Referer host passes",
          app_module._is_same_origin(FakeReq({"Host": "example.com",
                                              "Referer": "http://example.com/page"})))
    check("Referer fallback: mismatched Referer host fails",
          not app_module._is_same_origin(FakeReq({"Host": "example.com",
                                                   "Referer": "http://evil.example/page"})))
    check("no Origin, no Referer: treated as same-origin (non-browser client)",
          app_module._is_same_origin(FakeReq({"Host": "example.com"})))
    check("malformed Origin URL: fails closed, not open",
          not app_module._is_same_origin(FakeReq({"Host": "example.com",
                                                    "Origin": "not a url ://[["})))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All CSRF protection checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Regression test for the statements.py page-fetch SSRF guard (Security
Recommendations CRITICAL #1 and #2).

Before this fix, `_http_get` and `_canonical_target` fetched attacker-influenced
URLs (raw SearXNG results, and a fetched page's own declared canonical/og:url)
with no host/IP validation and automatic redirect-following on both transports
— a single indexed page could steer the server at internal targets (RFC1918,
loopback, link-local incl. 169.254.169.254 cloud metadata), and a redirect hop
bypassed even a hostname check. `_is_safe_url` now resolves and validates the
host BEFORE every request, including every redirect hop, and both transports
have automatic redirect-following disabled so each hop is re-checked.

Offline: DNS resolution is mocked via monkeypatching `socket.getaddrinfo`, and
the actual network transport (`_http_get_once`) is mocked/counted so a test
failure here means the guard let a request through, not that the network
behaved unexpectedly.

Run from the repo root:  python3 tests/test_ssrf_guard.py
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import statements as st  # noqa: E402


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ── _is_safe_url: host resolution gating ─────────────────────────────────
    def addrinfo_for(ip):
        return [(2, 1, 6, "", (ip, 0))]

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("10.0.0.5")):
        check("private RFC1918 host rejected", st._is_safe_url("http://internal.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("169.254.169.254")):
        check("cloud metadata address rejected", st._is_safe_url("http://metadata.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("127.0.0.1")):
        check("loopback rejected", st._is_safe_url("http://localhost.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("224.0.0.1")):
        check("multicast rejected", st._is_safe_url("http://mcast.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("0.0.0.0")):
        check("unspecified rejected", st._is_safe_url("http://unspec.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("::ffff:127.0.0.1")):
        check("IPv4-mapped IPv6 loopback rejected", st._is_safe_url("http://v6mapped.example/") is False)

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("93.184.216.34")):
        check("public IP accepted", st._is_safe_url("http://public.example/") is True)
        check("non-http(s) scheme rejected even with public IP",
              st._is_safe_url("file:///etc/passwd") is False)
        check("ftp scheme rejected", st._is_safe_url("ftp://public.example/") is False)

    check("no hostname rejected", st._is_safe_url("http:///path") is False)

    def boom(*a, **k):
        raise OSError("no such host")
    with mock.patch("socket.getaddrinfo", side_effect=boom):
        check("unresolvable host fails closed", st._is_safe_url("http://nope.invalid/") is False)

    # ── _http_get: never calls the transport for an unsafe host ──────────────
    calls = []

    def tracked_transport(url):
        calls.append(url)
        return None, None

    with mock.patch("socket.getaddrinfo", return_value=addrinfo_for("127.0.0.1")), \
         mock.patch.object(st, "_http_get_once", side_effect=tracked_transport):
        result = st._http_get("http://attacker-indexed.example/page")
        check("_http_get returns None for an unsafe host", result is None)
        check("_http_get_once never invoked for an unsafe host", calls == [],
              detail=f"calls={calls}")

    # ── _http_get: refuses to follow a redirect into an unsafe target ────────
    def addrinfo_by_host(host, *a, **k):
        if host == "safe.example":
            return addrinfo_for("93.184.216.34")
        if host == "evil.internal":
            return addrinfo_for("10.0.0.9")
        raise OSError("unexpected host in test")

    redirect_calls = []

    def redirecting_transport(url):
        redirect_calls.append(url)
        if url == "http://safe.example/stub":
            return None, "http://evil.internal/steal"
        return "<html>should never be reached</html>", None

    with mock.patch("socket.getaddrinfo", side_effect=addrinfo_by_host), \
         mock.patch.object(st, "_http_get_once", side_effect=redirecting_transport):
        result = st._http_get("http://safe.example/stub")
        check("redirect into a private target is refused", result is None)
        check("evil.internal never fetched", "http://evil.internal/steal" not in redirect_calls,
              detail=f"redirect_calls={redirect_calls}")

    # ── _http_get: a redirect chain among SAFE hosts is still followed ───────
    def addrinfo_safe(*a, **k):
        return addrinfo_for("93.184.216.34")

    chain_calls = []

    def chained_transport(url):
        chain_calls.append(url)
        if url == "http://safe.example/a":
            return None, "http://safe.example/b"
        if url == "http://safe.example/b":
            return "<html>final content</html>", None
        return None, None

    with mock.patch("socket.getaddrinfo", side_effect=addrinfo_safe), \
         mock.patch.object(st, "_http_get_once", side_effect=chained_transport):
        result = st._http_get("http://safe.example/a")
        check("a single safe redirect hop is still followed", result == "<html>final content</html>")

    # ── _http_get: redirect loop among safe hosts is bounded, not infinite ───
    loop_calls = []

    def looping_transport(url):
        loop_calls.append(url)
        return None, "http://safe.example/a" if url == "http://safe.example/b" else "http://safe.example/b"

    with mock.patch("socket.getaddrinfo", side_effect=addrinfo_safe), \
         mock.patch.object(st, "_http_get_once", side_effect=looping_transport):
        result = st._http_get("http://safe.example/a")
        check("redirect loop terminates (bounded, no hang)", result is None)
        check("redirect loop bounded by _MAX_REDIRECTS+1 requests",
              len(loop_calls) == st._MAX_REDIRECTS + 1,
              detail=f"{len(loop_calls)} calls, expected {st._MAX_REDIRECTS + 1}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All SSRF guard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

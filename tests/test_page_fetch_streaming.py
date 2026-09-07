"""Regression test for the SECURITY.md Phase 3 MEDIUM
finding: statements.py's PRIMARY page-fetch transport (curl_cffi, used when
installed — which it is for Congress.gov's Cloudflare bypass too) applied
`_FETCH_MAX_BYTES` as `resp.text[:_FETCH_MAX_BYTES]` — a slice AFTER the
non-streaming `.get()` had already downloaded and decoded the ENTIRE
response body. The documented 256 KB read bound therefore did not hold on
the transport actually used in production; only the urllib FALLBACK path
(`resp.read(n)`) bounded correctly. A malicious or merely large page — or an
internal endpoint streaming indefinitely — was read fully into memory, 8
concurrent (`_FETCH_WORKERS`). Combined with the SSRF finding fixed in Phase
1, this was a real memory-exhaustion amplifier.

Fix: the curl_cffi branch now requests with `stream=True` and reads via
`iter_content()` in 8 KB chunks, breaking out (and closing the connection)
the moment `_FETCH_MAX_BYTES` is reached — the cap is enforced AT THE
SOCKET, not by slicing an already-fully-buffered string.

This test proves the fix with a REAL local HTTP server that slow-drips a
16 MB response (one chunk every 5ms, so the client has many opportunities
to bail early if it's actually streaming) and counts the bytes the SERVER
actually wrote to the socket. Before the fix this counter would run to the
full ~16 MB; after the fix it should stop within roughly one chunk's worth
of the 256 KB cap. This is deliberately NOT a mocked/patched test — a mock
would just describe intended behavior, not prove the real curl_cffi
`stream=True` + `iter_content()` call sequence actually behaves this way.

Run from the repo root:  python3 tests/test_page_fetch_streaming.py
"""
import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import statements as st  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


CHUNK = b"<html>" + b"A" * 8192
N_CHUNKS = 2000   # ~16.4 MB if fully drained
FULL_SIZE = len(CHUNK) * N_CHUNKS


def make_server(sent_total: list, per_chunk_delay: float):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):   # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                for _ in range(N_CHUNKS):
                    self.wfile.write(CHUNK)
                    sent_total[0] += len(CHUNK)
                    self.wfile.flush()
                    if per_chunk_delay:
                        time.sleep(per_chunk_delay)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass   # client closed early — exactly what we want to prove

        def log_message(self, *a):   # silence default request logging
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def main() -> int:
    check("_FETCH_MAX_BYTES is the expected 256 KB cap",
          st._FETCH_MAX_BYTES == 262_144, st._FETCH_MAX_BYTES)

    if not st._HAVE_CURL:
        print("curl_cffi not installed in this environment — the bug this "
              "test targets is specific to the curl_cffi transport branch. "
              "The urllib fallback was already correct (resp.read(n)) and "
              "is not what changed. Nothing to verify here; treat as a "
              "skip, not a failure.")
        return 0

    sent_total = [0]
    srv, port = make_server(sent_total, per_chunk_delay=0.005)
    try:
        url = f"http://127.0.0.1:{port}/"
        start = time.time()
        # _http_get_once, not _http_get: bypasses the Phase-1 SSRF loopback
        # guard on purpose — that guard is tested separately
        # (test_ssrf_guard.py); this test targets the transport's read
        # bound against a real slow-drip server, which has to be local.
        html, redirect = st._http_get_once(url)
        elapsed = time.time() - start
        time.sleep(0.3)   # let any already-in-flight writes land

        check("request returned HTML (not a redirect/failure)",
              html is not None, f"redirect={redirect!r}")
        check("returned content is capped at _FETCH_MAX_BYTES",
              html is not None and len(html) <= st._FETCH_MAX_BYTES,
              len(html) if html else None)
        check("the fetch didn't block for the full slow-drip duration "
              "(2000 * 5ms = 10s if it read everything)",
              elapsed < 5.0, f"{elapsed:.2f}s")
        # The real proof: the SERVER's own byte counter. If the client
        # truly streamed-and-broke-early, the server writes far less than
        # the full body before hitting a closed/reset connection. Generous
        # multiplier (4x the cap) to absorb OS/library buffering slack
        # without weakening the test into a tautology — the full body is
        # ~64x the cap, so this margin still clearly distinguishes
        # "streamed and stopped" from "downloaded everything".
        check("server-side byte count proves the transfer stopped early "
              "(bound enforced at the socket, not by slicing a full buffer)",
              sent_total[0] < st._FETCH_MAX_BYTES * 4,
              f"server sent {sent_total[0]} bytes; full body would be {FULL_SIZE}; "
              f"cap*4={st._FETCH_MAX_BYTES * 4}")
    finally:
        srv.shutdown()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All page-fetch streaming-bound checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

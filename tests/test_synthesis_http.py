"""Offline test for the bounded-timeout + one-retry HTTP call in
synthesis._call_claude. Spins up a local mock server — no real API, no key,
nothing leaves the machine. Exercises five paths: success, 529-then-retry
(honoring Retry-After), permanent-error-no-retry (401), full server hang
(bounded by the daemon-thread wall clock — the DNS-stall class urlopen's
timeout can't see), and a slow-drip body (bounded by the read1() loop; plain
read(n) blocks until n bytes and would starve the deadline check — a real bug
this test caught).

Run from the repo root:  python3 tests/test_synthesis_http.py
Takes ~30s (the hang/drip cases must actually wait out the shrunken clocks).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import synthesis  # noqa: E402

OK_BODY = json.dumps({
    "content": [{"type": "text", "text": "# mock report"}],
    "stop_reason": "end_turn",
}).encode()

STATE = {"mode": "ok", "hits": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # quiet
        pass

    def do_POST(self):
        STATE["hits"] += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = STATE["mode"]
        if mode == "ok":
            self._ok()
        elif mode == "529_then_ok":
            if STATE["hits"] == 1:
                self._err(529, "overloaded_error", "Overloaded", retry_after="1")
            else:
                self._ok()
        elif mode == "401":
            self._err(401, "authentication_error", "invalid x-api-key")
        elif mode == "hang":
            time.sleep(999)
        elif mode == "drip":
            # send headers, then trickle the body slower than the wall clock allows
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(OK_BODY) + 10000))
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(1)
            except Exception:
                pass

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(OK_BODY)))
        self.end_headers()
        self.wfile.write(OK_BODY)

    def _err(self, code, etype, msg, retry_after=None):
        body = json.dumps({"type": "error",
                           "error": {"type": etype, "message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_case(name, mode, expect_ok, expect_hits, max_seconds):
    STATE["mode"], STATE["hits"] = mode, 0
    t0 = time.monotonic()
    try:
        out = synthesis._call_claude("prompt", "sk-ant-test", "claude-sonnet-5")
        ok, detail = True, out
    except synthesis.SynthesisError as e:
        ok, detail = False, str(e)
    dt = time.monotonic() - t0
    status = "PASS"
    if ok is not expect_ok:
        status = f"FAIL (ok={ok}, wanted {expect_ok})"
    elif STATE["hits"] != expect_hits and expect_hits is not None:
        status = f"FAIL (hits={STATE['hits']}, wanted {expect_hits})"
    elif dt > max_seconds:
        status = f"FAIL (took {dt:.1f}s > {max_seconds}s bound)"
    print(f"[{status}] {name}: {dt:.1f}s, hits={STATE['hits']}, -> {detail[:90]}")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    synthesis.ANTHROPIC_URL = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
    # shrink the clocks so the harness runs in seconds, ratios preserved
    synthesis.HTTP_TIMEOUT = 4
    synthesis.ATTEMPT_DEADLINE = 5

    run_case("success, one call", "ok", True, 1, 4)
    run_case("529 w/ Retry-After -> retried once, succeeds", "529_then_ok", True, 2, 10)
    run_case("401 -> immediate fail, NO retry", "401", False, 1, 4)
    run_case("server hang -> bounded + retried, still bounded", "hang", False, 2, 21)
    run_case("slow-drip body -> in-thread wall clock catches it, retried", "drip", False, 2, 21)
    print("done — interpreter must now exit cleanly (daemon threads never block exit)")

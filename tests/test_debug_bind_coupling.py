"""Regression test for two SECURITY.md Phase 4 LOW findings:

  1. `/health` unconditionally returned `{"has_fec_key": bool(...)}` — a
     reconnaissance signal, for an unauthenticated caller, distinguishing a
     real deployment (worth targeting) from a demo instance. Nothing in the
     app actually consumed the field (not static/app.js, nowhere), and the
     operator who genuinely wants to know already gets it from the startup
     console banner. Fix: dropped; `/health` now returns only `{"ok": true}`.
  2. `FLASK_DEBUG=1` (Werkzeug's interactive in-browser debugger — arbitrary
     code execution for anyone who reaches it) and `HOST=0.0.0.0` (or any
     non-loopback bind) were two independently-safe-by-default settings
     that could still be combined by an operator who sets both — with only
     a warning, two lines apart in the source, standing between that
     combination and a live RCE surface. Fix: the `__main__` block now
     refuses to start (exit code 1, no bind attempted) when both are set
     together, rather than merely warning.

This test drives the REAL app: `/health`'s response via the Flask test
client, and the debug+bind coupling via actual `python3 app.py` subprocess
launches (a source-text check wouldn't prove the process actually exits
before binding, or that the loopback+debug and wide-bind+no-debug cases
still work normally).

Run from the repo root:  python3 tests/test_debug_bind_coupling.py
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("FEC_API_KEY", None)
os.environ.pop("CONGRESS_API_KEY", None)
os.environ.pop("SEARXNG_URL", None)

import app as app_module  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_app(env_overrides: dict, port: int, timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("FEC_API_KEY", None)
    env.pop("CONGRESS_API_KEY", None)
    env.pop("SEARXNG_URL", None)
    env["PORT"] = str(port)
    env.update(env_overrides)
    try:
        return subprocess.run([sys.executable, "app.py"], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # The process didn't exit on its own (it's serving) — that's the
        # EXPECTED outcome for the "should start fine" cases below.
        return subprocess.CompletedProcess(
            args=e.cmd, returncode=None,
            stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""))


def start_app_bg(env_overrides: dict, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("FEC_API_KEY", None)
    env.pop("CONGRESS_API_KEY", None)
    env.pop("SEARXNG_URL", None)
    env["PORT"] = str(port)
    env.update(env_overrides)
    return subprocess.Popen([sys.executable, "app.py"], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def main() -> int:
    client = app_module.app.test_client()

    # ── 1. /health no longer discloses has_fec_key ───────────────────────────
    resp = client.get("/health")
    body = resp.get_json() or {}
    check("/health returns 200", resp.status_code == 200)
    check("/health body is ok:true", body.get("ok") is True, body)
    check("/health no longer includes has_fec_key at all",
          "has_fec_key" not in body, body)
    check("/health response is exactly {'ok': True} (nothing extra crept back in)",
          body == {"ok": True}, body)

    # ── 2a. FLASK_DEBUG=1 + non-loopback HOST: refused, exits 1, never binds ──
    port = free_port()
    result = run_app({"FLASK_DEBUG": "1", "HOST": "0.0.0.0"}, port, timeout=6)
    check("FLASK_DEBUG=1 + HOST=0.0.0.0: process exits (doesn't hang serving)",
          result.returncode is not None, "process was still running after 6s")
    if result.returncode is not None:
        check("FLASK_DEBUG=1 + HOST=0.0.0.0: exit code is 1 (refused, not a crash)",
              result.returncode == 1, result.returncode)
        combined = (result.stdout or "") + (result.stderr or "")
        check("refusal message names the actual risk (RCE / debugger)",
              "REFUSING TO START" in combined and "debugger" in combined.lower(),
              combined[-400:])
        # Prove it never actually bound the port.
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                bound = True
        except OSError:
            bound = False
        check("the port was never actually bound", not bound)

    # ── 2b. FLASK_DEBUG=1 alone (loopback default): still starts normally ────
    port = free_port()
    proc = start_app_bg({"FLASK_DEBUG": "1"}, port)
    try:
        deadline = time.time() + 8
        started = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    started = r.status == 200
                    break
            except Exception:   # noqa: BLE001
                time.sleep(0.3)
        check("FLASK_DEBUG=1 with default (loopback) HOST still starts and serves",
              started)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── 2c. HOST=0.0.0.0 alone (no debug): still starts, just the wide-bind
    #        warning — the existing Phase-2 behavior must be unaffected ──────
    port = free_port()
    proc = start_app_bg({"HOST": "0.0.0.0"}, port)
    try:
        deadline = time.time() + 8
        started = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    started = r.status == 200
                    break
            except Exception:   # noqa: BLE001
                time.sleep(0.3)
        check("HOST=0.0.0.0 without FLASK_DEBUG still starts normally (Phase 2 "
              "behavior unaffected by the new coupling check)", started)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All /health and debug+bind coupling checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

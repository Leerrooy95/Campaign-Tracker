"""Regression test for the SECURITY.md HIGH finding: the app
bound `0.0.0.0` unconditionally, with no authentication/authorization/TLS —
so the default an open-source cloner gets by just running `python3 app.py`
was network-reachable, not loopback-only.

Fix: `app.py`'s `__main__` block now defaults `host` to `127.0.0.1` and only
binds wider on an explicit `HOST=...` env var, printing a warning when it
does. This test runs the real script as a subprocess (not just a source
grep) so a refactor that quietly reintroduces a hardcoded `0.0.0.0` would be
caught by an actual bind failing outside loopback, not just a missing string.

Run from the repo root:  python3 tests/test_bind_host_default.py
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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


def start_app(env_overrides: dict, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("FEC_API_KEY", None)   # demo mode; no live calls
    env.pop("CONGRESS_API_KEY", None)
    env.pop("SEARXNG_URL", None)
    env["PORT"] = str(port)
    env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, "app.py"], cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_startup(proc: subprocess.Popen, timeout=8) -> str:
    """Collect stdout lines until the Flask 'Running on' banner appears or we
    time out; returns everything captured so far."""
    out = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            out.append(line)
            if "Press CTRL+C" in line:
                # last line Werkzeug prints before serving — all "Running on"
                # lines (there can be several, e.g. loopback + LAN address)
                # are guaranteed to have already been read by this point.
                time.sleep(0.3)
                break
        elif proc.poll() is not None:
            break
    return "".join(out)


def stop(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    src = (REPO / "app.py").read_text()
    check("app.py's __main__ block reads HOST from the environment",
          bool(re.search(r'os\.getenv\("HOST"', src)))
    check("app.py's __main__ block defaults HOST to 127.0.0.1, not 0.0.0.0",
          bool(re.search(r'os\.getenv\("HOST",\s*"127\.0\.0\.1"\)', src)))
    check("no remaining hardcoded host=\"0.0.0.0\" in app.run(...)",
          'host="0.0.0.0"' not in src)

    # ── default run: must bind loopback only, must NOT print the warning ────
    port = free_port()
    proc = start_app({}, port)
    try:
        banner = wait_for_startup(proc)
        check("default run's own banner confirms 127.0.0.1",
              "Running on http://127.0.0.1" in banner, banner)
        check("default run does NOT print the wide-bind warning",
              "WARNING: HOST=" not in banner)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                ok = r.status == 200
        except Exception as e:   # noqa: BLE001
            ok = False
        check("default run answers on loopback", ok)
    finally:
        stop(proc)

    # ── explicit opt-in: HOST=0.0.0.0 must bind wide AND print the warning ──
    port = free_port()
    proc = start_app({"HOST": "0.0.0.0"}, port)
    try:
        banner = wait_for_startup(proc)
        check("HOST=0.0.0.0 opt-in prints the wide-bind warning", "WARNING: HOST=" in banner, banner)
        check("HOST=0.0.0.0 opt-in still binds 127.0.0.1 among its addresses",
              "127.0.0.1" in banner, banner)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                ok = r.status == 200
        except Exception:   # noqa: BLE001
            ok = False
        check("HOST=0.0.0.0 opt-in still answers (didn't break the app)", ok)
    finally:
        stop(proc)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All bind-address checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

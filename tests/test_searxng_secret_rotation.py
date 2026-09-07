"""Regression test for the SECURITY.md HIGH finding: the
SearXNG `secret_key` rotation step was a silent no-op.

BUG — three files disagreed on the placeholder string:
  - docker/searxng/settings.yml SHIPPED the literal value "e2bctgctyctycytc"
  - its own in-file comment CLAIMED the placeholder was "e2bckiiubpiubvuib"
  - docker/searxng/README.md's (and, at the time, run.sh's own duplicate copy
    of the same instructions) documented setup command ran
    `sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml`
Three different strings, so `sed` never matched anything, exited 0, and every
clone that followed the documented setup kept running the SAME publicly-known
session secret with no signal the rotation failed.

Fix: docker/searxng/README.md is the ONE documented place this setup command
lives (docker/searxng/settings.yml's shipped placeholder now matches it
exactly), and run.sh points at that README instead of duplicating the
command — the duplication between two files is exactly how the placeholder
drifted in the first place, so removing it is part of the fix, not
incidental.

This test doesn't just grep for a string — it extracts the ACTUAL sed
command from README.md (so a future edit to the doc is caught if it drifts)
and runs it, via subprocess, against a real temp copy of settings.yml, then
confirms the file actually changed and no longer contains the placeholder.
It also asserts run.sh does NOT carry its own copy of the sed command, so
the single-source-of-truth property can't silently regress back into two
copies drifting apart again.

Run from the repo root:  python3 tests/test_searxng_secret_rotation.py
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def extract_sed_command(text: str) -> str | None:
    """Pull the documented `sed -i "s|OLD|NEW|g" settings.yml` line verbatim."""
    m = re.search(r'sed -i "s\|([^|]+)\|\$\(openssl rand -hex 32\)\|g" settings\.yml', text)
    return m.group(1) if m else None


def main() -> int:
    settings_path = REPO / "docker" / "searxng" / "settings.yml"
    readme_path = REPO / "docker" / "searxng" / "README.md"
    run_sh_path = REPO / "run.sh"

    settings_text = settings_path.read_text()
    readme_text = readme_path.read_text()
    run_sh_text = run_sh_path.read_text()

    readme_target = extract_sed_command(readme_text)
    run_sh_target = extract_sed_command(run_sh_text)

    check("README.md documents a sed rotation command", readme_target is not None)
    check("run.sh does NOT duplicate the sed rotation command (single source "
          "of truth in the README — duplication is how the placeholder "
          "drifted originally)",
          run_sh_target is None, f"run.sh sed target={run_sh_target!r}")
    check("run.sh points the operator at docker/searxng/README.md instead",
          "docker/searxng/README.md" in run_sh_text)

    m = re.search(r'secret_key:\s*"([^"]+)"', settings_text)
    check("settings.yml has a secret_key literal", m is not None)
    shipped_value = m.group(1) if m else None

    check("settings.yml's shipped secret_key EQUALS the documented sed target "
          "(this is the bug: it must match or rotation no-ops)",
          shipped_value == readme_target,
          f"shipped={shipped_value!r} documented_target={readme_target!r}")

    # ── prove it by actually running the real, documented command ───────────
    if readme_target is not None:
        with tempfile.TemporaryDirectory() as td:
            tmp_settings = Path(td) / "settings.yml"
            tmp_settings.write_text(settings_text)
            cmd = f'sed -i "s|{readme_target}|$(openssl rand -hex 32)|g" settings.yml'
            result = subprocess.run(["bash", "-c", cmd], cwd=td,
                                    capture_output=True, text=True)
            check("the documented sed command exits 0", result.returncode == 0,
                  result.stderr)
            rotated_text = tmp_settings.read_text()
            check("the file actually changed after rotation",
                  rotated_text != settings_text)
            check("the placeholder is GONE after rotation",
                  readme_target not in rotated_text
                  or f'secret_key: "{readme_target}"' not in rotated_text)
            new_m = re.search(r'secret_key:\s*"([^"]+)"', rotated_text)
            check("secret_key is now a 64-char hex string (openssl rand -hex 32)",
                  bool(new_m) and re.fullmatch(r"[0-9a-f]{64}", new_m.group(1) or ""),
                  new_m.group(1) if new_m else None)
            # Run it a second time on the ALREADY-rotated file — a real
            # operator re-running setup shouldn't silently no-op either, but
            # a second rotation on a random hex string just won't find the
            # original placeholder anymore, which is expected/fine — the
            # important property is the FIRST run always works.

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All SearXNG secret_key rotation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

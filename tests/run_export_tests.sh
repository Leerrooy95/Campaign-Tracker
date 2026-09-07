#!/usr/bin/env bash
# Export test: Node builds CSVs + a real ZIP from a sample result; Python opens
# that ZIP with the stdlib and byte-verifies every entry. Run from repo root or
# anywhere: bash tests/run_export_tests.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
node "$HERE/test_export_js.mjs"
echo "---"
python3 "$HERE/test_export_zip.py"

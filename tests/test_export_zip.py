"""Python verifier for the ZIP built by test_export_js.mjs. Confirms the
hand-rolled store-only ZIP writer produces an archive the standard library can
open, that CRCs/offsets are valid, and that each CSV's bytes round-trip exactly
(BOM present, content matches what Node emitted). Run after the Node step
(see run_export_tests.sh)."""
import io
import json
import sys
import zipfile
from pathlib import Path

ZIP = Path("/tmp/export_test.zip")
EXPECTED = Path("/tmp/export_expected.json")


def main() -> int:
    if not ZIP.exists() or not EXPECTED.exists():
        print("FAIL — Node step didn't produce the artifacts; run the .mjs first")
        return 1
    expected = json.loads(EXPECTED.read_text())
    raw = ZIP.read_bytes()
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        # testzip() recomputes every CRC — None means all entries are intact.
        check("archive opens and all CRCs valid", z.testzip() is None)
        names = z.namelist()
        check("entry count matches", len(names) == len(expected),
              f"{len(names)} vs {len(expected)}")
        check("all expected files present", set(names) == set(expected))

        for name in expected:
            if name not in names:
                continue
            data = z.read(name)
            check(f"{name}: UTF-8 BOM present", data[:3] == b"\xef\xbb\xbf")
            text = data[3:].decode("utf-8")
            check(f"{name}: content matches Node output", text == expected[name])

        # spot-check the trickiest cell survived: a value with comma, quotes,
        # and a newline must be quote-wrapped with interior quotes doubled.
        donors_name = next((n for n in names if n.endswith("_donors.csv")), None)
        if donors_name:
            dtext = z.read(donors_name)[3:].decode("utf-8")
            check("donors: embedded comma/quote/newline round-tripped",
                  '"Comma, Name ""Q"""' in dtext and '"Exec\nDir"' in dtext,
                  "quoting preserved")

        vts = next((n for n in names if n.endswith("_votes.csv")), None)
        if vts:
            vtext = z.read(vts)[3:].decode("utf-8")
            check("votes: quoted title, position, and citation round-tripped",
                  '"Motion to Invoke Cloture: DISCLOSE Act, the ""sunlight"" bill"' in vtext
                  and "Yea" in vtext and "Senate Roll Call 612" in vtext)
        else:
            check("votes: CSV present in archive", False)

        leg = next((n for n in names if n.endswith("_legislative.csv")), None)
        if leg:
            ltext = z.read(leg)[3:].decode("utf-8")
            check("legislative: non-money action included (full record, not just money)",
                  "S 100" in ltext and "S 3991" in ltext)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("python-side ZIP verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

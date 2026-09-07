"""Regression test for the SECURITY.md Phase 4 LOW finding:
votes.py's three `xml.etree.ElementTree.fromstring()` calls (Senate LIS vote
menus and per-vote XML) were documented-vulnerable to entity-expansion
("billion laughs") and quadratic-blowup DoS — stdlib ElementTree happily
expands `<!ENTITY>` declarations before your code ever sees the result.
(XXE file-disclosure does NOT apply here — ElementTree never resolves
external entities or fetches DTDs — so this is a DoS-class finding, not
data exfiltration; audit-rated LOW because exploitability requires
controlling a fixed `senate.gov` response, not user-supplied input.)

Fix: all three parse sites now go through `defusedxml.ElementTree.fromstring`
instead, and every `except ET.ParseError` around them was widened to also
catch `defusedxml.common.DefusedXmlException` — a rejected malicious payload
raises a `ValueError` subclass, NOT an `ET.ParseError` subclass, so the old
except clauses would have let it propagate as an unhandled crash instead of
being disclosed the same way a malformed payload already was
(`VotesAPIError` / `PARSE_FAILED`).

This test proves three things with a REAL classic "billion laughs" payload
(not a mock): (1) stdlib `ElementTree.fromstring` actually performs the
entity expansion on this payload — establishing the vulnerability is real,
not theoretical, in this exact XML shape; (2) `defusedxml`'s parser rejects
the same payload outright, before any expansion; (3) votes.py's actual
wrapper functions handle the rejection the way the rest of the module's
"disclosed, never a crash, never silently misreported" contract requires —
`_parse_senate_menu` raises `VotesAPIError` (not a bare exception),
`_senate_member_position` returns the `PARSE_FAILED` sentinel (not `None`,
which would misreport a blocked payload as "member simply wasn't on this
roll" — exactly the class of bug Rule 4 / the PARSE_FAILED sentinel exists
to prevent). A legitimate real fixture XML is also re-parsed to prove
nothing about normal operation regressed.

Run from the repo root:  python3 tests/test_xxe_guard.py
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import votes  # noqa: E402
from defusedxml.common import DefusedXmlException  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<vote_summary><congress_year>2025</congress_year><votes>&lol3;</votes></vote_summary>
"""

# A second attack shape: a DTD declaring an external entity (blocked at the
# DTD stage regardless of whether the SYSTEM target is ever reachable —
# defusedxml refuses the DTD itself, it doesn't just fail to resolve it).
EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE roll_call_vote [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<roll_call_vote><congress_year>2025</congress_year><members><member><last_name>&xxe;</last_name></member></members></roll_call_vote>
"""


def main() -> int:
    # ── establish the vulnerability is real in stdlib ElementTree ────────────
    root = ET.fromstring(BILLION_LAUGHS)
    expanded_len = len(root.find("votes").text or "")
    check("stdlib ElementTree DOES expand the entity bomb (proves the class "
          "of bug is real for this exact payload shape, not theoretical)",
          expanded_len == 300, f"expanded to {expanded_len} chars")

    # ── defusedxml rejects it outright, before expansion ──────────────────────
    try:
        from defusedxml.ElementTree import fromstring as safe_fromstring
        safe_fromstring(BILLION_LAUGHS)
        check("defusedxml rejects the entity bomb", False, "did not raise")
    except DefusedXmlException as e:
        check("defusedxml rejects the entity bomb (raises DefusedXmlException)",
              True, str(e))

    try:
        from defusedxml.ElementTree import fromstring as safe_fromstring
        safe_fromstring(EXTERNAL_ENTITY)
        check("defusedxml rejects the external-entity DTD", False, "did not raise")
    except DefusedXmlException as e:
        check("defusedxml rejects the external-entity DTD "
              "(raises DefusedXmlException, DTD never even resolved)",
              True, str(e))

    # ── votes.py's own wrapper functions handle rejection per the module's
    #    "disclosed, never a crash, never misreported" contract ─────────────
    try:
        votes._parse_senate_menu(BILLION_LAUGHS, 119, 1)
        check("_parse_senate_menu rejects the entity bomb", False, "did not raise")
    except votes.VotesAPIError as e:
        check("_parse_senate_menu converts the rejection into VotesAPIError "
              "(disclosed, not an unhandled crash)", True, str(e))
    except Exception as e:   # noqa: BLE001
        check(f"_parse_senate_menu handles the entity bomb gracefully "
              f"(got uncaught {type(e).__name__} instead of VotesAPIError)",
              False, f"{type(e).__name__}: {e}")

    pos = votes._senate_member_position(BILLION_LAUGHS, "Ossoff", "GA")
    check("_senate_member_position returns the PARSE_FAILED sentinel for a "
          "rejected payload — NOT None (None would misreport a blocked "
          "attack as \"member wasn't on this roll\", a real fact-invention bug)",
          pos is votes.PARSE_FAILED, pos)

    # ── legitimate operation is unaffected: a real fixture still parses ──────
    fixture = (Path(__file__).resolve().parent / "fixtures"
              / "senate_vote_menu_119_1.xml").read_bytes()
    parsed = votes._parse_senate_menu(fixture, 119, 1)
    check("a real Senate vote-menu fixture still parses correctly through "
          "the defusedxml path", len(parsed) > 0 and "title" in parsed[0],
          f"{len(parsed)} votes parsed")

    fixture2 = (Path(__file__).resolve().parent / "fixtures"
               / "senate_vote_119_1_00300.xml").read_bytes()
    pos2 = votes._senate_member_position(fixture2, "Ossoff", "GA")
    check("a real per-vote fixture still parses (result is None-or-a-position, "
          "never PARSE_FAILED, for a genuinely well-formed document)",
          pos2 is not votes.PARSE_FAILED, pos2)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("All XXE/entity-expansion guard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

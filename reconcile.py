"""
reconcile.py — deterministic guard over the synthesis layer's output.

The synthesis step (synthesis.py) is the ONLY place a language model touches the
data. Its "every line is sourced" guarantee rests entirely on the prompt — there
is no machine check that the prose actually matches the JSON. This module is that
check. It runs AFTER synthesis, compares the report text back against the source
result, and reports discrepancies. It is:

  - DETERMINISTIC. Pure string/number work, no API, no model. Same input → same
    output. Cheap enough to run on every report (including cache hits).
  - NON-FATAL. Findings are surfaced as caveats, never as failures. A
    reconciliation problem must never take down the factual result — same rule
    the rest of the pipeline follows.
  - CONSERVATIVE. Checks are tuned to avoid false alarms: a discrepancy is only
    raised when the evidence is clear. Rounding and model-computed sums are
    tolerated and, at worst, listed as "verify," not "wrong."

Why this exists: in testing, the model narrated an outside spender (Senate
Conservatives Fund) as "support" when the FEC data had its dollars on the OPPOSE
side — the kind of side-flip that quietly inverts the whole point of the report.
The chart, drawn straight from JSON, had it right; the prose did not. The
number-provenance check would also have caught it (the $47,581 support total
excludes that spender). This guard makes both catchable automatically.

Severity levels:
  - "high"   : a clear contradiction of the source (wrong support/oppose side,
               a cycle total not restated). Rides into the report as a caveat.
  - "review" : a figure in the prose that doesn't trace to a source number
               (often a legend-rounded value or a model-computed sum). Listed for
               a human to verify; does not, by itself, mark the report unsound.
"""
from __future__ import annotations

import math
import re
from typing import Any

# Directional language. Kept specific to avoid matching the ubiquitous word
# "for". "oppos" covers oppose/opposed/opposing/opposition.
_OPPOSE_WORDS = ("against", "oppos", "attack", "defeat", "to defeat")
_SUPPORT_WORDS = ("support", "in favor", "backing", "boost", "behalf", "to elect")
# "$X for" / "for <the candidate|pronoun>" mean support without the bare-"for"
# noise; "$X against" reinforces oppose. The candidate's own name is added
# dynamically per run (see _side_signal / _candidate_name_terms) so "for
# Boebert" reads as support for ANY candidate — not just a hardcoded name.
_SUP_FOR_RE = re.compile(r'\$[\d.,]+\s*[kmb]?\s+for\b', re.I)
_SUP_FOR2_RE = re.compile(r'\bfor (?:the candidate|him|her|them|his|hers)\b', re.I)
_OPP_AGAINST_RE = re.compile(r'\$[\d.,]+\s*[kmb]?\s+against\b', re.I)

# Split prose into sentence/line-sized segments so a spender's side cue is read
# from ITS clause, not one that bled in from the next line. Abbreviation periods
# (Inc., Corp., …) are shielded first so "Field Team 6, Inc. ($…)" doesn't get
# split off from the "Support spending came from …" header governing its list.
_ABBR_RE = re.compile(
    r'\b(?:Inc|Corp|Co|Ltd|LLC|Jr|Sr|St|vs|Mr|Mrs|Ms|Dr|No|Ave|Blvd|Rd|Sen|Rep|Gov)\.',
    re.I)
_SEG_RE = re.compile(r'[\n;•]+|(?<=[.!?])\s+')
_SENTINEL = "\x00"


def _split_segments(report: str) -> list[str]:
    shielded = _ABBR_RE.sub(lambda m: m.group(0).replace(".", _SENTINEL), report or "")
    return [seg.replace(_SENTINEL, ".") for seg in _SEG_RE.split(shielded)]


def _is_section_break(seg: str) -> bool:
    s = seg.strip()
    return s == "" or s.startswith("#")


def _candidate_name_terms(result: dict) -> tuple[str, ...]:
    """First and last name tokens for the candidate under analysis, lowercased,
    length ≥3, so "for <name>" can read as a support cue for THIS candidate
    (generalizing the old hardcoded 'for Ossoff'). Reads the FEC name
    ('LAST, FIRST …') and the typed query; de-duplicated."""
    terms: set[str] = set()
    fec_name = result.get("candidate_name") or ""
    if "," in fec_name:
        last, _, rest = fec_name.partition(",")
        toks = [last.strip()] + rest.strip().split()
    else:
        toks = fec_name.split()
    toks += (result.get("candidate") or "").split()
    for t in toks:
        t = re.sub(r"[^a-z]", "", t.lower())
        if len(t) >= 3 and t not in ("the", "jr", "sr", "iii"):
            terms.add(t)
    return tuple(terms)


def _side_signal(segment: str, cand_terms: tuple[str, ...] = ()) -> str | None:
    """'oppose' / 'support' / None for a single clause, only when unambiguous."""
    s = segment.lower()
    opp = any(w in s for w in _OPPOSE_WORDS) or bool(_OPP_AGAINST_RE.search(s))
    sup = (any(w in s for w in _SUPPORT_WORDS)
           or bool(_SUP_FOR_RE.search(s)) or bool(_SUP_FOR2_RE.search(s)))
    if not sup and cand_terms:
        sup = any(re.search(r'\bfor\s+(?:the\s+)?' + re.escape(t) + r'\b', s)
                  for t in cand_terms)
    if opp and not sup:
        return "oppose"
    if sup and not opp:
        return "support"
    return None

# A magnitude suffix must be a WHOLE word. Without the trailing boundary the
# `K` alternative bit the first letter off any k-word following a figure:
# "$12,093.06 known only from a notice" parsed as $12,093,060 (Run-19 false
# positive). Two of that run's three notice-only figures then matched an
# unrelated source amount by coincidence through the old 5% tolerance, so the
# provenance check reported clean on numbers it had itself corrupted. The
# lookahead requires the suffix to end at a non-letter; a bare digit run with
# no suffix is unaffected.
_MONEY_RE = re.compile(
    r'\$\s?(\d[\d,]*(?:\.\d+)?)'
    r'(?:\s?(K|M|B|thousand|million|billion)(?![A-Za-z]))?', re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
         "b": 1e9, "billion": 1e9}


def _money_to_float(numstr: str, suffix: str | None) -> float:
    v = float(numstr.replace(",", ""))
    return v * _MULT.get((suffix or "").lower(), 1.0)


def _find_dollar_values(text: str) -> list[tuple[str, float]]:
    """All $-figures in a string, as (raw, normalized_float)."""
    out = []
    for m in _MONEY_RE.finditer(text or ""):
        try:
            out.append((m.group(0).strip(), _money_to_float(m.group(1), m.group(2))))
        except ValueError:
            pass
    return out


def _collect_source_amounts(obj: Any, acc: set[float]) -> None:
    """Every dollar-ish number reachable in the result: numeric leaves, plus any
    $-figures embedded in string leaves (e.g. a statement excerpt quoting an
    amount). Over-collecting only makes the provenance check more forgiving,
    which is the safe direction (fewer false alarms)."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, str):
        for _, v in _find_dollar_values(obj):
            acc.add(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_source_amounts(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_source_amounts(v, acc)


# WHY NOT A FLAT PERCENTAGE BAND
# The tolerance exists so a legend-rounded rendering ("$488K" for $487,563.37,
# "$136.1M" for $136,098,062.14) isn't flagged as unsourced. The old blanket 5%
# did that, but a percentage band scales with the number: at 2020-cycle
# magnitudes 5% is a +/-$7.8M hole, and in Run 19 the corrupted figure
# $28,800,000 "matched" the unrelated $28,654,210 — a $146k gap waved through.
# A band wide enough for 2-significant-figure rounding is, by construction, wide
# enough to hide a six-figure error.
#
# So don't guess at a band: ask the actual question. "Is this figure a rounded
# rendering of a source number?" is answerable exactly — generate the roundings
# a writer would plausibly produce and compare against those. Precision no
# longer decays with scale, and the accepted set stays finite and inspectable.
_SIG_FIGS = (1, 2, 3, 4, 5, 6)


def _rounded_variants(v: float) -> set[float]:
    """The renderings a human or model would plausibly write for v: rounded to
    1-6 significant figures (covers "$65K", "$488K", "$136.1M", "$156.1M") plus
    the exact value. Never widens into a band — each variant is a single point."""
    out = {float(v)}
    if v == 0:
        return out
    mag = math.floor(math.log10(abs(v)))
    for sig in _SIG_FIGS:
        q = mag - sig + 1
        out.add(round(v, -q) if q > 0 else round(v, -q if q < 0 else 0))
    return out


def _matches_a_source(r: float, sources: set[float]) -> bool:
    """True if the report figure r is a source amount, or a plausible rounded
    rendering of one. Within $1 absolute counts as equal (exact cents, tiny
    values)."""
    for v in sources:
        for variant in _rounded_variants(v):
            if abs(r - variant) <= 1.0:
                return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _name_variants(name: str) -> list[str]:
    """Progressively looser forms of a committee name to locate it in prose."""
    n = name.strip()
    variants = [n]
    stripped = re.sub(r"[.,]", "", n)
    if stripped != n:
        variants.append(stripped)
    # drop common trailing tokens so "Field Team 6, Inc." also matches "Field Team 6"
    core = re.sub(r"[,]?\s*(inc\.?|llc|pac|fund|committee)\s*$", "", n, flags=re.I).strip()
    if len(core) >= 6 and core.lower() not in (v.lower() for v in variants):
        variants.append(core)
    return variants


# ── individual checks ────────────────────────────────────────────────────────

def _segment_has_amount(seg: str, amount: float) -> bool:
    """True if `seg` contains a dollar figure matching `amount` (5% or $1),
    i.e. the segment is talking about THIS spender's money specifically."""
    for _, v in _find_dollar_values(seg):
        if abs(v - amount) <= 1.0:
            return True
        denom = max(abs(amount), 1.0)
        if abs(v - amount) / denom <= 0.05:
            return True
    return False


def _mask_committee_names(seg: str, variants_by_idx: list[list[str]]) -> str:
    """Blank every committee name out of a segment so a cue word living inside a
    proper noun can't be read as a directional claim. Longest variant first, so
    "DEFEATING COMMUNISM PAC" is removed whole rather than leaving "PAC" behind.
    Case-insensitive; replaced with a space so surrounding words stay separated."""
    names = sorted({v for vs in variants_by_idx for v in vs if v},
                   key=len, reverse=True)
    for n in names:
        seg = re.sub(re.escape(n), " ", seg, flags=re.IGNORECASE)
    return seg


def check_spender_sides(result: dict, report: str) -> list[dict]:
    """Each outside spender's support/oppose side in the prose must match the
    side its dollars sit on in the FEC data. Flags only a clear INLINE flip: a
    segment names the spender, carries its OWN unambiguous side word, that word
    conflicts with the FEC side, AND the cue actually binds to this spender —
    the segment names no other spender, or it carries this spender's own dollar
    amount. Split spenders (real money on both sides) are skipped unless one
    side dwarfs the other (≥5×).

    It deliberately does NOT infer a side from a running/section cue that bled
    in from a neighbouring sentence. That fallback produced false "wrong side"
    highs (Run 13: all five SUPPORT spenders) when a spender's NAME appeared in
    a cue-less enumeration — e.g. a "… are marked traceable" list — immediately
    after a DIFFERENT spender's "opposing" sentence: the "oppose" cue drifted
    onto the whole list. A side is a claim only where the prose states it right
    next to the spender, so that's the only place we read one."""
    warns: list[dict] = []
    outside = (result.get("track_a_outside") or {})
    spenders = outside.get("spenders") or []
    segments = _split_segments(report)
    cand_terms = _candidate_name_terms(result)
    variants_by_idx = [
        [_norm(v) for v in _name_variants(
            sp.get("committee_name") or sp.get("committee_id") or "")]
        for sp in spenders
    ]
    # Read each segment's cue with EVERY committee name blanked out first.
    # Committee names are political slogans and routinely contain cue words:
    # "DEFEATING COMMUNISM PAC" carries "defeat", an oppose cue, so the segment
    # naming it read as "oppose" and the check flagged the PAC as being on the
    # wrong side — of a claim made by its own name (Run 20, high severity, false).
    # Others in the wild: "Stop ...", "Defend ...", "Beat ...", "... Victory Fund".
    # A cue inside a proper noun is never a directional claim about that spender,
    # so it must not be read as one. Masking every name (not just the one under
    # test) also stops one spender's name from injecting a cue that then binds
    # to a DIFFERENT spender named in the same segment.
    seg_sig = [_side_signal(_mask_committee_names(s, variants_by_idx), cand_terms)
               for s in segments]
    seg_low = [_norm(s) for s in segments]

    for si, s in enumerate(spenders):
        name = s.get("committee_name") or s.get("committee_id") or ""
        if not name:
            continue
        opp = float(s.get("oppose_total") or 0)
        sup = float(s.get("support_total") or 0)
        if opp <= 0 and sup <= 0:
            continue
        if opp > 0 and sup > 0 and max(opp, sup) / max(min(opp, sup), 1e-9) < 5:
            continue  # genuinely mixed — no single side to check
        true_side = "oppose" if opp >= sup else "support"
        amount = opp if true_side == "oppose" else sup
        variants = variants_by_idx[si]

        wrong = False
        for i, seg in enumerate(segments):
            own = seg_sig[i]
            if not own:
                continue                       # no side asserted here — not a claim
            if not any(v in seg_low[i] for v in variants):
                continue                       # this spender isn't named here
            # The cue must bind to THIS spender: either it's the only spender
            # named in the segment, or the segment carries this spender's own
            # dollar amount. Otherwise it's a shared/enumeration line and the
            # cue may belong to a different spender (the Run-13 false-positive).
            names_other = any(
                oi != si and any(v in seg_low[i] for v in variants_by_idx[oi])
                for oi in range(len(spenders)))
            if names_other and not _segment_has_amount(seg, amount):
                continue
            if own != true_side:
                wrong = True
                break
        if wrong:
            warns.append({
                "check": "spender_side",
                "severity": "high",
                "message": (f'"{name}" is described on the wrong side — FEC data '
                            f'has ${amount:,.0f} on the {true_side.upper()} side, but '
                            f'the report places it on the other side'),
            })
    return warns


def check_totals_restated(result: dict, report: str) -> list[dict]:
    """The outside-spending cycle totals (for / against) should appear in the
    report in some form. A missing total is a high-severity omission."""
    warns: list[dict] = []
    outside = (result.get("track_a_outside") or {})
    if not outside.get("spenders"):
        return warns
    report_vals = [v for _, v in _find_dollar_values(report)]

    def present(total: float) -> bool:
        if total <= 0:
            return True
        return any(abs(r - total) <= 1.0 or abs(r - total) / max(total, 1) <= 0.05
                   for r in report_vals)

    for key, human in (("oppose_total", "against"), ("support_total", "for")):
        total = float(outside.get(key) or 0)
        if total > 0 and not present(total):
            warns.append({
                "check": "cycle_total",
                "severity": "high",
                "message": (f'outside-spending total {human} (${total:,.0f}) '
                            f'is not restated in the report'),
            })
    return warns


def check_dollar_provenance(result: dict, report: str) -> list[dict]:
    """Every $-figure in the prose should trace to a source number (within a
    rounding tolerance). Unmatched figures are listed for review — they are
    usually legend-rounded values or model-computed sums, but they are also
    exactly where an invented number would show up."""
    sources: set[float] = set()
    _collect_source_amounts(
        {k: result.get(k) for k in
         ("track_a_composition", "track_a_direct", "track_a_outside",
          "track_b_record", "track_b_statements")}, sources)
    unmatched, seen = [], set()
    for raw, val in _find_dollar_values(report):
        if val in seen:
            continue
        seen.add(val)
        if not _matches_a_source(val, sources):
            unmatched.append(raw)
    warns: list[dict] = []
    if unmatched:
        shown = ", ".join(unmatched[:8]) + (" …" if len(unmatched) > 8 else "")
        warns.append({
            "check": "dollar_provenance",
            "severity": "review",
            "message": (f"{len(unmatched)} figure(s) in the report don't directly "
                        f"match a source amount (rounding or computed sums, or an "
                        f"error — verify): {shown}"),
        })
    return warns


def check_caveats_carried(result: dict, report: str) -> list[dict]:
    """Every audit caveat must survive into the report (preserve-the-audit-trail).
    Matched on the caveat's distinctive NUMERIC tokens, which are low-false-
    positive; a caveat whose numbers are all absent is flagged for review."""
    warns: list[dict] = []
    rlow = _norm(report)
    for a in (result.get("audit") or []):
        detail = str(a.get("detail") or "")
        label = str(a.get("label") or "")
        nums = re.findall(r"\d[\d,]*", detail)
        distinctive = [n for n in nums if len(n.replace(",", "")) >= 2]  # skip 1-digit noise
        if not distinctive:
            continue
        if not any(_norm(n) in rlow for n in distinctive):
            warns.append({
                "check": "caveat_carried",
                "severity": "review",
                "message": (f'caveat "{label}" may not be reflected in the report '
                            f'(none of its figures {distinctive} appear)'),
            })
    return warns


# ── orchestration ─────────────────────────────────────────────────────────────

def check_oppose_attribution(result: dict, report: str) -> list[dict]:
    """Outside "oppose" spending is money spent AGAINST THIS CANDIDATE — the
    target is known, not ambiguous, and it is not the opponent. A real Run-17
    report wrote the oppose total as "Opposing (presumably his opponent, though
    the data does not specify the target)", which inverts the single most
    important fact in that race ($7.0M spent attacking the candidate). The
    per-spender `spender_side` check passed because the timeline entries were
    individually correct — only the SUMMARY sentence was wrong — so this needs
    its own check.

    Flags when the oppose figure appears in a sentence that either hands the
    money to an opponent or claims the target is unspecified."""
    outside = (result.get("track_a_outside") or {})
    oppose = float(outside.get("oppose_total") or 0)
    if oppose <= 0 or not report:
        return []

    misattrib = re.compile(
        r"(?:presumably|likely|apparently|possibly)\s+(?:his|her|their|the)\s+opponent"
        r"|(?:against|opposing)\s+(?:his|her|their|the)\s+opponent"
        r"|(?:data|dataset)\s+does\s*n[o']t\s+specify\s+(?:the\s+)?target"
        r"|target\s+is\s+not\s+specified"
        r"|does\s*n[o']t\s+say\s+who\s+(?:it|the\s+money)\s+(?:was|is)\s+(?:spent\s+)?against",
        re.I)

    for seg in _split_segments(report):
        if not misattrib.search(seg):
            continue
        # Only flag where the oppose MONEY is actually being discussed, so a
        # legitimate mention of an opponent elsewhere isn't caught.
        if any(abs(v - oppose) <= max(1.0, abs(oppose) * 0.02)
               for _, v in _find_dollar_values(seg)) or "outside" in seg.lower():
            return [{
                "check": "oppose_attribution",
                "severity": "high",
                "message": (f"outside opposition spending (${oppose:,.0f}) is described as "
                            f"aimed at an opponent or as having an unspecified target — it is "
                            f"money spent AGAINST this candidate, and the target is known"),
            }]
    return []


def reconcile(result: dict, report: str) -> dict:
    """Run all checks. Returns {ok, warnings, counts}. `ok` is False only when a
    high-severity contradiction was found; review-level items leave ok True."""
    if not report:
        return {"ok": True, "warnings": [], "counts": {}}
    warnings: list[dict] = []
    warnings += check_spender_sides(result, report)
    warnings += check_oppose_attribution(result, report)
    warnings += check_totals_restated(result, report)
    warnings += check_dollar_provenance(result, report)
    warnings += check_caveats_carried(result, report)

    highs = [w for w in warnings if w["severity"] == "high"]
    counts = {
        "warnings": len(warnings),
        "high": len(highs),
        "review": len(warnings) - len(highs),
    }
    return {"ok": len(highs) == 0, "warnings": warnings, "counts": counts}


# ── offline self-test (no API, no network) ────────────────────────────────────

if __name__ == "__main__":
    # Real 2026 Ossoff outside data: Senate Conservatives Fund's dollars are on
    # the OPPOSE side (that's what makes the totals reconcile).
    result = {
        "track_a_outside": {
            "cycle": 2026, "oppose_total": 934835.78, "support_total": 47581.49,
            "spenders": [
                {"committee_name": "SLF PAC", "oppose_total": 627199.96, "support_total": 0, "traceable": True},
                {"committee_name": "Senate Conservatives Fund", "oppose_total": 7352.22, "support_total": 0, "traceable": True},
                {"committee_name": "Forward Blue", "oppose_total": 0, "support_total": 20100.0, "traceable": True},
            ],
        },
        "audit": [{"label": "Gather public statements (Track B)",
                   "detail": "49 statements from 23 sources — 40 undated"}],
    }

    # A) The bug: prose puts Senate Conservatives Fund on the SUPPORT side.
    bad = ("Outside groups spent $935K against and $48K for. Senate Conservatives "
           "Fund spent $7,352 in support of the candidate. SLF PAC spent $627K "
           "against. Forward Blue spent $20,100 for. Caveat: 40 undated of 49.")
    ra = reconcile(result, bad)
    side_flag = [w for w in ra["warnings"] if w["check"] == "spender_side"]
    print("A/ side-flip caught :", bool(side_flag), "| ok =", ra["ok"])
    if side_flag:
        print("   →", side_flag[0]["message"])

    # B) Correct prose: Senate Conservatives Fund on the OPPOSE side.
    good = ("Outside groups spent $935K against and $48K for. Senate Conservatives "
            "Fund spent $7,352 against the candidate. SLF PAC spent $627K against. "
            "Forward Blue spent $20,100 in support. 40 undated of 49 statements.")
    rb = reconcile(result, good)
    print("B/ clean prose ok   :", rb["ok"], "| high =", rb["counts"]["high"],
          "| review =", rb["counts"]["review"])

    # C) Invented number surfaces as review.
    inv = good + " A mystery PAC spent $4.2M against."
    rc = reconcile(result, inv)
    prov = [w for w in rc["warnings"] if w["check"] == "dollar_provenance"]
    print("C/ invented $ flagged:", bool(prov))
    if prov:
        print("   →", prov[0]["message"])

    # D) Missing total surfaces as high.
    miss = "Senate Conservatives Fund spent $7,352 against. 40 undated of 49."
    rd = reconcile(result, miss)
    tot = [w for w in rd["warnings"] if w["check"] == "cycle_total"]
    print("D/ missing total high:", bool(tot), "| ok =", rd["ok"])

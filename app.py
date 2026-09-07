"""
app.py — server + async job worker for the money-vs-record tracker.

This is the orchestration layer. It does NOT contain analysis logic; it drives a
StepLog through the pipeline and exposes it for polling. Each stage is wrapped in
`with log.step(...)` so it is self-reporting, and downstream stages call
log.require(...) so they refuse to run on missing/partial input.

The tool assembles two FACTUAL tracks and leaves the judgment to the reader (and
to a report layer, later) — there is no scoring and no correlation verdict:
  - Track A (money): direct contributions, outside spending, and per-cycle
    composition (big-donor / PAC / small-dollar / outside), all from the FEC.
  - Track B (record): what the candidate did and said — bills sponsored and
    cosponsored (Congress.gov), and public statements on money/influence gathered
    by web search (statements.py). Both factual, dated, sourced. No scoring.

THREE ROUTES:
  GET  /                     → the single-page frontend
  POST /search               → spawns a worker, returns {"job_id": ...} immediately
  GET  /status/<job_id>      → returns the live StepLog + result (the poll target)

RUNTIME MODES:
  - REAL (default in deployment): uses the CREATOR's FEC key (FEC_API_KEY) for
    the money side and CONGRESS_API_KEY (free, api.data.gov) for the record. If
    the shared FEC key is rate-limited, the UI offers an optional field for a
    user to paste their own. No CONGRESS_API_KEY → the record stage is skipped
    and disclosed; the money side still completes.
  - DEMO (no FEC_API_KEY, or ?demo=1): every stage is simulated with synthetic
    fixtures so the app runs with zero setup and you can watch the live display.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

import fec
import congress
import statements
import synthesis
import timeline
import votes
from steps import StepLog, StepGateError

# flask-limiter is optional — mirror your other apps: rate-limit if present,
# degrade gracefully if not.
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _HAS_LIMITER = True
except ImportError:  # pragma: no cover
    _HAS_LIMITER = False


# ── job registry ────────────────────────────────────────────────────────────
# In-process, thread-safe. One entry per search. Fine for a single-worker
# deployment; if you later run multiple gunicorn workers, move this to Redis
# (the jobs would otherwise live in whichever worker happened to handle /search,
# and a /status poll routed to a different worker wouldn't find them).
JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 1800  # seconds to keep a finished job around for polling/export


def _new_job() -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "log": StepLog(),
            "result": None,
            "error": None,
            "rate_limited": False,
            "done": False,
            "created": time.time(),
        }
    return job_id


def _sweep_old_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if now - v["created"] > _JOB_TTL]:
            del JOBS[jid]


# ── input builders (factual Track A composition + demo data) ──────────────────
# These assemble the inputs the new modules consume. Two of them depend on
# collectors. Track B collection is statements.py (representative, bias-guarded);
# Track A composition is fec.funding_by_cycle (factual). DEMO uses synthetic
# fixtures so the full path runs with no keys and no scoring.


def _default_cycles() -> list[int]:
    """The cycles the funding composition is built over: current + prior 4
    (~10 years)."""
    cur = fec._current_cycle()
    return [cur - 2 * k for k in range(5)]


def _demo_composition() -> list[dict]:
    """Synthetic per-cycle funding composition for DEMO — illustrative, NOT real.
    Small-dollar share falling while PAC + outside money rise."""
    yrs = [2018, 2020, 2022, 2024, 2026]
    large = [0.32, 0.41, 0.52, 0.63, 0.74]
    pac = [0.3e6, 0.6e6, 1.0e6, 1.5e6, 2.2e6]
    outside = [0.8e6, 2.5e6, 5e6, 9e6, 15e6]
    out = []
    for y, ls, pc, os_ in zip(yrs, large, pac, outside):
        out.append({
            "cycle": y, "receipts": 10e6,
            "individual_itemized": round(10e6 * ls), "individual_unitemized": round(10e6 * (1 - ls)),
            "pac_contributions": pc, "large_share": ls, "small_share": round(1 - ls, 4),
            "outside_support": os_, "outside_oppose": 0.0,
            "outside_traceable": os_ * 0.6, "outside_dark": os_ * 0.4,
            "incomplete": False, "incomplete_reason": "",
        })
    return out


def _summarize_direct(groups: list[dict], top_n: int = 25) -> dict:
    """Collapse the Schedule A pull into the top-N donors by amount.

    Schedule A is now pulled capped + largest-first (see fec.max_pages), so this
    is the biggest itemized donors — not the full quarter-million-row list that
    made the result 1,000+ pages. The campaign's real receipts total is NOT this
    sum; it comes from FEC /totals in the composition section. We surface the sum
    of what we pulled only as 'largest_contributions_total' so nothing here is
    mistaken for the headline total."""
    all_donors: list[dict] = []
    all_txns: list[dict] = []
    pulled_total = 0.0
    memo_skipped = 0
    refunds_applied = 0
    refund_total = 0.0
    any_truncated = False
    truncated_reasons: list[str] = []
    for g in groups:
        pulled_total += g.get("total_raised", 0) or 0
        all_donors.extend(g.get("donors", []))
        all_txns.extend(g.get("transactions", []))
        memo_skipped += g.get("memo_skipped", 0) or 0
        refunds_applied += g.get("refunds_applied", 0) or 0
        refund_total += g.get("refund_total", 0) or 0
        if g.get("truncated"):
            any_truncated = True
            reason = g.get("truncated_reason") or ""
            if reason:
                truncated_reasons.append(reason)
    top = sorted(all_donors, key=lambda d: d.get("total", 0) or 0, reverse=True)[:top_n]
    all_txns.sort(key=lambda t: t.get("amount", 0) or 0, reverse=True)   # largest-first
    # Bound the retained rows: the pull is already page-capped and largest-first,
    # but a candidate with many committees could still stack up. 5000 largest
    # receipts is plenty for detail work and keeps the result JSON sane.
    TXN_CAP = 5000
    txns_capped = len(all_txns) > TXN_CAP
    txns = all_txns[:TXN_CAP]
    return {
        "note": ("Top itemized donors (largest contributions). The campaign's full "
                 "receipts total is in the composition section, from FEC /totals — "
                 "not the sum below."),
        "largest_contributions_total": round(pulled_total, 2),
        "donor_count": len(all_donors),
        "top_donors": top,
        # Full pulled list (largest-first) — same capped 10-page Schedule A pull,
        # just not truncated to top_n. Powers the CSV export so it reflects every
        # donor actually pulled (~70), not only the ~25 shown on-page. Still the
        # largest-itemized pull, NOT the quarter-million-row full file.
        "pulled_donors": sorted(all_donors, key=lambda d: d.get("total", 0) or 0,
                                reverse=True),
        # Transaction-level receipts (largest-first, capped) for the detail CSV —
        # the individual contributions behind each donor summary.
        "transactions": txns,
        "transaction_count": len(all_txns),
        "transactions_capped": txns_capped,
        # Corrections applied to the raw Schedule A rows, disclosed rather than
        # silent: memo re-itemizations dropped, refunds netted.
        "memo_skipped": memo_skipped,
        "refunds_applied": refunds_applied,
        "refund_total": round(refund_total, 2),
        # Aggregated across committees, mirroring how search_fec_candidate
        # already aggregates any_truncated/truncated_reasons per candidate —
        # a truncated pull on ANY committee must not be dropped here, or the
        # sched_a step below has no way to know the pull was incomplete.
        "truncated": any_truncated,
        "truncated_reason": "; ".join(truncated_reasons),
    }


def _run_search(job_id: str, name: str, fec_key: str, demo: bool,
                anthropic_key: str = "",
                candidate_id: str = "", candidate_name: str = "",
                office: str = "", state: str = "") -> None:
    """Runs in a worker thread. Drives the StepLog stage by stage. Never raises
    out of here — a gate failure or crash is recorded on the job as an error and
    the offending step is left FAIL, so the frontend can show exactly where and
    why it stopped instead of just spinning forever."""
    job = JOBS[job_id]
    log: StepLog = job["log"]
    cycle = fec._current_cycle()

    log.plan([
        ("resolve",     "Resolve candidate (FEC candidate_id)"),
        ("sched_a",     "Pull direct contributions (Schedule A)"),
        ("sched_e",     "Pull outside spending (Schedule E)"),
        ("composition", "Per-cycle funding composition (big-donor / PAC / outside)"),
        ("record",      "Pull legislative record (Congress.gov)"),
        ("votes",       "Pull roll-call votes (House Clerk / Senate LIS)"),
        ("statements",  "Gather public statements on money (Track B)"),
        ("synthesize",  "Plain-language report (translation layer)"),
    ])

    result: dict = {"candidate": name, "cycle": cycle}

    try:
        # ── 1. resolve ──────────────────────────────────────────────────────
        with log.step("resolve") as s:
            if demo:
                time.sleep(0.4)
                cid, cand_name = "S8GA00180", "OSSOFF, T. JONATHAN"
                result["state"], result["office"] = "GA", "S"
            elif candidate_id:
                # The user confirmed an exact FEC candidate in the "Did You Mean?"
                # picker — trust it. No name search, so multi-run people (House
                # vs Senate) and nicknames FEC can't match are already resolved.
                cid, cand_name = candidate_id, (candidate_name or name)
                result["state"], result["office"] = state, office
            else:
                cands = fec.search_fec_candidates(name, fec_key)
                if not cands:
                    s.fail(f"no federal candidate matched {name!r}")
                    job["error"] = "no candidate match"
                    return
                top = cands[0]
                cid, cand_name = top.get("candidate_id"), top.get("name") or name
                result["state"], result["office"] = top.get("state", ""), top.get("office", "")
            result["candidate_id"], result["candidate_name"] = cid, cand_name
            s.ok(f"matched {cand_name}  ({cid})")

        # ── 2. Schedule A — direct contributions (Pipe 1) ───────────────────
        with log.step("sched_a") as s:
            if demo:
                cb = s.progress()
                for p in range(1, 6):
                    cb(p, 5)
                    time.sleep(0.25)
                result["track_a_direct"] = {"_demo": True, "total_raised": 57_300_000,
                                            "donor_count": 233_000}
                s.ok("57.3M raised, primarily small-dollar (demo data)")
            else:
                # Cap the pull: largest-first, ~10 pages. We want the biggest
                # donors, not a quarter-million $5 records. The real total is
                # the composition stage (/totals), so this is fast and honest.
                groups = fec.search_fec_candidate(cand_name, fec_key,
                                                  progress_cb=s.progress(), max_pages=10,
                                                  only_candidate_id=result.get("candidate_id"))
                result["track_a_direct"] = _summarize_direct(fec.to_jsonable(groups))
                n = result["track_a_direct"]["donor_count"]
                shown = len(result["track_a_direct"]["top_donors"])
                # Disclose the Schedule A corrections so the donor figures are
                # auditable: memo re-itemizations dropped (not new money) and
                # refunds netted against donor totals.
                adj = []
                if result["track_a_direct"].get("memo_skipped"):
                    adj.append(f"{result['track_a_direct']['memo_skipped']} memo "
                               f"re-itemizations dropped")
                if result["track_a_direct"].get("refunds_applied"):
                    adj.append(f"{result['track_a_direct']['refunds_applied']} refund(s) "
                               f"netted (${abs(result['track_a_direct']['refund_total']):,.0f})")
                extra = f" — {', '.join(adj)}" if adj else ""
                msg = (f"top {shown} of {n:,} largest itemized donors "
                       f"(campaign total in composition, below){extra}")
                # A later page failing after retries (sustained rate limiting,
                # an outage, or a request-construction bug like the one in
                # Claude_Recommendations.md) means the "largest donors" pull
                # stopped short of its max_pages cap — that's a real
                # incompleteness, not the deliberate cap, and must warn rather
                # than ok, mirroring the sched_e step's incomplete check above.
                if result["track_a_direct"].get("truncated"):
                    reason = result["track_a_direct"].get("truncated_reason") or ""
                    s.warn(msg + f" — pull stopped early: {reason}")
                else:
                    s.ok(msg)

        # ── 3. Schedule E — outside spending (Pipe 2) ───────────────────────
        with log.step("sched_e") as s:
            if demo:
                time.sleep(0.6)
                result["track_a_outside"] = {"_demo": True, "support_total": 4_100_000,
                                             "oppose_total": 1_200_000}
                # show the #3396 warn path live
                s.warn("returned 312 of 487 reported rows — openFEC pagination "
                       "bug (#3396); outside-spending totals are a lower bound")
            else:
                outspend = fec.outside_spending_for_candidate(
                    result.get("candidate_id"), fec_key, cycle,
                    candidate_name=cand_name, progress_cb=s.progress())
                result["track_a_outside"] = fec.outside_spending_to_jsonable(outspend)
                # Disclose the notice/report collapse: raw Schedule E rows list
                # the same expenditure twice (24/48-hour notice + regular
                # report). Totals here are deduped; any dollars known ONLY from
                # a notice (regular report not yet processed) are called out so
                # the totals reconcile against fec.gov's own aggregate.
                dd = outspend.dedup or {}
                extra = ""
                if dd.get("notice_deduped"):
                    extra += f" ({dd['notice_deduped']} notice rows deduped vs regular reports)"
                no_sum = (dd.get("notice_only_support") or 0) + (dd.get("notice_only_oppose") or 0)
                if no_sum:
                    extra += (f" — ${no_sum:,.0f} of the total is from 24/48-hour "
                              f"notices not yet on a processed regular report")
                if outspend.incomplete:
                    s.warn(outspend.incomplete_reason + extra)
                else:
                    s.ok(f"${outspend.support_total:,.0f} support / "
                         f"${outspend.oppose_total:,.0f} oppose, "
                         f"{len(outspend.spenders)} spenders" + extra)

        # ── 4. per-cycle funding composition (factual Track A) ──────────────
        cycles = _default_cycles()
        with log.step("composition") as s:
            # allow_warn=True: sched_a warns (rather than fails) when the
            # capped donor pull was truncated — composition doesn't consume
            # that capped list (it re-pulls totals from FEC's own /totals),
            # so a disclosed-but-partial sched_a must not halt everything
            # downstream, same as sched_e's incomplete flag never does.
            log.require("sched_a", allow_warn=True)
            if demo:
                time.sleep(0.5)
                result["track_a_composition"] = _demo_composition()
                s.ok("5 cycles: small-dollar share falling, PAC + outside rising (demo)")
            else:
                fcs = fec.funding_by_cycle(result.get("candidate_id"), fec_key,
                                           cycles, include_outside=True,
                                           current_cycle=cycle,
                                           current_outside=outspend if not demo else None,
                                           progress_cb=s.progress())
                result["track_a_composition"] = [fec.funding_cycle_to_jsonable(fc) for fc in fcs]
                if any(fc.incomplete for fc in fcs):
                    s.warn(f"{len(fcs)} cycles compiled — some outside-spending "
                           f"totals are lower bounds (#3396)")
                else:
                    s.ok(f"{len(fcs)} cycles of funding composition compiled")

        # ── 5. legislative record (factual Track B — Congress.gov) ──────────
        with log.step("record") as s:
            log.require("resolve")
            if demo:
                time.sleep(0.5)
                rec = congress.demo_record()
                result["track_b_record"] = congress.record_to_jsonable(rec)
                s.warn(f"{len(rec.actions)} legislative actions, "
                       f"{sum(1 for a in rec.actions if a.money_related)} money-related "
                       f"(demo fixtures)")
            else:
                ckey = os.getenv("CONGRESS_API_KEY", "")
                if not ckey:
                    result["track_b_record"] = {}
                    s.warn("no CONGRESS_API_KEY set — legislative record skipped "
                           "(money side still complete)")
                else:
                    last_name = (cand_name.split(",")[0] or cand_name).strip()
                    try:
                        rec = congress.legislative_record(
                            result.get("state", ""), last_name, ckey,
                            office=result.get("office", ""), progress_cb=s.progress())
                        result["track_b_record"] = congress.record_to_jsonable(rec)
                        money_n = sum(1 for a in rec.actions if a.money_related)
                        msg = (f"{len(rec.actions)} legislative actions, "
                               f"{money_n} money/finance-related")
                        if rec.incomplete:
                            s.warn(msg + " — record may be a lower bound (page cap)")
                        else:
                            s.ok(msg)
                    except congress.CongressAPIError as e:
                        # A challenger with no record, or an API problem — disclose,
                        # don't fake. The money side already succeeded.
                        result["track_b_record"] = {"error": str(e)}
                        s.warn(f"legislative record unavailable: {e}")

        # ── 6. roll-call votes (factual Track B — how they VOTED) ───────────
        with log.step("votes") as s:
            log.require("resolve")
            if demo:
                time.sleep(0.4)
                vrec = votes.demo_votes()
                result["track_b_votes"] = votes.record_to_jsonable(vrec)
                linked = votes.link_votes_to_record(
                    result["track_b_votes"], result.get("track_b_record") or {})
                s.warn(f"{len(vrec.votes)} money-related roll calls of "
                       f"{vrec.total_votes_scanned:,} scanned"
                       + (f", {linked} on their own bill(s)" if linked else "")
                       + " (demo fixtures)")
            else:
                voffice = result.get("office", "")
                last_name = (cand_name.split(",")[0] or cand_name).strip()
                bioguide = (result.get("track_b_record") or {}).get("bioguide_id", "")
                ckey = os.getenv("CONGRESS_API_KEY", "")
                if voffice.upper().startswith("H") and not ckey:
                    result["track_b_votes"] = {}
                    s.warn("no CONGRESS_API_KEY set — House roll-call votes skipped "
                           "(the beta house-vote API needs it; money side complete)")
                else:
                    try:
                        vrec = votes.vote_record(
                            voffice, bioguide_id=bioguide, last_name=last_name,
                            state=result.get("state", ""), api_key=ckey,
                            progress_cb=s.progress())
                        result["track_b_votes"] = votes.record_to_jsonable(vrec)
                        # Factual join: flag votes cast on the candidate's OWN
                        # sponsored/cosponsored bills (exact congress + bill
                        # match — a recorded relationship, not a judgment).
                        linked = votes.link_votes_to_record(
                            result["track_b_votes"],
                            result.get("track_b_record") or {})
                        msg = (f"{len(vrec.votes)} money/finance-related roll calls "
                               f"(of {vrec.total_votes_scanned:,} scanned, "
                               f"{vrec.chamber}) with the member's position"
                               + (f", {linked} on their own bill(s)" if linked else ""))
                        if vrec.incomplete:
                            s.warn(msg + " — " + vrec.incomplete_reason)
                        else:
                            s.ok(msg)
                    except votes.VotesAPIError as e:
                        # A challenger with no chamber, or a source problem —
                        # disclose, don't fake. Everything upstream stands.
                        result["track_b_votes"] = {"error": str(e)}
                        s.warn(f"roll-call votes unavailable: {e}")
                    except congress.CongressAPIError as e:
                        result["track_b_votes"] = {"error": str(e)}
                        s.warn(f"roll-call votes unavailable (Congress.gov): {e}")

        # ── 7. public statements (factual Track B — what they SAID) ─────────
        with log.step("statements") as s:
            log.require("resolve")
            # Web search needs the human name ("Jon Ossoff"), NOT the FEC format
            # ("OSSOFF, T. JONATHAN") — the latter matches nothing online. Use the
            # name as typed.
            search_name = name.strip() or cand_name
            office = result.get("office") or ""
            if demo:
                time.sleep(0.5)
                ss = statements.collect_statements(search_name, searxng_url="", office=office)  # fixtures
                result["track_b_statements"] = statements.statements_to_jsonable(ss)
                s.warn(f"{len(ss.statements)} statements gathered (offline fixtures — "
                       f"set SEARXNG_URL for live)")
            else:
                searxng = os.getenv("SEARXNG_URL", "")
                if not searxng:
                    result["track_b_statements"] = {}
                    s.warn("no SEARXNG_URL set — statement gathering skipped "
                           "(money + record still complete)")
                else:
                    ss = statements.collect_statements(search_name, searxng_url=searxng, office=office)
                    result["track_b_statements"] = statements.statements_to_jsonable(ss)
                    cov = ss.coverage
                    notes = []
                    if cov.get("source_skewed"):
                        notes.append(f"source-skewed ({cov.get('source_skew')})")
                    if cov.get("undated"):
                        yo = cov.get("undated_year_only") or 0
                        notes.append(f"{cov['undated']} undated"
                                     + (f" ({yo} year-only)" if yo else ""))
                    bc = (cov.get("by_class") or {})
                    msg = f"{len(ss.statements)} statements gathered from {len(cov.get('by_source', {}))} sources"
                    if bc:
                        own = bc.get("by_candidate", 0)
                        off = sum(v for k, v in bc.items() if k != "by_candidate")
                        msg += f" ({own} his voice on timeline, {off} coverage/boilerplate held off)"
                    if cov.get("dated_via_page"):
                        msg += f" ({cov['dated_via_page']} dated via page fetch)"
                    # A zero-own-voice result is a limit of the method, not a
                    # fact about the candidate — say so, or the report reads it
                    # as a finding (see statements._own_voice_note).
                    if cov.get("own_voice_note"):
                        notes.append(cov["own_voice_note"])
                    if notes:
                        s.warn(msg + " — caveats: " + "; ".join(notes))
                    else:
                        s.ok(msg)

        # Deterministic Side-by-Side timeline: built in code from the assembled
        # tracks (sorting is not the model's job — it mis-sorted adjacent dates
        # in two real runs). The synthesis layer restates this IN ORDER.
        result["timeline"] = timeline.build_timeline(result)

        # audit must be assembled BEFORE synthesis so the report can carry the
        # caveats through (preserve-the-audit-trail rule).
        result["audit"] = [{"label": w.label, "detail": w.detail}
                           for w in log.warnings()]

        # ── 8. translation layer (the ONLY interpretive step; isolated) ─────
        # Activates only when an Anthropic key is posted from the UI. Blank the
        # field and the tool is exactly the validated factual version — a failure
        # here warns and moves on, never touching the factual result.
        with log.step("synthesize") as s:
            # UI-field key only — no env var fallback. This local variable is the
            # ONLY place the key lives — it is never written into `result`, the
            # job record, or any log line, so it cannot leak through /status or
            # be persisted anywhere.
            akey = anthropic_key
            if demo:
                time.sleep(0.3)
                s.warn("synthesis skipped in demo mode (add an Anthropic key "
                       "and run real for the plain-language report)")
            elif not akey:
                s.warn("no Anthropic key provided — plain-language report "
                       "skipped (factual result above is complete)")
            else:
                try:
                    out = synthesis.synthesize(result, akey)
                    result["report"] = out["report"]
                    result["report_meta"] = {k: out[k] for k in
                                             ("model", "content_hash", "cached",
                                              "readability_grade")}
                    bits = ["reused cached report (data unchanged)" if out["cached"]
                            else f"report generated ({out['model']})"]
                    if out.get("readability_grade", -1) >= 0:
                        bits.append(f"reading grade {out['readability_grade']}")
                    # Deterministic reconciliation guard: does the prose actually
                    # match the JSON? High-severity contradictions (a spender on
                    # the wrong support/oppose side, a cycle total not restated)
                    # ride into the audit as a caveat; review-level items (a
                    # figure that doesn't trace to a source number) are noted.
                    rec = out.get("reconciliation") or {}
                    result["report_reconciliation"] = rec
                    highs = [w for w in rec.get("warnings", [])
                             if w.get("severity") == "high"]
                    reviews = [w for w in rec.get("warnings", [])
                               if w.get("severity") == "review"]
                    if highs:
                        s.warn(" — ".join(bits) + " — report reconciliation flagged "
                               + f"{len(highs)} contradiction(s): "
                               + "; ".join(w["message"] for w in highs)
                               + (f" (+{len(reviews)} figure(s) to verify)"
                                  if reviews else ""))
                    else:
                        if reviews:
                            bits.append(f"reconciliation: {len(reviews)} figure(s) "
                                        f"to verify")
                        else:
                            bits.append("reconciliation clean")
                        s.ok(" — ".join(bits))
                except synthesis.SynthesisError as e:
                    result["report_error"] = str(e)
                    s.warn(f"synthesis failed (factual result unaffected): {e}")

        # refresh audit so synthesis warnings are visible in the header too
        result["audit"] = [{"label": w.label, "detail": w.detail}
                           for w in log.warnings()]
        job["result"] = result

    except StepGateError as e:
        # A gate stopped the pipeline before a bad result could be built. This is
        # a SUCCESS of the design, surfaced as a clear halt, not a crash.
        job["error"] = f"halted by gate: {e}"
    except fec.FECAPIError as e:
        # A rate limit on the shared (creator) key is the one error a user can do
        # something about — set a flag so the frontend reveals the optional
        # "use your own FEC key" field with a clear message. A bad/own-key 403 or
        # a connection failure is reported plainly instead.
        if getattr(e, "is_rate_limit", False):
            job["rate_limited"] = True
            job["error"] = ("The shared FEC API key has hit its hourly rate limit. "
                            "Try again shortly, or add your own free FEC key below "
                            "to keep going now.")
        elif getattr(e, "is_rate_limit", False):
            job["error"] = ("That FEC key is also rate-limited. Wait for the hourly "
                            "window to reset, or use a different key.")
        else:
            job["error"] = f"FEC API error: {e}"
    except Exception as e:  # noqa: BLE001 — never spin forever; record and stop
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job["done"] = True


# ── app factory ───────────────────────────────────────────────────────────────
def create_app() -> Flask:
    app = Flask(__name__)

    if _HAS_LIMITER:
        limiter = Limiter(key_func=get_remote_address, app=app,
                          default_limits=[])
        search_limit = limiter.limit("20 per minute")
    else:
        def search_limit(f):  # no-op decorator
            return f

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/candidates", methods=["POST"])
    @search_limit
    def candidates():
        """Fast, synchronous 'Did You Mean?' lookup — runs BEFORE the heavy
        pipeline. Turns a typed name (incl. nicknames FEC can't match) into a
        ranked shortlist the UI shows so the user confirms the exact
        office/cycle. Uses the creator's FEC key; no user key involved."""
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if len(name) < 2:
            return jsonify({"error": "enter a candidate name"}), 400
        fec_key = os.getenv("FEC_API_KEY", "")
        if not fec_key:
            # Demo mode has no live FEC key — offer the demo candidate so the
            # picker flow still works end-to-end without a key.
            return jsonify({"demo": True, "query": name, "broadened": False,
                            "candidates": [{
                                "candidate_id": "S8GA00180",
                                "name": "OSSOFF, T. JONATHAN (demo)",
                                "office": "S", "office_full": "U.S. Senate",
                                "state": "GA", "district": None, "party": "Democratic Party",
                                "cycles": [2020, 2026], "last_cycle": 2026,
                                "status": "C", "inactive": False,
                                "incumbent_challenge": "Incumbent", "has_raised_funds": True,
                            }]})
        try:
            out = fec.resolve_candidate_options(name, fec_key)
        except fec.FECAPIError as e:
            return jsonify({"error": f"FEC candidate lookup failed: {e}"}), 502
        out["demo"] = False
        return jsonify(out)

    @app.route("/search", methods=["POST"])
    @search_limit
    def search():
        _sweep_old_jobs()
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if len(name) < 2:
            return jsonify({"error": "enter a candidate name"}), 400

        # Key model:
        #  - FEC_API_KEY (creator's, env) runs the money side for everyone.
        #  - The optional UI field now carries the USER'S ANTHROPIC KEY for the
        #    report layer. It is used for THIS request only and is never stored,
        #    logged, or returned — see _run_search.
        fec_key = os.getenv("FEC_API_KEY", "")
        anthropic_key = (data.get("anthropic_key") or "").strip()
        demo = (str(data.get("demo", "")).lower() in ("1", "true", "yes")
                or not fec_key)

        # A candidate the user confirmed in the "Did You Mean?" picker (exact
        # FEC id + its office/state/name). Optional — a bare name still works.
        candidate_id = (data.get("candidate_id") or "").strip()
        candidate_name = (data.get("candidate_name") or "").strip()
        office = (data.get("office") or "").strip()
        state = (data.get("state") or "").strip()

        job_id = _new_job()
        threading.Thread(target=_run_search,
                         args=(job_id, name, fec_key, demo, anthropic_key,
                               candidate_id, candidate_name, office, state),
                         daemon=True).start()
        return jsonify({"job_id": job_id, "demo": demo})

    @app.route("/status/<job_id>")
    def status(job_id):
        with _JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown or expired job"}), 404
        # The SAME log.to_dict() the export layer will embed as an audit trail.
        return jsonify({
            "done": job["done"],
            "error": job["error"],
            "rate_limited": job["rate_limited"],   # frontend reveals the key field on this
            "result": job["result"],
            "log": job["log"].to_dict(),
        })

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "has_fec_key": bool(os.getenv("FEC_API_KEY"))})

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    has_key = bool(os.getenv("FEC_API_KEY"))
    print(f"\n  Campaign Tracker — http://localhost:{port}")
    print(f"  Mode: {'REAL (FEC_API_KEY found)' if has_key else 'DEMO (no FEC_API_KEY — simulated data)'}")
    print(f"  Set FEC_API_KEY to run live. Users never need their own key unless the")
    print(f"  shared one is rate-limited, in which case the UI offers an optional field.\n")
    # threaded=True so /status polls aren't blocked by a running worker.
    # debug defaults OFF: Werkzeug's debug mode ships an interactive
    # in-browser debugger that allows arbitrary code execution to anyone who
    # can reach it — fine on a laptop you alone can reach, not something an
    # open-source tool should default to for everyone who clones it. Opt in
    # for local development with FLASK_DEBUG=1.
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, threaded=True, debug=debug)

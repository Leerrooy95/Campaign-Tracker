# Campaign Tracker — v1.2

*Working name.* A federal-first tool that lays a candidate's **money** next to
their **record** — factually, with every line sourced — so you can judge for
yourself whether their funding and their actions on money-in-politics line up.

Point it at any current or former U.S. House or Senate member (or challenger),
and it pulls their FEC filings and Congress.gov/House Clerk/Senate LIS record
itself, lines them up on one dated timeline, and shows its work — every
dollar traces to an FEC record ID, every bill and vote to a congress.gov/
clerk citation you can click and verify yourself.

See [`CHANGELOG.md`](CHANGELOG.md) for what changed release to release.

---

## The question it answers

Not "is this candidate bought." Something narrower and more honest:

> Is a candidate's stance on money in politics a **principle** (it shows up in
> what they actually do) or a **position** (they object to who's currently
> winning, not to the money itself)?

The tool doesn't answer that for you. It assembles the two factual records —
how they're funded, and what they did legislatively — on one timeline, each item
citing its primary source, and leaves the judgment to the reader. **No scoring,
no verdict, no correlation number.** That's deliberate: a score is an
interpretation that can be biased and can't be cleanly checked; a sourced fact
can.

---

## Why federal-first

Every candidate for federal office discloses 100% of their money to the **FEC**,
and every sitting member's legislative record is on **Congress.gov**. Both are
free, official, one API each. No 50-state normalization problem, no paywalled
data. (State-office targets would need a state plugin; federal candidates don't.)

---

## How it works

Two **factual** tracks, put on one timeline:

**Track A — the money (FEC).** Two pipes, never conflated:
- **Pipe 1 — direct money** (per cycle, from `/totals`): the itemized
  (big-donor) vs unitemized (small-dollar) split, **plus PAC contributions** —
  because "I refuse corporate PAC money" is often the exact claim at issue. The
  per-cycle totals come from `/totals`; the **biggest individual donors** are
  listed from Schedule A pulled *largest-first and capped* — so the tool surfaces
  the meaningful donors in seconds instead of crawling a candidate's full
  quarter-million-row contribution history (the headline total still comes from
  `/totals`, never from summing that capped list). Donor figures are
  **memo-filtered and refund-netted** — FEC re-itemizes already-counted receipts
  on later reports (they'd otherwise double a donor's total) and posts refunds
  as negative rows the largest-first pull never reaches; both corrections are
  applied and disclosed.
- **Pipe 2 — outside spending** (Schedule E): uncapped super-PAC money spent for
  or against the candidate, with a **two-hop trace** (who funded the super PAC?)
  that closes for super PACs and flags the dead-end at dark money. Totals are
  **notice-deduplicated** — FEC data reports the same expenditure twice (a
  24/48-hour notice, then the regular report), so raw sums run ~2x high; the
  tool collapses those pairs, keeps not-yet-reported notice-only spending
  flagged and disclosed, and reconciles to the cent against FEC's own
  by-candidate aggregate. For the **funding-composition chart**, the current
  cycle uses this row-level detail while **prior cycles pull their for/against
  totals straight from FEC's authoritative by-candidate aggregate** — the
  row-level dedup is exact-verified on the current cycle but doesn't scale to
  old high-volume filings, and the aggregate is FEC's own de-duplicated figure.

**Track B — the record (two factual sources).** What the candidate *did* and
*said*, both dated, sourced, and unscored:
- **Bills** (`congress.py`, Congress.gov): legislation sponsored and cosponsored,
  money/finance-tagged by a whole-word regex with **opt-in plurals** — tuned
  against thousands of real bill/vote titles, so "Ban Corporate PACs" matches
  and "Living Donor Protection Act" doesn't.
- **Roll-call votes** (`votes.py`): how they actually voted on the floor —
  House via the Congress.gov beta house-vote API (118th Congress/2023+,
  member position matched by bioguideId), Senate via **LIS XML** (no key,
  member matched by last name + state). Money-tagged with the *same* term
  list as the bills (one matcher, no drift), each vote citing its primary
  source (clerk.house.gov / senate.gov). Votes on the candidate's **own**
  sponsored/cosponsored bills are flagged (`candidate_bill_role`) — a
  recorded relationship, not a judgment. Coverage limits and lookup caps are
  disclosed, never papered over.
- **Statements** (`statements.py`): public statements on money/influence, gathered
  by web search across neutral angles, returned as a full dated set with a
  coverage report — never pre-filtered to the damning quotes. Each statement is
  **classified** as the candidate's own voice (`by_candidate`), coverage *about*
  him (`about_candidate`), or `boilerplate` (donation CTAs / data pages), each
  with a reason so the call is auditable — and only his own voice reaches the
  timeline, since rhetoric-vs-funding is only meaningful on his own words.
  Dates come from the search engine, a `YYYY-MM-DD` URL slug, or — for the many
  results search returns undated — a **page-fetch tier** that reads the
  publication date from the page's own metadata. Undatable results stay honestly
  undated (never guessed). Factual leads (excerpt + date + source URL);
  precise-quote extraction is a later refinement.

Note both are **data**, not judgments. The three tracks are merged onto a single
dated **timeline** assembled deterministically *in code* (`timeline.py`) — sorted
newest-first and grouped into month bands — never model-sorted, so ordering can't
drift. The interpretive "rhetoric" pass — a model reading the whole assembled
picture for nuance — runs as a *final synthesis layer* (`synthesis.py`), built
last, against real gathered data, and only *restates* that pre-built timeline in
order.

```
candidate name
      │
      ▼
 "Did You Mean?" picker ── FEC candidate search (nicknames broaden to last name, no alias table)
      │                    → pick exact candidate (office + cycle confirmed; people run for >1 seat)
      ▼
 resolve (exact FEC id + state) ─┬─► Schedule A: top donors (largest contributions) ─► Schedule E (Pipe 2) ─► per-cycle composition
                           ├─► Congress.gov: sponsored + cosponsored legislation (money-tagged)
                           ├─► roll-call votes (House Clerk via Congress.gov API / Senate LIS XML, money-tagged, positions cited)
                           └─► public statements on money (SearXNG: dated, sourced, unscored, full search log kept)
      │
      ├──── every step logged, gated, shown live → factual result the reader judges
      ├─► translation layer (Claude Sonnet 5): plain-language report, every line cited (optional)
      └─► browser renders: money charts (SVG, from FEC data) + report (markdown) + raw JSON (collapsed)
```

## What it outputs right now

A **structured, fully-sourced result**, rendered in the browser as:

- **Four charts, drawn straight from the result data** (inline SVG, zero
  dependencies, works offline): *funding composition by cycle* (small-dollar /
  large-donor / PAC stacked, total receipts labeled), *outside spending by
  group* (divergent for/against bars per super PAC, untraceable **dark money**
  hatched and flagged ⚠, labels laid out from measured text width), the
  *cross-cycle outside-spending trend* (for/against per cycle — prior cycles
  from FEC's own aggregate), and the *side-by-side timeline swimlane* — money /
  record / statements lanes over the same month bands the timeline was built
  in, bills as circles, roll-call votes as diamonds, empty months compressed to
  an honest "⋯" gap, opening scrolled to the newest end. All render whether or
  not a report is generated.
- **A roll-call votes ledger**: one row per money-relevant vote — date, Yea/Nay
  position badge, "their bill" tag where the vote was on the candidate's own
  legislation, and the citation linking to the official record — with the
  coverage limits restated in the footnote.
- **Data export** (between the caveats and the first chart): the full collected
  dataset as CSVs — donors and **per-contribution donor transactions**, outside
  spenders and **per-expenditure spender transactions**, per-cycle composition,
  the entire legislative record, **roll-call votes with positions and
  citations**, statements, the **raw statement search log** (every engine
  result with its kept/dropped disposition — the statements stage's audit
  trail), and the merged timeline — with a per-table button each plus a
  **Download-all ZIP** (CSVs + the raw `result` JSON). Built entirely in the
  browser from data already delivered, so it adds no server route and touches
  nothing sensitive; the dataset stays a download, not an on-page dump.
  Transaction rows are the largest-first capped Schedule A pull and the deduped
  current-cycle Schedule E — individual-transaction grain, not the full
  quarter-million-row file.
- **A plain-language report** (only when an Anthropic key is provided): the
  translation layer (`synthesis.py`, Claude Sonnet 5) restates the cited facts
  side by side — Money → Record → Roll-Call Votes → Statements → a merged dated
  **timeline** (built deterministically in code, sorted newest-first and
  grouped into month bands) → Gaps & Caveats — drawing no conclusions and using
  only the provided data (no imported biography). The two most error-prone
  number blocks are **not written by the model at all**: the per-cycle Money
  Picture and the per-spender ledger are machine-rendered from the JSON and
  swapped in for placeholders, so no figure can be dropped and no spender's
  support/oppose side can flip. Before it's shown, the report is run back
  through a deterministic **reconciliation guard** (`reconcile.py`) that checks
  the remaining prose against the source JSON — a wrong spending side, a
  missing cycle total, or a dollar figure that doesn't trace to the data
  (rounded-variant matching, not a loose percentage band) is caught and ridden
  into the caveats. The one interpretive step is verified, not trusted.
- **The raw JSON**, collapsed behind a toggle — the verifiable source where every
  donation traces to an FEC `sub_id`, every action to a Congress.gov bill, and
  every statement to a source URL. Collapsed so it doesn't bury the report, and
  dropped entirely from print/PDF export.

Underneath: Track A gives the money composition over recent cycles plus the top
itemized donors; Track B gives the money-tagged legislative record by year and
the candidate's public statements with a coverage report.

How you verify it isn't biased: there's nothing scored to be biased — every line
links to a primary source you can click. Spot-check a few; they'll match.

---

## Using it

Once the app is running (see **Quick start** below), the flow in the browser is:

1. **Type a name and hit Search.** Nicknames are fine ("Mike" broadens to the
   last name automatically) — no alias table to keep up to date.
2. **Confirm who you mean.** A "Did You Mean?" picker shows every matching FEC
   candidate — office, state/district, party, cycles — because plenty of
   people have run for more than one seat, and the tool won't guess which one
   you meant.
3. **Watch the eight steps run live**, polling in place: resolve → direct
   contributions → outside spending → funding composition → legislative
   record → roll-call votes → public statements → (optional) the
   plain-language report. Each step turns ok/warn/fail in real time — a warn
   (e.g. a disclosed pagination lower bound) is not a bug, it's the tool
   telling you exactly what it couldn't get and why.
4. **Read the result.** Four charts render immediately (funding composition,
   outside spending by group, the cross-cycle trend, and the timeline
   swimlane), followed by a roll-call votes ledger, then — if you pasted an
   Anthropic key — the plain-language report, then the raw JSON collapsed
   behind a toggle for anyone who wants to verify a figure by hand.
5. **Export if you want the data**, not just the page: a CSV button per table
   (donors, transactions, spenders, votes, statements, the raw search log,
   the timeline) plus a "download all" ZIP, built entirely in your browser
   from the page's own data — nothing round-trips through the server again.

The Anthropic key field is optional and per-request: paste a key to get the
plain-language report, or leave it blank and you get the exact same factual
result minus that one section. Nothing you type into that field is ever
stored — see **Quick start** below for the details.

---

## Quick start

```bash
pip install -r requirements.txt    # Flask + flask-limiter (add --break-system-packages on Debian/Crostini)
python3 app.py                     # http://localhost:5000
```

With no keys set, searches run in **demo mode** on synthetic fixtures — watch the
eight steps light up, the Schedule-E warn drop into the audit box, and the factual
result print (the synthesis step shows as skipped without a key).
No keys, no network.

For **live** data, the easiest path is the included **`run.sh`** — set your keys
in a `.env` file once, then launch:

```bash
cp .env.example .env               # then edit .env: paste FEC_API_KEY + CONGRESS_API_KEY, set SEARXNG_URL
# (Anthropic key is UI-only now — paste it into the web field per-request)
bash run.sh                        # sources .env, then runs the app
```

`.env` is gitignored — real keys never end up in `run.sh` itself or in git
history.

**The app binds to `127.0.0.1` (this machine only) by default and has no
built-in authentication, authorization, or TLS.** That's the right default
for local use. If you want it reachable from another machine, set
`HOST=0.0.0.0` (or a LAN address) explicitly — you'll get a printed warning
— and put a reverse proxy doing TLS + auth in front of it first. See
`Security_Recommendations.md` before exposing this beyond your own machine.

Or set them by hand:

```bash
export FEC_API_KEY=your_fec_key            # the creator's key — users don't need their own
export CONGRESS_API_KEY=your_congress_key  # free from api.data.gov (same key system)
export SEARXNG_URL=http://localhost:8080   # your self-hosted SearXNG (for statements); optional
python3 app.py
```

**Getting SearXNG running.** `docker/searxng/` has a ready-to-use compose
file (a stock SearXNG install answers only HTML, not the JSON API
`statements.py` needs, so a settings override is required either way):

```bash
cd docker/searxng
cp .env.example .env                                       # port override only
sed -i "s|ultrasecretkey|$(openssl rand -hex 32)|g" settings.yml
docker compose up -d
```

See [`docker/searxng/README.md`](docker/searxng/README.md) for how to verify
the JSON API is actually enabled before your first real run. Skipping this
entirely is also fine — the statements stage is skipped and disclosed, and
the money + legislative record + votes tracks run complete without it.

**Key model.** `FEC_API_KEY`, `CONGRESS_API_KEY`, and `SEARXNG_URL` are all
server-side (set in `run.sh` / the environment). Without the Congress key the
record stage is skipped and disclosed; without the SearXNG URL the statements
stage is skipped and disclosed; the money side always runs.

The **Anthropic key is different** — it only has a field in the UI (no env
var), and is treated as **secret and transient: used for the single request
only, never written to disk, logs, the job record, or any response, and
cleared from the page the instant you submit.** With a key, the translation
layer writes the report; without one, that stage is skipped and disclosed and
the tool is exactly the validated factual version. No paid search
services are used anywhere — the free routes are a self-hosted SearXNG or
`ddgs` (DuckDuckGo, no key), never a pay-to-browse API.

---

## For developers

**Getting an AI assistant to help you set this up or extend it.** This repo
ships a [`CLAUDE.md`](CLAUDE.md) — a detailed architecture and design-rules
document written for AI coding assistants (Claude Code and similar tools that
read project context files automatically). If you're using one, it already
has everything it needs to walk you through installation, explain why a
module works the way it does before you change it, or help you debug a run
that came back with an unexpected warning. Point it at this repo and just
ask.

**Running the tests.** Offline, plain-Python, no framework, no network, no
keys:

```bash
python3 tests/test_schedule_e_dedup.py   # or any other tests/test_*.py file
bash tests/run_export_tests.sh           # the client-side CSV/ZIP export (needs Node)
```

**Project layout, in brief** — see `CLAUDE.md`'s file map for the full
picture with rationale:

| Area | Files |
|------|-------|
| Money (FEC) | `fec.py` |
| Legislative record + roll-call votes | `congress.py`, `votes.py` |
| Public statements | `statements.py` |
| Merged timeline | `timeline.py` |
| Plain-language report + its verification guard | `synthesis.py`, `reconcile.py` |
| Server, job orchestration, frontend | `app.py`, `templates/`, `static/` |
| Visible-process spine | `steps.py` |
| Regression tests | `tests/` |

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | The single-page UI |
| `/candidates` | POST | `{name}` → ranked "Did You Mean?" shortlist (fast, synchronous) so the exact office/cycle is confirmed before the heavy run |
| `/search` | POST | `{name, anthropic_key?, candidate_id?…}` → `{job_id, demo}`; spawns the worker (key used transiently, never stored) |
| `/status/<job_id>` | GET | Live step log + result; the frontend polls this |
| `/health` | GET | Liveness + whether a FEC key is configured |

---

## The visible process *is* the safety mechanism

The step list isn't a progress bar. Each step is a checkpoint the tool **gates
on**: a later stage refuses to run unless its inputs actually succeeded. A
partial pull (e.g. the openFEC Schedule E pagination bug,
[#3396](https://github.com/fecgov/openFEC/issues/3396)) shows as a warn, rides
into the result as a disclosed lower bound, and never masquerades as a finished
total. So the thing you watched happen is provably the thing that ran.

---

## Data & integrity notes

- **Primary sources only.** FEC for money, Congress.gov for the record. Every
  output line cites a record ID or bill number you can verify.
- **Facts, not scores.** The tool never assigns intensity, sentiment, or a
  consistency grade. It presents sourced facts; the reader concludes.
- **Pipe 1 ≠ Pipe 2.** A maxed-out direct contribution and a super-PAC buy are
  categorically different sizes; they're kept separate.
- **Disclosed incompleteness.** Pagination shortfalls and skipped stages are
  surfaced, never hidden.
- **The report is checked, not trusted.** The one interpretive step — the
  plain-language report — is reconciled against the source JSON by a
  deterministic guard (`reconcile.py`) before it's shown. A spender put on the
  wrong spending side, a missing cycle total, or a figure that doesn't trace to
  the data becomes a caveat rather than shipping silently. The guard is
  non-fatal: it can only add disclosure, never suppress the report.

---

## License

[MIT](LICENSE).

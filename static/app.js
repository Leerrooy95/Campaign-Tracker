// app.js — drives the search and renders the live step trace by polling /status.
// No framework, no build step. The whole job is: start a search, then poll and
// re-render the StepLog until the job is done.

const POLL_MS = 750; // sub-second; each real step takes seconds so this is invisible

const $ = (id) => document.getElementById(id);
const form = $("search-form");

// status glyphs mirror the demo in steps.py so the browser view matches the CLI
const GLYPH = { pending: "○", running: "◐", ok: "●", warn: "▲", fail: "✕" };

// The Anthropic key is read+cleared the moment Search is pressed (so it never
// lingers in the DOM), then held in this closure var only until the run starts.
let pendingKey = "";
let pendingName = "";

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("name").value.trim();
  if (name.length < 2) return;

  resetView();
  $("go").disabled = true;

  const keyField = $("anthropic-key");
  pendingKey = keyField.value.trim();
  keyField.value = "";
  pendingName = name;

  // Phase 1: resolve the name to FEC candidates and let the user confirm which
  // one (office/cycle) before committing to the heavy run.
  try {
    const res = await fetch("/candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "candidate lookup failed");
    renderPicker(data);
  } catch (err) {
    showError(err.message);
    $("go").disabled = false;
  }
});

function renderPicker(data) {
  const cands = data.candidates || [];
  const list = $("picker-list");
  list.innerHTML = "";

  if (!cands.length) {
    $("picker-title").textContent = "No matching candidate";
    $("picker-note").textContent =
      `Nothing on file at the FEC for "${data.query}". Try the last name on its own, ` +
      `the full legal first name, or add a state.`;
    $("picker").hidden = false;
    $("go").disabled = false;
    return;
  }

  $("picker-title").textContent = "Did you mean…";
  $("picker-note").textContent = data.broadened
    ? `No exact match for "${data.query}", so these are the closest by name — pick the right office and cycle.`
    : `Confirm the exact candidate — office and cycle matter (people run for more than one seat).`;

  for (const c of cands) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "picker-row";
    const bits = [c.office_full, c.state && c.district ? `${c.state}-${c.district}` : c.state,
                  c.party, c.last_cycle ? `through ${c.last_cycle}` : null,
                  c.incumbent_challenge,
                  c.status === "C" ? "current" : (c.inactive ? "inactive" : "prior")]
                 .filter(Boolean).join(" · ");
    row.innerHTML =
      `<span class="picker-name">${escapeHtml(prettyName(c.name))}</span>` +
      `<span class="picker-meta">${escapeHtml(bits)}</span>`;
    row.addEventListener("click", () => startRun(c));
    list.appendChild(row);
  }
  $("picker").hidden = false;
  $("go").disabled = false;
}

// "COLLINS, MICHAEL A JR" -> "Michael A Jr Collins" (readable, without guessing)
function prettyName(fecName) {
  if (!fecName || !fecName.includes(",")) return fecName || "";
  const [last, rest] = fecName.split(/,(.+)/);
  const title = (s) => s.trim().toLowerCase().replace(/\b\w/g, (m) => m.toUpperCase());
  return `${title(rest || "")} ${title(last)}`.trim();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function startRun(cand) {
  $("picker").hidden = true;
  $("go").disabled = true;

  let job;
  try {
    const res = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: pendingName,
        anthropic_key: pendingKey,
        candidate_id: cand.candidate_id,
        candidate_name: cand.name,
        office: cand.office,
        state: cand.state,
      }),
    });
    job = await res.json();
    if (!res.ok) throw new Error(job.error || "search failed");
  } catch (err) {
    showError(err.message);
    $("go").disabled = false;
    return;
  } finally {
    pendingKey = "";   // consumed — never keep it around
  }

  showMode(job.demo);
  $("trace").hidden = false;
  poll(job.job_id);
}

function poll(jobId) {
  const tick = async () => {
    let state;
    try {
      const res = await fetch(`/status/${jobId}`);
      state = await res.json();
      if (!res.ok) throw new Error(state.error || "lost the job");
    } catch (err) {
      showError(err.message);
      $("go").disabled = false;
      return;
    }

    renderSteps(state.log);

    if (state.done) {
      $("go").disabled = false;
      renderAudit(state.log);
      if (state.error) {
        showError(state.error);
        if (state.rate_limited) revealKeyBlock();
      }
      if (state.result) { renderExportPanel(state.result); renderMoneyPanel(state.result); renderOutsidePanel(state.result); renderTrendPanel(state.result); renderTimelinePanel(state.result); renderVotesPanel(state.result); renderResult(state.result); }
      return; // stop polling
    }
    setTimeout(tick, POLL_MS);
  };
  tick();
}

// ── rendering ─────────────────────────────────────────────────────────────
function renderSteps(log) {
  const ol = $("steps");
  ol.innerHTML = "";
  for (const s of log.steps) {
    const li = document.createElement("li");
    li.className = `step ${s.status}`;
    const glyph = `<span class="glyph">${GLYPH[s.status] || "○"}</span>`;
    const label = `<span class="label">${escape(s.label)}</span>`;
    const elapsed = s.elapsed != null ? `<span class="elapsed">${s.elapsed}s</span>` : "";
    const detail = s.detail ? `<div class="detail">${escape(s.detail)}</div>` : "";
    li.innerHTML = `<div class="row">${glyph}${label}${elapsed}</div>${detail}`;
    ol.appendChild(li);
  }
}

function renderAudit(log) {
  const warns = (log.steps || []).filter((s) => s.status === "warn");
  if (!warns.length) return;
  const ul = $("audit-list");
  ul.innerHTML = "";
  for (const w of warns) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escape(w.label)}:</strong> ${escape(w.detail)}`;
    ul.appendChild(li);
  }
  $("audit").hidden = false;
}

function renderResult(result) {
  // Plain-language report (translation layer) renders above the raw JSON when
  // present; the JSON stays as the verifiable source of every line.
  const rep = $("report");
  if (rep) {
    if (result.report) {
      $("report-text").innerHTML = mdToHtml(result.report);
      const m = result.report_meta || {};
      $("report-meta").textContent =
        [m.cached ? "cached (data unchanged)" : (m.model || ""),
         m.readability_grade >= 0 ? `reading grade ${m.readability_grade}` : "",
         m.content_hash ? `data hash ${m.content_hash}` : ""]
          .filter(Boolean).join("  ·  ");
      rep.hidden = false;
    } else {
      rep.hidden = true;
    }
  }
  const raw = { ...result };
  delete raw.report;               // shown above; keep the JSON pane data-only
  $("result-json").textContent = JSON.stringify(raw, null, 2);
  $("result").hidden = false;
}

// ── data export (CSV per table + a Download-all ZIP) ────────────────────────
// Built entirely from `result` already in the browser (see static/export.js).
// The dataset never renders on-page — only counts + download buttons — so the
// 200-page raw dump stays a file, not clutter.
function triggerDownload(bytes, filename, mime) {
  const blob = new Blob([bytes], { type: mime || "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function renderExportPanel(result) {
  const panel = $("export-panel"), list = $("export-list");
  if (!panel || !list || !window.CTExport) return;
  const CT = window.CTExport;
  const tables = CT.buildTables(result);
  if (!tables.length) { panel.hidden = true; return; }

  const prefix = CT.filePrefix(result);
  list.innerHTML = "";
  for (const t of tables) {
    const n = t.rows.length;
    const row = document.createElement("div");
    row.className = "export-row";
    const label = document.createElement("span");
    label.className = "export-label";
    label.textContent = `${t.label} — ${n.toLocaleString()} row${n === 1 ? "" : "s"}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "export-dl";
    btn.textContent = "Download CSV";
    btn.addEventListener("click", () => {
      triggerDownload(CT.csvBytes(CT.toCsv(t.headers, t.rows)),
                      `${prefix}_${t.key}.csv`, "text/csv;charset=utf-8");
    });
    row.appendChild(label); row.appendChild(btn);
    list.appendChild(row);
  }

  const allBtn = $("export-all");
  if (allBtn) {
    allBtn.onclick = () => {
      const files = tables.map(t => ({
        name: `${prefix}_${t.key}.csv`,
        bytes: CT.csvBytes(CT.toCsv(t.headers, t.rows)),
      }));
      // include the full result JSON too, for anyone who wants the raw source
      files.push({ name: `${prefix}_full.json`,
                   bytes: CT.encodeUtf8(JSON.stringify(result, null, 2)) });
      triggerDownload(CT.buildZip(files), `${prefix}_campaign_tracker.zip`,
                      "application/zip");
    };
  }
  panel.hidden = false;
}

// ── helpers ─────────────────────────────────────────────────────────────────
function resetView() {
  for (const id of ["picker", "trace", "audit", "export-panel", "money-panel", "outside-panel", "trend-panel", "timeline-panel", "votes-panel", "report", "result", "error", "mode"]) $(id).hidden = true;
  $("steps").innerHTML = "";
  $("audit-list").innerHTML = "";
  $("error").textContent = "";
}
function showMode(demo) {
  const m = $("mode");
  // Only surface a banner in demo mode (dev, no shared key). In live mode the
  // shared key is used silently — users shouldn't have to think about keys.
  if (!demo) { m.hidden = true; return; }
  m.textContent = "demo mode — simulated data (no FEC key configured)";
  m.className = "mode demo";
  m.hidden = false;
}
function revealKeyBlock() {
  // The shared FEC data key is rate-limited. There's no user FEC field anymore
  // (that slot now holds the Anthropic key), so just surface an informational
  // note rather than revealing a field.
  const m = $("mode");
  m.textContent = "the shared FEC data key is rate-limited — please try again shortly";
  m.className = "mode demo";
  m.hidden = false;
}

// Minimal, safe markdown → HTML for the report. Escapes HTML first (so donor
// names/employers can't inject anything), then applies headers, bold, and
// bullet lists — the only markdown the synthesis prompt produces.
function mdToHtml(md) {
  const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  let html = "", inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const raw of String(md).split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (/^#{1,6}\s/.test(line)) {
      closeList();
      const level = line.match(/^#+/)[0].length;
      const tag = "h" + Math.min(level + 1, 6);   // "# " → h2, "## " → h3
      html += `<${tag}>${inline(line.replace(/^#+\s/, ""))}</${tag}>`;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inline(line)}</p>`;
    }
  }
  closeList();
  return html;
}
function showError(msg) {
  const e = $("error");
  e.textContent = msg;
  e.hidden = false;
}
function escape(str) {
  const d = document.createElement("div");
  d.textContent = str == null ? "" : String(str);
  return d.innerHTML;
}

// Measure rendered text width in SVG user units, using a cached canvas 2D
// context with the chart's font. This lets us ellipsize/lay out labels against
// a real pixel budget instead of a fragile character count (which was clipping
// long spender names off the left edge). Falls back to an estimate if canvas
// is unavailable. Font family mirrors the body font in app.css.
const _CHART_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
let _measureCtx = null;
function textWidth(text, sizePx) {
  try {
    if (!_measureCtx) _measureCtx = document.createElement("canvas").getContext("2d");
    _measureCtx.font = `${sizePx}px ${_CHART_FONT}`;
    return _measureCtx.measureText(String(text)).width;
  } catch {
    return String(text).length * sizePx * 0.58; // rough fallback if no canvas
  }
}
function ellipsizeToWidth(text, maxW, sizePx) {
  text = String(text);
  if (textWidth(text, sizePx) <= maxW) return text;
  let t = text;
  while (t.length > 1 && textWidth(t + "…", sizePx) > maxW) t = t.slice(0, -1);
  return t + "…";
}

// ── cross-cycle outside-spending trend ────────────────────────────────────────
// Paired bars per cycle — spent AGAINST (red) and FOR (green) — straight from
// track_a_composition, whose prior-cycle numbers come from FEC's own
// /schedules/schedule_e/totals/by_candidate/ aggregate and whose current cycle
// reuses the per-spender pull shown in the chart above. Same source, new axis:
// time instead of spender.
function renderTrendPanel(result) {
  const panel = $("trend-panel");
  if (!panel) return;
  const comp = result && result.track_a_composition;
  if (!Array.isArray(comp) || comp.length === 0) { panel.hidden = true; return; }
  const rows = [...comp]
    .sort((a, b) => (a.cycle || 0) - (b.cycle || 0))
    .map(c => ({
      cycle: c.cycle,
      oppose: Math.max(0, +c.outside_oppose || 0),
      support: Math.max(0, +c.outside_support || 0),
      incomplete: !!c.incomplete,
    }));
  if (!rows.some(r => r.oppose > 0 || r.support > 0)) { panel.hidden = true; return; }

  const maxVal = Math.max(...rows.map(r => Math.max(r.oppose, r.support)), 1);
  const W = 640, H = 300, pad = { l: 66, r: 16, t: 26, b: 42 };
  const plotH = H - pad.t - pad.b, gap = (W - pad.l - pad.r) / rows.length;
  const bw = Math.min(30, gap * 0.28);
  const yOf = v => pad.t + plotH * (1 - v / maxVal);
  const fmt = v => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
                        : v >= 1e3 ? Math.round(v / 1e3) + "K" : Math.round(v));
  const OP = "#c0483f", SP = "#4b9e5f";

  let grid = "";
  for (let t = 0; t <= 4; t++) {
    const v = maxVal * t / 4, yy = yOf(v);
    grid += `<line x1="${pad.l}" y1="${yy.toFixed(1)}" x2="${W - pad.r}" y2="${yy.toFixed(1)}" class="grid"/>`;
    grid += `<text x="${pad.l - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end" class="ax-y">${fmt(v)}</text>`;
  }

  let bars = "", labels = "";
  rows.forEach((r, i) => {
    const cx = pad.l + gap * i + gap / 2;
    for (const [v, color, dx, word] of [[r.oppose, OP, -bw - 1.5, "against"],
                                        [r.support, SP, 1.5, "for"]]) {
      if (v <= 0) continue;
      const h = Math.max(2, plotH * (v / maxVal));   // min 2px so small cycles show
      const y = pad.t + plotH - h;
      bars += `<rect x="${(cx + dx).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${color}" rx="1"><title>${r.cycle} — ${fmt(v)} spent ${word}${r.incomplete ? " (lower bound — see caveats)" : ""}</title></rect>`;
      bars += `<text x="${(cx + dx + bw / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" class="bar-cap">${fmt(v)}</text>`;
    }
    labels += `<text x="${cx.toFixed(1)}" y="${H - pad.b + 18}" text-anchor="middle" class="ax-x">${r.cycle}${r.incomplete ? " ⚠" : ""}</text>`;
  });

  $("trend-chart").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Outside spending for and against, by cycle">${grid}${bars}${labels}</svg>`;
  $("trend-legend").innerHTML =
    `<span><i class="c-red"></i>Spent against</span>` +
    `<span><i class="c-green"></i>Spent for</span>`;
  const anyIncomplete = rows.some(r => r.incomplete);
  $("trend-note").textContent =
    "Independent expenditures (Schedule E) per two-year cycle. Prior-cycle totals " +
    "come from FEC's own by-candidate aggregate; the current cycle matches the " +
    "per-spender chart above." +
    (anyIncomplete ? " ⚠ marks a cycle whose total is a disclosed lower bound." : "");
  panel.hidden = false;
}

// ── timeline swimlane (the side-by-side method as a picture) ──────────────────
// Three lanes — money / record / statements — over the SAME month bands
// timeline.py already built. A pure re-render of timeline.bands: no re-sorting,
// no re-grouping, no model. Record-lane markers distinguish bills (circle) from
// roll-call votes (diamond) using the event label timeline.py generated. Months
// with no events are compressed to a "⋯" gap marker (bands only exist where
// events do), so the axis never fakes continuous coverage. Every dot's tooltip
// is the event's own label + source; the full set is the timeline CSV.
function renderTimelinePanel(result) {
  const panel = $("timeline-panel");
  if (!panel) return;
  const tl = (result && result.timeline) || {};
  const bands = Array.isArray(tl.bands) ? [...tl.bands].reverse() : []; // oldest → newest
  if (!bands.length) { panel.hidden = true; return; }

  const LANES = [["money", "Money"], ["record", "Record"], ["statement", "Statements"]];
  const COLORS = { money: "#e0a83b", record: "#5b9dff", statement: "#8a6fd1" };
  const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const shortLabel = (ym) => {
    const [y, m] = String(ym).split("-");
    return (MONTHS[+m] || ym) + " " + y;
  };
  const esc = t => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // Columns: one per band, plus a narrow gap column between non-adjacent months.
  const BW = 58, GAPW = 16, GUTTER = 86, laneH = 46, padT = 8, padB = 26;
  const nextMonth = (ym) => {
    let [y, m] = ym.split("-").map(Number);
    m += 1; if (m > 12) { m = 1; y += 1; }
    return `${y}-${String(m).padStart(2, "0")}`;
  };
  const cols = [];
  bands.forEach((b, i) => {
    if (i > 0 && nextMonth(bands[i - 1].band) !== b.band) cols.push({ gap: true });
    cols.push({ band: b });
  });
  const W = GUTTER + cols.reduce((a, c) => a + (c.gap ? GAPW : BW), 0) + 10;
  const H = padT + LANES.length * laneH + padB;

  let svg = "";
  // lane separators + labels
  LANES.forEach(([key, label], li) => {
    const y0 = padT + li * laneH;
    svg += `<line x1="${GUTTER}" y1="${y0 + laneH}" x2="${W - 6}" y2="${y0 + laneH}" class="lane-line"/>`;
    svg += `<text x="${GUTTER - 10}" y="${y0 + laneH / 2 + 4}" text-anchor="end" class="lane-label" fill="${COLORS[key]}">${label}</text>`;
  });
  svg += `<line x1="${GUTTER}" y1="${padT}" x2="${GUTTER}" y2="${padT + LANES.length * laneH}" class="lane-line"/>`;

  // Dot layout inside a band-lane cell: grid of 4 per row, up to 2 rows (8);
  // beyond that the 8th slot becomes a "+N" count so nothing is hidden silently.
  const DOT_R = 4.5, PER_ROW = 4, MAX_DOTS = 8;
  let x = GUTTER;
  for (const col of cols) {
    if (col.gap) {
      svg += `<text x="${(x + GAPW / 2).toFixed(1)}" y="${padT + LANES.length * laneH / 2 + 4}" text-anchor="middle" class="band-gap">⋯<title>months with no recorded events (compressed)</title></text>`;
      x += GAPW;
      continue;
    }
    const b = col.band;
    svg += `<text x="${(x + BW / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle" class="band-label">${esc(shortLabel(b.band))}</text>`;
    LANES.forEach(([key], li) => {
      const evs = (b.events || []).filter(e => e.track === key);
      if (!evs.length) return;
      const y0 = padT + li * laneH;
      const overflow = evs.length > MAX_DOTS;
      const shown = overflow ? MAX_DOTS - 1 : Math.min(evs.length, MAX_DOTS);
      for (let k = 0; k < shown; k++) {
        const e = evs[k];
        const row = Math.floor(k / PER_ROW), colIdx = k % PER_ROW;
        const cx = x + 10 + colIdx * (DOT_R * 2 + 3);
        const cy = y0 + 14 + row * (DOT_R * 2 + 5);
        const isVote = key === "record" && String(e.label).startsWith("voted ");
        const tip = `<title>${esc(e.date)} — ${esc(e.label)}\n[${esc(e.source || "")}]</title>`;
        svg += isVote
          ? `<rect x="${(cx - DOT_R).toFixed(1)}" y="${(cy - DOT_R).toFixed(1)}" width="${DOT_R * 2}" height="${DOT_R * 2}" fill="${COLORS[key]}" transform="rotate(45 ${cx.toFixed(1)} ${cy.toFixed(1)})">${tip}</rect>`
          : `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${DOT_R}" fill="${COLORS[key]}">${tip}</circle>`;
      }
      if (overflow) {
        const row = Math.floor((MAX_DOTS - 1) / PER_ROW), colIdx = (MAX_DOTS - 1) % PER_ROW;
        const cx = x + 10 + colIdx * (DOT_R * 2 + 3);
        const cy = y0 + 14 + row * (DOT_R * 2 + 5);
        svg += `<text x="${(cx - DOT_R).toFixed(1)}" y="${(cy + 3).toFixed(1)}" class="more">+${evs.length - shown}<title>${evs.length} ${key} events in ${esc(shortLabel(b.band))} — full list in the timeline CSV</title></text>`;
      }
    });
    x += BW;
  }

  // Fixed pixel width + scrollable container: long histories scroll instead of
  // shrinking dots into invisibility, and no band is ever dropped.
  $("timeline-chart").innerHTML =
    `<svg width="${W}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Side-by-side timeline: money, record, and statements by month">${svg}</svg>`;
  $("timeline-legend").innerHTML =
    `<span><i class="c-gold"></i>Money (spenders + top donors)</span>` +
    `<span><i class="c-blue"></i>Record — bill</span>` +
    `<span><i class="diamond c-blue"></i>Record — roll-call vote</span>` +
    `<span><i class="c-purple"></i>Statement (own voice)</span>`;
  const extras = [];
  if (tl.year_only && tl.year_only.length) extras.push(`${tl.year_only.length} year-only statement(s) not placed (no exact date)`);
  if (tl.undated_count) extras.push(`${tl.undated_count} undated item(s) counted, not placed`);
  const ex = tl.statements_excluded || {};
  const exN = Object.values(ex).reduce((a, n) => a + n, 0);
  if (exN) extras.push(`${exN} coverage/boilerplate statement(s) held off the timeline`);
  $("timeline-note").textContent =
    `${(tl.event_count || 0).toLocaleString()} dated events in ${bands.length} month band(s), ` +
    "oldest to newest — the same deterministic timeline the report restates; hover any marker " +
    "for the event and its source. Placement is chronology, not causation." +
    (extras.length ? " " + extras.join("; ") + "." : "");
  panel.hidden = false;
  // Bands run oldest → newest, so a fresh scroller opens on the OLDEST months —
  // usually the least interesting end, and for a long record the current cycle
  // is off-screen entirely. Start at the newest end; the reader scrolls left
  // into history rather than right to find the present. This MUST come after
  // panel.hidden = false: a display:none element reports scrollWidth and
  // clientWidth of 0, so scrolling while hidden is a silent no-op (verified in
  // a real headless-browser run — scrollLeft stayed 0 with the earlier order).
  const scroller = $("timeline-chart").closest(".chart-scroll") || $("timeline-chart");
  if (scroller && scroller.scrollWidth > scroller.clientWidth) {
    scroller.scrollLeft = scroller.scrollWidth;
  }
}

// ── roll-call votes panel (Track B) ───────────────────────────────────────────
// A deterministic ledger of the money/finance-relevant floor votes with the
// member's own position, rendered straight from track_b_votes — same rule as
// the charts: built from the JSON, no model in the loop. Every row links to
// the primary source (House Clerk / Senate LIS), and the caps/coverage limits
// votes.py disclosed are restated in the footnote, never hidden.
function renderVotesPanel(result) {
  const panel = $("votes-panel");
  if (!panel) return;
  const tv = (result && result.track_b_votes) || {};
  const votes = Array.isArray(tv.money_related) ? tv.money_related : [];
  if (!votes.length) { panel.hidden = true; return; }

  const posClass = (p) => {
    const s = String(p || "").toLowerCase();
    if (s === "yea" || s === "aye" || s === "yes") return "yea";
    if (s === "nay" || s === "no") return "nay";
    return "oth"; // Present / Not Voting / anything else — shown as-is, neutral
  };

  const list = $("votes-list");
  list.innerHTML = "";
  for (const v of votes) {
    const row = document.createElement("div");
    row.className = "vote-row";
    const badge = `<span class="vote-pos ${posClass(v.position)}">${escapeHtml(v.position || "?")}</span>`;
    const date = `<span class="vote-date">${escapeHtml(v.date || "")}</span>`;
    const cite = v.url
      ? `<a class="vote-cite" href="${escapeHtml(v.url)}" target="_blank" rel="noopener">${escapeHtml(v.citation || "")}</a>`
      : `<span class="vote-cite">${escapeHtml(v.citation || "")}</span>`;
    const meta = [v.question, v.result].filter(Boolean).join(" · ");
    const role = v.candidate_bill_role
      ? `<span class="vote-role">their bill · ${escapeHtml(v.candidate_bill_role)}</span>` : "";
    row.innerHTML =
      `<div class="vote-head">${date}${badge}${cite}${role}</div>` +
      `<div class="vote-title">${escapeHtml(v.title || "")}</div>` +
      (meta ? `<div class="vote-meta">${escapeHtml(meta)}</div>` : "");
    list.appendChild(row);
  }

  const bits = [];
  bits.push(`${votes.length.toLocaleString()} money/finance-relevant roll call${votes.length === 1 ? "" : "s"}` +
            (tv.total_votes_scanned ? ` of ${tv.total_votes_scanned.toLocaleString()} scanned` : "") +
            (tv.chamber ? ` (${tv.chamber})` : "") + ".");
  if (tv.not_in_roll) bits.push(`${tv.not_in_roll} relevant vote${tv.not_in_roll === 1 ? "" : "s"} where the member was not listed on the roll call (e.g., before taking office).`);
  if (tv.coverage_note) bits.push(tv.coverage_note + ".");
  if (tv.incomplete && tv.incomplete_reason) bits.push("Caveat: " + tv.incomplete_reason + ".");
  $("votes-note").textContent = bits.join(" ");
  panel.hidden = false;
}

// ── money composition chart (inline SVG, no dependency) ──────────────────────
// Stacked bars per cycle: small-dollar (unitemized) + large-donor (itemized) +
// PAC. Bar height = those three summed; total receipts labeled above each bar.
// Renders straight from track_a_composition, so it shows with or without a report.
function renderMoneyPanel(result) {
  const panel = $("money-panel");
  if (!panel) return;
  const comp = result && result.track_a_composition;
  if (!Array.isArray(comp) || comp.length === 0) { panel.hidden = true; return; }

  const rows = [...comp]
    .sort((a, b) => (a.cycle || 0) - (b.cycle || 0))
    .map(c => ({
      cycle: c.cycle,
      small: Math.max(0, +c.individual_unitemized || 0),
      large: Math.max(0, +c.individual_itemized || 0),
      pac:   Math.max(0, +c.pac_contributions || 0),
      receipts: +c.receipts || 0,
    }));
  const maxStack = Math.max(...rows.map(r => r.small + r.large + r.pac), 1);

  const W = 640, H = 340, pad = { l: 66, r: 16, t: 30, b: 46 };
  const plotH = H - pad.t - pad.b, gap = (W - pad.l - pad.r) / rows.length;
  const bw = Math.min(72, gap * 0.55);
  const yOf = v => pad.t + plotH * (1 - v / maxStack);
  const fmt = v => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
                        : v >= 1e3 ? Math.round(v / 1e3) + "K" : Math.round(v));
  const colors = { small: "#4b9e5f", large: "#d98c2b", pac: "#8a6fd1" };

  let grid = "";
  for (let t = 0; t <= 4; t++) {
    const v = maxStack * t / 4, yy = yOf(v);
    grid += `<line x1="${pad.l}" y1="${yy.toFixed(1)}" x2="${W - pad.r}" y2="${yy.toFixed(1)}" class="grid"/>`;
    grid += `<text x="${pad.l - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end" class="ax-y">${fmt(v)}</text>`;
  }

  let bars = "", labels = "";
  rows.forEach((r, i) => {
    const cx = pad.l + gap * i + gap / 2, x = cx - bw / 2;
    let yb = pad.t + plotH;
    for (const [k, v] of [["small", r.small], ["large", r.large], ["pac", r.pac]]) {
      if (v <= 0) continue;
      const h = plotH * (v / maxStack);
      yb -= h;
      bars += `<rect x="${x.toFixed(1)}" y="${yb.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[k]}" rx="1"><title>${r.cycle} — ${k}: ${fmt(v)}</title></rect>`;
    }
    const topY = yOf(r.small + r.large + r.pac);
    labels += `<text x="${cx.toFixed(1)}" y="${(topY - 7).toFixed(1)}" text-anchor="middle" class="bar-total">${fmt(r.receipts)}</text>`;
    labels += `<text x="${cx.toFixed(1)}" y="${H - pad.b + 18}" text-anchor="middle" class="ax-x">${r.cycle}</text>`;
  });

  $("money-chart").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Funding composition by cycle">${grid}${bars}${labels}</svg>`;
  $("money-legend").innerHTML =
    `<span><i class="c-green"></i>Small-dollar (unitemized)</span>` +
    `<span><i class="c-orange"></i>Large-donor (itemized)</span>` +
    `<span><i class="c-purple"></i>PAC</span>`;
  panel.hidden = false;
}

// ── outside spending chart (Schedule E) — current cycle, per spender ──────────
// Divergent horizontal bars: money spent AGAINST (left, red) vs FOR (right,
// green), one row per group, biggest first. Untraceable ("dark") spenders are
// drawn hatched and tagged ⚠ — the funders couldn't be identified. Renders from
// track_a_outside (FEC data), independent of any report.
function renderOutsidePanel(result) {
  const panel = $("outside-panel");
  if (!panel) return;
  const os = (result && result.track_a_outside) || {};
  const spenders = Array.isArray(os.spenders) ? os.spenders : [];
  const rows = spenders
    .map(s => ({
      name: s.committee_name || s.committee_id || "(unknown)",
      oppose: Math.max(0, +s.oppose_total || 0),
      support: Math.max(0, +s.support_total || 0),
      dark: s.traceable === false,
    }))
    .filter(r => r.oppose > 0 || r.support > 0)
    .sort((a, b) => (b.oppose + b.support) - (a.oppose + a.support))
    .slice(0, 12);
  if (rows.length === 0) { panel.hidden = true; return; }

  const maxVal = Math.max(...rows.map(r => Math.max(r.oppose, r.support)), 1);
  const NAME_FS = 11.5, AMT_FS = 10.5;              // must match .sp-name / .sp-amt in app.css
  const W = 660, rowH = 26, gutter = 188, padT = 34, padR = 62, padB = 10;
  const plotW = W - gutter - padR, centerX = gutter + plotW / 2, halfW = plotW / 2;
  const H = padT + rows.length * rowH + padB;
  const fmt = v => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
                        : v >= 1e3 ? Math.round(v / 1e3) + "K" : Math.round(v));
  const esc = t => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const amtLabel = (v, dark) => fmt(v) + (dark ? " ⚠" : "");

  // Reserve a value lane so amount labels never collide with the name column.
  // (Old bug: the longest bar's "$627K" landed on top of "SLF PAC" → "SLF$6P2A7CK".)
  // Cap bar length at halfW minus the widest amount label, so every value label —
  // oppose (left of its bar tip) or support (right of its) — has clear space.
  let maxAmtW = 0;
  for (const r of rows) {
    if (r.oppose > 0)  maxAmtW = Math.max(maxAmtW, textWidth(amtLabel(r.oppose, r.dark), AMT_FS));
    if (r.support > 0) maxAmtW = Math.max(maxAmtW, textWidth(amtLabel(r.support, r.dark), AMT_FS));
  }
  const barMax = Math.max(40, halfW - (maxAmtW + 8));
  const wOf = v => v <= 0 ? 0 : Math.max(2, (v / maxVal) * barMax);   // min 2px so tiny bars show

  // Names right-align against the bars; ellipsize to the measured column width
  // so long names never spill off the left edge (old "HARDWORKING"→"RDWORKING",
  // "SENATE"→"NATE" clip). Full name still shows on hover via <title>.
  const nameMaxW = gutter - 14;

  const defs = `<defs>` +
    `<pattern id="hatchR" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">` +
    `<rect width="6" height="6" fill="#5c2420"/><line x1="0" y1="0" x2="0" y2="6" stroke="#c0483f" stroke-width="2.5"/></pattern>` +
    `<pattern id="hatchG" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">` +
    `<rect width="6" height="6" fill="#274b30"/><line x1="0" y1="0" x2="0" y2="6" stroke="#4b9e5f" stroke-width="2.5"/></pattern></defs>`;

  let svg = defs;
  svg += `<text x="${(centerX - 8).toFixed(1)}" y="20" text-anchor="end" class="dir dir-op">← spent AGAINST</text>`;
  svg += `<text x="${(centerX + 8).toFixed(1)}" y="20" text-anchor="start" class="dir dir-sp">spent FOR →</text>`;
  svg += `<line x1="${centerX.toFixed(1)}" y1="26" x2="${centerX.toFixed(1)}" y2="${H - padB}" class="axis-c"/>`;

  rows.forEach((r, i) => {
    const yc = padT + i * rowH + rowH / 2, barH = 15, by = yc - barH / 2;
    const nm = ellipsizeToWidth(r.name, nameMaxW, NAME_FS);
    const nameTitle = nm === r.name ? "" : `<title>${esc(r.name)}</title>`;
    svg += `<text x="${gutter - 8}" y="${(yc + 4).toFixed(1)}" text-anchor="end" class="sp-name${r.dark ? " dark" : ""}">${esc(nm)}${nameTitle}</text>`;
    if (r.oppose > 0) {
      const w = wOf(r.oppose);
      svg += `<rect x="${(centerX - w).toFixed(1)}" y="${by}" width="${w.toFixed(1)}" height="${barH}" fill="${r.dark ? "url(#hatchR)" : "#c0483f"}"${r.dark ? ' stroke="#c0483f" stroke-width="1"' : ""}><title>${esc(r.name)}: ${fmt(r.oppose)} against${r.dark ? " — UNTRACEABLE (dark money)" : ""}</title></rect>`;
      svg += `<text x="${(centerX - w - 5).toFixed(1)}" y="${(yc + 4).toFixed(1)}" text-anchor="end" class="sp-amt">${amtLabel(r.oppose, r.dark)}</text>`;
    }
    if (r.support > 0) {
      const w = wOf(r.support);
      svg += `<rect x="${centerX.toFixed(1)}" y="${by}" width="${w.toFixed(1)}" height="${barH}" fill="${r.dark ? "url(#hatchG)" : "#4b9e5f"}"${r.dark ? ' stroke="#4b9e5f" stroke-width="1"' : ""}><title>${esc(r.name)}: ${fmt(r.support)} for${r.dark ? " — UNTRACEABLE (dark money)" : ""}</title></rect>`;
      svg += `<text x="${(centerX + w + 5).toFixed(1)}" y="${(yc + 4).toFixed(1)}" text-anchor="start" class="sp-amt">${amtLabel(r.support, r.dark)}</text>`;
    }
  });

  const cyc = os.cycle || result.cycle || "";
  $("outside-title").textContent = `Outside spending by group${cyc ? " — " + cyc + " cycle" : ""}`;
  $("outside-chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Outside spending by group, against versus for">${svg}</svg>`;
  const darkRows = rows.filter(r => r.dark);
  const darkSum = darkRows.reduce((a, r) => a + r.oppose + r.support, 0);
  $("outside-note").innerHTML =
    `Totals this cycle: <strong>${fmt(os.oppose_total || 0)}</strong> against, ` +
    `<strong>${fmt(os.support_total || 0)}</strong> for.` +
    (darkRows.length
      ? ` ⚠ ${darkRows.length} spender${darkRows.length > 1 ? "s" : ""} (${fmt(darkSum)}) untraceable — funders not identifiable in FEC data ("dark money"), shown hatched.`
      : " All spenders traceable to identifiable funders.");
  panel.hidden = false;
}


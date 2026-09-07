/* Client-side data export for Campaign Tracker — CSV + a store-only ZIP.
 *
 * WHY THIS IS CLIENT-SIDE: /status already ships the entire `result` JSON to
 * the browser (it's what the charts and report render from), so exporting adds
 * NO backend route, NO server-side file storage, and NO job-state plumbing —
 * and it never touches the API-key path, which only ever lives in a request-
 * scoped variable server-side. Everything here runs on data already in memory.
 *
 * Everything is pure and DOM-free (hangs off window.CTExport / globalThis) so
 * the CSV encoder and the ZIP builder can be unit-tested in Node — see
 * tests/test_export_js.mjs, which builds a real archive and unzips it to verify
 * bytes, CRCs, and offsets. No dependencies.
 */
(function (root) {
  "use strict";

  // ── CSV (RFC 4180) ──────────────────────────────────────────────────────
  // Quote a field iff it contains a comma, quote, CR, or LF; double interior
  // quotes. Titles and statement excerpts routinely contain all four, so this
  // is the one part that actually has to be correct.
  //
  // CSV/DDE FORMULA-INJECTION GUARD (Security_Recommendations.md MEDIUM).
  // Statement titles/excerpts come from arbitrary indexed web pages
  // (statements.py) and committee/employer/occupation are FEC free-text
  // fields — none of that is sanitized upstream. A crafted value starting
  // with =, +, -, @, tab, or CR is interpreted as a formula by
  // Excel/LibreOffice/Sheets on open (e.g. `=HYPERLINK(...)`, a DDE
  // payload). Neutralize by prefixing a leading apostrophe, which every
  // major spreadsheet app treats as "force text" and hides on display.
  // NOT applied to a value that's already a plain number: this app exports
  // plenty of legitimate negative figures (refunds, net donor totals), and
  // forcing "-1500.00" to text would silently change it from a usable
  // number into a display-only string — a real regression, not a safe one.
  var _PLAIN_NUMBER_RE = /^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$/;
  var _FORMULA_LEAD_RE = /^[=+\-@\t\r]/;
  function csvCell(v) {
    if (v === null || v === undefined) return "";
    if (Array.isArray(v)) v = v.join("; ");
    if (typeof v === "boolean") v = v ? "true" : "false";
    let s = String(v);
    if (_FORMULA_LEAD_RE.test(s) && !_PLAIN_NUMBER_RE.test(s)) {
      s = "'" + s;
    }
    if (s.indexOf('"') !== -1 || s.indexOf(",") !== -1 ||
        s.indexOf("\n") !== -1 || s.indexOf("\r") !== -1) {
      s = '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }
  function toCsv(headers, rows) {
    const out = [headers.map(csvCell).join(",")];
    for (const row of rows) out.push(row.map(csvCell).join(","));
    return out.join("\r\n") + "\r\n";
  }

  // ── table builders: result JSON → [{key,label,headers,rows}] ────────────
  // Each table is a flat, one-row-per-entity view. Donor/spender rows are
  // per-entity summaries (that's all `result` carries — not transaction-level).
  function buildTables(result) {
    result = result || {};
    const tables = [];

    // Donors — prefer the full pulled list (~70) if present, else the
    // display-capped top_donors (~25).
    const direct = result.track_a_direct || {};
    const donors = (direct.pulled_donors && direct.pulled_donors.length)
      ? direct.pulled_donors
      : (direct.top_donors || []);
    if (donors.length) tables.push({
      key: "donors", label: "Donors (largest itemized)",
      headers: ["name", "total", "count", "first_date", "last_date",
                "employer", "occupation", "location", "bucket"],
      rows: donors.map(d => [d.name, d.total, d.count, d.first_date, d.last_date,
                             d.employer, d.occupation, d.location, d.bucket]),
    });

    // Donor transactions — one row per individual contribution (largest-first),
    // the detail behind each donor summary above.
    const donorTxns = (result.track_a_direct || {}).transactions || [];
    if (donorTxns.length) tables.push({
      key: "donor_transactions", label: "Donor transactions (per contribution)",
      headers: ["contributor_name", "amount", "date", "employer", "occupation",
                "city", "state", "bucket", "committee_id", "sub_id"],
      rows: donorTxns.map(t => [t.contributor_name, t.amount, t.date, t.employer,
                                t.occupation, t.city, t.state, t.bucket,
                                t.committee_id, t.sub_id]),
    });

    // Outside spenders (Schedule E, per committee).
    const spenders = (result.track_a_outside || {}).spenders || [];
    if (spenders.length) tables.push({
      key: "outside_spenders", label: "Outside spenders",
      headers: ["committee_id", "committee_name", "committee_type",
                "committee_type_full", "support_total", "oppose_total", "count",
                "first_date", "last_date", "traceable", "notice_only_total"],
      rows: spenders.map(s => [s.committee_id, s.committee_name, s.committee_type,
                               s.committee_type_full, s.support_total, s.oppose_total,
                               s.count, s.first_date, s.last_date, s.traceable,
                               s.notice_only_total]),
    });

    // Spender transactions — one row per deduped independent expenditure
    // (newest-first), the detail behind each spender summary above.
    const spenderTxns = (result.track_a_outside || {}).transactions || [];
    if (spenderTxns.length) tables.push({
      key: "spender_transactions", label: "Spender transactions (per expenditure)",
      headers: ["date", "committee_name", "committee_id", "support_oppose", "amount",
                "payee", "description", "is_notice", "notice_only", "filing_form", "sub_id"],
      rows: spenderTxns.map(t => [t.date, t.committee_name, t.committee_id,
                                  t.support_oppose, t.amount, t.payee, t.description,
                                  t.is_notice, t.notice_only, t.filing_form, t.sub_id]),
    });

    // Funding composition (per cycle).
    const comp = result.track_a_composition || [];
    if (comp.length && !comp[0]._demo) tables.push({
      key: "composition", label: "Funding composition by cycle",
      headers: ["cycle", "receipts", "individual_itemized", "individual_unitemized",
                "pac_contributions", "large_share", "small_share",
                "outside_support", "outside_oppose", "outside_traceable",
                "outside_dark", "outside_totals_source", "incomplete",
                "incomplete_reason"],
      rows: comp.map(c => [c.cycle, c.receipts, c.individual_itemized,
                           c.individual_unitemized, c.pac_contributions,
                           c.large_share, c.small_share, c.outside_support,
                           c.outside_oppose, c.outside_traceable, c.outside_dark,
                           c.outside_totals_source, c.incomplete, c.incomplete_reason]),
    });

    // Legislative record — ALL actions (they live under by_year[*].items, not
    // just the money-related subset), newest-first.
    const byYear = (result.track_b_record || {}).by_year || {};
    let actions = [];
    Object.keys(byYear).forEach(y => (byYear[y].items || []).forEach(a => actions.push(a)));
    actions.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
    if (actions.length) tables.push({
      key: "legislative", label: "Legislative record",
      headers: ["date", "year", "role", "citation", "title",
                "money_related", "money_terms", "latest_action"],
      rows: actions.map(a => [a.date, a.year, a.role, a.citation, a.title,
                              a.money_related, (a.money_terms || []).join("; "),
                              a.latest_action]),
    });

    // Roll-call votes — the money/finance-relevant floor votes with the
    // member's own position, each row carrying its citation + primary-source
    // URL (clerk.house.gov / senate.gov LIS). That's what result carries:
    // per-vote detail is only fetched for money-relevant votes (votes.py caps).
    const voteRows = (result.track_b_votes || {}).money_related || [];
    if (voteRows.length) tables.push({
      key: "votes", label: "Roll-call votes (money-relevant)",
      headers: ["date", "chamber", "citation", "position", "question", "title",
                "result", "legislation", "candidate_bill_role", "money_terms", "url"],
      rows: voteRows.map(v => [v.date, v.chamber, v.citation, v.position,
                               v.question, v.title, v.result, v.legislation,
                               v.candidate_bill_role,
                               (v.money_terms || []).join("; "), v.url]),
    });

    // Public statements.
    const stmts = (result.track_b_statements || {}).statements || [];
    if (stmts.length) tables.push({
      key: "statements", label: "Public statements",
      headers: ["date", "dated", "year_hint", "date_source", "classify",
                "classify_reason", "source", "url", "excerpt", "angle"],
      rows: stmts.map(s => [s.date, s.dated, s.year_hint, s.date_source, s.classify,
                            s.classify_reason, s.source, s.url, s.excerpt, s.angle]),
    });

    // Statement search audit log — every row the engine returned, before
    // dedup/filtering, with each row's disposition. Live search isn't
    // reproducible run to run; this table is what makes the statements stage
    // auditable after the fact (which rows were offered, kept, dropped, why).
    const searchLog = (result.track_b_statements || {}).search_log || [];
    if (searchLog.length) tables.push({
      key: "search_log", label: "Statement search log (audit)",
      headers: ["angle", "query", "rank", "url", "title", "excerpt",
                "published_date", "engine", "disposition", "statement_id", "detail"],
      rows: searchLog.map(e => [e.angle, e.query, e.rank, e.url, e.title,
                                e.excerpt, e.published_date, e.engine,
                                e.disposition, e.statement_id, e.detail]),
    });

    // Side-by-side timeline (the correlation-ready view), newest-first.
    const events = (result.timeline || {}).events || [];
    if (events.length) tables.push({
      key: "timeline", label: "Side-by-side timeline",
      headers: ["date", "track", "label", "source"],
      rows: events.map(e => [e.date, e.track, e.label, e.source]),
    });

    return tables;
  }

  function filePrefix(result) {
    result = result || {};
    const name = (result.candidate_name || result.candidate_id || "candidate");
    const cyc = (result.track_a_outside && result.track_a_outside.cycle) || "";
    let p = String(name).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (cyc) p += "_" + cyc;
    return p || "campaign_tracker";
  }

  // ── bytes helpers ───────────────────────────────────────────────────────
  const _enc = (typeof TextEncoder !== "undefined") ? new TextEncoder() : null;
  function encodeUtf8(s) {
    if (_enc) return _enc.encode(s);
    // Node < 11 fallback (tests run on modern Node, so TextEncoder exists)
    return Uint8Array.from(Buffer.from(s, "utf8"));
  }
  function concat(arrays) {
    let len = 0;
    for (const a of arrays) len += a.length;
    const out = new Uint8Array(len);
    let o = 0;
    for (const a of arrays) { out.set(a, o); o += a.length; }
    return out;
  }
  // Excel opens UTF-8 cleanly with a BOM; harmless for pandas/csv readers.
  const BOM = new Uint8Array([0xEF, 0xBB, 0xBF]);
  function csvBytes(csvString) { return concat([BOM, encodeUtf8(csvString)]); }

  // ── CRC-32 (IEEE, for ZIP) ──────────────────────────────────────────────
  const CRC = (function () {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[n] = c >>> 0;
    }
    return t;
  })();
  function crc32(bytes) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) c = CRC[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  // ── ZIP writer (store / method 0 — no compression, no dependency) ────────
  function _u16(n) { return new Uint8Array([n & 0xff, (n >>> 8) & 0xff]); }
  function _u32(n) { return new Uint8Array([n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff]); }
  function _dosDateTime(d) {
    d = d || new Date();
    const time = ((d.getHours() & 0x1f) << 11) | ((d.getMinutes() & 0x3f) << 5) | ((d.getSeconds() >> 1) & 0x1f);
    const yr = Math.max(0, d.getFullYear() - 1980);
    const date = ((yr & 0x7f) << 9) | (((d.getMonth() + 1) & 0x0f) << 5) | (d.getDate() & 0x1f);
    return { time: time & 0xffff, date: date & 0xffff };
  }

  // files: [{name: string, bytes: Uint8Array}]
  function buildZip(files, when) {
    const { time, date } = _dosDateTime(when);
    const chunks = [];
    const central = [];
    let offset = 0;
    const push = (a) => { chunks.push(a); offset += a.length; };

    for (const f of files) {
      const nameBytes = encodeUtf8(f.name);
      const data = f.bytes;
      const crc = crc32(data);
      const localOffset = offset;

      push(_u32(0x04034b50));           // local file header signature
      push(_u16(20));                   // version needed to extract
      push(_u16(0));                    // general purpose flags
      push(_u16(0));                    // method 0 = store
      push(_u16(time)); push(_u16(date));
      push(_u32(crc));
      push(_u32(data.length));          // compressed size
      push(_u32(data.length));          // uncompressed size
      push(_u16(nameBytes.length));
      push(_u16(0));                    // extra field length
      push(nameBytes);
      push(data);

      central.push(concat([
        _u32(0x02014b50),               // central dir signature
        _u16(20), _u16(20),             // version made by / needed
        _u16(0), _u16(0),               // flags / method
        _u16(time), _u16(date),
        _u32(crc),
        _u32(data.length), _u32(data.length),
        _u16(nameBytes.length),
        _u16(0), _u16(0),               // extra / comment length
        _u16(0), _u16(0),               // disk# / internal attrs
        _u32(0),                        // external attrs
        _u32(localOffset),
        nameBytes,
      ]));
    }

    const centralStart = offset;
    let centralSize = 0;
    for (const rec of central) { push(rec); centralSize += rec.length; }

    push(concat([
      _u32(0x06054b50),                 // end of central directory
      _u16(0), _u16(0),                 // disk numbers
      _u16(files.length), _u16(files.length),
      _u32(centralSize),
      _u32(centralStart),
      _u16(0),                          // comment length
    ]));

    return concat(chunks);
  }

  root.CTExport = {
    csvCell, toCsv, buildTables, filePrefix,
    encodeUtf8, concat, csvBytes, crc32, buildZip, _dosDateTime,
  };
})(typeof window !== "undefined" ? window : globalThis);

/* Node test for static/export.js — the client-side CSV + ZIP export.
 * Builds tables from a representative `result`, asserts CSV quoting, then
 * writes a real ZIP to /tmp for the Python half (test_export_zip.py) to unzip
 * and byte-verify. Run via tests/run_export_tests.sh (node then python).
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
// export.js hangs its API off globalThis when there's no window.
const src = readFileSync(join(here, "..", "static", "export.js"), "utf8");
(0, eval)(src);
const CT = globalThis.CTExport;

let failures = 0;
const check = (name, cond, detail = "") => {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
  if (!cond) failures++;
};

// ── CSV quoting edge cases ────────────────────────────────────────────────
check("plain cell unquoted", CT.csvCell("hello") === "hello");
check("comma triggers quoting", CT.csvCell("a,b") === '"a,b"');
check("interior quotes doubled", CT.csvCell('she said "hi"') === '"she said ""hi"""');
check("newline triggers quoting", CT.csvCell("line1\nline2") === '"line1\nline2"');
check("null -> empty", CT.csvCell(null) === "" && CT.csvCell(undefined) === "");
check("array joined with semicolons", CT.csvCell(["pac", "corporate pac"]) === "pac; corporate pac");
check("boolean -> true/false", CT.csvCell(true) === "true" && CT.csvCell(false) === "false");

const csv = CT.toCsv(["a", "b"], [["1", "x,y"], ["2", 'q"q']]);
check("toCsv row assembly + CRLF",
  csv === 'a,b\r\n1,"x,y"\r\n2,"q""q"\r\n', JSON.stringify(csv));

// ── representative result (mirrors the real JSON shapes) ──────────────────
const result = {
  candidate_name: "OSSOFF, T. JONATHAN",
  candidate_id: "S8GA00180",
  track_a_direct: {
    donor_count: 70,
    top_donors: [{ name: "Capped Guy", total: 1, count: 1 }],
    pulled_donors: [
      { name: "Tim Disney", total: 14000, count: 3, first_date: "2025-08-25",
        last_date: "2026-04-17", employer: "Self-employed", occupation: "Writer",
        location: "Encino, CA", bucket: "large" },
      { name: 'Comma, Name "Q"', total: 7000, count: 2, employer: "A, B & C",
        occupation: "Exec\nDir", location: "SF, CA", bucket: "large" },
    ],
    transactions: [
      { contributor_name: "Tim Disney", amount: 6600, date: "2026-04-17",
        employer: "Self-employed", occupation: "Writer", city: "Encino", state: "CA",
        bucket: "individual", committee_id: "C00696948", sub_id: "AX1" },
      { contributor_name: 'Comma, Name "Q"', amount: 3500, date: "2025-09-05",
        employer: "A, B & C", occupation: "Exec\nDir", city: "SF", state: "CA",
        bucket: "individual", committee_id: "C00696948", sub_id: "AX2" },
    ],
    transaction_count: 2, transactions_capped: false,
  },
  track_a_outside: {
    cycle: 2026,
    transactions: [
      { committee_id: "C00571703", committee_name: "SLF PAC", support_oppose: "O",
        amount: 200000, date: "2026-02-02", payee: "Ad Buy LLC",
        description: "media, opposing", is_notice: false, notice_only: false,
        filing_form: "F3X", sub_id: "EX1" },
      { committee_id: "C00835041", committee_name: "FORWARD BLUE", support_oppose: "S",
        amount: 6000, date: "2026-07-11", payee: "Field Ops",
        description: "canvassing, supporting", is_notice: true, notice_only: true,
        filing_form: "F24", sub_id: "EX2" },
    ],
    spenders: [
      { committee_id: "C00571703", committee_name: "SLF PAC", committee_type: "O",
        support_total: 0, oppose_total: 313599.98, count: 5, traceable: true,
        notice_only_total: 0 },
      { committee_id: "C00835041", committee_name: "FORWARD BLUE", support_total: 28800,
        oppose_total: 0, count: 3, traceable: true, notice_only_total: 28800 },
    ],
  },
  track_a_composition: [
    { cycle: 2020, receipts: 156146537.53, outside_support: 18216333.17,
      outside_oppose: 136098062.14, outside_traceable: null, outside_dark: null,
      outside_totals_source: "fec_aggregate", incomplete: false },
    { cycle: 2026, receipts: 60439612.91, outside_support: 64684.32,
      outside_oppose: 480063.37, outside_traceable: 526808.95, outside_dark: 17938.74,
      outside_totals_source: "spenders", incomplete: false },
  ],
  track_b_record: {
    total_actions: 3,
    by_year: {
      "2022": { items: [
        { date: "2022-01-12", year: 2022, role: "sponsored", citation: "S 3494",
          title: "Ban Congressional Stock Trading Act", money_related: true,
          money_terms: ["stock trading act"], latest_action: "Referred to committee" },
      ] },
      "2026": { items: [
        { date: "2026-03-04", year: 2026, role: "cosponsored", citation: "S 3991",
          title: "DISCLOSE Act of 2026", money_related: true,
          money_terms: ["disclose act"], latest_action: "Read twice, referred" },
        { date: "2026-02-01", year: 2026, role: "cosponsored", citation: "S 100",
          title: "Some, Unrelated \"Bill\"", money_related: false, money_terms: [],
          latest_action: "Referred" },
      ] },
    },
  },
  track_b_votes: {
    chamber: "Senate", member: "Ossoff (GA)",
    total_votes_scanned: 1240, money_related_seen: 2, not_in_roll: 0,
    money_related: [
      { date: "2025-12-04", year: 2025, chamber: "Senate", congress: 119,
        session: 1, number: 612,
        citation: "Senate Roll Call 612 (119th Congress, Session 1)",
        question: "On the Cloture Motion",
        title: 'Motion to Invoke Cloture: DISCLOSE Act, the "sunlight" bill',
        result: "Cloture Motion Rejected", position: "Yea", legislation: "S. 512",
        candidate_bill_role: "cosponsored",
        money_related: true, money_terms: ["disclose act"],
        url: "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00612.htm" },
      { date: "2025-06-11", year: 2025, chamber: "Senate", congress: 119,
        session: 1, number: 300,
        citation: "Senate Roll Call 300 (119th Congress, Session 1)",
        question: "On Passage of the Bill", title: "Ban Corporate PACs Act",
        result: "Bill Passed", position: "Nay", legislation: "S. 2113",
        candidate_bill_role: "",
        money_related: true, money_terms: ["corporate pac"],
        url: "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00300.htm" },
    ],
    incomplete: false, incomplete_reason: "",
    coverage_note: "Senate LIS roll calls, 119th Congress scanned",
  },
  track_b_statements: {
    statements: [
      { date: "2026-01-22", dated: true, classify: "by_candidate",
        source: "ossoff.senate.gov", url: "https://ossoff.senate.gov/x",
        excerpt: "Recognized for anti-corruption; got an \"A+\".", angle: "money" },
    ],
    search_log: [
      { angle: "money", query: '"Jon Ossoff" money', rank: 1,
        url: "https://ossoff.senate.gov/x", title: "Release",
        excerpt: "Recognized for anti-corruption", published_date: "2026-01-22",
        engine: "duckduckgo", disposition: "kept", statement_id: "s1", detail: "" },
      { angle: "money", query: '"Jon Ossoff" money', rank: 2,
        url: "https://dupe.example/x", title: "Dupe, with \"quotes\"",
        excerpt: "Recognized for anti-corruption", published_date: "",
        engine: "brave", disposition: "dropped_near_duplicate_text",
        statement_id: "", detail: "" },
    ],
  },
  timeline: {
    events: [
      { date: "2026-02-02", track: "money", label: "SLF PAC (C00571703): oppose", source: "FEC Schedule E" },
      { date: "2026-03-04", track: "record", label: "cosponsored S 3991", source: "Congress.gov" },
    ],
  },
};

const tables = CT.buildTables(result);
const byKey = Object.fromEntries(tables.map(t => [t.key, t]));

check("all ten tables built",
  ["donors", "donor_transactions", "outside_spenders", "spender_transactions",
   "composition", "legislative", "votes", "statements", "search_log", "timeline"]
    .every(k => k in byKey), Object.keys(byKey).join(","));
check("search log: every row with its disposition (kept AND dropped)",
  byKey.search_log.rows.length === 2 &&
  byKey.search_log.rows[0][8] === "kept" && byKey.search_log.rows[0][9] === "s1" &&
  byKey.search_log.rows[1][8] === "dropped_near_duplicate_text");
check("donors uses pulled_donors (2 rows), not capped top_donors (1)",
  byKey.donors.rows.length === 2);
check("donor_transactions present (2 rows, per contribution)",
  byKey.donor_transactions.rows.length === 2);
check("spender_transactions present (2 rows, per expenditure)",
  byKey.spender_transactions.rows.length === 2);
check("spender_transactions carries notice_only flag",
  byKey.spender_transactions.rows.some(r => r[8] === true));
check("legislative includes ALL actions, not just money-related (3 rows)",
  byKey.legislative.rows.length === 3);
check("legislative sorted newest-first",
  byKey.legislative.rows[0][0] === "2026-03-04" && byKey.legislative.rows[2][0] === "2022-01-12");
check("votes table: one row per money-relevant vote with position + citation",
  byKey.votes.rows.length === 2 &&
  byKey.votes.rows[0][3] === "Yea" && byKey.votes.rows[1][3] === "Nay" &&
  byKey.votes.rows[0][2] === "Senate Roll Call 612 (119th Congress, Session 1)");
check("votes table: primary-source URL and joined money_terms in row",
  byKey.votes.rows[0][10].endsWith("vote_119_1_00612.htm") &&
  byKey.votes.rows[1][9] === "corporate pac");
check("votes table: candidate_bill_role column (their-own-bill join)",
  byKey.votes.headers[8] === "candidate_bill_role" &&
  byKey.votes.rows[0][8] === "cosponsored" && byKey.votes.rows[1][8] === "");
check("composition null traceable/dark survives to row",
  byKey.composition.rows[0][9] === null && byKey.composition.rows[0][10] === null);
check("file prefix from name + cycle", CT.filePrefix(result) === "ossoff_t_jonathan_2026");

// ── demo runs must stay identifiable after export ─────────────────────────
// A demo export used to be indistinguishable from a real one once the files
// were on disk: no run-level flag anywhere in the JSON, and the composition
// table was silently DROPPED when its rows carried "_demo" (a missing table,
// not a disclosed one). Now the run flag rides in result.demo, the filename
// says so, and the table exports like any other.
const demoResult = { ...result, demo: true };
check("demo run gets a demo_ filename prefix",
  CT.filePrefix(demoResult) === "demo_ossoff_t_jonathan_2026");
check("a real run's prefix is unchanged (no stray prefix)",
  CT.filePrefix(result) === "ossoff_t_jonathan_2026");
const demoTables = CT.buildTables({
  demo: true,
  track_a_composition: [{ cycle: 2026, receipts: 10e6, individual_itemized: 7.4e6,
                          _demo: true }],
});
check("demo composition rows are exported, not silently dropped",
  demoTables.some(t => t.key === "composition") &&
  demoTables.find(t => t.key === "composition").rows.length === 1);

// ── build the ZIP and write everything out for the Python verifier ────────
const files = tables.map(t => ({
  name: `${CT.filePrefix(result)}_${t.key}.csv`,
  bytes: CT.csvBytes(CT.toCsv(t.headers, t.rows)),
}));
const zipBytes = CT.buildZip(files, new Date(Date.UTC(2026, 6, 13, 12, 0, 0)));
writeFileSync("/tmp/export_test.zip", Buffer.from(zipBytes));

// also dump expected per-file text (BOM-stripped) for content comparison
const expected = {};
for (const f of files) {
  expected[f.name] = Buffer.from(f.bytes.slice(3)).toString("utf8"); // strip BOM
}
writeFileSync("/tmp/export_expected.json", JSON.stringify(expected));
console.log(`\nwrote /tmp/export_test.zip (${zipBytes.length} bytes, ${files.length} entries)`);

if (failures) { console.log(`\n${failures} FAILURE(S)`); process.exit(1); }
console.log("\nnode-side export checks passed");

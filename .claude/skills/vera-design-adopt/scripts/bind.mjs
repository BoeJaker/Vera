import fs from "node:fs"; import path from "node:path";
const s = fs.readFileSync(path.join(process.argv[2], "Harness.dc.html"), "utf8");
const tpl = s.slice(s.indexOf("<x-dc>"), s.indexOf("</x-dc>"));
// every {{hole}} used in the template must exist in renderVals output
const js = s.match(/<script data-dc-script[^>]*>([\s\S]*?)<\/script>/)[1];
class Base { constructor(p){ this.props = p||{}; this.state = {}; } setState(o){ Object.assign(this.state, o); } }
const C = new Function("DCLogic", '"use strict";' + js + "; return Component;")(Base);
const v = new C({ style:"standard", theme:"dusk" }).renderVals();
const holes = new Set([...tpl.matchAll(/\{\{\s*([A-Za-z_$][\w$]*)/g)].map(m => m[1]));
const loopVars = new Set([...tpl.matchAll(/as="([^"]+)"/g)].map(m => m[1]));
const missing = [...holes].filter(h => !(h in v) && !loopVars.has(h) && h !== "true" && h !== "false");
console.log(missing.length ? "missing bindings: " + missing.join(", ") : "all top-level holes bound");
// how many sc-for lists are actually arrays
for (const m of tpl.matchAll(/<sc-for list="\{\{(\w+)\}\}"/g)) {
  const val = v[m[1]];
  if (!Array.isArray(val)) console.log("sc-for list not an array: " + m[1] + " = " + typeof val);
}

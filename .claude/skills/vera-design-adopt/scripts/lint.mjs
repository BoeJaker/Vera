import fs from "node:fs";
import path from "node:path";
const dir = process.argv[2];
const files = fs.readdirSync(dir).filter((f) => f.endsWith(".dc.html"));
let bad = 0;
for (const f of files) {
  const s = fs.readFileSync(path.join(dir, f), "utf8");
  const m = s.match(/<script data-dc-script[^>]*>([\s\S]*?)<\/script>/);
  if (!m) { console.log(f + "  NO SCRIPT"); bad++; continue; }
  try {
    new Function("DCLogic", '"use strict";' + m[1] + "; return Component;")(class { constructor(){} });
  } catch (e) { console.log(f + "  SYNTAX: " + e.message); bad++; continue; }
  const sf=(s.match(/<sc-for/g)||[]).length, sfc=(s.match(/<\/sc-for>/g)||[]).length;
  const si=(s.match(/<sc-if/g)||[]).length, sic=(s.match(/<\/sc-if>/g)||[]).length;
  if (sf!==sfc){console.log(f+"  sc-for "+sf+"/"+sfc);bad++;}
  if (si!==sic){console.log(f+"  sc-if "+si+"/"+sic);bad++;}
  // props must be valid JSON after entity decoding
  const dp = s.match(/data-props='([^']*)'/);
  if (dp) { try { JSON.parse(dp[1].replace(/&amp;/g,"&").replace(/&#39;/g,"'")); }
            catch(e){ console.log(f+"  data-props: "+e.message); bad++; } }
  else { console.log(f+"  no data-props"); bad++; }
}
console.log(bad===0 ? "ALL BOARDS CLEAN" : bad+" problem(s)");

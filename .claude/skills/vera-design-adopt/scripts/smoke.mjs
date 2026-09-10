import fs from "node:fs";
import path from "node:path";
const dir = process.argv[2];
const files = fs.readdirSync(dir).filter((f) => f.endsWith(".dc.html"));
let bad = 0;
class Base { constructor(p){ this.props = p || {}; this.state = {}; }
  setState(o){ Object.assign(this.state, typeof o === "function" ? o(this.state) : o); } }
for (const f of files) {
  const s = fs.readFileSync(path.join(dir, f), "utf8");
  const js = s.match(/<script data-dc-script[^>]*>([\s\S]*?)<\/script>/)[1];
  const props = JSON.parse(s.match(/data-props='([^']*)'/)[1].replace(/&amp;/g,"&").replace(/&#39;/g,"'"));
  const defaults = {};
  for (const k of Object.keys(props)) if (props[k] && props[k].default !== undefined) defaults[k] = props[k].default;
  try {
    const C = new Function("DCLogic", '"use strict";' + js + "; return Component;")(Base);
    const inst = new C(defaults);
    const v = inst.renderVals();
    // every handler the template can fire must survive being called
    let fired = 0;
    const walk = (o, d) => { if (d > 3 || !o) return;
      for (const k of Object.keys(o)) { const x = o[k];
        if (typeof x === "function") { try { x(); fired++; } catch(e){ throw new Error("handler "+k+": "+e.message); } }
        else if (x && typeof x === "object") walk(x, d + 1); } };
    walk(v, 0);
    inst.renderVals();
    console.log(f.padEnd(22) + "ok · " + Object.keys(v).length + " values, " + fired + " handlers fired");
  } catch (e) { console.log(f.padEnd(22) + "RUNTIME: " + e.message); bad++; }
}
console.log(bad===0 ? "ALL BOARDS RENDER" : bad+" broken");

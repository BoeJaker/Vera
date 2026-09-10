import fs from "node:fs"; import path from "node:path";
const VOID = new Set(["br","hr","img","input","meta","link","source"]);
function rootCount(body){
  let depth = 0, roots = 0;
  const re = /<(\/?)([a-zA-Z][\w-]*)([^>]*?)(\/?)>/g;
  let m;
  while ((m = re.exec(body))){
    const closing = m[1] === "/", tag = m[2].toLowerCase(), self = m[4] === "/";
    if (closing){ depth = Math.max(0, depth - 1); continue; }
    if (depth === 0) roots++;
    if (!self && !VOID.has(tag)) depth++;
  }
  return roots;
}
const dir = process.argv[2];
let bad = 0;
for (const f of fs.readdirSync(dir).filter(x => x.endsWith(".dc.html"))){
  const s = fs.readFileSync(path.join(dir, f), "utf8");
  const tpl = s.slice(s.indexOf("<x-dc>"), s.indexOf("</x-dc>"));
  for (const g of tpl.matchAll(/<svg[\s\S]*?<\/svg>/g))
    if (/<sc-(for|if)/.test(g[0])){ console.log(f + "  sc-* inside <svg> — will not render"); bad++; }
  for (const m of tpl.matchAll(/<sc-for\b[^>]*>([\s\S]*?)<\/sc-for>/g)){
    const n = rootCount(m[1]);
    if (n > 1){ console.log(f + "  sc-for has " + n + " roots: " + m[1].trim().slice(0,50).replace(/\s+/g," ")); bad++; }
  }
  for (const m of tpl.matchAll(/<sc-if\b[^>]*>([\s\S]*?)<\/sc-if>/g)){
    const n = rootCount(m[1]);
    if (n > 1){ console.log(f + "  sc-if has " + n + " roots: " + m[1].trim().slice(0,50).replace(/\s+/g," ")); bad++; }
  }
}
console.log(bad === 0 ? "template shape ok" : bad + " structural problem(s)");

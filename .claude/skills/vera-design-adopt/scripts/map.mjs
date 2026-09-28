// boards → surfaces. Reads a design working dir (canvas.json + *.dc.html) and the Vera repo,
// writes adopt-map.json: per board — title, subtitle, workstream (from Notes/40), the prod
// files whose panel/route names the board mentions, and the design vocabulary it uses.
//   node map.mjs <design dir> <vera repo> [out.json]
import fs from 'node:fs';
import path from 'node:path';
const [D, R, OUT = path.join(process.argv[2], 'adopt-map.json')] = process.argv.slice(2);
const canvas = JSON.parse(fs.readFileSync(path.join(D, 'canvas.json'), 'utf8'));
const note40 = fs.existsSync(path.join(R, 'Notes', '40-implementation-scope.md')) ? fs.readFileSync(path.join(R, 'Notes', '40-implementation-scope.md'), 'utf8') : '';
// workstreams: "## W<n> · <title>" headings in Note 40, with their bodies for keyword matching
const WS = [];
for (const m of note40.matchAll(/^##\s*(W\d+)[^\n]*·\s*([^\n]+)\n([\s\S]*?)(?=^##\s|\Z)/gm)) WS.push({ id:m[1], title:m[2].trim(), body:m[3].toLowerCase() });
const VOCAB = ['data-den', 'data-blocks', 'data-style', 'panel.open', 'panel.dispatch', 'canvas.show', 'canvas.add', 'canvas.pin', 'canvas.park', 'canvas.size', 'canvas.ask', 'lhm.focus', 'context.bundle', 'ISO.proj', 'ISO.frame', 'ISO.route', 'dc-import', 'envelope', 'VeraDash', 'vera_graph'];
// prod panels: register_ui(...) names and *_panel.html files
const panels = [];
const walk = (dir, depth) => { if (depth > 5) return; for (const e of fs.readdirSync(dir, { withFileTypes:true })){
  if (e.name.startsWith('.') || e.name === 'node_modules' || e.name === '__pycache__') continue;
  const p = path.join(dir, e.name);
  if (e.isDirectory()) walk(p, depth + 1);
  else if (/_panel\.html$|\.js$|\.py$/.test(e.name) && fs.statSync(p).size < 3e6){
    const t = fs.readFileSync(p, 'utf8');
    if (e.name.endsWith('_panel.html')) panels.push({ file:path.relative(R, p), key:e.name.replace('_panel.html', '') });
    for (const m of t.matchAll(/register_ui\(\s*["']([^"']+)["']/g)) panels.push({ file:path.relative(R, p), key:m[1] });
  } } };
try { walk(path.join(R, 'vera'), 0); } catch (e) { console.error('repo walk: ' + e.message); }
const out = [];
for (const a of canvas.artboards){
  const f = path.join(D, a.file); if (!fs.existsSync(f)) continue;
  const t = fs.readFileSync(f, 'utf8');
  const title = (t.match(/<h1[^>]*>([^<]+)</) || [])[1] || a.title || a.file;
  const sub = (t.match(/class="sub"[^>]*>([^<]{0,400})/) || [])[1] || '';
  const low = (title + ' ' + sub).toLowerCase();
  const words = low.split(/[^a-z0-9]+/).filter((w) => w.length > 4);
  const ws = WS.map((w) => ({ id:w.id, title:w.title, hits:words.filter((x) => w.body.includes(x)).length })).sort((x, y) => y.hits - x.hits).slice(0, 2).filter((w) => w.hits > 0);
  const files = panels.filter((p) => low.includes(p.key.toLowerCase().replace(/[_-]/g, ' ')) || low.includes(p.key.toLowerCase())).map((p) => p.file);
  out.push({ board:a.file, title, subtitle:sub.slice(0, 200), size:[a.w, a.h], workstreams:ws, prodFiles:[...new Set(files)], vocabulary:VOCAB.filter((v) => t.includes(v)) });
}
fs.writeFileSync(OUT, JSON.stringify({ generated:'by vera-design-adopt/map.mjs', boards:out, panelsSeen:panels.length }, null, 2));
console.log(out.length + ' boards mapped · ' + panels.length + ' prod panels seen → ' + OUT);

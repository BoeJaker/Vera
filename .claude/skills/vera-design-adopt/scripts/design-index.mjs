// The DESIGN INDEX — a canvas as data. Reads a design working dir (canvas.json, the canvas-*.json set layouts,
// every *.dc.html) and writes design-index.json: per board — its set(s), title, size, kind (board · shell ·
// storyboard · qc), the parts it declares (data-w="label · tag" widgets, WREC widget records, the adoption rows),
// its regions (#ids) and CSS classes, headings and control labels, the holes its logic fills, the props it
// accepts, the boards it embeds, the directive vocabulary it shows (ui.* panel.* canvas.* widget.* …), the
// catalogued capabilities it names (given estate.json), and the demo states (sc-if names).
// The mapper (adopt-map.mjs) matches THIS against the estate index, so a fresh canvas never starts from memory.
//   node design-index.mjs <design dir> [estate.json] [out.json]
import fs from 'node:fs';
import path from 'node:path';
const [D, EST, OUT = path.join(process.argv[2] || '.', 'design-index.json')] = process.argv.slice(2);
if (!D) { console.error('usage: node design-index.mjs <design dir> [estate.json] [out.json]'); process.exit(2); }
const read = (p) => fs.readFileSync(p, 'utf8');
const strip = (s) => s.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
const uniq = (a) => [...new Set(a)];
const catalog = new Set(EST && fs.existsSync(EST) ? (JSON.parse(read(EST)).catalog || []) : []);

// sets: canvas.json is the whole canvas; canvas-<set>.json are the artifact sets a board is published in
const canvas = JSON.parse(read(path.join(D, 'canvas.json')));
const sets = {};
for (const f of fs.readdirSync(D).filter((x) => /^canvas-[\w-]+\.json$/.test(x))){
  const name = f.replace(/^canvas-|\.json$/g, ''); let j; try { j = JSON.parse(read(path.join(D, f))); } catch { continue; }
  for (const a of j.artboards || []) (sets[a.file] = sets[a.file] || []).push(name);
}
const notes = (canvas.annotations || []).map((a) => String(a.text || '').slice(0, 300));

// the directive / dispatch vocabulary a design shows: dotted names under the UI's own prefixes
const DIR = /\b((?:ui|panel|canvas|widget|chat|lhm|iso|context|loop|board|registry|content|memory|session|sandbox|evolve|obs|dash|graph|style|theme|settings|layout|policy|script|room|voice|council|diffuse|paste|reply|tab)\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3})\b/g;
const CAP = /\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3})\b/g;

const boards = [];
for (const a of canvas.artboards || []){
  const f = path.join(D, a.file); if (!fs.existsSync(f)) continue;
  const t = read(f);
  const title = strip((t.match(/<h1[^>]*>([\s\S]*?)<\/h1>/) || [])[1] || '') || a.title || a.file.replace('.dc.html', '');
  const sub = strip((t.match(/class="sub"[^>]*>([\s\S]{0,600}?)<\/(?:p|div)>/) || [])[1] || '').slice(0, 300);
  const embeds = [...t.matchAll(/<dc-import\s+name="([^"]+)"([^>]*)>/g)].map((m) => ({ name:m[1], props:Object.fromEntries([...m[2].matchAll(/([\w-]+)="([^"]*)"/g)].map((x) => [x[1], x[2]])) }));
  const holes = uniq([...t.matchAll(/\{\{\s*([a-zA-Z_][\w.]*)/g)].map((m) => m[1]));
  const kind = /arrival|storyboard|motion/i.test(title + a.file) ? 'storyboard' : /^QC\b|quality/i.test(title + a.file) ? 'qc'
    : embeds.length && holes.length < 40 ? 'shell' : 'board';
  // parts: every element the design declares as a widget, plus the widget records and the adoption rows
  const parts = [];
  for (const m of t.matchAll(/data-w="([^"{}]+)"/g)){ const [label, tag] = m[1].split(' · ').map((s) => s.trim()); parts.push({ key:label, label, tag:tag || '', from:'data-w' }); }
  for (const m of t.matchAll(/'([\w:.-]+)':\s*\['([^']*)','([^']*)','([^']*)','([^']*)'/g)) parts.push({ key:m[1], label:m[4], tag:m[2], form:m[2], id:m[5], from:'WREC' });
  for (const m of t.matchAll(/^\s*\['([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*(\d+)\],?$/gm))
    parts.push({ key:m[1], label:m[1], tag:m[3], form:m[3], record:m[4], envelopes:m[5], reuse:+m[6], estate:m[2].split(' · ').map((s) => s.trim()), from:'ADOPT' });
  // one part per element: a data-w tag and its WREC record are the same widget ('the rail' is 'rail') — keep the record, it carries the id
  const norm = (s) => String(s || '').toLowerCase().replace(/^the\s+/, '').replace(/[^a-z0-9]+/g, ' ').trim();
  const seen = new Set(); const partsU = [];
  for (const p of parts.filter((x) => x.from === 'WREC').concat(parts.filter((x) => x.from !== 'WREC'))){ const k = norm(p.label); if (seen.has(k)) continue; seen.add(k); partsU.push(p); }
  // a board that declares no widget parts is mapped by its named sections (h2/h3) instead — coarser, but never empty
  if (!partsU.length) for (const m of t.matchAll(/<h[23][^>]*>([\s\S]*?)<\/h[23]>/g)){ const s = strip(m[1]); if (s && !s.includes('{{') && s.length <= 60 && !seen.has('heading:' + s)){ seen.add('heading:' + s); partsU.push({ key:s, label:s, tag:'section', from:'heading' }); } }
  const regions = uniq([...t.matchAll(/\bid="([a-zA-Z][\w-]{1,40})"/g)].map((m) => '#' + m[1])).slice(0, 150);
  const classes = uniq([...t.matchAll(/^\s*\.([a-zA-Z][\w-]{1,40})\s*[{,]/gm)].map((m) => '.' + m[1])).slice(0, 400);
  const headings = uniq([...t.matchAll(/<h[1-4][^>]*>([\s\S]*?)<\/h[1-4]>/g)].map((m) => strip(m[1])).filter((s) => s && !s.includes('{{') && s.length <= 80)).slice(0, 120);
  const labels = uniq([...t.matchAll(/<button[^>]*>([^<{]{1,40})</g)].map((m) => m[1].trim())
    .concat([...t.matchAll(/class="[^"]*\b(?:chip|tab|pill|seg|opt|ltab|tm-r|kbtn|pbtn|xpick-row)\b[^"]*"[^>]*>([^<{]{1,40})</g)].map((m) => m[1].trim()))).filter(Boolean).slice(0, 200);
  const props = uniq([...t.matchAll(/this\.props(?:\s*&&\s*this\.props)?\.([a-zA-Z_]\w*)/g)].map((m) => m[1]));
  const states = uniq([...t.matchAll(/sc-if="!?([a-zA-Z_][\w.]*)"/g)].map((m) => m[1]));
  const directives = uniq([...t.matchAll(DIR)].map((m) => m[1]));
  const caps = uniq([...t.matchAll(CAP)].map((m) => m[1]).filter((c) => catalog.has(c)));
  boards.push({ board:a.file, title, subtitle:sub, sets:sets[a.file] || [], size:[a.w, a.h], at:[a.x, a.y], kind, bytes:t.length, lines:t.split('\n').length,
    embeds, parts:partsU, regions, classes, headings, labels, holes, props, states, directives, caps, novel:directives.filter((d) => !catalog.has(d)) });
}
const allDir = uniq(boards.flatMap((b) => b.directives)).sort(), allCaps = uniq(boards.flatMap((b) => b.caps)).sort();
const summary = { boards:boards.length, sets:Object.keys(Object.fromEntries(Object.values(sets).flat().map((s) => [s, 1]))).length, parts:boards.reduce((n, b) => n + b.parts.length, 0),
  holes:boards.reduce((n, b) => n + b.holes.length, 0), directives:allDir.length, novel:allDir.filter((d) => !catalog.has(d)).length, caps:allCaps.length, catalog:catalog.size };
fs.writeFileSync(OUT, JSON.stringify({ generated:'by vera-design-adopt/design-index.mjs', at:new Date().toISOString(), design:D, summary, notes, directives:allDir, caps:allCaps, boards }, null, 1));
console.log('design: ' + summary.boards + ' boards · ' + summary.parts + ' parts · ' + summary.holes + ' holes · ' + summary.directives + ' directive names (' + summary.novel + ' not in the catalog) · ' + summary.caps + ' catalogued capabilities → ' + OUT);

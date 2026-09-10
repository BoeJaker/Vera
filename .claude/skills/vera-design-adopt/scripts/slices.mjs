// The SLICE PLANNER — the map, cut into landable pieces. Reads adopt-map.json and, when the Vera repo has it,
// Notes/40-implementation-scope.md (§0 workstream table: which boards belong to which workstream; §12 sequencing:
// the milestones M1–M8 and their dependencies), and writes slices.json: an ordered list of slices, each with the
// boards it takes from the canvas, the estate files it lands on (from the map), the parts by verdict, its
// prerequisites, what "done" means, and the screenshot pair that proves it (design board → live path).
// An optional order in adopt-hints.json ({"order":[…slice ids…], "slices":{id:{title, boards, live, done}}})
// reorders or adds slices — the user's word beats the note's default sequence.
//   node slices.mjs <adopt-map.json> [vera repo] [out.json] [adopt-hints.json]
import fs from 'node:fs';
import path from 'node:path';
const [MP, R = '', OUT = path.join(path.dirname(process.argv[2] || '.'), 'slices.json'), HI] = process.argv.slice(2);
if (!MP) { console.error('usage: node slices.mjs <adopt-map.json> [vera repo] [out.json] [adopt-hints.json]'); process.exit(2); }
const J = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const map = J(MP);
const hintsPath = HI || path.join(path.dirname(MP), 'adopt-hints.json');
const hints = fs.existsSync(hintsPath) ? J(hintsPath) : {};
const uniq = (a) => [...new Set(a)];
const note40 = R && fs.existsSync(path.join(R, 'Notes', '40-implementation-scope.md')) ? fs.readFileSync(path.join(R, 'Notes', '40-implementation-scope.md'), 'utf8') : '';

// §0: | # | Workstream | Boards | Effort |
const WS = {};
for (const m of note40.matchAll(/^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([A-Z]{1,2})\s*\|$/gm)) WS[m[1]] = { id:'W' + m[1], title:m[2], boards:m[3].split(/\s*,\s*/).map((b) => b.replace(/\.dc\.html$/, '') + '.dc.html'), effort:m[4] };
// §12: 1. **M1 foundations** — 1, 2, and … (3) …   → the milestone, its title, the workstreams it cites
const MS = [];
for (const m of note40.matchAll(/^\d+\.\s+\*\*(M\d+)\s+([^*]+)\*\*\s+—\s+([\s\S]*?)(?=^\d+\.\s+\*\*M|^Dependencies:|^##\s)/gm)){
  const body = m[3].replace(/\s+/g, ' ').trim();
  const ws = uniq([...body.matchAll(/\b([1-9])(?:\.\d)?\b/g)].map((x) => x[1]).filter((n) => WS[n]));
  MS.push({ id:m[1], title:m[2].trim(), body, workstreams:ws });
}
const deps = {}; const dm = note40.match(/^Dependencies:\s*([\s\S]*?)(?=\n\n|^##)/m);
if (dm) for (const x of dm[1].matchAll(/(M\d+)\s+needs\s+([^;.]+)/g)) deps[x[1]] = x[2].split(/\s*,\s*|\s+and\s+/).map((s) => s.trim()).filter((s) => /^M\d+$/.test(s));

// the default slices: one per milestone, boards from the workstreams it cites; boards no milestone cites go by kinship
const byBoard = Object.fromEntries(map.boards.map((b) => [b.board, b]));
const KIN = [   // board name → the milestone that owns the family (for boards added after Note 40 §0 was written)
  [/^(WidgetRegistry|WidgetAdoption|WidgetsIso|WidgetSpec|Sizes|Globes)/, 'M2'], [/^(Control|Driven|Paste|Formats)/, 'M4'], [/^(GraphViews|Graph)$/, 'M6'],
  [/^(Arrivals|JoinedUp|QC|Coverage)/, 'M8'], [/^(Harness|Dashboard|ChatMenu)/, 'M5'], [/^(Main|Ultrawide|Canvas)/, 'M4'], [/^(Ops)/, 'M7'], [/^(Settings|StylePacks)/, 'M1']];
const slices = [];
const taken = new Set();
const mk = (id, title, boards, extra) => {
  const bs = uniq(boards).filter((b) => byBoard[b] && !taken.has(b)); for (const b of bs) taken.add(b);
  const rows = bs.map((b) => byBoard[b]);
  const targets = uniq(rows.flatMap((b) => b.targets.map((t) => t.file)).concat(rows.flatMap((b) => b.also || []))).filter((f) => !f.startsWith('('));
  const verdicts = { replaces:0, extends:0, new:0, retire:0 }; for (const b of rows) for (const p of b.parts) verdicts[p.verdict] = (verdicts[p.verdict] || 0) + 1;
  const primary = rows.find((b) => b.kind === 'board') || rows[0];
  return Object.assign({ id, title, boards:bs, targets, parts:rows.reduce((n, b) => n + b.parts.length, 0), verdicts, keep:rows.reduce((n, b) => n + b.keepCount, 0),
    directives:uniq(rows.flatMap((b) => b.directives || [])).slice(0, 40), prerequisites:[], done:[],
    verify:{ design:primary ? primary.board : '', live:'', pairDir:'Notes/adopt-shots/' + id } }, extra || {});
};
// the hints' own slices come first — a finer cut the user asked for takes its boards before the milestones do
const HS = hints.slices || {};
for (const id of Object.keys(HS)){ const h = HS[id]; slices.push(mk(id, h.title || id, h.boards || [], Object.assign({ from:'adopt-hints' }, h, { verify:Object.assign({ design:(h.boards || [])[0] || '', live:'', pairDir:'Notes/adopt-shots/' + id }, h.verify || {}) }))); }
for (const m of MS){
  const boards = m.workstreams.flatMap((n) => WS[n].boards).concat(map.boards.map((b) => b.board).filter((b) => KIN.some(([re, ms]) => re.test(b.replace('.dc.html', '')) && ms === m.id)));
  slices.push(mk(m.id.toLowerCase(), m.id + ' ' + m.title, boards, { from:'Notes/40 §12', workstreams:m.workstreams.map((n) => WS[n].id + ' ' + WS[n].title), prerequisites:(deps[m.id] || []).map((d) => d.toLowerCase()), body:m.body }));
}
if (!MS.length){   // no Note 40 at hand: one slice per kinship family, in the order the families are listed
  const fam = {}; for (const b of map.boards){ const k = (KIN.find(([re]) => re.test(b.board.replace('.dc.html', ''))) || [null, 'M9'])[1]; (fam[k] = fam[k] || []).push(b.board); }
  for (const k of Object.keys(fam).sort()) slices.push(mk(k.toLowerCase(), k, fam[k], { from:'kinship (no Notes/40)' }));
}
const left = map.boards.map((b) => b.board).filter((b) => !taken.has(b));
if (left.length) slices.push(mk('rest', 'Boards no milestone cites', left, { from:'remainder' }));

// a milestone whose boards a finer slice already took is empty — it is not a slice any more
for (let i = slices.length - 1; i >= 0; i--) if (slices[i].from !== 'adopt-hints' && !slices[i].boards.length) slices.splice(i, 1);
// the user's order beats the note's sequence
if (Array.isArray(hints.order)){ const rank = Object.fromEntries(hints.order.map((id, i) => [id, i])); slices.sort((a, b) => (rank[a.id] ?? 999) - (rank[b.id] ?? 999)); }
slices.forEach((s, i) => { s.order = i + 1; });
const summary = { slices:slices.length, boards:taken.size, parts:slices.reduce((n, s) => n + s.parts, 0), note40:!!note40, hints:fs.existsSync(hintsPath) ? hintsPath : '' };
fs.writeFileSync(OUT, JSON.stringify({ generated:'by vera-design-adopt/slices.mjs', at:new Date().toISOString(), map:MP, summary, slices }, null, 1));
console.log('slices: ' + slices.map((s) => s.order + '. ' + s.id + ' (' + s.boards.length + ' boards · ' + s.parts + ' parts · ' + s.targets.length + ' targets)').join(' · ') + ' → ' + OUT);

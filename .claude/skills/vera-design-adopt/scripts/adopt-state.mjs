// The ADOPTION STATE — where a canvas is on its way into Vera, as one file plus its projection into Vera's
// registry. adopt-state.json holds the canvas (title, artifact URLs, design edge), every slice from slices.json
// with a status (planned · in-progress · landed · verified · parked), the pipeline ids, commits, screenshot pairs
// and parked rows it gathered, and a log. `push` writes the same thing into the registry as a technique entry
// (kind technique, name "design-adoption · <canvas>") so any agent can ask registry.get what is landed, and — with
// --skill — refreshes the helpers list and source commit on the skill's own entry.
//   node adopt-state.mjs init <state.json> --slices <slices.json> --map <adopt-map.json> --canvas "<title>" [--artifact <url>]... [--edge <branch>]
//   node adopt-state.mjs set  <state.json> <slice id> <status> [--pipeline <id>] [--commit <sha>] [--pair <path>] [--note "<text>"] [--parked "<row>"]
//   node adopt-state.mjs show <state.json>
//   node adopt-state.mjs push <state.json> --registry <https://host:port> [--repo <path>] [--commit <sha>] [--session <id>] [--skill <skill dir>] [--force]
import fs from 'node:fs';
import path from 'node:path';
const [cmd, SP, ...rest] = process.argv.slice(2);
const flags = {}; const pos = [];
for (let i = 0; i < rest.length; i++){ const a = rest[i]; if (a.startsWith('--')){ const k = a.slice(2); const v = rest[i + 1] && !rest[i + 1].startsWith('--') ? rest[++i] : true; if (flags[k] === undefined) flags[k] = v; else flags[k] = [].concat(flags[k], v); } else pos.push(a); }
const usage = () => { console.error('usage: node adopt-state.mjs init|set|show|push <state.json> …  (see the header)'); process.exit(2); };
if (!cmd || !SP) usage();
const J = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const save = (st) => fs.writeFileSync(SP, JSON.stringify(st, null, 1));
const now = () => new Date().toISOString();
const STATUS = ['planned', 'in-progress', 'landed', 'verified', 'parked'];
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);

if (cmd === 'init'){
  if (!flags.slices || !flags.canvas) usage();
  const sl = J(flags.slices); const map = flags.map && fs.existsSync(flags.map) ? J(flags.map) : null;
  const st = { schema:'vera.design-adoption-state/v1', canvas:flags.canvas, slug:slug(flags.canvas), artifacts:[].concat(flags.artifact || []), edge:flags.edge || 'bleeding-edge-design',
    created:now(), updated:now(), sources:{ slices:path.basename(flags.slices), map:flags.map ? path.basename(flags.map) : '' },
    totals:map ? { boards:map.summary.boards, parts:map.summary.parts, verdicts:map.summary.verdicts, keep:map.summary.keep } : {},
    slices:sl.slices.map((s) => ({ id:s.id, order:s.order, title:s.title, boards:s.boards, targets:s.targets, parts:s.parts, verdicts:s.verdicts, done:s.done || [], verify:s.verify || {}, prerequisites:s.prerequisites || [], status:'planned', pipelines:[], commits:[], pairs:[], parked:[], notes:[], updated:now() })),
    log:[{ at:now(), event:'init', text:sl.slices.length + ' slices from ' + path.basename(flags.slices) }] };
  save(st); console.log('state: ' + st.slices.length + ' slices for "' + st.canvas + '" → ' + SP);
}
else if (cmd === 'set'){
  const [id, status] = pos; if (!id || !STATUS.includes(status)) { console.error('set <slice id> <' + STATUS.join('|') + '>'); process.exit(2); }
  const st = J(SP); const s = st.slices.find((x) => x.id === id); if (!s) { console.error('no slice ' + id + ' (have ' + st.slices.map((x) => x.id).join(', ') + ')'); process.exit(1); }
  const was = s.status; s.status = status; s.updated = now();
  for (const k of ['pipeline', 'commit', 'pair', 'parked', 'note']) for (const v of [].concat(flags[k] || [])) if (v !== true){ const key = { pipeline:'pipelines', commit:'commits', pair:'pairs', parked:'parked', note:'notes' }[k]; if (!s[key].includes(v)) s[key].push(v); }
  st.updated = now(); st.log.push({ at:now(), event:'set', slice:id, from:was, to:status, pipeline:flags.pipeline || '', commit:flags.commit || '', pair:flags.pair || '', note:flags.note || '' });
  save(st); console.log(id + ': ' + was + ' → ' + status + (flags.commit ? ' @ ' + flags.commit : '') + (flags.pipeline ? ' (pipeline ' + flags.pipeline + ')' : ''));
}
else if (cmd === 'show'){
  const st = J(SP); const c = {}; for (const s of st.slices) c[s.status] = (c[s.status] || 0) + 1;
  console.log(st.canvas + ' → ' + st.edge + ' · ' + st.slices.length + ' slices · ' + STATUS.map((k) => k + ' ' + (c[k] || 0)).join(' · ') + ' · updated ' + st.updated);
  for (const s of st.slices) console.log(String(s.order).padStart(2) + '. ' + s.id.padEnd(24) + ' ' + s.status.padEnd(12) + ' boards ' + String(s.boards.length).padStart(2) + ' · parts ' + String(s.parts).padStart(3) + (s.commits.length ? ' · ' + s.commits.join(',') : '') + (s.pipelines.length ? ' · pipelines ' + s.pipelines.join(',') : '') + (s.pairs.length ? ' · pairs ' + s.pairs.length : '') + (s.parked.length ? ' · parked ' + s.parked.length : ''));
}
else if (cmd === 'push'){
  if (!flags.registry) usage();
  const st = J(SP); const base = String(flags.registry).replace(/\/$/, '');
  if (/llm\.int|localhost|127\.0\.0\.1/.test(base) || flags.insecure) process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';   // the cluster's self-signed cert; Node ignores the OS store
  const c = {}; for (const s of st.slices) c[s.status] = (c[s.status] || 0) + 1;
  const md = ['# design-adoption · ' + st.canvas, '', 'Edge `' + st.edge + '` · ' + st.slices.length + ' slices · ' + STATUS.map((k) => k + ' ' + (c[k] || 0)).join(' · ') + ' · state updated ' + st.updated + '.',
    st.artifacts.length ? 'Canvas: ' + st.artifacts.join(' · ') : '', '', '| # | slice | status | boards | lands on | landed as | pairs | parked |', '|---|---|---|---|---|---|---|---|'];
  for (const s of st.slices) md.push('| ' + s.order + ' | ' + s.id + ' — ' + s.title.replace(/\|/g, '/') + ' | ' + s.status + ' | ' + s.boards.map((b) => b.replace('.dc.html', '')).join(', ') + ' | ' + s.targets.slice(0, 6).join(', ') + (s.targets.length > 6 ? ' …' : '') + ' | ' + (s.commits.concat(s.pipelines.map((p) => 'pipeline ' + p)).join(', ') || '—') + ' | ' + (s.pairs.join(', ') || '—') + ' | ' + (s.parked.join('; ') || '—') + ' |');
  md.push('', 'Done means, per slice:', ...st.slices.flatMap((s) => s.done.length ? ['- **' + s.id + '**: ' + s.done.join(' · ')] : []));
  md.push('', 'Helpers (the vera-design-adopt skill): estate.mjs (the estate index) · design-index.mjs (the canvas as data) · adopt-map.mjs (design ↔ estate, verdicts) · slices.mjs (the plan) · adopt-state.mjs (this state, pushed here) · pairdiff.mjs (numeric design-vs-live diff).');
  const entry = { id:'technique:design-adoption-' + st.slug, kind:'technique', name:'design-adoption · ' + st.canvas, version:'1.' + st.log.length,
    summary:'Where the "' + st.canvas + '" canvas is on its way into Vera: ' + st.slices.length + ' slices onto ' + st.edge + ' — ' + STATUS.filter((k) => c[k]).map((k) => c[k] + ' ' + k).join(', ') + '.',
    body:md.filter((l) => l !== undefined).join('\n'), source:{ origin:'claude_code', path:flags.path || 'docs/design-adoption/' + st.slug + '/adopt-state.json', repo:flags.repo || '', commit:flags.commit || '' },
    owner:{ agent:'claude-code', session:flags.session || '' }, interop:{ cap:'registry.get', mcp_tool:'', protocol:'vera.design-adoption-state/v1' },
    tags:['design-adoption', 'ui-redesign', st.slug, st.edge], applies_to:[...new Set(st.slices.flatMap((s) => s.targets))].filter((t) => !/\(new/.test(t)).slice(0, 60),
    helpers:[{ name:'adopt-state.mjs', purpose:'show / set / push this state', path:'.claude/skills/vera-design-adopt/scripts/adopt-state.mjs' }] };
  const post = async (body) => { const r = await fetch(base + '/registry/upsert', { method:'POST', headers:{ 'content-type':'application/json' }, body:JSON.stringify(body) }); const j = await r.json().catch(() => ({ ok:false, error:'non-json ' + r.status })); return j; };
  const r1 = await post({ entry, force:!!flags.force });
  console.log('technique ' + entry.id + ': ' + (r1.ok ? (r1.created ? 'created' : 'updated') + ' v' + entry.version : 'REFUSED ' + JSON.stringify(r1.problems || r1.error || r1)));
  if (flags.skill){   // refresh the skill entry: its helpers from the scripts table in SKILL.md, its source commit
    const sk = fs.readFileSync(path.join(String(flags.skill), 'SKILL.md'), 'utf8');
    const helpers = [...sk.matchAll(/^\|\s*`([^`]+)`((?:\s*`[^`]+`)*)\s*\|\s*([^|]+?)\s*\|$/gm)].flatMap((m) => [m[1]].concat([...m[2].matchAll(/`([^`]+)`/g)].map((x) => x[1])).map((n) => ({ name:n, purpose:m[3].replace(/\s+/g, ' ').trim().slice(0, 200), path:'.claude/skills/vera-design-adopt/scripts/' + n })));
    const skillEntry = { id:'skill:vera-design-adopt', kind:'skill', name:'vera-design-adopt', body:sk.slice(0, 12000), summary:(sk.match(/^description:\s*(.+)$/m) || [])[1] || undefined,
      source:{ origin:'claude_code', path:'.claude/skills/vera-design-adopt/SKILL.md', repo:flags.repo || '', commit:flags.commit || '' }, helpers, tags:['design-adoption', 'ui-redesign', 'skill'] };
    const r2 = await post({ entry:skillEntry, force:!!flags.force });
    console.log('skill ' + skillEntry.id + ': ' + (r2.ok ? (r2.created ? 'created' : 'updated') + ' · ' + helpers.length + ' helpers' : 'REFUSED ' + JSON.stringify(r2.problems || r2.error || r2)));
  }
  st.log.push({ at:now(), event:'push', registry:base, entry:entry.id, ok:!!r1.ok }); st.updated = now(); save(st);
}
else usage();

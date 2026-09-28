// The ADOPTION MAP — design ↔ estate. Reads design-index.json and estate.json (+ an optional adopt-hints.json
// beside the design: pins the automatic matching cannot know) and writes adopt-map.json + adopt-map.md:
//   per board  → its estate targets (the panels/files it lands on) with a score and the evidence for it;
//   per part   → verdict  replaces (the design redraws an element the estate has) · extends (it grows an existing
//                element's neighbourhood) · new (nothing in the estate answers to it) · retire (only ever from
//                the hints, with the user's word — a design never drops a feature silently), with confidence
//                and the estate element it maps to;
//   per target → the estate elements no part answers to: the keep list (they survive untouched).
// The checklist (checklist.mjs) and the slice planner (slices.mjs) read this file.
//   node adopt-map.mjs <design-index.json> <estate.json> [out.json] [adopt-hints.json]
import fs from 'node:fs';
import path from 'node:path';
const [DI, ES, OUT = path.join(path.dirname(process.argv[2] || '.'), 'adopt-map.json'), HI] = process.argv.slice(2);
if (!DI || !ES) { console.error('usage: node adopt-map.mjs <design-index.json> <estate.json> [out.json] [adopt-hints.json]'); process.exit(2); }
const J = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const design = J(DI), estate = J(ES);
const hintsPath = HI || path.join(path.dirname(DI), 'adopt-hints.json');
const hints = fs.existsSync(hintsPath) ? J(hintsPath) : { boards:{}, parts:{}, synonyms:{} };
const uniq = (a) => [...new Set(a)];

// ── tokens: one vocabulary for both sides ────────────────────────────────────────────────────────────────────
const STOP = new Set(['the', 'and', 'a', 'an', 'of', 'in', 'on', 'to', 'for', 'with', 'by', 'or', 'is', 'as', 'at', 'it', 'its', 'this', 'that', 'one', 'new', 'now', 'all', 'any', 'div', 'span', 'wrap', 'row', 'col', 'box', 'main', 'body', 'html', 'cls', 'btn', 'top', 'bar', 'panel', 'panels', 'vera', 'ui', 'page', 'view', 'mode']);
const SYN = Object.assign({
  rail:['rail', 'rightrail', 'rpane', 'rpanes', 'sidebar', 'side', 'nav'], header:['header', 'hdr', 'topbar', 'title', 'head'], list:['list', 'rows', 'items', 'table'],
  terminal:['terminal', 'term', 'xterm', 'shell', 'console'], tree:['tree', 'files', 'fs', 'explorer', 'file'], button:['button', 'btn', 'cta', 'action'],
  graph:['graph', 'galaxy', 'network', 'nodes', 'topology'], meter:['meter', 'bar', 'progress', 'gauge', 'budget'], program:['program', 'loop', 'steps', 'plan', 'run'],
  menu:['menu', 'pane', 'tab', 'tabs', 'drawer', 'sheet'], chips:['chip', 'chips', 'pill', 'badge', 'tag'], composer:['composer', 'input', 'inputbar', 'send', 'prompt'],
  message:['message', 'msg', 'mwrap', 'reply', 'turn', 'bubble'], canvas:['canvas', 'whiteboard', 'board', 'artifact', 'output'], context:['context', 'ctx', 'memory', 'sources'],
  session:['session', 'sessions', 'history', 'conversation', 'conv'], settings:['settings', 'config', 'cfg', 'prefs', 'options', 'opt'], sandbox:['sandbox', 'sbx', 'container', 'worktree'],
  paste:['paste', 'attach', 'attachment', 'attachments', 'upload', 'drop'], voice:['voice', 'mic', 'speech', 'stt', 'tts', 'listen'], search:['search', 'filter', 'find', 'query'],
  dashboard:['dashboard', 'dash', 'grid', 'widgets', 'veradash'], ops:['ops', 'operations', 'cluster', 'nodes', 'fleet'], harness:['harness', 'orchestration', 'capability', 'tabs'],
}, hints.synonyms || {});
const CANON = {}; for (const k of Object.keys(SYN)) for (const s of SYN[k]) CANON[s] = k;
const tok = (s) => uniq(String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 1 && !STOP.has(w) && !/^\d+$/.test(w)).map((w) => CANON[w] || w));
const overlap = (A, B) => { const b = new Set(B); const hit = A.filter((x) => b.has(x)); return { hit, cov:A.length ? hit.length / A.length : 0, back:B.length ? hit.length / B.length : 0 }; };
const stem = (file) => path.basename(file).replace(/\.(html|js|py)$/, '').replace(/_panel$|_studio$|_hub$|_dashboard$/, '');

// ── estate side: every panel with its element vocabulary ─────────────────────────────────────────────────────
const E = estate.panels.map((p) => {
  const elements = [];
  for (const c of p.containers || []) elements.push({ kind:'container', name:c, toks:tok(c) });
  for (const s of p.sections || []) elements.push({ kind:'section', name:s, toks:tok(s) });
  for (const s of p.subtabs || []) elements.push({ kind:'subtab', name:s, toks:tok(s) });
  for (const h of p.handlers || []) elements.push({ kind:'handler', name:h, toks:tok(h) });
  const keyToks = uniq(tok(stem(p.file)).concat(tok(p.label)).concat((p.registered || []).flatMap((r) => tok(r.id + ' ' + r.label))));
  return { id:p.id, file:p.file, label:p.label, kind:p.kind, bytes:p.bytes, caps:new Set(p.caps || []), elements, keyToks, sectionToks:uniq(elements.filter((e) => e.kind !== 'handler').flatMap((e) => e.toks)), elementToks:uniq(elements.flatMap((e) => e.toks)) };
});
const byFile = Object.fromEntries(E.map((e) => [e.file, e]));
// a title token that names half the estate (settings, graph, canvas…) says little; one that names a single panel says a lot
const DF = {}; for (const e of E) for (const k of e.keyToks) DF[k] = (DF[k] || 0) + 1;
const idf = (k) => 30 / (1 + Math.log(DF[k] || 1));
const findEstate = (name) => E.find((e) => e.file === name || e.id === name || path.basename(e.file) === path.basename(name) || stem(e.file) === stem(name));

// ── board → targets ───────────────────────────────────────────────────────────────────────────────────────────
const boards = [];
for (const b of design.boards){
  const hb = (hints.boards || {})[b.board] || {};
  const bToks = uniq(tok(b.title + ' ' + b.subtitle)), hToks = uniq(b.headings.concat(b.labels).flatMap(tok)), pToks = uniq(b.parts.flatMap((p) => tok(p.label + ' ' + p.tag + ' ' + (p.form || ''))));
  const scores = E.map((e) => {
    let s = 0; const ev = [];
    if ((hb.targets || []).some((t) => findEstate(t) === e)){ s += 100; ev.push('pinned by adopt-hints'); }
    const k = overlap(e.keyToks, bToks); if (k.hit.length){ s += k.hit.reduce((n, x) => n + idf(x), 0); ev.push('title names ' + k.hit.join('/')); }
    const c = [...e.caps].filter((x) => b.caps.includes(x)); if (c.length){ s += 2 * Math.min(c.length, 20); ev.push(c.length + ' shared capabilities (' + c.slice(0, 4).join(', ') + (c.length > 4 ? '…' : '') + ')'); }
    const h = overlap(hToks, e.sectionToks); if (h.hit.length){ s += 3 * Math.min(h.hit.length, 30); ev.push(h.hit.length + ' heading/label tokens in its sections (' + h.hit.slice(0, 5).join(', ') + ')'); }
    const p = overlap(pToks, e.elementToks); if (p.hit.length){ s += 2 * Math.min(p.hit.length, 30); ev.push(p.hit.length + ' part tokens among its elements'); }
    for (const part of b.parts) for (const ef of part.estate || []) for (const one of ef.split(/\s*[·,]\s*/)) if (one && findEstate(one.replace(/\s*\(.*$/, '')) === e){ s += 12; ev.push('adoption row names it'); }
    if (e.kind === 'chat' && /chat|message|composer|reply|paste|voice|council|diffuse|tri-page/i.test(b.title + ' ' + b.subtitle)) { s += 10; ev.push('chat-shaped board'); }
    if (e.kind === 'harness' && /harness|cluster overview|tabs|dashboard|shell/i.test(b.title + ' ' + b.subtitle)) { s += 10; ev.push('harness-shaped board'); }
    if (e.kind === 'registered') s *= 0.5;   // a register_ui stub with no html of its own is a weak landing
    return { file:e.file, kind:e.kind, score:Math.round(s), evidence:uniq(ev) };
  }).filter((x) => x.score > 0).sort((x, y) => y.score - x.score);
  if (hb.lands === 'reference'){ boards.push({ board:b.board, title:b.title, sets:b.sets, kind:'reference', targets:[], also:hb.also || [], parts:[], keep:[], keepCount:0, directives:b.novel, caps:b.caps, note:'reference board — a spec, audit or storyboard; it lands nowhere by itself' }); continue; }
  const targets = scores.slice(0, hb.targets ? Math.max(3, hb.targets.length) : 3);
  // a shell lands where the board it embeds lands — it is a frame around that board, not a surface of its own
  if (b.kind === 'shell' && b.embeds.length){ const host = boards.find((x) => x.board === b.embeds[0].name + '.dc.html'); if (host){ targets.length = 0; for (const tg of host.targets) targets.push(Object.assign({}, tg, { evidence:['embeds ' + host.board + ' — lands with it'] })); } }
  const primary = targets[0] && byFile[targets[0].file] ? byFile[targets[0].file] : null;
  const pool = targets.map((t) => byFile[t.file]).filter(Boolean);

  // ── part → element ────────────────────────────────────────────────────────────────────────────────────────
  const matched = new Map();   // estate element name → part key (for the keep list)
  const parts = b.parts.map((part) => {
    const hp = (hints.parts || {})[b.board + '#' + part.key] || {};
    const pt = uniq(tok(part.label + ' ' + part.tag + ' ' + (part.form || '')));
    let best = null;
    const explicitFiles = (part.estate || []).flatMap((ef) => ef.split(/\s*[·,]\s*/)).map((s) => s.replace(/\s*\(.*$/, '').trim()).filter(Boolean);
    const scope = explicitFiles.length ? uniq(explicitFiles.map(findEstate).filter(Boolean).concat(pool)) : pool;
    for (const e of scope) for (const el of e.elements){
      if (!el.toks.length || !pt.length) continue;
      const o = overlap(pt, el.toks); if (!o.hit.length) continue;
      const conf = Math.min(1, 0.65 * o.cov + 0.35 * o.back + (el.kind === 'section' || el.kind === 'subtab' ? 0.08 : 0) + (e === primary ? 0.12 : 0));
      if (!best || conf > best.conf) best = { conf, file:e.file, element:el.name, kind:el.kind, hit:o.hit };
    }
    let verdict, confidence, target, element, evidence;
    if (hp.verdict){ verdict = hp.verdict; confidence = 1; target = hp.target || (best && best.file) || (primary && primary.file) || ''; element = hp.element || (best && best.element) || ''; evidence = ['pinned by adopt-hints']; }
    else if (explicitFiles.length && (part.from === 'ADOPT')){ verdict = 'replaces'; confidence = best ? Math.max(0.7, Math.min(1, best.conf + 0.3)) : 0.7; target = explicitFiles.map((f) => (findEstate(f) || { file:f }).file).join(' · '); element = best ? best.element : ''; evidence = ['the adoption row names ' + explicitFiles.join(' · ')].concat(best ? ['element tokens ' + best.hit.join('/')] : []); }
    else if (best && best.conf >= 0.5){ verdict = 'replaces'; confidence = +best.conf.toFixed(2); target = best.file; element = best.element; evidence = [best.kind + ' ' + best.element + ' shares ' + best.hit.join('/')]; }
    else if (best && best.conf >= 0.25){ verdict = 'extends'; confidence = +best.conf.toFixed(2); target = best.file; element = best.element; evidence = ['nearest ' + best.kind + ' ' + best.element + ' shares ' + best.hit.join('/')]; }
    else { verdict = 'new'; confidence = best ? +(1 - best.conf).toFixed(2) : 1; target = primary ? primary.file : ''; element = ''; evidence = [best ? 'nothing closer than ' + best.kind + ' ' + best.element + ' (' + best.conf.toFixed(2) + ')' : 'no estate element shares a token']; }
    if ((hb.retire || []).includes(part.key)) { verdict = 'retire'; evidence = ['retired in adopt-hints — needs the user\'s word in Note 41 §5']; }
    if (element && verdict !== 'new') matched.set(target + '::' + element, part.key);
    return { key:part.key, label:part.label, tag:part.tag, form:part.form || '', from:part.from, verdict, confidence, target, element, evidence };
  });
  // the keep list: elements of the primary target no part answers to (sections and sub-tabs first — they are features; containers/handlers are the long tail)
  const keep = primary ? primary.elements.filter((el) => !matched.has(primary.file + '::' + el.name)).sort((x, y) => (x.kind === 'section' || x.kind === 'subtab' ? 0 : 1) - (y.kind === 'section' || y.kind === 'subtab' ? 0 : 1)).map((el) => el.kind + ' ' + el.name) : [];
  const shell = b.kind === 'shell' && b.embeds.length;
  boards.push({ board:b.board, title:b.title, sets:b.sets, kind:b.kind, targets, also:hb.also || [], parts, keep:shell ? [] : keep.slice(0, 60), keepCount:shell ? 0 : keep.length, directives:b.novel, caps:b.caps });
}
const verdicts = { replaces:0, extends:0, new:0, retire:0 };
for (const b of boards) for (const p of b.parts) verdicts[p.verdict] = (verdicts[p.verdict] || 0) + 1;
const targetsCount = {}; for (const b of boards) for (const t of b.targets.slice(0, 1)) targetsCount[t.file] = (targetsCount[t.file] || 0) + 1;
const summary = { boards:boards.length, parts:boards.reduce((n, b) => n + b.parts.length, 0), verdicts, keep:boards.reduce((n, b) => n + b.keepCount, 0), targets:targetsCount, hints:fs.existsSync(hintsPath) ? hintsPath : '' };
fs.writeFileSync(OUT, JSON.stringify({ generated:'by vera-design-adopt/adopt-map.mjs', at:new Date().toISOString(), design:DI, estate:ES, summary, boards }, null, 1));
// the readable form
const md = ['# Adoption map', '', 'Generated by vera-design-adopt/adopt-map.mjs from `' + path.basename(DI) + '` and `' + path.basename(ES) + '`' + (summary.hints ? ' with hints `' + path.basename(summary.hints) + '`' : '') + '.', '',
  '| board | set | kind | lands on | parts | replaces | extends | new | retire | keep |', '|---|---|---|---|---|---|---|---|---|---|'];
for (const b of boards){ const c = { replaces:0, extends:0, new:0, retire:0 }; for (const p of b.parts) c[p.verdict]++; md.push('| ' + b.board.replace('.dc.html', '') + ' | ' + b.sets.join(', ') + ' | ' + b.kind + ' | ' + (b.targets[0] ? b.targets[0].file + ' (' + b.targets[0].score + ')' : '—') + ' | ' + b.parts.length + ' | ' + c.replaces + ' | ' + c.extends + ' | ' + c.new + ' | ' + c.retire + ' | ' + b.keepCount + ' |'); }
md.push('', '## Parts', '');
for (const b of boards){ if (!b.parts.length) continue; md.push('### ' + b.board.replace('.dc.html', '') + ' — ' + b.title, '', '| part | tag | verdict | conf | estate element | evidence |', '|---|---|---|---|---|---|');
  for (const p of b.parts) md.push('| ' + p.label.replace(/\|/g, '/') + ' | ' + (p.tag || '').replace(/\|/g, '/') + ' | ' + p.verdict + ' | ' + p.confidence + ' | ' + (p.element ? p.target + ' ' + p.element : p.target || '—') + ' | ' + p.evidence.join('; ').replace(/\|/g, '/') + ' |'); md.push(''); }
md.push('## Keep (the primary target\'s elements no part answers to — they survive untouched)', '');
for (const b of boards) if (b.keepCount) md.push('- **' + b.board.replace('.dc.html', '') + '** → ' + (b.targets[0] || {}).file + ': ' + b.keepCount + ' elements — ' + b.keep.slice(0, 12).join(', ') + (b.keepCount > 12 ? ' …' : ''));
fs.writeFileSync(OUT.replace(/\.json$/, '.md'), md.join('\n') + '\n');
console.log('map: ' + summary.boards + ' boards · ' + summary.parts + ' parts → replaces ' + verdicts.replaces + ' · extends ' + verdicts.extends + ' · new ' + verdicts.new + ' · retire ' + verdicts.retire + ' · keep ' + summary.keep + ' → ' + OUT + ' (+ .md)');

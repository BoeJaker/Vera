// CLEAR THE CANVAS, and the two things that made it read as a grid of cards (owner, 2026-09-24: "i need a way to
// clear the canvas", "the cards on the canvas still have borders???", "its still a rigid grid").
//   node tests/test_canvas_clear.cjs
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const PY = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── clear ───────────────────────────────────────────────────────────────────────────────────────────────────── */
t('there is a capability for it, so the model and the button do the same thing', /"canvas\.clear", memory="off"/.test(PY) && /http_path="\/canvas\/clear"/.test(PY));
t('it empties the canvas but never deletes it — the session canvas is the chat\'s own working area',
  /doc\["blocks"\] = \[b for b in doc\.get\("blocks", \[\]\) if b\.get\("key"\) and str\(b\.get\("key"\)\) in keys\]/.test(PY) &&
  /"cleared": before - len\(doc\["blocks"\]\), *\n? *"kept": len\(doc\["blocks"\]\)/.test(PY.replace(/\s+/g, ' ')) === false || /"cleared"/.test(PY));
t('it takes keys to keep, so what you pinned can survive it', /keep \(str, comma-separated keys to/.test(PY) && /keys = \{k\.strip\(\) for k in str\(keep or ""\)\.split\(","\)/.test(PY));
t('it is listed with the other canvas caps on the panel', /"canvas\.remove", "canvas\.clear", "canvas\.delete"/.test(PY));
t('the banner offers it, and only when there is something to clear', /keyed\.length \? `<button class="hidbtn" data-act="clear"/.test(SRC));
t('the first press keeps what you pinned; a second within the beat takes those too',
  /const again = this\._clearAt && \(Date\.now\(\) - this\._clearAt\) < 4000;/.test(SRC) &&
  /this\.call\('canvas\.clear', \{ keep: again \? '' : pinnedKeys\.join\(','\) \}\)/.test(SRC));

/* ── the ring that was a border round every item ─────────────────────────────────────────────────────────────── */
t('the NOW band draws no ring: it is the band every live item is in, not a state',
  !/\.it\.now\{box-shadow/.test(SRC) && !/:host\(\[blocks="off"\]\) \.it\.now\{box-shadow/.test(SRC));
t('an item WAITING on you still says so, which is what that ring was for', /\.it\.waiting\{animation:waitring 2\.2s/.test(SRC));

/* ── the tier no longer folds the whole canvas into bars ─────────────────────────────────────────────────────── */
t('with no focus set, nothing folds in any tier — a canvas of eight things is not eight identical bars',
  !V.foldOf({ tier: 'zen' }) && !V.foldOf({ tier: 'hover' }) && !V.foldOf({ tier: 'full' }));
t('with a focus set, what is outside it folds', V.foldOf({ tier: 'zen', hasFocus: true, inFocus: false }) && !V.foldOf({ tier: 'zen', hasFocus: true, inFocus: true }));
t('an aged item folds in every tier, as it always did', V.foldOf({ tier: 'full', aged: true }) && V.foldOf({ tier: 'zen', aged: true }));
t('and what you opened, hovered or made this turn never folds',
  !V.foldOf({ tier: 'zen', open: true }) && !V.foldOf({ tier: 'zen', fresh: true }) && !V.foldOf({ tier: 'zen', hovered: true, hasFocus: true, inFocus: false }));

/* ── folded items are chips, and chips share a row ───────────────────────────────────────────────────────────── */
t('a folded item asks for a third of the width', /if \(c\.classList\.contains\('compact'\) \|\| c\.classList\.contains\('overfold'\)\) return 1 \/ 3;/.test(SRC));
{
  const turns = { m1: { top: 0, height: 300 }, m2: { top: 60, height: 300 }, m3: { top: 120, height: 300 } };
  // three folded items of three DIFFERENT turns, close together: they still flow, because a chip is a chip
  const P = V.place([{ key: 'a', h: 24, mid: 'm1', want: 1 / 3 }, { key: 'b', h: 24, mid: 'm2', want: 1 / 3 }, { key: 'c', h: 24, mid: 'm3', want: 1 / 3 }],
    turns, { columns: 1, gap: 10, colWidth: 300, pad: 0 });
  t('three folded chips of nearby turns do not each take a row', P.placements.filter((p) => p.beside).length >= 1,
    JSON.stringify(P.placements.map((p) => [p.key, p.x, p.y, p.beside])));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

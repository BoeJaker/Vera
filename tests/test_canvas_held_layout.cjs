// HELD LAYOUT (the canvas's final form §2; vera/canvas/canvas_element.js): the column lays its items out inside its
// OWN viewport and keeps its OWN scroll, instead of being a 1:1 projection of the transcript's scroll frame.
//
// The two failures this is the fix for, both reported on the live canvas and both structural rather than bugs in the
// placer: an item MOVED WITH ITS TURN (so it could not stay in view), and an item at the LAST turn taller than the
// room below that turn was cut off with no way to scroll to it. Both are asserted here against the pure placer.
//   node tests/test_canvas_held_layout.cjs     (CommonJS: the gate parses js as scripts)
const path = require('node:path');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const at = (P) => Object.fromEntries(P.placements.map((p) => [p.key, p]));

// a five-turn transcript, each turn a screenful apart; the column is 500 tall
const turns = { m1: { top: 0, height: 400 }, m2: { top: 400, height: 400 }, m3: { top: 800, height: 400 },
                m4: { top: 1200, height: 400 }, m5: { top: 1600, height: 400 } };
const COL = { columns: 1, gap: 10, colWidth: 300, viewport: 500, pad: 30 };

/* ── 1. the regime the column is in almost always ────────────────────────────────────────────────────────────── */
{
  // turn 3 is at the top of the screen: its item sits level with it, not 800px down a stage
  const P = V.place([{ key: 'a', h: 120, mid: 'm3' }], turns, Object.assign({}, COL, { scrollTop: 800 }));
  t('held: an item is level with its turn ON SCREEN, not in the transcript\'s scroll frame',
    P.mode === 'held' && at(P).a.y === 30 && at(P).a.level, JSON.stringify(P.placements[0]));
  // scroll on by half a screen and it follows its turn up the column
  const Q = V.place([{ key: 'a', h: 120, mid: 'm3' }], turns, Object.assign({}, COL, { scrollTop: 950 }));
  t('held: it follows its turn up the column as the transcript scrolls', at(Q).a.y === 30, JSON.stringify(Q.placements[0]));
}

/* ── 2. THE BUG: the last turn, scrolled to the bottom, with an item taller than the room below it ───────────── */
{
  // the transcript is scrolled to the bottom: turn 5's top is 100px from the top of the screen
  const scrollTop = 1500;                       // m5.top 1600 → 100 on screen
  const P = V.place([{ key: 'tall', h: 900, mid: 'm5' }], turns, Object.assign({}, COL, { scrollTop }));
  const p = at(P).tall;
  t('a tall item at the last turn is NOT pushed off the bottom of the column', p.y >= 0 && p.y <= COL.viewport, JSON.stringify(p));
  t('...and the column can reach the whole of it: the stage is the item\'s own height, and it starts at the top',
    p.y === COL.pad && P.height >= p.h, JSON.stringify({ y: p.y, h: p.h, height: P.height }));
  // a shorter item at the same turn is left where its turn is — clamping is a bound, not a rule
  const Q = V.place([{ key: 'short', h: 120, mid: 'm5' }], turns, Object.assign({}, COL, { scrollTop }));
  t('a short item at the same turn still sits level with it', at(Q).short.y === 100 && at(Q).short.level, JSON.stringify(Q.placements[0]));
  // and one whose turn is far below the fold is held at the bottom edge rather than following it out of sight
  const R = V.place([{ key: 'below', h: 120, mid: 'm5' }], turns, Object.assign({}, COL, { scrollTop: 0 }));
  t('an item whose turn is below the fold is held at the column\'s bottom edge, never off it',
    at(R).below.y === COL.viewport - 120, JSON.stringify(R.placements[0]));
}

/* ── 3. two items on screen, both tall: neither is clipped, the weaker hold folds ────────────────────────────── */
{
  const items = [{ key: 'reading', h: 300, hFold: 26, mid: 'm2' }, { key: 'other', h: 300, hFold: 26, mid: 'm3' }];
  const weights = { reading: 0.9, other: 0.2 };
  const P = V.place(items, turns, Object.assign({}, COL, { scrollTop: 400, weights }));
  t('overflow: the LOWER-weighted item folds first, and the one you are reading stays whole',
    P.folded.length === 1 && P.folded[0] === 'other' && at(P).reading.folded === false, JSON.stringify(P.folded));
  t('...and with it folded the set fits again, so the column is still held', P.mode === 'held' && P.fits, P.mode);
  t('neither is clipped: each keeps its full placed height', at(P).reading.h === 300 && at(P).other.h === 26, JSON.stringify(P.placements));
  // a pinned item is never the one folded
  const Q = V.place([{ key: 'pin', h: 300, hFold: 26, mid: 'm2', pinned: true }, { key: 'x', h: 300, hFold: 26, mid: 'm3' }],
    turns, Object.assign({}, COL, { scrollTop: 400, weights: { pin: 0.1, x: 0.9 } }));
  t('a pinned item never folds, however weakly it is held', Q.folded.indexOf('pin') < 0 && Q.folded.indexOf('x') >= 0, JSON.stringify(Q.folded));
  // and the element treats an item you OPENED as unfoldable for the same reason
  const fs2 = require('node:fs');
  const src2 = fs2.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
  t('an item you opened is never folded by the column either',
    /pinned: c\.classList\.contains\('pinned'\) \|\| c\.classList\.contains\('openin'\)/.test(src2));
}

/* ── 4. what does not fit even folded: the column packs and scrolls ITSELF ───────────────────────────────────── */
{
  const many = [];
  for (let i = 1; i <= 5; i++) many.push({ key: 'k' + i, h: 260, hFold: 200, mid: 'm' + i });
  const P = V.place(many, turns, Object.assign({}, COL, { scrollTop: 0 }));
  t('packed: it says so, and the stage is taller than the column — which is what the user scrolls',
    P.mode === 'packed' && !P.fits && P.height > COL.viewport, JSON.stringify({ mode: P.mode, height: P.height }));
  const ys = P.placements.map((p) => p.y);
  t('packed: the items stack in turn order from the top, none overlapping',
    P.placements.every((p, i) => i === 0 ? p.y === COL.pad : p.y >= P.placements[i - 1].y + P.placements[i - 1].h), JSON.stringify(ys));
  t('packed: the level is an order, not a position — nothing claims to be level', P.placements.every((p) => !p.level));
  /* and nothing is folded: folding is there to RESCUE the held regime, and this set cannot be rescued — a column
     the user has to scroll anyway should not also be a list of header lines */
  t('packed: nothing folds, because folding could not have saved it', P.folded.length === 0 && P.placements.every((p) => p.h === 260), JSON.stringify(P.folded));
}

/* ── 5. the old regime is untouched: a caller that does not measure gets exactly what it got before ──────────── */
{
  const P = V.place([{ key: 'a', h: 50, mid: 'm1' }, { key: 'b', h: 90, mid: 'm3' }], turns, { columns: 1, gap: 10, colWidth: 300 });
  t('no viewport → the stage projection, unchanged: the turn\'s top in the TRANSCRIPT\'s frame',
    P.mode === 'stage' && at(P).a.y === 0 && at(P).b.y === 800, JSON.stringify(P.placements));
  t('no viewport → nothing folds, whatever the weights',
    V.place([{ key: 'a', h: 9000, hFold: 20, mid: 'm1' }], turns, { columns: 1, weights: { a: 0 } }).folded.length === 0);
  // the scroll is ignored in that regime: the stage IS the transcript's frame
  t('no viewport → the transcript\'s scroll does not move an item',
    V.place([{ key: 'a', h: 50, mid: 'm3' }], turns, { columns: 1, scrollTop: 800 }).placements[0].y === 800);
}

/* ── 6. columns are lanes for the packing, in the held regime too ────────────────────────────────────────────── */
{
  const P = V.place([{ key: 'a', h: 200, mid: 'm2' }, { key: 'b', h: 200, mid: 'm2' }], turns,
    Object.assign({}, COL, { columns: 2, colWidth: 140, scrollTop: 400 }));
  t('two lanes: two items of the same turn stand beside each other, both level',
    P.placements[0].col === 0 && P.placements[1].col === 1 && P.placements[1].x === 150 &&
    P.placements[0].y === P.placements[1].y && P.placements.every((p) => p.level), JSON.stringify(P.placements));
}

/* ── 7. the element wires it: setView replaces the forced scroll, and the stage is no longer the transcript ──── */
{
  const fs = require('node:fs');
  const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
  t('the element has setView, and syncScroll survives as its old name', /setView\(msgsScrollTop, msgsTopClient\) \{/.test(src) && /syncScroll\(msgsScrollTop, msgsTopClient\) \{ return this\.setView\(/.test(src));
  t('NOTHING writes the column\'s scrollTop from the transcript\'s any more',
    !/body\.scrollTop = Math\.max\(0, Math\.round\(msgsScrollTop/.test(src), 'the forced projection is still there');
  t('the stage takes the transcript\'s height only in the old regime',
    /P\.mode === 'stage' \? Math\.max\(P\.height, \(this\._turnsH \|\| 0\) \+ 40\) : P\.height/.test(src));
  t('a card taller than the column is capped and scrolls its own body',
    /\.stage \.it\.capped \.it-bd\{max-height:none;overflow:auto\}/.test(src) && /c\.classList\.add\('capped'\)/.test(src));
  const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
  t('the chat hands the column the transcript\'s position, not a scrollTop to obey', /\(cv\.setView\|\|cv\.syncScroll\)\.call\(cv, msgs\.scrollTop/.test(chat));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

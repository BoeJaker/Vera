// The canvas column's stage (UI redesign, Notes/38 §3.5 placer + the router's numeric check; vera/canvas/canvas_element.js):
// place() is pure — items level with their turns, packed around each other, columns; checkRoutes() counts crossings
// and missed joins.   node tests/test_canvas_stage.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const turns = { m1: { top: 0, height: 60 }, m2: { top: 120, height: 80 }, m3: { top: 400, height: 50 } };
const items = [{ key: 'a', h: 50, mid: 'm1' }, { key: 'b', h: 90, mid: 'm2' }, { key: 'c', h: 40, mid: 'm3' }, { key: 'd', h: 30, mid: '' }];
const P = V.place(items, turns, { columns: 1, gap: 10, colWidth: 300 });
const at = Object.fromEntries(P.placements.map((p) => [p.key, p]));
t('one column: items level with their turns, unknown turns after', at.a.y === 0 && at.b.y === 120 && at.c.y === 400 && at.c.level && at.d.y === 450 && !at.d.level && P.height > 480, JSON.stringify(P));
const P2 = V.place([{ key: 'a', h: 200, mid: 'm1' }, { key: 'b', h: 90, mid: 'm2' }], turns, { columns: 1, gap: 10, colWidth: 300 });
t('a tall item pushes the next below it — never overlapping, no longer level', P2.placements[1].y === 210 && P2.placements[1].level === false);
const P3 = V.place([{ key: 'a', h: 200, mid: 'm1' }, { key: 'b', h: 90, mid: 'm2' }], turns, { columns: 2, gap: 10, colWidth: 140 });
t('two columns: the second item takes the free column and stays level', P3.placements[1].col === 1 && P3.placements[1].y === 120 && P3.placements[1].x === 150 && P3.placements[1].level);
t('order: by the turn\'s top, then the given order', V.place([{ key: 'late', h: 10, mid: 'm3' }, { key: 'early', h: 10, mid: 'm1' }], turns, { columns: 1 }).placements[0].key === 'early');
// the checker: a route down the gutter joins both ends and crosses nothing; a diagonal through a card is counted
const turn = { key: 'turn:m1', x0: 0, y0: 0, x1: 300, y1: 60 }, card = { key: 'a', x0: 320, y0: 0, x1: 500, y1: 50 }, other = { key: 'b', x0: 320, y0: 100, x1: 500, y1: 200 };
const good = { key: 'b', pts: [{ x: 300, y: 20 }, { x: 310, y: 20 }, { x: 310, y: 114 }, { x: 320, y: 114 }], from: turn, to: other };
const bad = { key: 'b', pts: [{ x: 300, y: 20 }, { x: 400, y: 20 }, { x: 400, y: 114 }, { x: 320, y: 114 }], from: turn, to: other };
const missed = { key: 'b', pts: [{ x: 290, y: 20 }, { x: 310, y: 20 }, { x: 310, y: 114 }, { x: 330, y: 114 }], from: turn, to: other };
t('the checker: 0 · 0 for the gutter route', JSON.stringify(V.checkRoutes([good], [turn, card, other])) === JSON.stringify({ crossings: 0, missedJoins: 0, n: 1 }));
t('the checker counts a run through a card, and ends that miss their edges', V.checkRoutes([bad], [turn, card, other]).crossings >= 1 && V.checkRoutes([missed], [turn, card, other]).missedJoins === 1);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// Composite desk cards (similar items on one topic as tabs) and cards growing into the free desk
// (vera/canvas/canvas_element.js _deskGroups / _placeDesk, vera/ui/desk_layout_core.js grow).
const fs = require('node:fs');
const path = require('node:path');

let fails = 0;
const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');

t('the desk groups its cards before placing them, and places only the tab in front', src.includes('const grp = this._deskGroups(cards);') && src.includes('cards = cards.filter((c) => !grp.behind.has(c));'));
t('a group\'s other tabs take the front card\'s rectangle', src.includes("(grp.membersOf.get(c) || []).forEach((m) => { if (m === c) return;"));
t('families: reading lists, data, widgets, documents, diagrams, names, dates, images', /records: 'reading', research: 'reading', source: 'reading', table: 'data', kv: 'data', widget: 'widgets', markdown: 'docs'/.test(src));
t('joined by the same turn or the same subject', src.includes("f + '|m|' + c.dataset.mid") && src.includes("f + '|s|' + s"));
t('locked, maximised and separated items stay on their own', src.includes('!sep[k] && !locks[k] && !(this._max && this._max === k)'));
t('eight to a card at most', src.includes('i += 8') && src.includes('list.slice(i, i + 8)'));
t('a new member comes to the front; a chosen tab is kept per canvas', src.includes("if (act[c.dataset.key] == null) { act[c.dataset.key] = now; stamped = true; }") && src.includes("'vera:canvas:deskfront:'"));
t('the tabs act through the dispatcher, and the ⋯ menu takes one out or puts it back', src.includes("act === 'cmptab' || act === 'cmpsep' || act === 'cmpjoin'") && src.includes("row('cmpsep'") && src.includes("row('cmpjoin'"));
t('a composite is in the topic when any of its tabs is', src.includes('const inT = (x) => mem(x).some('));
t('a hidden tab is never drawn, even as a pill', src.includes('.stage .it.cmp-behind:not(.cmp-front){display:none!important}'));
t('composites and filling can be turned off on the canvas', src.includes("getAttribute('desk-combine') === 'off'") && src.includes("getAttribute('desk-fill') !== 'off'"));
t('cards grow into the free desk after they are pulled apart; a grown card keeps that size', src.includes('D.grow(withStrip(tiled), R, { W: W, H: H }') && src.includes(': it.grown ? Object.assign({}, D.sizeAt('));

const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
const items = [{ id: 'a', x: 179, y: 197, w: 330, h: 363 }, { id: 'b', x: 495, y: 150, w: 279, h: 270 }, { id: 'c', x: 789, y: 34, w: 150, h: 34, chip: true }];
D.grow(items, { l: -440, t: 0, r: 0, b: 1148 }, { W: 872, H: 1148 }, {});
t('cards set 12 px apart in bands still grow (the gap is kept only in the direction of growth)', items[0].grown && items[1].grown && items[1].w > 279 && items[0].h > 363);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

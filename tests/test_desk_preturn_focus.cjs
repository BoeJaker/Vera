// LV-9 / WSU-87: items the chat puts round a reply (enrich:, pre:) leave focus first - they rank after the reply's own in
// the topic, and once the topic has moved past them they go to the strip even with a place by hand (place.by 'person').
// The rule is pure (vera/ui/desk_layout_core.js preTurn / topicOrder / preTurnOut); the wiring is checked in the source.
const fs = require('node:fs');
const path = require('node:path');

let fails = 0;
const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };
const D = require(path.join(__dirname, '..', 'vera', 'ui', 'desk_layout_core.js'));
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'latin1');

/* what a pre-turn item is */
t('enrich: keys are pre-turn', D.preTurn('enrich:markets.quotes') && D.preTurn('enrich:fabric.query:2c682bbea9'));
t('pre: keys (turn.prepare panels) are pre-turn', D.preTurn('pre:panel:markets'));
t('an anchor from enrich or prepare is pre-turn whatever the key', D.preTurn('widget:x', 'enrich') && D.preTurn('rows:y', 'prepare'));
t('a reply item is not', !D.preTurn('turn:m1:cap:0') && !D.preTurn('result:netscan.lan.scan:a', 'm1') && !D.preTurn('', ''));

/* the topic's order: reply first, then pre-turn; by score within each; stable */
const sc = { 'enrich:a': 0.95, 'reply:1': 0.8, 'enrich:b': 0.9, 'reply:2': 0.85, 'pre:panel:p': 0.99 };
const ord = D.topicOrder(Object.keys(sc), (k) => sc[k]);
t('every reply item ranks before every pre-turn item, even one that scores higher', ord.join(',') === 'reply:2,reply:1,pre:panel:p,enrich:a,enrich:b');
t('with no scores (the chat\'s first topic) the pre-turn items no longer lead', D.topicOrder(['enrich:a', 'enrich:b', 'r1', 'r2']).join(',') === 'r1,r2,enrich:a,enrich:b');
t('an isPre callback decides (an anchor from enrich on a plain key)', D.topicOrder(['w', 'r'], null, (k) => k === 'w').join(',') === 'r,w');

/* the topic moved past them */
const items = [
  { key: 'enrich:markets.quotes', from: 'enrich', anchors: ['m1'] },
  { key: 'enrich:markets.news.feed', from: 'prepare', anchors: ['m1'] },
  { key: 'enrich:fabric.query:2c', from: 'prepare', anchors: ['m2'] },   // made for the turn in focus
  { key: 'enrich:pinned', from: 'enrich', anchors: ['m1'], pinned: true },
  { key: 'enrich:locked', from: 'enrich', anchors: ['m1'], locked: true },
  { key: 'enrich:kept', from: 'enrich', anchors: ['m1'], kept: true },
  { key: 'enrich:in', from: 'enrich', anchors: ['m1'] },
  { key: 'reply:old', from: 'm1', anchors: ['m1'] },
];
const out = D.preTurnOut(items, ['reply:new', 'enrich:in'], 'm2');
t('pre-turn items out of the topic and not of the turn in focus leave', out.join(',') === 'enrich:markets.quotes,enrich:markets.news.feed');
t('one made for the turn in focus stays (its anchors hold the turn)', out.indexOf('enrich:fabric.query:2c') < 0);
t('pinned, locked and opened-again ones stay', ['enrich:pinned', 'enrich:locked', 'enrich:kept'].every((k) => out.indexOf(k) < 0));
t('one in the topic stays', out.indexOf('enrich:in') < 0);
t('a reply item is never forced out (it follows the ordinary rules)', out.indexOf('reply:old') < 0);
t('no topic keys and no mid: every unpinned pre-turn item goes', D.preTurnOut([{ key: 'enrich:x' }], [], '').join(',') === 'enrich:x');
t('the item records are not part of the rule (a place by hand is not an input)', !/place|by\b/.test(D.preTurnOut.toString().replace(/\/\*[\s\S]*?\*\//g, '')));

/* the wiring in the canvas element */
t('setTopic orders the topic by VeraDesk.topicOrder with the element\'s own pre-turn test', src.includes('D.topicOrder(ks, val, isPre)') && src.includes('const D = root.VeraDesk, isPre = (k) => this._isPreTurn(k);'));
t('a new topic forgets the pre-turn items opened again under the last one', src.includes('this._preKeep = null; this._preOutC = null;'));
t('the pill view adds the pre-turn items the topic moved past; the stored pills are _pillReal', src.includes('_pillGet() { const m = this._pillReal(), po = this._preOutGet();') && src.includes('const v = Object.assign({}, m); po.forEach((k) => { v[k] = 1; }); return v; }'));
t('_pillSet writes only the person\'s pills; opening a topic pill keeps it in, writing nothing', src.includes('_pillSet(key, on) { const m = this._pillReal();') && src.includes("if (!on && !m[key] && this._preOutGet().indexOf(key) >= 0) { (this._preKeep || (this._preKeep = new Set())).add(key); this._preOutC = null; return; }"));
t('the item records hydrate the person\'s pills only', src.includes('pills = this._pillReal(), views = viewStore(this)'));
t('a click on a topic pill opens it as a window, like any out-of-topic chip', src.includes("!this._pillReal()[wc.dataset.key] && !(this._wins && this._wins.has(wc.dataset.key))"));
t('_preOutGet asks VeraDesk.preTurnOut with the topic\'s keys and mid; off when bring-in is off', src.includes('D.preTurnOut(items, [...T.keys], T.mid)') && src.includes("if (!T || !T.keys || !T.keys.size || this.getAttribute('bring-in') === 'off') return [];"));
t('...locks and pins count', src.includes("pinned: b.state === 'pinned', locked: !!locks[key], kept: keep.has(key)"));
t('_placeDesk still reads the pills through _pillGet (unchanged there)', src.includes("{ const pills = this._pillGet(); const pilled = ranked.filter(") && src.includes('const pilledKey = !lk && !!this._pillGet()[key];'));

/* the chat's first topic (WSU-84 "at once") */
t('the chat\'s first topic takes a pre-turn item only as the turn\'s own, never by a name in its title', H.includes('const keys=bl.filter(b=>own(b)||(named(b)&&!pre(b))).map(b=>String(b.key));') && H.includes("return k.indexOf('enrich:')===0||k.indexOf('pre:')===0||f==='enrich'||f==='prepare'; };"));
t('...and still sets it at once (WSU-84)', H.includes('if(keys.length) cv.setTopic(keys, {}, mid); }catch(_){} }'));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

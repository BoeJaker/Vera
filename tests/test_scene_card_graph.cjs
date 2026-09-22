// A card's own structure, drawn ON the card (EXPLODE.md §8.2): "the chat exploded iso, cards and front modes
// could display the graphs" (owner, 2026-09-22). All three modes draw one FACE per card, so one graph face
// serves all three — and it is ASKED for, never assumed, because a diagram is a server call and a scene of forty
// cards is not forty calls.
//   node tests/test_scene_card_graph.cjs
const path = require('node:path'), fs = require('node:fs');
const SCENE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── what a card must hold to be worth a diagram, as a function ───────────────────────────────────────────────
const src = SCENE.match(/function graphable\(c\) \{[\s\S]*?\n  \}/);
t('the rule for what can be drawn is there to be tested', !!src);
const graphable = src ? eval('(' + src[0] + ')') : () => false;

t('a code card with a body can be drawn', graphable({ kind: 'code', body: 'def f():\n    return 1\n' }));
t('a capability call with its output can be drawn', graphable({ kind: 'cap', body: 'x'.repeat(40) }));
t('an empty code card cannot', !graphable({ kind: 'code', body: '  ' }));
t('a card standing for a record can be drawn', graphable({ kind: 'read', record: { id: 'rec-1' } }));
t('a long passage can be drawn, a short caption cannot',
  graphable({ kind: 'read', body: 'a passage. '.repeat(30) }) && !graphable({ kind: 'read', body: 'two words' }));
t('nothing at all cannot', !graphable(null) && !graphable({}));

// ── the face, and the three modes that share it ──────────────────────────────────────────────────────────────
t('there is one graph face, and it carries the slot the element mounts into',
  /function graphFaceHtml\(id, state, wsz\)/.test(SCENE) && /data-graph="' \+ esc\(id\) \+ '"/.test(SCENE));
t('it says it is drawing, and says when there is nothing to draw',
  /drawing\\u2026/.test(SCENE) && /no diagram for this card/.test(SCENE));
const uses = (SCENE.match(/graphFaceHtml\(c\.id|graphFaceHtml\(c\.id, /g) || []).length;
t('all three modes take it — front, iso and cards', (SCENE.match(/graphFaceHtml\(/g) || []).length >= 4,
  String((SCENE.match(/graphFaceHtml\(/g) || []).length));
t('the affordance is on the card in every mode — iso, cards and front',
  /editBtn\(wg\) \+ gBtn\(wg\)/.test(SCENE) && /editBtn\(c\) \+ gBtn\(c\)/.test(SCENE)
  && /click for the record">' \+ graphBtn\(c\.card,/.test(SCENE));
t('pressing it toggles, and pressing again gives the card back',
  /graph\(id\) \{/.test(SCENE) && /pressed again: the card comes back/.test(SCENE));

// ── asked for, never assumed ─────────────────────────────────────────────────────────────────────────────────
t('the scene asks the HOST rather than the server — it knows no endpoints',
  /new CustomEvent\('vera:xpl:graph'/.test(SCENE) && !/\/code\/explode|\/nlp\/explode/.test(SCENE));
t('the contract comes back through a method, and an error is a state the card can show',
  /setCardGraph\(id, doc\)/.test(SCENE) && /this\._graph\[id\] = 'error'/.test(SCENE));
t('the element is kept per card and re-attached, so a render does not throw away a pan',
  /this\._graphEls\[id\] = el/.test(SCENE) && /if \(el\.parentNode !== slot\)/.test(SCENE)
  && /this\._graphMount\(\)/.test(SCENE));
t('the structured renderer is fetched only when a card asks for it',
  /function ensureStruct\(doc\)/.test(SCENE) && /a scene that never asks for/.test(SCENE));

// ── the host's half ──────────────────────────────────────────────────────────────────────────────────────────
t('the chat listens for it and hands the contract back to that card',
  /_xplEl\.addEventListener\('vera:xpl:graph'/.test(CHAT) && /_xplEl\.setCardGraph\(d\.id, doc\)/.test(CHAT));
t('a record goes through the resolver, code and prose go straight to their cap',
  /async function _xpContractFor\(d\)/.test(CHAT) && /resolveTarget\(\{ id: d\.record/.test(CHAT)
  && /url = '\/code\/explode'/.test(CHAT));

// -- what the live run caught: two modes had the affordance and drew nothing -----------------------------------
t('iso looks the graph up under the ITEM\'s id, which is what data-id carries',
  /const face = \(this\._graph && this\._graph\[wg\.id\]\)/.test(SCENE)
  && /graphFaceHtml\(wg\.id, this\._graph\[wg\.id\]/.test(SCENE));
t('a plain card in iso is answered where it is actually drawn — xitHtml, which gets no face at all',
  /const xitHtml = \(wg, face\) => \{[\s\S]{0,400}?if \(this\._graph && this\._graph\[wg\.id\]\) face = graphFaceHtml\(wg\.id,/.test(SCENE));
t('front rebuilds its carousel when a card is asked to be its diagram — it is built once per station otherwise',
  /\+ '\|' \+ Object\.keys\(this\._graph \|\| \{\}\)\.sort\(\)\.join\(','\)/.test(SCENE));
console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);

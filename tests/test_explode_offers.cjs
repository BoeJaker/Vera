// The last two ways in (EXPLODE.md §9, S6f): the exploded scene's card-level DEEP DIVE, and the canvas OFFERING
// a diagram for code or a passage that has none. Both are rules, so both are run here rather than matched: the
// canvas's `suggestionsOf` is pulled out and called, and the scene's dive is checked as the wiring it is.
//   node tests/test_explode_offers.cjs
const path = require('node:path'), fs = require('node:fs');
const CANVAS = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const SCENE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the canvas's offer, as a function ────────────────────────────────────────────────────────────────────────
const src = CANVAS.match(/function suggestionsOf\(doc, blocks, focusMid\) \{[\s\S]*?\n  \}/);
t('the canvas\'s suggestion rule is there to be tested', !!src);
const suggestionsOf = src ? eval('(' + src[0] + ')') : () => [];

const codeBlock = (key, lines, extra) => Object.assign({ key, type: 'code', state: 'now',
  content: { code: Array.from({ length: lines }, (_, i) => 'x = ' + i).join('\n'), filename: key + '.py' } }, extra || {});

{ const s = suggestionsOf({}, [codeBlock('a', 30)], 'm1');
  t('a code item of more than a screenful offers its own diagram',
    s.length === 1 && s[0].kind === 'explode' && /Explode this code/.test(s[0].n), JSON.stringify(s));
  t('and the offer is BOUND to that item, so the diagram and the source light each other',
    s[0].content.binds === 'a' && s[0].key === 'explode:a', JSON.stringify(s[0].content));
  t('a short snippet is its own best picture — it does not ask',
    suggestionsOf({}, [codeBlock('b', 6)], 'm1').length === 0);
  const already = suggestionsOf({}, [codeBlock('a', 30), { key: 'explode:a', type: 'explode', content: { binds: 'a' } }], 'm1');
  t('what is already exploded does not ask again', already.length === 0, JSON.stringify(already));
  const many = suggestionsOf({}, [codeBlock('a', 30), codeBlock('b', 30), codeBlock('c', 30), codeBlock('d', 30)], 'm1');
  t('a canvas of code offers at most two at once — the rail stays a rail', many.length === 2, String(many.length));
  const prose = suggestionsOf({}, [{ key: 'p', type: 'markdown', content: { md: 'a passage. '.repeat(120) } }], 'm1');
  t('a passage long enough to have structure offers too, as text rather than a binding',
    prose.length === 1 && /passage/.test(prose[0].n) && typeof prose[0].content.text === 'string' && !prose[0].content.binds);
  t('a short note does not', suggestionsOf({}, [{ key: 'n', type: 'note', content: { text: 'remember this' } }], 'm1').length === 0);
  t('the document\'s own suggestions still come first, and none of this changes them',
    suggestionsOf({ suggestions: [{ n: 'Draw the plan', kind: 'diagram' }] }, [codeBlock('a', 30)], 'm1')[0].n === 'Draw the plan'); }

// ── the scene's deep dive ────────────────────────────────────────────────────────────────────────────────────
t('a card double-clicked asks what is IN it', /this\.addEventListener\('dblclick', \(e\) => this\._dive\(e\)\)/.test(SCENE));
t('the scene stays a scene: it says what the card holds and lets the host draw it',
  /new CustomEvent\('vera:xpl:explode'/.test(SCENE) && !/\/code\/explode|\/nlp\/explode/.test(SCENE));
t('what it says is what a card can hold — a record, code with its language, or a passage',
  /record: rec/.test(SCENE) && /code: \/\^\(code\|term\|terminal\|cap\|capability\|log\)\$\//.test(SCENE) && /text: body/.test(SCENE));
t('the chat listens, and resolves an id through the server rather than assuming it is a record',
  /_xplEl\.addEventListener\('vera:xpl:explode'/.test(CHAT) && /async function _xplDeepDive/.test(CHAT)
  && /resolveTarget\(\{ id: d\.record/.test(CHAT));
t('a second dive replaces the first, under the scene', /one dive at a time/.test(CHAT)
  && /host\.querySelector\(':scope > \.xp-inline'\)/.test(CHAT));
t('a card with nothing in it says so, rather than drawing an empty diagram',
  /Nothing to explode in that card/.test(CHAT));

console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);

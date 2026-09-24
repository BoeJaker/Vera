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
const src = CANVAS.match(/function suggestionsOf\(doc, blocks, focusMid(?:, o)?\) \{[\s\S]*?\n  \}/);
t('the canvas\'s suggestion rule is there to be tested', !!src);
const suggestionsOf = src ? eval('(' + src[0] + ')') : () => [];

const codeBlock = (key, lines, extra) => Object.assign({ key, type: 'code', state: 'now',
  content: { code: Array.from({ length: lines }, (_, i) => 'x = ' + i).join('\n'), filename: key + '.py' } }, extra || {});

/* ⛔ THE OFFER IS ON THE ITEM, NOT IN A CARD. It used to arrive as a ghost item in the NOW band — a card, in the
   reader's way, about another card (owner, 2026-09-24). `canExplode` is the same rule; what changed is where it is
   drawn: the item that can be exploded carries the switch, and taking it still builds the bound explode item. */
{ const src2 = CANVAS.match(/function canExplode\(b, offer\) \{[\s\S]*?\n  \}/);
  t('the rule is there to be tested', !!src2);
  const canExplode = src2 ? eval('(' + src2[0] + ')') : () => '';
  t('a code item of more than a screenful can be exploded', canExplode(codeBlock('a', 30), 'both') === 'code');
  t('a short snippet is its own best picture — it does not offer', canExplode(codeBlock('b', 6), 'both') === '');
  t('a passage long enough to have structure offers too',
    canExplode({ key: 'p', type: 'markdown', content: { md: 'a passage. '.repeat(120) } }, 'both') === 'prose');
  t('a short note does not', canExplode({ key: 'n', type: 'note', content: { text: 'remember this' } }, 'both') === '');
  t('the reader\'s setting still narrows it, and "never" silences it',
    canExplode({ key: 'p', type: 'markdown', content: { md: 'a passage. '.repeat(120) } }, 'code') === '' &&
    canExplode(codeBlock('a', 30), 'never') === '');
  // and the switch is drawn on the item, toggling the SAME bound explode item as before
  t('the item carries the switch, and knows which way round it is',
    /const xplodable = !bid && !!canExplode\(b, this\.explodeOffer\(\)\);/.test(CANVAS) &&
    /const xploded = xplodable && xplodedKeys\.has\(String\(b\.key\)\);/.test(CANVAS) &&
    /data-act="explode"/.test(CANVAS));
  t('taking it builds the bound item, and taking it back removes it',
    /this\.call\('canvas\.add', \{ kind: 'explode', key: 'explode:' \+ key, content, at: 'now', size: 'l' \}\)/.test(CANVAS) &&
    /if \(bound\) return this\.call\('canvas\.remove'/.test(CANVAS));
  t('the document\'s own suggestions are untouched by any of it',
    suggestionsOf({ suggestions: [{ n: 'Draw the plan', kind: 'diagram' }] }, [codeBlock('a', 30)], 'm1')[0].n === 'Draw the plan');
  t('and an item no longer adds itself to the band', suggestionsOf({}, [codeBlock('a', 30)], 'm1').length === 0); }

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

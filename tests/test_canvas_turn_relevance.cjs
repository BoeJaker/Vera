// A canvas item stays while its TURN is on screen, not while one message is.
// (vera/chat/chat_panel.html, vera/canvas/canvas_element.js)
//
// The report: "the panels of the canvas are not staying in view as I scroll the message that created them - note
// responses can be made of multiple messages from the llm, if it calls a tool it'll respond to the tool output in
// a second message... because of this the canvas item doesn't stay on the canvas for the second part."
//
// Measured, that is exactly what the code did. itemRects() exposed an item's `mid` and `from` - two single
// values - and the focus pass took `it.from || it.mid`, ONE key, and looked up that one message's visibility.
// The block's `anchors` array, which the server keeps and grows additively and never replaces, was never
// consulted for screen focus at all. So a turn that calls a tool makes two assistant messages, the item is
// anchored to one of them, and scrolling to the other took it off the canvas in the middle of the answer.
//
// Three changes, and this executes the arithmetic of all three:
//   1. the item carries every message it belongs to, not only the last;
//   2. a message reports its TURN's weight - a turn is a question and everything answering it - so any message
//      of a visible turn holds the turn's items;
//   3. a parked item whose turn comes back on screen is RECALLED rather than left lost.
//
//   node tests/test_canvas_turn_relevance.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const cv = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');

// ── cut _cvScreenWeights out and run it against a transcript we lay out by hand ────────────────────────────────
const A = "  function _cvScreenWeights(msgs){", B = "  function _cvScreenFocus(){";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the weighting is present in the page', i0 >= 0 && i1 > i0);
if (i0 < 0 || i1 < i0) { console.log(fails + ' FAILED'); process.exit(1); }
const ctx = {}; vm.createContext(ctx);
vm.runInContext(src.slice(i0, i1) + '\nthis.W=_cvScreenWeights;', ctx);
const W = ctx.W;

// a transcript: a question, the tool call that answers it, the reply to the tool output, then the next question.
// The viewport is 400 tall; each message is 200. Scrolling moves the messages, not the viewport.
function msgs(rows) {
  const VP = { top: 0, bottom: 400, height: 400 };
  const made = rows.map((r) => ({
    dataset: { mid: r.mid },
    classList: { contains: (c) => c === 'u' && !!r.user },
    getBoundingClientRect: () => ({ top: r.top, bottom: r.top + r.h, height: r.h }),
  }));
  return { getBoundingClientRect: () => VP, querySelectorAll: () => made };
}
/* m1 question · m2 tool call · m3 the reply to the tool output · m4 the next question. Each message fills the
   viewport exactly, so "scrolled to m3" means m3 and NOTHING else is on screen - otherwise the next turn is
   genuinely visible too and the case proves nothing. */
const turn = (topOfFirst) => msgs([
  { mid: 'm1', user: true, top: topOfFirst, h: 400 },
  { mid: 'm2', top: topOfFirst + 400, h: 400 },
  { mid: 'm3', top: topOfFirst + 800, h: 400 },
  { mid: 'm4', user: true, top: topOfFirst + 1200, h: 400 },
]);

// ── the bug, in arithmetic ─────────────────────────────────────────────────────────────────────────────────────
{
  // scrolled so ONLY m3 - the reply to the tool output - is on screen
  const w = W(turn(-800));
  t('the message on screen has weight', w.m3 > 0, JSON.stringify(w));
  /* and m2, the tool call that MADE the item, is off screen. Under the old rule the item's single key was m2,
     w.m2 was undefined, and the item left the canvas mid-answer. It inherits its turn's weight now. */
  t('the tool call that made the item is held by its turn', (w.m2 || 0) > 0, 'm2=' + w.m2);
  t('so is the question that opened it', (w.m1 || 0) > 0, 'm1=' + w.m1);
  t('and the NEXT turn is not dragged in with it', !(w.m4 > 0), 'm4=' + w.m4);
}
{
  // scrolled so only the tool call is on screen: the reply, still to come, is part of the same turn
  const w = W(turn(-400));
  t('the whole turn is held from any part of it', (w.m1 || 0) > 0 && (w.m2 || 0) > 0 && (w.m3 || 0) > 0);
  t('and still not the next turn', !(w.m4 > 0), 'm4=' + w.m4);
}
{
  // scrolled right past, onto the next question only
  const w = W(turn(-1200));
  t('a turn scrolled away is not held', !(w.m1 > 0) && !(w.m2 > 0), JSON.stringify(w));
  t('the turn now on screen is', (w.m4 || 0) > 0);
}
{
  // a turn only half in view is weighted, not all-or-nothing - the focus pass ranks by this
  const w = W(turn(-1000));
  t('partial visibility is a fraction, not a flag', w.m3 > 0 && w.m3 < 1, 'm3=' + w.m3);
}

// ── the item carries every message it belongs to ───────────────────────────────────────────────────────────────
{
  t('the element collects the anchors the document kept', /const anchorMids = \(\(\) => \{/.test(cv)
    && /\(b\.anchors \|\| \[\]\)\.concat\(a \? \[a\] : \[\]\)/.test(cv));
  t('every field an anchor names is taken', /\[x\.turn, x\.mid, x\.from, x\.beside\]/.test(cv));
  t('they reach the item', /data-anchors="' \+ esc\(anchorMids\.join\(' '\)\)/.test(cv));
  t('and itemRects reports them', /anchors: String\(el\.dataset\.anchors \|\| ''\)\.split\(' '\)\.filter\(Boolean\)/.test(cv));
  t('the focus pass takes the BEST of them, not the first',
    /const mids=\(it\.anchors&&it\.anchors\.length\?it\.anchors:\[\]\)\.concat\(\[it\.from,it\.mid\]\)/.test(src)
    && /let s=0; mids\.forEach\(k=>\{ const v=w\[k\]; if\(v>s\) s=v; \}\)/.test(src));
}

// ── and a parked item comes back when its turn does ────────────────────────────────────────────────────────────
{
  t('the element can say what is parked and where it belongs', /parkedAnchors\(\) \{/.test(cv)
    && /b\.state !== 'parked'/.test(cv));
  t('a parked item whose turn is on screen is recalled', /cv\.parkedAnchors\(\)\.filter\(p=>p\.anchors\.some\(m=>\(w\[m\]\|\|0\)>=0\.5\)\)/.test(src));
  /* recall, not re-create: canvas.add with the KEY goes through the resolver, so the item keeps its anchors and
     its history instead of arriving as a new thing that has never been seen before */
  t('by key, so it is recalled rather than remade', /_capCall\('canvas\.add',\{ session_id:SID, key:k \}\)/.test(src));
  /* half the screen, or scrolling PAST a turn drags its shelf up behind you; and paced, because this runs on
     every scroll frame and a recall is a write */
  t('it takes half the screen to bring one back', />=0\.5/.test(src));
  t('and they come back one at a time, with a gap', /Date\.now\(\)-_cvRecallT>1200/.test(src) && /_cvRecallT=Date\.now\(\)/.test(src));
  t('one that failed to come back is not marked as returned', /catch\(\(\)=>\{ delete _cvRecalled\[k\]; \}\)/.test(src));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// The relation runs follow a GLIDE, not just an event (Notes/42 defect 96; vera/chat/chat_panel.html).
// A run lands on the exploded view's pan container (transition:transform .38s) and the canvas stage's items
// (transition:top .32s,left .32s). Every redraw hangs off a discrete event, and 'vera:canvas:placed' fires as the
// new top/left go on — the START of the glide — so the runs were drawn to where the items still were and then
// stood still for a third of a second while the items travelled out from under them. That is the tearing.
// getBoundingClientRect() reports a transitioning element where it is NOW, so the cure is to redraw while it
// glides: a transitionstart holds a frame loop open for that transition's own computed duration.
// This runs the text the page actually ships — the driver is cut out of chat_panel.html by its markers.
//   node tests/test_chat_runs_glide.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const A = "let _runsHoldT=0, _runsHoldRaf=0;", B = "document.addEventListener('transitionstart', _runsGlide, true);";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the glide driver is present in the page', i0 >= 0 && i1 > i0);
if (i0 < 0 || i1 < i0) { process.exit(1); }
const block = src.slice(i0, i1 + B.length);

// a clock and a frame queue we drive by hand, so the test never waits on real time
let NOW = 1000; const FakeDate = { now: () => NOW };
let queue = []; const raf = (fn) => { queue.push(fn); return queue.length; };
const frame = (ms) => { NOW += (ms == null ? 16 : ms); const q = queue; queue = []; q.forEach((f) => f()); };
// let a running follow finish: the clock is pushed well past any hold so the loop closes itself. Emptying the
// queue by hand instead would leave the driver believing a loop is still live, and it would refuse the next one.
const drain = () => { for (let i = 0; i < 200 && queue.length; i++) { NOW += 1000; const q = queue; queue = []; q.forEach((f) => f()); } };
let ctxDraws = 0, cvDraws = 0;
let bound = null; const doc = { addEventListener: (type, fn) => { if (type === 'transitionstart') bound = fn; } };
// fake elements: each knows the selectors it or an ancestor matches, and carries its own computed style
const mkEl = (matches, cs) => { const o = { nodeType: 1, cs: cs || {}, closest: (q) => (q.split(',').map((s) => s.trim()).some((s) => matches.includes(s)) ? o : null) }; return o; };
const gcs = (el) => { if (!el || el.nodeType !== 1 && !el.cs) throw new TypeError('not an element'); return el.cs || {}; };   // the browser throws on a non-element, and the driver must survive that
const api = new Function('document', 'getComputedStyle', 'requestAnimationFrame', 'Date', '_ctxRunsDraw', '_cvRunsDraw',
  block + '\nreturn { _runsFollowFrames, _trMs, _runsGlide };')(doc, gcs, raf, FakeDate, () => { ctxDraws++; }, () => { cvDraws++; });
t('the driver binds itself to transitionstart on the document', typeof bound === 'function');

// _trMs — the glide's own length, read off the element, so retiming the CSS needs no change here
const ms = (prop, dur, delay, p) => api._trMs({ cs: { transitionProperty: p || prop, transitionDuration: dur, transitionDelay: delay || '0s' } }, prop);
t('.xp-view transform .38s reads as 380ms', ms('transform', '0.38s') === 380);
t('.stage .it top/left .32s reads as 320ms on each', ms('top', '0.32s, 0.32s', '0s, 0s', 'top, left') === 320 && ms('left', '0.32s, 0.32s', '0s, 0s', 'top, left') === 320);
t('milliseconds are not multiplied', ms('transform', '250ms') === 250);
t('a delay counts toward the hold', ms('transform', '0.2s', '100ms') === 300);
t('the property picks its OWN slot, not the first', ms('left', '1s, 0.4s', '0s, 0s', 'top, left') === 400);
t('an unlisted property falls back to the all slot', ms('top', '0.5s', '0s', 'all') === 500);
t('an unreadable element does not throw', api._trMs(null, 'top') === 400);

// _runsGlide — what starts a follow, and what must not
const fire = (propertyName, target, first) => { const before = ctxDraws; bound({ propertyName, target, composedPath: () => [first || target, target] }); return ctxDraws !== before || queue.length > 0; };
const xpView = mkEl(['vera-exploded', '.xp-view'], { transitionProperty: 'transform', transitionDuration: '0.38s', transitionDelay: '0s' });
const stageIt = mkEl(['.stage'], { transitionProperty: 'top, left', transitionDuration: '0.32s, 0.32s', transitionDelay: '0s, 0s' });
const host = mkEl(['vera-canvas'], {});
const outside = mkEl(['#msgs', '.mwrap'], { transitionProperty: 'transform', transitionDuration: '1s', transitionDelay: '0s' });
drain();
t('an opacity transition starts nothing', !fire('opacity', xpView));
drain();
t('a transition outside the three surfaces starts nothing', !fire('transform', outside));
drain();
t('the exploded view panning starts a follow', fire('transform', xpView));
drain();
// the canvas keeps its items in a shadow root: the event is retargeted to the host, the item is on the composed path
t('a canvas item gliding under a retargeted event starts a follow', fire('top', host, stageIt));
drain();
t('the same event seen from inside the shadow root starts a follow', fire('top', stageIt, stageIt));

// _runsFollowFrames — redraw every frame for the hold, then stop dead
drain(); ctxDraws = 0; cvDraws = 0;
api._runsFollowFrames(100);
let frames = 0; for (let i = 0; i < 40 && queue.length; i++) { frame(16); frames++; }
t('both overlays are redrawn every frame while the glide runs', ctxDraws === frames && cvDraws === frames && frames >= 6 && frames <= 8, 'frames=' + frames + ' ctx=' + ctxDraws);
t('the loop stops itself once the glide has ended — nothing is left scheduled', queue.length === 0);

// idle is genuinely idle: with nothing animating, not one frame is scheduled
drain(); ctxDraws = 0;
t('nothing animating schedules no frames at all', queue.length === 0 && ctxDraws === 0);

// a second glide starting mid-flight extends the hold instead of stacking a second loop
drain(); ctxDraws = 0;
api._runsFollowFrames(100);
t('one loop, one scheduled frame', queue.length === 1);
api._runsFollowFrames(100);
t('a second call while running does not stack a second loop', queue.length === 1);
frame(16); api._runsFollowFrames(200);
let n = 0; for (let i = 0; i < 60 && queue.length; i++) { frame(16); n++; }
t('the later glide extends the hold past where the first would have ended', n >= 12, 'n=' + n);
t('and it still stops', queue.length === 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// The session canvas column as the Canvas board draws it (vera/canvas/canvas_element.js): the add bar, the NOW band
// (the decision this turn waits on with its answers; what Vera can also do), items that fold to a header line in
// Hover and Zen and open in place, the aged/ghost/now states, the corner grip's size — the pure helpers, and the
// source strings the column is held together by. Nothing the element had is gone: place · checkRoutes · the
// controls · the events.   node tests/test_canvas_column_board.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the decision this turn is waiting on ──
const loopWait = { key: 'loop:r7', type: 'loop', state: 'now', ts: '2026-09-12T14:41:00', content: { goal: 'verify the digest gate', status: 'waiting', run: 'r7', steps: [{ n: 'recall', status: 'ok' }, { n: 'test', status: 'running' }],
  ask: { question: 'Full suite (≈4 min) or only the boot path (≈40 s)?', options: ['Full suite', { n: 'Boot path only', v: 'boot' }], why: 'loop v7 · step 5 is waiting on you' } } };
const d = V.decisionOf(loopWait);
t('a loop waiting on the user is a decision: its question, its answers, its reason', d && d.question.startsWith('Full suite') && d.options.length === 2 && d.options[1].v === 'boot' && d.options[0].v === 'Full suite' && d.why === 'loop v7 · step 5 is waiting on you' && d.run === 'r7' && !d.answer);
t('a note that is a question with choices is a decision too', (V.decisionOf({ key: 'note:q', type: 'note', content: { question: 'ship it?', choices: ['yes', 'no'] } }) || {}).options.length === 2);
t('a loop whose status is waiting is a decision even without a question object', !!V.decisionOf({ key: 'loop:x', type: 'loop', content: { goal: 'g', status: 'hitl', prompt: 'approve the merge?' } }));
t('an answered decision keeps its answer', V.decisionOf({ key: 'k', type: 'note', content: { ask: { question: 'q', answer: 'boot', answered: '2026-09-12T14:42:00' } } }).answer === 'boot');
t('an ordinary item is not a decision', V.decisionOf({ key: 'code:f', type: 'code', content: { code: 'x' } }) === null && V.decisionOf(null) === null);

// ── what Vera can also do ──
const doc = { mode: 'session', suggestions: ['open ssh to ct126', { n: 'pin the boot log', kind: 'note', key: 'note:boot-log', content: { text: 'boot log' } }] };
const blocks = [loopWait, { key: 'note:boot-log', type: 'note', state: 'now', content: { text: 'boot log' } }, { key: 'widget:gpu', type: 'widget', state: 'now', anchor: { turn: 'm2' }, content: { widget: 'gpu', suggestions: [{ n: 'watch the embed queue', kind: 'widget', content: { widget: 'queue' } }] } }];
const S = V.suggestionsOf(doc, blocks, 'm4');
t('suggestions come from the document and from live items, each naming the item it becomes', S.length === 3 && S[0].kind === 'note' && S[0].key === 'note:open-ssh-to-ct126' && S[0].mid === 'm4' && S[2].kind === 'widget' && S[2].mid === 'm2' && S[2].content.widget === 'queue');
t('one already on the canvas is taken (its key exists)', S[1].taken === true && S[0].taken === false && S[2].taken === false);

// ── the NOW bar's words ──
/* The bar says only what the items cannot say for themselves. It used to count them — "3 now · 2 in focus" over a
   column in which those three are visible — and the owner asked for that to go (2026-09-23); an empty bar is not
   drawn at all. What is still worth a line: this turn is waiting on you, the answer went, and what else Vera could
   put here. */
t('waiting: the time, the input, the suggestions', /^14:41 · waiting on you · 1 input · 3 suggested$/.test(V.nowText([loopWait], d, S)));
t('answered says so, and no longer counts what is on the canvas', V.nowText([loopWait], Object.assign({}, d, { answer: 'boot', answered: '' }), []) === 'answered');
t('live items without a decision: nothing to say, so the bar is not drawn', V.nowText([1, 2, 3], null, []) === '');
t('nothing live either: still nothing to say', V.nowText([], null, []) === '');
t('but what Vera could also put here is always worth a line', V.nowText([1, 2], null, S) === '3 suggested');

// ── folding, ageing, the grip's size ──
const order = V.turnOrder({ m1: { top: 0 }, m3: { top: 400 }, m2: { top: 120 }, m4: { top: 600 }, m5: { top: 800 } });
t('turns in order of their measured tops', order.join(',') === 'm1,m2,m3,m4,m5');
t('an item from a turn more than two behind the focus has aged; a nearer one has not', V.isAged('m1', 'm4', order) && !V.isAged('m2', 'm4', order) && !V.isAged('m4', 'm4', order) && !V.isAged('', 'm4', order) && !V.isAged('m1', 'zz', order));
t('Full keeps items open; Hover and Zen fold them; an aged item folds in every tier', !V.foldOf({ tier: 'full' }) && V.foldOf({ tier: 'hover' }) && V.foldOf({ tier: 'zen' }) && V.foldOf({ tier: 'full', aged: true }));
t('an opened, a hovered or a NOW item never folds', !V.foldOf({ tier: 'zen', open: true }) && !V.foldOf({ tier: 'hover', hovered: true }) && !V.foldOf({ tier: 'zen', aged: true, now: true }));
t('a dragged height becomes the size record', V.sizeOfHeight(60) === 's' && V.sizeOfHeight(150) === 'm' && V.sizeOfHeight(300) === 'l' && V.sizeOfHeight(500) === 'xl');

// ── the add bar: real block types through canvas.add ──
const kinds = V.ADD_KINDS.map(k => k.n);
t('the add bar is the board\'s row: note · terminal · panel · widget · chart', kinds.join(' · ') === 'note · terminal · panel · widget · chart');
const REAL = ['markdown', 'code', 'diagram', 'image', 'note', 'table', 'widget', 'session', 'schedule', 'html', 'loop', 'notebook', 'panel'];   // BLOCK_TYPES, canvas_capabilities.py
t('every kind is a real block type with its seed content', V.ADD_KINDS.every(k => REAL.includes(k.kind) && k.content && typeof k.content === 'object'));
t('a terminal is a session item; a chart is a widget record with its form', V.ADD_KINDS.find(k => k.n === 'terminal').kind === 'session' && V.ADD_KINDS.find(k => k.n === 'chart').content.draw.form === 'trace');
t('a new note opens for editing', V.ADD_KINDS.find(k => k.n === 'note').edit === true);

// ── the source: the column's parts are widgets or record-backed (data-w), and nothing the element had is gone ──
for (const s of ['data-w="canvas.add"', 'data-w="canvas.now"', 'data-w="canvas.decision"', 'data-w="canvas.suggest"', 'data-w="canvas.item.rail"', 'data-w="canvas.size"', 'data-w="canvas.update"']) t('labelled: ' + s, SRC.includes(s));
for (const s of ['class="addbar"', 'class="add" data-act="add" data-kind="${k.n}"', 'class="askb"', 'class="sugb"', 'class="rz"', 'class="it-ft"', "' compact'", "' openin'", "' aged'", "' waiting'", 'class="it now ghost"', '.it.compact .it-bd,.it.compact .it-ft,.it.compact .rz{display:none}', '.it.aged{opacity:.55}', '.it.ghost{background:transparent;border:1px dashed', '@keyframes waitring', 'cursor:nwse-resize']) t('drawn: ' + s, SRC.includes(s));
for (const s of ['data-act="pin"', 'data-act="park"', 'data-act="size"', 'data-act="remove"', 'data-act="edit"', 'data-act="ctx"', 'data-act="open"', 'data-act="answer"', 'data-act="take"', 'data-act="save"', 'class="hidbtn" data-act="${n}"', "shelf('hid', 'hidden',", "shelf('parkpop', 'parked',", 'class="band pinned"', 'class="band now"', "<b>${plainDoc ? 'BLOCKS' : 'NOW'}</b>", 'class="band parked"', 'class="chips"', '<div class="stage" id="stage">']) t('control: ' + s, SRC.includes(s));
for (const s of ['vera:canvas:rendered', 'vera:canvas:placed', 'vera:canvas:hover', 'vera:canvas:answer', 'vera:canvas:suggest', 'vera:canvas:add', 'vera:canvas:resized', 'vera:canvas:open', 'vera:canvas:edited']) t('event: ' + s, SRC.includes("'" + s + "'"));
for (const s of ["'canvas.add'", "'canvas.pin'", "'canvas.park'", "'canvas.remove'", "'canvas.size'", "'canvas.update'"]) t('through the capability: ' + s, SRC.includes("this.call(" + s) || SRC.includes("this.call(it && it.classList.contains('pinned') ? 'canvas.add' : 'canvas.pin'"));
for (const s of ['setTurns(turns, o) {', 'syncScroll(msgsScrollTop, msgsTopClient) {', 'itemRects() {', '_placeNow() {', 'setFocus(keys, scores) {', 'retier() {', 'tier() {', 'static get observedAttributes() {', 'root.VeraCanvas = Object.assign(root.VeraCanvas || {}, api);', "const fcls = inF ? (F ? ' inf' : '') : (tier === 'zen' ? ' out' : ' dim');", '.it.out{opacity:.45;transition:opacity .15s}', 'loop: c => {', 'class="vc-steps"']) t('kept: ' + s, SRC.includes(s));
t('the API keeps the placer and the checker and adds the column\'s helpers', typeof V.place === 'function' && typeof V.checkRoutes === 'function' && V.ITEM_SIZES.join('') === 'smlxl' && V.version >= 2);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

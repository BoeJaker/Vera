// THREE REPORTS (owner, 2026-09-24):
//   1. "ive just tested a mermaidchat and it still goes to the canvas after its been fuly rendered in chat"
//      — the WRITES were already streaming (canvas.timeline: 1 add + 13 updates, ~450ms apart). What could not
//        stream was the DRAWING: the canvas mounted the diagram with render(), which only draws a complete diagram,
//        so every partial source threw and the item stayed blank until the last token.
//   2. "i dont see an option to only display canvas compatible elements in the canvas (and not the chat) ideally
//      this should be the default if the canvas is open and if the canvas is closed everything can move back into
//      the chat inline."
//   3. "i want the canvas to choose its column count from the width, and the content of the canvas."
//   node tests/test_canvas_stream_draw_and_auto_cols.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── 1. the diagram is DRAWN as it streams, not at the end ───────────────────────────────────────────────────── */
// vera-mermaid has both: render(code) draws a complete diagram and throws on a partial one; stream(code) draws the
// complete lines and holds the partial one. The chat's live fence has always used stream; the canvas used render.
t('the canvas mounts a diagram with stream() when the element has it',
  /if \(typeof inner\.stream === 'function'\) \{ try \{ inner\.stream\(code\); \} catch \(e\) \{ try \{ inner\.render\(code\); \} catch \(_\) \{\} \} return; \}/.test(SRC));
t('...falling back to render() for an element that cannot stream, so an older lib still draws',
  /if \(typeof inner\.render === 'function'\) \{ try \{ inner\.render\(code\); \} catch \(e\) \{\} return; \}/.test(SRC));
t('the stream branch comes FIRST — the order is the whole fix',
  SRC.indexOf("typeof inner.stream === 'function'") < SRC.indexOf("if (typeof inner.render === 'function') { try { inner.render(code)"));
t('an updated slot re-mounts through the same path, so an update streams exactly like the first write',
  /else if \(kind === 'mermaid'\) \{ if \(h\.textContent\) h\.textContent = ''; this\._mermaidInto\(el\.firstChild, key\);/.test(SRC));

/* ── 2. ONE PLACE: on the canvas while the canvas is open, back in the reply when it closes ──────────────────── */
t('the reader chooses where canvas-compatible elements are drawn, and the canvas is the default',
  /<select id="cfgCvWhere"[^>]*><option value="canvas">On the canvas, while it is open<\/option><option value="both">In the chat as well<\/option>/.test(CHAT)
  && /function _cvOneplace\(\)\{ const s=document\.getElementById\('cfgCvWhere'\); return \(s&&s\.value\)\|\|'canvas'; \}/.test(CHAT));
t('it is a PAGE-side setting — no attribute on the element (the element draws either way)',
  /cfgCvWhere:\['', 'canvas'\]/.test(CHAT));
t('the harvest keeps hold of the element each item came from, or there is nothing to hide',
  /out\[j\]\._el=el/.test(CHAT));
t('landing an item on the canvas hides the piece of the reply that made it', /el\.classList\.add\('cv-elsewhere'\); el\.dataset\.cvKey=key;/.test(CHAT)
  && /\.cv-elsewhere\{display:none!important\}/.test(CHAT));
t('...and leaves a line in its place that takes you to it on the canvas',
  /chip\.textContent=\(m\.n\?String\(m\.n\)\.slice\(0,48\):'item'\)\+' · on the canvas ↗';/.test(CHAT));
// both an add and an update go through it — but only once the reply is FINISHED: swapping the element for a line while
// the stream rebuilds that markup on every token is what made both columns flicker (owner, 2026-09-25)
t('both an ADD and an UPDATE go through it, and neither does while the reply is still streaming',
  /if\(!live\) _cvOnCanvas\(m, key\);          \/\/ finished: it is over there/.test(CHAT)
  && /if\(r&&r\.ok\)\{ _CV_SIG\[key\]=sig; if\(!live\) _cvOnCanvas\(m, key\); \}/.test(CHAT));
t('nothing is hidden while the column is CLOSED, or the reply would lose elements with no canvas to show them',
  /function _cvOneplaceOff\(\)\{ return !_pages\.has\('canvas'\) \|\| _cvOneplace\(\)==='both'; \}/.test(CHAT));
t('closing the column brings every one of them back inline — a view of the reply, never an edit of it',
  /document\.querySelectorAll\('#msgs \.cv-elsewhere'\)\.forEach\(el=>el\.classList\.remove\('cv-elsewhere'\)\)/.test(CHAT)
  && /document\.querySelectorAll\('#msgs \.cv-moved'\)\.forEach\(el=>el\.remove\(\)\)/.test(CHAT));
t('...on the page toggle and on a settings change alike', (CHAT.match(/_cvOneplaceSync\(\)/g) || []).length >= 3);

/* ── 3. HOW MANY COLUMNS: the width says how many fit, the content says how many are wanted ──────────────────── */
const chips = (n, d) => Array.from({ length: n }, () => d || { size: 'xs' });
t('an empty canvas is one column', V.autoCols(1200, []) === 1);
t('one diagram in a wide stage is still one column — three columns with one thing in them is the empty grid',
  V.autoCols(1200, [{ type: 'diagram', size: 'm' }]) === 1, String(V.autoCols(1200, [{ type: 'diagram', size: 'm' }])));
t('a dozen sticker-sized widgets in the same stage take two', V.autoCols(1200, chips(12)) === 2, String(V.autoCols(1200, chips(12))));
t('a wall of code and tables takes everything the width allows',
  V.autoCols(1200, chips(6, { type: 'code' })) === 3 && V.autoCols(2400, chips(8, { type: 'code' })) === 4,
  JSON.stringify([V.autoCols(1200, chips(6, { type: 'code' })), V.autoCols(2400, chips(8, { type: 'code' }))]));
t('a NARROW stage is one column whatever is on it — a column under 300px stops being readable',
  V.autoCols(320, chips(20, { type: 'code' })) === 1 && V.autoCols(640, chips(20, { type: 'code' })) === 2,
  JSON.stringify([V.autoCols(320, chips(20, { type: 'code' })), V.autoCols(640, chips(20, { type: 'code' }))]));
t('never more columns than there are items', V.autoCols(2400, chips(2, { type: 'code', size: 'xl' })) === 2);
t('the count is capped at four however wide the stage is', V.autoCols(6000, chips(40, { type: 'code' })) === 4);

// the widths the count is chosen from are the SAME widths the placer lays out at
t('a kind you read wants a reading width; a kind you glance at wants a share of a column',
  V.unitsOf({ type: 'code' }) === 2 && V.unitsOf({ size: 'l' }) === 2 && V.unitsOf({ size: 's' }) === 0.5
  && V.unitsOf({ size: 'xs' }) === 1 / 3 && V.unitsOf({}) === 1);
t('an item folded to its header line is a chip, whatever kind it is', V.unitsOf({ type: 'code', folded: true }) === 1 / 3);
t('an item you opened in place or dragged takes the stage', V.unitsOf({ type: 'table', open: true }) === 4 && V.unitsOf({ size: 'xl' }) === 4);
t('one classifier, read by both the chooser and the placer — the count and the widths cannot disagree',
  /const u = unitsOf\(descOf\(c\)\); return u > 1 \? Math\.max\(1, Math\.min\(cols, Math\.round\(u\)\)\) : u;/.test(SRC)
  && /const area = list\.reduce\(\(a, d\) => a \+ Math\.min\(2, unitsOf\(d\)\), 0\);/.test(SRC));

/* ── the element and the banner ──────────────────────────────────────────────────────────────────────────────── */
t('auto is the element\'s default, and 1-4 pin the count',
  /const attr = String\(this\.getAttribute\('columns'\) \|\| 'auto'\)\.trim\(\)\.toLowerCase\(\);/.test(SRC)
  && /const cols = pinned \|\| autoCols\(W, cards\.map\(descOf\), \{ gap \}\);/.test(SRC));
t('the cards are read BEFORE the count is chosen — it is chosen from them',
  SRC.indexOf('const cards = [...st.querySelectorAll(\'.it\')];') < SRC.indexOf('const cols = pinned || autoCols('));
t('a change to the stage\'s WIDTH re-places, since the width is an input now',
  /this\._stageRO = new ResizeObserver\(\(\) => \{/.test(SRC) && /if \(ww && ww !== this\._stageW\) this\._placeNow\(\);/.test(SRC));
t('...width only: the height this pass writes would otherwise loop', /this\._stageW = W;/.test(SRC));
t('the chosen count is published — on the element and in the placed event',
  /this\.dataset\.cols = String\(cols\)/.test(SRC) && /columns: cols, auto: !pinned,/.test(SRC));
t('the banner offers AUTO alongside 1-4, and remembers which you picked',
  /data-cols="auto" onclick="CH\._cvColumns\('auto'\)"/.test(CHAT)
  && /localStorage\.getItem\('vera:canvas:cols'\)\|\|'auto'/.test(CHAT));
t('AUTO says what it chose, refreshed only when that count actually moves',
  /a\.textContent = v==='auto' \? \('auto'\+\(\(cv&&cv\.dataset\.cols\)\?' '\+cv\.dataset\.cols:''\)\) : 'auto';/.test(CHAT)
  && /const n=\(ev\.detail\|\|\{\}\)\.columns; if\(n!==_cvColShown\)\{ _cvColShown=n; _cvColsSync\(\); \}/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

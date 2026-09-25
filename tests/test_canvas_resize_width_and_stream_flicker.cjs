// TWO REPORTS (owner, 2026-09-25):
//   1. "if i resize an element in the canvas it takes the full width and cant be shrunk in width."
//      The grip's cursor has always said nwse-resize, but only clientY was ever read: the height followed the mouse
//      and the WIDTH was whatever the placer handed out — and a dragged item asked for the stage, so that was every
//      column of it. Dragging left could not shrink it because the width was never the mouse's to set.
//   2. "when the content is streamed to the canvas they flicker in the chat and canvas as it streams in and its
//      nowhere near as smooth as it was in chat alone."
//      Two separate churns, one per column: the canvas replaced the whole column's markup on every write even when
//      the markup was identical (so the live overlay was re-laid over new boxes several times a second), and the chat
//      hid the streaming element and inserted an "on the canvas" line on every beat, into markup that the stream
//      rebuilds on every token.
//   node tests/test_canvas_resize_width_and_stream_flicker.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const at = (P) => Object.fromEntries(P.placements.map((p) => [p.key, p]));
// three columns of 300 with 10px gaps: a 920px stage
const COL = { columns: 3, gap: 10, colWidth: 300, viewport: 600, pad: 0 };

/* ── 1. a dragged width is a number of pixels, and it outranks every share and every span ───────────────────── */
{
  const P = V.place([{ key: 'a', h: 200, want: 4, wpx: 380 }], {}, COL);
  t('an item with a dragged width keeps that width, not the whole stage',
    at(P).a.w === 380, JSON.stringify(at(P).a));
  const Q = V.place([{ key: 'a', h: 200, want: 4, wpx: 140 }], {}, COL);
  t('...and it can be SHRUNK below one column — the report', at(Q).a.w === 140, JSON.stringify(at(Q).a));
  // 380px is a column and a bit: it covers two, and is cut to neither
  t('it covers the columns it reaches into, and is cut to none of them',
    at(P).a.span === 2 && at(P).a.w === 380, JSON.stringify(at(P).a));
  // and a pixel width DEFINES the want: a caller that sends both cannot disagree with itself
  t('the want the placer reports is the one the pixels imply, not the one it was handed',
    Math.abs(at(P).a.want - 390 / 310) < 0.001, String(at(P).a.want));
  const R = V.place([{ key: 'a', h: 200, want: 4, wpx: 5000 }], {}, COL);
  t('never wider than the stage there is', at(R).a.w === 920, String(at(R).a.w));
  // and a normal item still snaps to its share or its span, which is what the grid is for
  const S = V.place([{ key: 'x', h: 200, want: 2 }, { key: 'y', h: 50, want: 0.5 }], {}, COL);
  t('an item with no dragged width is unchanged: a span of whole columns, a share of one',
    at(S).x.w === 610 && at(S).y.w === 140, JSON.stringify([at(S).x.w, at(S).y.w]));
  // the flow still works around one: two half-column items go beside each other
  const F = V.place([{ key: 'p', h: 40, want: 0.5 }, { key: 'q', h: 40, want: 0.5 }], {}, COL);
  t('and things still sit beside each other', at(F).q.beside === true);
}

/* ── the element: the drag reads both axes, and the placer stops fighting the mouse ──────────────────────────── */
t('the grip records where the pointer started in BOTH axes, and how wide the card was',
  /x0: ev\.clientX, w0: it\.offsetWidth, w: it\.offsetWidth \}/.test(SRC));
t('a drag sets the width as well as the height',
  /const w = Math\.max\(120, r\.w0 \+ \(ev\.clientX - r\.x0\)\); r\.w = w; r\.el\.style\.width = w \+ 'px';/.test(SRC));
t('the drop remembers it', /this\._px\[key\] = r\.h; this\._pw\[key\] = r\.w;/.test(SRC) && /this\._pw = \{\};/.test(SRC));
t('THE DRAG IN PROGRESS COUNTS TOO, or the placer sets the card back on the next frame and the mouse does nothing',
  /const live = this\._rz && this\._rz\.el === c \? this\._rz\.w : 0;/.test(SRC));
t('a dragged width beats the share the kind would ask for',
  /const wants = cards\.map\(\(c, i\) => pws\[i\] \? Math\.min\(cols, \(pws\[i\] \+ gap\) \/ \(w \+ gap\)\) : wantOf\(c\)\);/.test(SRC)
  && /if \(pws\[i\]\) \{ c\.style\.width = pws\[i\] \+ 'px'; return; \}/.test(SRC));
t('...and the content-fitting pass leaves a card you sized alone', /if \(pws\[i\] \|\| wants\[i\] >= 1 \|\|/.test(SRC));
t('it survives the markup being rewritten', /data-pw="' \+ Math\.round\(pw\) \+ '"/.test(SRC) && /parseInt\(c\.dataset\.pw \|\| '0', 10\)/.test(SRC));
t('and the placer is given it, so what it reports is what the card has', /wpx: pws\[ci\] \|\| 0,/.test(SRC));
t('cycling the size button throws the dragged width away with the dragged height',
  /delete this\._px\[key\]; delete this\._pw\[key\];/.test(SRC) && /this\._px = \{\}; this\._pw = \{\};/.test(SRC));

/* ── 2. the canvas does not redraw markup it already has ─────────────────────────────────────────────────────── */
t('the column\'s markup is written only when it is different',
  /if \(this\._html !== mark \|\| !layers\.items\.childElementCount\) \{/.test(SRC));
t('...and what changed in a streamed write is the LIVE content, which the overlay takes',
  SRC.indexOf('if (this._html !== mark') < SRC.indexOf('this._mountLive(body);'));
t('another canvas\'s identical markup is not this canvas\'s',
  /const mark = this\.canvasId \+ '\\u0000' \+ html;/.test(SRC));
t('an empty column is always written, whatever the cache says', /!layers\.items\.childElementCount/.test(SRC));
t('the reader\'s scroll is still kept when it IS rewritten', /if \(!stage\) body\.scrollTop = keepTop;/.test(SRC));

/* ── 3. the chat swaps the element for a line ONCE, when the message is finished ─────────────────────────────── */
t('a live land does not touch the reply — it is still being written',
  /Promise\.resolve\(_cvLandFrom\(bubEl, _cvRelMid, \(bubEl\.dataset&&bubEl\.dataset\.mid\)\|\|_cvRelMid, true\)\)/.test(CHAT)
  && /async function _cvLandFrom\(body, mid, from, live\)\{/.test(CHAT));
t('...on the add and on the update alike',
  /if\(!live\) _cvOnCanvas\(m, key\);/.test(CHAT) && /if\(r&&r\.ok\)\{ _CV_SIG\[key\]=sig; if\(!live\) _cvOnCanvas\(m, key\); \}/.test(CHAT));
t('the finished reply still swaps it, so "draw them on the canvas" still means that',
  /return _cvLandFrom\(body, mid, \(w&&w\.dataset&&w\.dataset\.mid\)\|\|mid\);/.test(CHAT));
t('and the writes themselves are unchanged: it still streams to the canvas every beat',
  /if\(_cvLiveBusy\|\|Date\.now\(\)-_cvLiveAt<450\) return;/.test(CHAT)
  && /_capCall\('canvas\.update',\{session_id:SID, key, content:m\.content\}\)/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

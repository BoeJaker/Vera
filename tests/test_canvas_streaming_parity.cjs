// THREE REPORTS (owner, 2026-09-24): "the streaming parity isnt working", "seem to have broken the in chat and
// session widgets when the llm creates them", "if i resize a canvas element the boarder comes back and does not go
// unless i click the element after the resize".
//   node tests/test_canvas_streaming_parity.cjs
const path = require('node:path'), fs = require('node:fs');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const WEL = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const WREC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_record.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── 1. a widget the model writes is drawn, not emptied ──────────────────────────────────────────────────────── */
// The forms list IS the model's menu. A form whose data comes from a capability cannot be written by hand, so
// offering it gets it picked for an ordinary chart — and every such widget draws its empty state, in the reply and
// on the canvas alike. That is one added form breaking every widget the model wrote.
t('a form can declare that its data is DERIVED, not authored', /boards=\(\), derived=False:/.test(WREC) && /"derived": bool\(derived\)/.test(WREC));
t('the structured graph is one: its data is the Explode contract', /_F\("structgraph", "graph"[\s\S]{0,200}derived=True\)/.test(WREC));
t('the element reports it, so every picker can leave it out', /const DERIVED = new Set\(\['structgraph'\]\);/.test(WEL) && /derived: DERIVED\.has\(id\)/.test(WEL));
t('the MODEL\'S menu leaves derived forms out', /VeraWidget\.forms\(\)\.filter\(f=>f&&!f\.derived\)\.map\(f=>f\.id\)/.test(CHAT));
t('...but a record that ARRIVES with one still renders — knowing a form is not the same as offering it',
  /function _widgetFormKnown\(f\)\{[\s\S]{0,260}VeraWidget\.forms\(\)\.map\(x=>x&&x\.id\)/.test(CHAT));

/* ── 2. the ring that came back after a resize ───────────────────────────────────────────────────────────────── */
// a drag marks the item open (it has its own height now), so the openin ring appeared the moment the drag ended
// and stayed until the item was clicked
t('an opened item draws no ring', /\.it\.openin\{\}/.test(SRC) && !/\.it\.openin\{box-shadow/.test(SRC));
t('...in blocks-off either', !/:host\(\[blocks="off"\]\) \.it\.openin\{box-shadow/.test(SRC));
t('an item WAITING on you is still the one thing that rings', /\.it\.waiting\{animation:waitring/.test(SRC));
t('and an opened item is still unbounded and fills the card — the ring was the only thing removed',
  /\.it\.sized \.it-bd,\.it\.openin \.it-bd\{max-height:none!important;display:flex/.test(SRC));

/* ── 3. streaming parity ─────────────────────────────────────────────────────────────────────────────────────── */
t('the harvest runs on the STREAM, from the paint that draws each token', /_paintStream\(bubEl, fullText\);\s*\n\s*try\{ _cvLandLive\(bubEl\); \}catch\(_\)\{\}/.test(CHAT));
t('it is throttled to a beat, and never runs two at once', /if\(_cvLiveBusy\|\|Date\.now\(\)-_cvLiveAt<450\) return;/.test(CHAT) && /_cvLiveBusy=true;/.test(CHAT));
t('it does nothing when the canvas column is closed', /if\(!SID\|\|!bubEl\|\|!_pages\.has\('canvas'\)\) return;/.test(CHAT));
t('one function lands from either source — the stream and the finished reply cannot drift apart',
  /async function _cvLandFrom\(body, mid, from\)\{/.test(CHAT) && /return _cvLandFrom\(body, mid, \(w&&w\.dataset&&w\.dataset\.mid\)\|\|mid\);/.test(CHAT));
t('an item already on the canvas is UPDATED when what it holds has changed, not landed twice',
  /if\(_CV_SIG\[key\]===sig\) continue;/.test(CHAT) && /_capCall\('canvas\.update',\{session_id:SID, key, content:m\.content\}\)/.test(CHAT));
t('...and an unchanged item costs nothing', /let sig=''; try\{ sig=JSON\.stringify\(m\.content\); \}catch\(_\)\{ sig=String\(i\); \}/.test(CHAT));
t('the signature is remembered when the item first lands, or the first update would be a no-op',
  /if\(r&&r\.ok\)\{ _CV_SIG\[key\]=sig;/.test(CHAT));
t('the key is the turn and the place in the reply, which is what makes an update possible at all',
  /const key=m\.key\|\|\('turn:'\+mid\+':'\+m\.k\+':'\+i\);/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

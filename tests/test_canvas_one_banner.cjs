// ONE BANNER, AND BLOCKS OFF (the canvas's final form §4.1–§4.2; vera/canvas/canvas_element.js + vera/chat/chat_panel.html).
//
// The owner asked for two things about the session canvas: "the top 2 rows of the header to be combined so 'session
// canvas rev 1' is on the same line as the add buttons", and "with blocks off the session canvas should not have a
// background (but the button [bar] at the top should - 1 banner for all of them)"; and, of the items, "if blocks is
// off, they could loose their containers".
//   node tests/test_canvas_one_banner.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── the banner is ONE row: the add bar carries the title and the host's controls ─────────────────────────────── */
t('the add bar opens with a banner-start slot and closes with a banner-end slot',
  /<div class="addbar" data-w="canvas.add"><slot name="banner-start"><\/slot>/.test(SRC) && /<slot name="banner-end"><\/slot><\/div>/.test(SRC));
t('the controls are pushed to the far end of that row, and the title is bounded so it cannot eat it',
  /::slotted\(\[slot="banner-end"\]\)\{margin-left:auto\}/.test(SRC) && /::slotted\(\[slot="banner-start"\]\)\{[^}]*max-width:52%/.test(SRC));
t('the column no longer draws a header row of its own', !/<div class="tri-hd"><b>Session canvas<\/b>/.test(CHAT));
t('only the controls claim the free space — the hidden-items button sits with the kinds it restores',
  /\.addbar \.hidwrap\{margin-left:6px\}/.test(SRC));
t('its controls are a template, cloned into the element so their ids exist exactly once',
  /<template id="cvBannerTpl">/.test(CHAT) && /getElementById\('cvBannerTpl'\); if\(tpl&&tpl\.content\) el\.appendChild\(tpl\.content\.cloneNode\(true\)\)/.test(CHAT));
/* the clone rides on the MOUNT line, which stays one line of live code from end to end: a note left mid-line once
   swallowed the append and the column drew empty with nothing thrown (defect 81; test_chat_tripage guards it too) */
{ const mk = CHAT.split('\n').find((ln) => ln.includes("el=document.createElement('vera-canvas')")) || '';
  t('the clone and the append are on the mount line itself, still live code end to end',
    mk.includes("tpl.content.cloneNode(true)") && mk.includes('host.appendChild(el);') && !/\/\/[^*]*$/.test(mk)); }
// the ids and handlers the page looks up must survive the move, or the counts stop moving and the ribbon goes dead
['cvColSub', 'cvColCols', 'cvDrvRibbon'].forEach((id) => {
  const inTpl = new RegExp('<template id="cvBannerTpl">[\\s\\S]*?id="' + id + '"[\\s\\S]*?<\\/template>').test(CHAT);
  t('the banner still carries #' + id + ', so the page can still find it', inTpl);
});
t('and it is in the template exactly once — a second copy would shadow the first',
  ['cvColSub', 'cvColCols', 'cvDrvRibbon'].every((id) => (CHAT.match(new RegExp('id="' + id + '"', 'g')) || []).length === 1));
t('the NOW counter is off the banner — the items it counted are in the column below it',
  !/id="cvColNow"/.test(CHAT) && !/\.cv-bn \.nowbar\{/.test(CHAT));
t('the title and the revision are in the SAME slotted span as each other',
  /slot="banner-start"[^>]*><b>Session canvas<\/b><span class="lbl" id="cvColSub">/.test(CHAT));
t('Runs, COLS and the close button are in the other one',
  /slot="banner-end"[\s\S]*?id="cvColCols"[\s\S]*?data-runs="cv"[\s\S]*?togglePage\('canvas'\)[\s\S]*?<\/span>/.test(CHAT));
t('the column can still be closed before a session exists, when there is no element to carry the banner',
  /class="cv-nobanner"[\s\S]{0,200}togglePage\(\\?'canvas\\?'\)/.test(CHAT) && /\.cv-nobanner\{/.test(CHAT));
t('the slotted controls are styled by the host, since they are the host\'s nodes', /^\.cv-bn\{/m.test(CHAT) && /\.cv-bn \.lbl\{/.test(CHAT) && /\.cv-bn \.drv-ribbon\{/.test(CHAT));
t('the graph column keeps its own header row and its rules', /<div class="tri-hd"><b>Graph<\/b>/.test(CHAT) && /^\.tri-hd\{/m.test(CHAT));

/* ── blocks off: no ground under the canvas, no container round an item, but the banner keeps its background ──── */
t('the canvas itself has no background with blocks off', /:host\(\[blocks="off"\]\) \.wrap\{background:transparent;border-color:transparent\}/.test(SRC));
t('AN ITEM LOSES ITS CONTAINER: no ground, no box, no shadow',
  /:host\(\[blocks="off"\]\) \.it\{background:transparent;box-shadow:none;border-color:transparent\}/.test(SRC), 'the 55% border box is still there');
t('what says where an item ends is its own header line', /:host\(\[blocks="off"\]\) \.it > \.it-hd\{border-bottom:1px solid/.test(SRC));
t('the rail is held back until you are on the item',
  /:host\(\[blocks="off"\]\) \.it > \.it-ft\{opacity:0/.test(SRC) && /:host\(\[blocks="off"\]\) \.it:hover > \.it-ft,:host\(\[blocks="off"\]\) \.it:focus-within > \.it-ft\{opacity:1\}/.test(SRC));
// blocks off is about grounds - it must not cost the states that MEAN something, which a bare box-shadow:none did
['openin', 'hovopen'].forEach((k) => t('a ' + k + ' item keeps its ring with blocks off',
  new RegExp(':host\\(\\[blocks="off"\\]\\) \\.it\\.' + k + '\\{box-shadow:0 0 0 1\\.5px').test(SRC)));
t('a suggestion is still drawn as a ghost', /:host\(\[blocks="off"\]\) \.it\.ghost\{border:1px dashed/.test(SRC));
t('THE BANNER KEEPS ITS BACKGROUND whatever blocks says — it is sticky, and a sticky row with nothing behind it is unreadable',
  /\.addbar\{position:sticky;[^}]*background:var\(--bg1/.test(SRC) && !/:host\(\[blocks="off"\]\) \.addbar\{[^}]*background:transparent/.test(SRC));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

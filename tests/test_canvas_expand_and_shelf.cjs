// THREE THINGS THE CANVAS DID NOT DO (owner, 2026-09-24; vera/canvas/canvas_element.js + vera/chat/chat_panel.html):
//   "re-sizing the canvas items doesnt function - i expand them but the visible area does not"
//   "open in place doesnt do anything"
//   "every item in the canvas is still displayed inside a box"
//   "i cant find the parked items once parked"
// The first two are one cause and it was self-inflicted: the viewport cap (held layout §2) clamped any card taller
// than the column, including one the reader had just asked to be taller. The third is a wiring bug — the blocks tier
// was sampled once at mount, before the appearance had written it. The fourth is a place: park put items on a shelf
// at the foot of a stage as tall as the transcript.
//   node tests/test_canvas_expand_and_shelf.cjs
const path = require('node:path'), fs = require('node:fs');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── 1 + 2. AN ITEM YOU ASKED TO BE BIG IS NEVER CAPPED ──────────────────────────────────────────────────────── */
t('the cap knows what you asked for: opened in place, or dragged to a size',
  /const asked = \(c\) => c\.classList\.contains\('openin'\) \|\| c\.classList\.contains\('sized'\);/.test(SRC));
t('...and it skips those cards, so the gesture is not clamped back inside the same frame',
  /if \(hs\[i\] > capH && !asked\(c\)\) \{ c\.style\.maxHeight = capH \+ 'px'; c\.classList\.add\('capped'\); \}/.test(SRC));
t('an item that was NOT asked for is still capped — the cap is why a tall item can be reached at all',
  /const capH = Math\.max\(120, V - pad - 10\);/.test(SRC) && /hs\[i\] > capH/.test(SRC));
t('an opened item\'s body is unbounded, and so is a dragged one\'s',
  /\.it\.openin \.it-bd\{max-height:none!important\}/.test(SRC) && /\.it\.sized \.it-bd\{max-height:none!important\}/.test(SRC));
// the point of the exemption: the COLUMN takes the strain, which is the packed regime the placer already has
t('nothing else had to change for it: an over-tall set already makes the column scroll',
  /const mode = !V \? 'stage' : \(fits \? 'held' : 'packed'\);/.test(SRC));
t('and an expanded item still cannot be folded away by the column', /c\.classList\.contains\('pinned'\) \|\| c\.classList\.contains\('openin'\)/.test(SRC));

/* ── 3. THE BLOCKS TIER IS FOLLOWED, NOT SAMPLED ONCE ────────────────────────────────────────────────────────── */
t('one place computes it, for every canvas on the page', /function _cvSyncBlocks\(\)\{[\s\S]*?data-blocks'\)!=='off'/.test(CHAT));
t('an observer on <html> keeps them in step — the seed, a patch, another tab',
  /new MutationObserver\(_cvSyncBlocks\)\.observe\(document\.documentElement,\{attributes:true,attributeFilter:\['data-blocks'\]\}\)/.test(CHAT));
t('and a canvas that has just mounted is told the tier as it stands NOW', /try\{ _cvSyncBlocks\(\); \}catch\(_\)\{\}/.test(CHAT));
t('the element still carries the tier itself, because a rule on <html> cannot cross a shadow root',
  /:host\(\[blocks="off"\]\) \.it\{background:transparent;box-shadow:none;border-color:transparent\}/.test(SRC));

/* ── 4. PARKED IS A SHELF YOU CAN REACH ──────────────────────────────────────────────────────────────────────── */
t('the banner carries the shelves, built by one helper', /const shelf = \(n, label, title, items\) => items\.length/.test(SRC));
t('parked and hidden are both on it, in the row that is always in view',
  /shelf\('parkpop', 'parked',[^)]*stage \? parked : \[\]\)/.test(SRC) && /shelf\('hid', 'hidden',/.test(SRC));
t('the shelf button opens its own popover, and does not collide with parking an item',
  /if \(act === 'hid' \|\| act === 'parkpop'\)/.test(SRC) && /if \(act === 'park'\) return this\.call\('canvas\.park', \{ key \}\);/.test(SRC));
t('the band at the foot of the stage is gone (it is kept for the flow projection)',
  /if \(parked\.length && !stage\) html \+= `<div class="band parked">/.test(SRC));
t('a chip on the shelf brings its item back through the resolver — recall, not re-create',
  /closest\('\.chip\[data-key\]'\); if \(ch\) \{ ev\.stopPropagation\(\); this\.call\('canvas\.add', \{ key: ch\.dataset\.key \}\)/.test(SRC));
t('and it says what it is for', /Parked items — shelved, not gone/.test(SRC) && /Parked — click to bring it back/.test(SRC));

/* ── the shelf is only in the banner where the banner exists ─────────────────────────────────────────────────── */
t('a named canvas (no stage) keeps its parked band, so nothing is lost where there is no banner shelf',
  (() => { const i = SRC.indexOf("shelf('parkpop'"), j = SRC.indexOf('if (parked.length && !stage)');
    return i > 0 && j > i; })());

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// THE CANVAS'S OWN SETTINGS (the owner's decisions, 2026-09-23; vera/canvas/canvas_element.js + vera/chat/chat_panel.html).
//
// Three readings the canvas cannot make for you, and had nowhere to be asked: how an item is aligned with the turn
// that made it (held · strict), whether an HTML item draws or shows its source, and how freely the canvas offers to
// explode what is on it. The canvas reads them off itself, as attributes, rather than the page reaching inside it.
//   node tests/test_canvas_settings.cjs
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── the settings exist, with the defaults being what the canvas already did ─────────────────────────────────── */
t('the element takes the three settings as attributes', /'bare', 'blocks', 'align', 'preview', 'explode-offer'\]/.test(SRC));
t('each is read in ONE place, and defaults to today\'s behaviour',
  /strictAlign\(\) \{ return String\(this\.getAttribute\('align'\) \|\| 'held'\)/.test(SRC) &&
  /previewOn\(\) \{ return String\(this\.getAttribute\('preview'\) \|\| 'on'\)[^}]*!== 'off'/.test(SRC) &&
  /explodeOffer\(\) \{ const v = String\(this\.getAttribute\('explode-offer'\) \|\| 'both'\)/.test(SRC));
t('a changed setting is acted on: preview and the offer redraw, alignment re-places',
  /\(name === 'preview' \|\| name === 'explode-offer'\) && this\._doc\) this\.render/.test(SRC) &&
  /name === 'align' && this\.hasAttribute\('stage'\)\) \{ this\._view = null; this\._placeNow\(\); \}/.test(SRC));
t('the page offers the three in Settings, each with its own persisted value',
  /id="cfgCvAlign"[\s\S]*?value="held"[\s\S]*?value="strict"/.test(CHAT) &&
  /id="cfgCvPreview"[\s\S]*?value="on"[\s\S]*?value="off"/.test(CHAT) &&
  /id="cfgCvExplode"[\s\S]*?value="both"[\s\S]*?value="code"[\s\S]*?value="never"/.test(CHAT) &&
  /_CV_CFG=\{ cfgCvAlign:\['align','held'\], cfgCvPreview:\['preview','on'\], cfgCvExplode:\['explode-offer','both'\] \}/.test(CHAT));
t('and they are handed to the element as attributes, on change and on mount',
  /function _cvApplyCfg\(\)/.test(CHAT) && /el\.setAttribute\(a,v\)/.test(CHAT) && /try\{ _cvApplyCfg\(\); \}catch\(_\)\{\}/.test(CHAT) && /_persistCanvasCfg,/.test(CHAT));

/* ── STRICT alignment is the old projection, kept on purpose ─────────────────────────────────────────────────── */
t('strict hands the placer no viewport, which IS the old stage regime — not a second placer',
  /const V = strict \? 0 : VH;/.test(SRC) && /const VH = body \? body\.clientHeight : 0;/.test(SRC));
t('...and only strict writes the column\'s scroll from the transcript\'s',
  /if \(this\.strictAlign\(\)\) \{[\s\S]{0,400}?body\.scrollTop = Math\.max\(0, Math\.round\(this\._scrollTop \+ st\.offsetTop/.test(SRC));
t('held never has its scroll written, which was the bug this replaced',
  (() => { const m = SRC.match(/setView\(msgsScrollTop, msgsTopClient\) \{[\s\S]*?\n    \}/); const body = m ? m[0] : '';
    const writes = (body.match(/body\.scrollTop = /g) || []).length;
    return writes === 1 && /if \(this\.strictAlign\(\)\)/.test(body); })());
t('the size ceilings are a share of the column in BOTH regimes — a bound is not an alignment',
  /if \(VH && this\._vh !== VH\)/.test(SRC));
t('strict caps nothing: an item is as tall as it is and the column scrolls the transcript',
  /Strict alignment caps nothing \(V is 0 there\)/.test(SRC) && /if \(V\) \{ cards\.forEach/.test(SRC));
t('and strict is never re-zeroed by the placer after setView wrote it', /if \(body && !strict && P\.mode !== 'packed'/.test(SRC));
// the pure placer still answers both regimes, which is the whole reason strict costs no new code
{
  const turns = { m1: { top: 0, height: 400 }, m5: { top: 1600, height: 400 } };
  // the transcript is scrolled to the last turn, which is 100px down the screen
  const item = [{ key: 'a', h: 120, mid: 'm5' }];
  const held = V.place(item, turns, { columns: 1, pad: 30, viewport: 500, scrollTop: 1500 });
  const strict = V.place(item, turns, { columns: 1, pad: 30 });
  t('held puts the item where its turn is ON SCREEN; strict leaves it level with the turn, far down the stage',
    held.mode === 'held' && held.placements[0].y === 100 && strict.mode === 'stage' && strict.placements[0].y === 1600,
    JSON.stringify([held.mode, held.placements[0].y, strict.mode, strict.placements[0].y]));
  // and a tall one: held brings it to the top of the column so all of it can be reached, strict does not
  const tall = [{ key: 'a', h: 900, mid: 'm5' }];
  const heldTall = V.place(tall, turns, { columns: 1, pad: 30, viewport: 500, scrollTop: 1500 });
  t('a tall item: held starts it at the top of the column (nothing fits, so the column scrolls); strict keeps it at its turn',
    heldTall.mode === 'packed' && heldTall.placements[0].y === 30 && V.place(tall, turns, { columns: 1, pad: 30 }).placements[0].y === 1600,
    JSON.stringify([heldTall.mode, heldTall.placements[0].y]));
}

/* ── an HTML item DRAWS, in the sandboxed frame, and says how to see the source ──────────────────────────────── */
t('an html item is a preview slot, not live markup in the shadow root',
  /html: \(c, size, key, el\) => \{/.test(SRC) && /data-live="preview" data-key="\$\{esc\(key\)\}" data-lang="html"/.test(SRC) &&
  !/^    html: c => c\.html \|\| '',$/m.test(SRC), 'the old injection is still there');
t('the frame it draws in is the sandboxed one, with no network and no same-origin',
  /inner\.setAttribute\('sandbox', 'allow-scripts'\)/.test(SRC) && /cc\.code \|\| cc\.html \|\| ''/.test(SRC));
t('the setting decides which way it starts, and the item keeps its own switch either way',
  /const pdef = !el \|\| typeof el\.previewOn !== 'function' \|\| el\.previewOn\(\);/.test(SRC) &&
  /const prev = key \? \(pset \? !!el\._prevOn\[key\] : pdef\) : pdef;/.test(SRC) &&
  /data-act="cprev"[\s\S]{0,120}Show the source/.test(SRC));
t('a whole-page code block also answers the setting', /const prev = PREVIEWABLE\(c\.lang\) && \(pset \? !!el\._prevOn\[key\] : \(pdef && WHOLE_PAGE\(c\.lang, c\.code\)\)\);/.test(SRC));
t('an html item with nothing in it says so rather than drawing an empty frame', /nothing to draw yet/.test(SRC));

/* ── the explode offer is the reader's ───────────────────────────────────────────────────────────────────────── */
{
  const codeItem = { key: 'code:a', type: 'code', content: { code: Array(20).fill('x = 1').join('\n') } };
  const proseItem = { key: 'note:b', type: 'markdown', content: { md: 'a'.repeat(900) } };
  const names = (o) => V.suggestionsOf({}, [codeItem, proseItem], '', o).map((s) => s.n);
  t('both (the default): code and prose are each offered', JSON.stringify(names({ explodeOffer: 'both' })) === JSON.stringify(['Explode this code', 'Explode this passage']), JSON.stringify(names({ explodeOffer: 'both' })));
  t('the default when nothing is passed is still both', JSON.stringify(names(undefined)) === JSON.stringify(['Explode this code', 'Explode this passage']));
  t('code: the passage is not offered', JSON.stringify(names({ explodeOffer: 'code' })) === JSON.stringify(['Explode this code']), JSON.stringify(names({ explodeOffer: 'code' })));
  t('never: neither is', names({ explodeOffer: 'never' }).length === 0, JSON.stringify(names({ explodeOffer: 'never' })));
  // a document's own suggestions are not the explode offer and are never suppressed by it
  t('the document\'s own suggestions survive "never"',
    V.suggestionsOf({ suggestions: [{ n: 'Open the diary', kind: 'calendar' }] }, [codeItem], '', { explodeOffer: 'never' }).map((s) => s.n).join('') === 'Open the diary');
  t('the element hands its own setting in', /suggestionsOf\(doc, keyed, focusMid, \{ explodeOffer: this\.explodeOffer\(\) \}\)/.test(SRC));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

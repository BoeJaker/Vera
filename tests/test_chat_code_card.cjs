// The in-chat code card: Copy / Save / Preview / Pane / Run, and the language on the element.
// (vera/chat/chat_panel.html)
//
// THIS TEST RUNS THE CARD. The one it replaces asserted the source TEXT:
//
//     assert "if(_cbWholePage(lang, code)) setTimeout(" in HTML, "a whole page opens drawn"
//
// which is the exact line that was broken - `code` is declared nowhere in _mdFenceCard, whose parameter is
// `raw` - so the assertion pinned the defect in place and passed for two days on a card that threw a
// ReferenceError on EVERY fenced block. Nothing surfaced, because the shared markdown renderer swallows a fence
// callback that throws and quietly substitutes its own plain card:
//
//     try { card = fence(...); } catch (_) { card = ''; }
//     out.push(card || fenceCard(...));            // vera_markdown_element.js
//
// Measured in a browser on the broken build: 0 chat cards, 6 fallback cards, 0 preview buttons, 0 toolbar
// buttons, and an empty console. The reply showed a lone lowercase "copy" and no way to preview anything.
// Silently dead with it: code linting, auto-save to the session artifact dir, and auto-preview - all three look
// for .code-card[data-cb].
//
// So: cut the real card out of the page and CALL it. A throw fails the test instead of passing it.
//
//   node tests/test_chat_code_card.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// the real text the page ships, cut out by its own markers
const cut = (a, b, label) => {
  const i = src.indexOf(a); if (i < 0) { t('present in the page: ' + label, false, a.slice(0, 60)); return ''; }
  const j = src.indexOf(b, i + a.length); if (j < 0) { t('terminated in the page: ' + label, false, b.slice(0, 60)); return ''; }
  return src.slice(i, j);
};
const helpers = cut("  const _cbWholePage=(lang,code)=>", "  const _cbDoc=(lang,code)=>", '_cbWholePage/_langClass');
const previewable = cut("  function _isPreviewable(lang){", "\n  //", '_isPreviewable');
// the real filename parser, not a stub — the card's naming rule (a fence's filename beats a caller's title) is
// only meaningful against the thing that actually parses it
const fname = cut("  function _parseFenceFilename(info){", "\n  /*", '_parseFenceFilename');
const card = cut("  function _mdFenceCard(lang,info,raw,st,opts){", "\n  function renderMd(t,opts){", '_mdFenceCard');
if (!helpers || !previewable || !fname || !card) { console.log(fails + ' FAILED'); process.exit(1); }

// stubs for what the card reaches outside itself; everything the card DECIDES is the real thing
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const api = new Function(
  'document', '_MM_LIVE_KEY', '_mmLiveTaken', '_MM_BLOCKS', '_widgetFence',
  '_registerCodeBlock', 'esc', 'highlightCode', '_isServerRunnable', '_codeSaveArtifact',
  'let _mmSeq=0;\n' + helpers + previewable + fname + card + '\nreturn { _mdFenceCard, _cbWholePage, _langClass, _isPreviewable };'
)(
  { getElementById: () => null },              // no cfgAutoRender checkbox in the harness
  '', false, {},                               // no live mermaid slot
  () => '',                                    // no widget fence
  (lang, raw, fn) => 'cb1',                    // a stable block id
  esc, (raw) => esc(raw),                      // highlight = escape, so the assertions read the source back
  (lang) => ['python', 'py', 'node', 'ruby'].includes(String(lang || '').toLowerCase()),
  () => {}
);

const FRAGMENT = '<b>hello</b>';
const WHOLE = '<!doctype html><html><body><h1>hi</h1></body></html>';
const render = (lang, code, st) => api._mdFenceCard(lang, '', code, st || {}, {});

// ---- the card is produced AT ALL (a throw here is the whole defect) -----------------------------------------
let html = null, threw = null;
try { html = render('html', FRAGMENT); } catch (e) { threw = e.message; }
t('an html fence produces a card without throwing', threw === null, 'threw: ' + threw);
t('...and it is the CHAT\'s card, not the shared renderer\'s fallback',
  !!html && html.indexOf('class="code-card"') >= 0 && html.indexOf('vmd-code') < 0);

// ---- the toolbar the user actually lost ---------------------------------------------------------------------
t('Copy is there', !!html && html.indexOf("CH._codeCopy('cb1'") >= 0);
t('Save is there', !!html && html.indexOf("CH._codeSaveArtifact('cb1'") >= 0);
t('Preview renders it in place', !!html && html.indexOf('data-prev') >= 0 && html.indexOf("CH._codeInline('cb1')") >= 0);
t('Pane opens it larger', !!html && html.indexOf("CH._codePreview('cb1')") >= 0);
// the pop-out render.html used to force on the reader, offered on the card instead - same window, their call
t('Pop out is offered, not done to you', !!html && html.indexOf("CH._codePop('cb1')") >= 0);
t('a non-previewable language gets no pop-out either', render('python', 'print(1)').indexOf('_codePop') < 0);
// render.html has no fence to carry a filename, so it names the card through opts.title
t('a card can be named by its caller', /html · <span[^>]*>My Preview</.test(
  api._mdFenceCard('html', '', FRAGMENT, {}, { title: 'My Preview' })));
t('...but a fence\'s own filename still wins', /html · <span[^>]*>app\.html</.test(
  api._mdFenceCard('html', 'file=app.html', FRAGMENT, {}, { title: 'My Preview' })));
t('a fragment offers "Preview"', !!html && />Preview</.test(html));
t('a whole document offers "Source", because it is already drawn',
  />Source</.test(render('html', WHOLE)));
t('js gets its own run button', /Preview JS</.test(render('js', 'console.log(1)')));
t('a server language gets Run', /&#9654; Run</.test(render('python', 'print(1)')));
t('a non-previewable language gets no preview button', render('python', 'print(1)').indexOf('data-prev') < 0);

// ---- the language on the element: what the canvas and Explode read back --------------------------------------
// Both harvesters take a code item's language from this class and nothing else:
//   (String(el.className||'').match(/language-([\w-]+)/)||[])[1] || ''
// It had no writer anywhere, so every harvested item arrived with lang:'' and a canvas code item could never
// offer a preview, because PREVIEWABLE('') is false.
const langOf = (h) => { const m = /<code class="language-([\w-]+)"/.exec(h || ''); return m ? m[1] : ''; };
t('the card tags the code element with its language', langOf(html) === 'html');
t('...for every language, not just previewable ones', langOf(render('python', 'print(1)')) === 'python');
t('...and while the block is still streaming', langOf(render('css', 'b{}', { incomplete: true })) === 'css');
t('an unlabelled fence carries no class at all',
  render('', 'plain text').indexOf('class="language-') < 0, 'a block with no language genuinely has none');
t('the harvesters\' own regex reads it back',
  (String('language-html').match(/language-([\w-]+)/) || [])[1] === 'html');
t('a canvas code item built from that would offer a preview',
  ['html', 'js', 'javascript', 'css', 'jsx', 'svg'].indexOf(langOf(html)) >= 0);

// ---- the identifier that was undefined -----------------------------------------------------------------------
t('the whole-page check reads this function\'s own parameter',
  src.indexOf('_cbWholePage(lang, raw)') >= 0 && src.indexOf('_cbWholePage(lang, code)') < 0,
  '`code` is not declared in _mdFenceCard; its source parameter is `raw`');

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

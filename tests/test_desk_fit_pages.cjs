// WSU-44: on the desk an html item is as tall as its page, a diagram grows with a wide card, long code wraps in a narrow one.
//   node tests/test_desk_fit_pages.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const MM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'render', 'vera_mermaid.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const ph = C.match(/const PREVIEW_H = ('[\s\S]*?<\\\/script>');/);
t('every preview page says its content height (the body, not the frame) to its parent', !!ph && ph[1].includes('parent.postMessage({veraPreviewH:') && ph[1].includes('b.scrollHeight') && ph[1].includes('ResizeObserver'));
t('...appended to every preview the canvas draws', C.includes('const d = previewDoc(lang, src) + PREVIEW_H;'));
t('only a frame of this canvas is heard, matched by its window', C.includes('f.contentWindow === ev.source') && C.includes("typeof d.veraPreviewH !== 'number'"));
t('the listener comes and goes with the element', C.includes("window.addEventListener('message', this._pvMsg)") && C.includes("window.removeEventListener('message', this._pvMsg)"));
t('_deskFit fits a drawn page to what it said: shrinks to a short page, lets a long one grow',
  C.includes("const pv = c.querySelector('.vc-preview'), ph = this._pvH && this._pvH[key];") && C.includes('else if (nat[key] != null && want > nat[key] + 16) { delete nat[key]; changed = true; }'));
t('a diagram may grow past 1.25x where its host allows (max-scale), the desk allows 2',
  MM.includes('this._maxScale() || 1.25') && MM.includes('Math.max(1.6, this._maxScale())') && C.includes("if (this.deskAlign()) inner.setAttribute('max-scale', '2');"));
t('on the desk a diagram slot is the drawing, not the size class ceiling',
  C.includes(':is(.vc-diag,.vc-diagwrap) .vc-live{max-height:none;flex:none;height:var(--dh,150px)}'));
t('long code in a narrow desk card wraps under its line numbers', C.includes('@container deskcard (max-width: 460px) { .it-bd pre.vc-pre code{white-space:pre-wrap;overflow-wrap:anywhere}'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// 2026-10-03: a block on the canvas leaves the chat whole; a page streams its preview in the chat as a diagram does;
// the explode view's verdict strip is one line
//   node tests/test_canvas_oneplace_html_live_explode_verdict.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const S = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'structgraph_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const i = H.indexOf('function _cvOnCanvas(m, key){'), oc = H.slice(i, i + 2600);
t('the whole card moves to the canvas, not the <code> inside it', oc.includes("el=(el.closest&&el.closest('.code-card'))||el;") && /let el=m&&m\._el;/.test(oc));
t('...and the chip follows the card', oc.indexOf("el=(el.closest&&el.closest('.code-card'))||el;") < oc.indexOf("el.classList.add('cv-elsewhere')"));
t('a streaming page previews in the chat: one frame per message, carried across repaints', H.includes('const _HTML_LIVE={};') && H.includes("try{ _htmlLive(bubEl, key); }catch(_){}") && H.includes("card.classList.add('prev'); card.appendChild(L.holder);"));
t('...redrawn at most every 350 ms, double-buffered (never blank)', H.includes("const wait=350-(Date.now()-L.at);") && H.includes("nf.onload=()=>{ holder.querySelectorAll('iframe').forEach(f=>{ if(f!==nf) f.remove(); }); nf.style.cssText=''; };"));
t('...only a whole page; a fragment waits for its fence to close', H.includes("if(!/<\\s*(!doctype\\s+html|html[\\s>]|body[\\s>]|svg[\\s>])/i.test(src)) return;"));
t('the verdict strip keeps its content\'s width, on one line', S.includes('vera-structgraph .sg-vr{position:absolute;display:flex;gap:6px;align-items:center;z-index:5;width:max-content;max-width:none;white-space:nowrap}') && S.includes('vera-structgraph .sg-vr > *{flex:0 0 auto}'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

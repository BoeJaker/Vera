// 2026-10-03: the editor has line numbers, live lint and the definition tip; every card carries "Send to"; a mermaid
// diagram is a card with the same bar; Pane opens where there is no pane; the canvas keeps a page on source when asked;
// Full/Min is the conversation's width; a message's explode panel sits on the reply's column; the outline does not wrap
//   node tests/test_output_cards_send_diagram_editor.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const K = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera_output_element.js'), 'utf8');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

// the kit, loaded: what a card and a diagram draw
const doc = { readyState: 'complete', head: { querySelector: () => null, insertBefore() {}, appendChild() {}, firstChild: null }, addEventListener() {}, createElement: () => ({ setAttribute() {}, style: {} }), getElementById: () => null, querySelectorAll: () => [] };
const win = { document: doc, customElements: { get: () => null }, innerHeight: 900, innerWidth: 1400 };
const ctx = vm.createContext({ window: win, document: doc, customElements: win.customElements, console, setTimeout, clearTimeout, Map, Set, CustomEvent: function () {}, requestAnimationFrame: (f) => f() });
vm.runInContext(K, ctx); const VO = win.VeraOut;

const py = VO.card('python', 'def a():\n    return 1\n\ndef b():\n    return a()\n', { filename: 'x.py' });
t('a code card carries Send to in its bar', /<div class="vo-acts vo-send" data-vo-prose="cb:[^"]+"><div class="ma-dd">/.test(py) && py.indexOf('vo-send') < py.indexOf('</div><pre'));
t('...without a podcast of code', !/data-vp="podcast"/.test(py) && /data-vp="html"/.test(py) && /data-vp="notebook"/.test(py) && /data-vp="channel"/.test(py));
const id = (py.match(/data-cb="([^"]+)"/) || [])[1];
t('...and hands on the code as a fence; a file in the IDE gets the bare code', VO.prose['cb:' + id].getMd() === '```python file=x.py\ndef a():\n    return 1\n\ndef b():\n    return a()\n```' && VO.prose['cb:' + id].raw() === 'def a():\n    return 1\n\ndef b():\n    return a()\n');
const page = VO.card('html', '<!doctype html><html><body>x</body></html>', {});
t('a whole page opens drawn in the chat', /data-auto-prev="1"/.test(page) && />Source</.test(page));
const held = VO.card('html', '<!doctype html><html><body>x</body></html>', { autoPrev: false, prevOn: false });
t('...but where the host decides (autoPrev:false) the kit does not draw it back over the source', !/data-auto-prev/.test(held) && />Preview</.test(held));
const dg = VO.diagram('graph TD\n A-->B\n', { title: 'Flow' });
t('a mermaid diagram is a card: Copy, Edit, Source, Pop out, SVG, PNG, Save, Canvas, Send to', /^<div class="code-card vo-diagram" data-cb="/.test(dg)
  && ['copy', 'edit', 'dsrc', 'dpop', 'dsvg', 'dpng', 'save', 'canvas'].every((a) => dg.includes('data-vo="' + a + '"')) && dg.includes('vo-send'));
t('...its drawing in a slot, its source (numbered) under it', dg.includes('<div class="vo-mm" data-vo-mm="') && dg.includes('<pre class="vo-lines vc-pre vc-code"><code class="language-mermaid">'));
t('...and a closed mermaid fence is that card', /code-card vo-diagram/.test(VO.fence('mermaid', '', 'graph TD\n A-->B\n', {}, {})));
t('a host hands in its own drawing (the chat\'s slot, the canvas\'s live layer)', VO.diagram('graph TD\n A-->B\n', { body: '<div class="mm-slot" data-mm="mm9"></div>' }).includes('<div class="mm-slot" data-mm="mm9"></div><pre'));
t('...the source shows on request; the drawing is bare inside the card', K.includes("if (name === 'dsrc') { const on = c.classList.toggle('src');") && K.includes(".code-card.vo-diagram.src > pre{display:block;") && K.includes("el.setAttribute(slot.closest('.vo-diagram') ? 'bare' : 'popout', '')"));
t('...SVG and PNG come from the drawing, wherever the host keeps it', K.includes("const hk = hookFor(c, 'dimg');") && C.includes("dimg: (blk, id, b, c, fmt) => { const L = this._live && this._live[keyOf(c)];"));
t('...an applied edit redraws it', K.includes("if (c.classList.contains('vo-diagram')) { const vm = c.querySelector('vera-mermaid'); if (vm && typeof vm.render === 'function')"));
t('...on the canvas it is stored where a diagram draws from (mermaid)', C.includes("if (bl.type === 'diagram' || was.mermaid != null) { next.mermaid = d.code;"));
t('...and put on the canvas as a diagram, not as code', K.includes("? { session_id: sid, kind: 'diagram', key: 'diagram:' + hash(blk.code), content: { mermaid: blk.code, title: blk.title || 'Diagram' }, at: 'now' }"));

// the editor
t('the editor: a textarea over the highlighted code, a line gutter, lint as you type', K.includes("function mountEditor(c, text, lang) {") && K.includes("hl.innerHTML = highlight(v, lang) + '\\n';") && K.includes("clearTimeout(lt); lt = setTimeout(lintNow, 250);") && K.includes("for (let i = 1; i <= k; i++) s += i + '\\n'; gut.textContent = s;"));
t('...a pause over a call shows its definition, read from what is being typed', K.includes("const tok = el && el.closest && el.closest('.vo-ed-h span.hl-fn, .vo-ed-h span.hl-type');") && K.includes("tipShow(tok, c, ta.value)") && K.includes("const sy = symbols(id === own && text != null ? text : blk.code, blk.lang);"));
t('...the highlighted layer is hit-testable (elementFromPoint skips pointer-events:none)', !/\.vo-ed \.vo-ed-h\{[^}]*pointer-events:none/.test(K));
t('...the inline and full-screen editors are the same editor', K.includes('const ed = mountEditor(c, blk.code, blk.lang);') && K.includes('const ed = mountEditor(c, text, blk.lang); m.querySelector(\'.vo-modal-b\').appendChild(ed.box);'));
t('...both layers keep the same metrics whatever the page styles', K.includes('.vo-ed .vo-ed-h,.vo-ed .vo-ed-ta{grid-area:1/1;margin:0!important;padding:10px 12px!important;'));

// Pane, the menu's place, the outline
t('Pane where there is no pane on screen: a large preview over the page', K.includes("pane(blk) {") && K.includes('<iframe class="vo-modal-fr" sandbox="allow-scripts" title="Preview"></iframe>')
  && H.includes("pane:(blk,id)=>{ const r=document.getElementById('rightRail'); if(!r||r.getBoundingClientRect().width<40||getComputedStyle(r).display==='none') return false;"));
t("a card's Send menu opens over the page, under its button, put right if an ancestor moves 'fixed'", K.includes("if (row.classList.contains('vo-send')) {") && K.includes("if (Math.abs(got.left - wantL) > 0.5 || Math.abs(got.top - wantT) > 0.5)"));
t('the outline kind never wraps', K.includes('.code-card .vo-ol-k{flex:0 0 auto;min-width:64px;white-space:nowrap;'));

// the canvas
t('the canvas keeps a page on source when the reader switches it', C.includes("cls: 'vc-kit', incomplete: streaming, autoPrev: false, bar: false }));")   /* WSU-58 S6: inside kitDeclare */);
t('a canvas diagram is the kit\'s diagram card over the live drawing', C.includes("+ kitDeclare(el, VO.diagram(src, { id: 'cvd-' + key, title, canvas: false, cls: 'vc-kit',") && C.includes('.vc-diagwrap .vc-live{height:var(--dh,150px);max-height:400px}'));
t('a canvas html item carries Send to', C.includes("VO.sendMenu('cvh-' + key, { getMd: () => '```html\\n' + src.replace(/\\n$/, '') + '\\n```', raw: () => src,"));

// the chat
t('a closed mermaid fence in the chat is the diagram card around its slot', H.includes("return (typeof VeraOut!=='undefined' && VeraOut.diagram) ? VeraOut.diagram(raw, {body:slot}) : slot; }"));
t('...it goes to the canvas whole (its slot is not a second thing)', H.includes("if(el.classList.contains('mm-slot')&&el.parentElement&&el.parentElement.closest('.code-card')) return;"));
t('Full / Min is the width of the conversation, the layout the same', H.includes("body.dataset.view='minimal';\n    if(full) body.dataset.width='full'; else delete body.dataset.width;")
  && H.includes('body[data-width="full"] #chatColumn{--measure:min(1240px, calc(100% - 32px))}') && H.includes('body[data-width="full"][data-view="minimal"].has-msgs:not(.has-panel) #msgs > .mwrap,'));
t("a message's explode panel sits on the reply's column", H.includes('#msgs .mwrap > .xp-inline{margin-left:72px!important;margin-right:14px!important}'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

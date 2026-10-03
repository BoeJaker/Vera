// 2026-10-03: the canvas draws code and markdown with the shared kit, and an item that grows is placed again
//   node tests/test_canvas_shared_output.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('a code item is the kit\'s card: its bar, Copy/Pop/Run/Save from the kit', C.includes("head = VO.bar(vid, c.lang, c.filename || c.path || '', { actions: ['copy', 'pop', 'run', 'save'], extra });"));
t('...with the canvas\'s own persisted Edit and live-layer Preview in the same look', C.includes(`VO.hostBtn('data-act="edit"', 'edit', 'Edit'`) && C.includes(`VO.hostBtn('data-act="cprev"', 'prev'`));
t('...registered in the kit under the item\'s key, with an output strip for Run', C.includes("VO.blocks[vid] = { lang: String(c.lang || ''), code: String(c.code || '')") && C.includes('data-exec-out="${esc(vid)}"'));
t('the body stays line-addressable (an explode item still binds to it)', C.includes('<span class="vc-line" data-line="${i + 1}">'));
t('one highlighter: the kit\'s', C.includes("const VC = (VO && typeof VO.highlight === 'function') ? VO : root.VeraCode;"));
t('a markdown item renders through the kit (its fences are the shared cards)', C.includes("root.VeraOut.render(String(c.md || c.text || ''), { canvas: false })"));
t('the canvas\'s shadow root takes the kit and hydrates each draw, loading it when a page did not', C.includes('root.VeraOut.wire(this.shadowRoot); root.VeraOut.hydrate(layers.items);') && C.includes("ensureLib('/ui/elements/vera_output.js', '')"));
t('lint without the chat: the kit\'s linter', C.includes('lint: VO.lintCode } : null;'));
t('an item that grows is placed again (no overlap, the last item shows in full)', C.includes('this._cardRO = new ResizeObserver(') && C.includes('this._cardRaf = requestAnimationFrame(() => { this._cardRaf = 0; this._placeNow(); })'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

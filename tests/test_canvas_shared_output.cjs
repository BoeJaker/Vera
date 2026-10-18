// 2026-10-03: the canvas draws code and prose with the shared kit - the same card and the same row as the chat - and an
// item that grows is placed again
//   node tests/test_canvas_shared_output.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('a code item IS the kit\'s card (VeraOut.card): every action the chat\'s has', C.includes("const cardHtml = kitDeclare(el, VO.card(String(c.lang || ''), String(c.code || ''), { id: vid, filename: String(c.filename || c.path || ''), canvas: false, body: live, prevOn: prev, cls: 'vc-kit', incomplete: streaming, autoPrev: false, bar: false }));")   /* WSU-58 S6: the body declares the kit's actions */);
t('...inside the wrap an explode item binds to (its numbered lines are the kit\'s)', C.includes('return `<div class="vc-codewrap" data-code="1"${c.path || c.filename ? ` data-path="${esc(c.path || c.filename)}"` : \'\'}>${cardHtml}</div>`;'));
t('...the streaming card while the chat is writing into it', C.includes('const streaming = !!(el && el._liveOv && el._liveOv[key]);'));
t('the canvas plugs in only what it does its own way: Preview (live layer), Explode (bound item), an edit kept with the item',
  C.includes("preview: (blk, id, b) => viaAct('cprev', b),") && C.includes("explode: (blk, id, b) => viaAct('explode', b),") && C.includes("return this.call('canvas.update', Object.assign(this._ref(key), { content: Object.assign({}, was, next) }));") && C.includes("const was = bl.content || {}; const next = { code: d.code };"));
t('an edit in progress is never redrawn away', C.includes("if (this._html !== mark && layers.items.querySelector('.code-card.editing')) this._editHeld = true;") && C.includes("addEventListener('vera:code:editend'"));
t('a reading item renders through the kit and carries the shared prose row, with its own Edit and Explode', C.includes("row = VO.proseMenu('cvp-' + key, { md, canvas: false, explode: false,") && C.includes("own('edit', 'edit', 'Edit'") && C.includes("own('explode', 'explode', 'Explode'"));
t('the shadow root takes the kit with the canvas\'s hooks and hydrates each draw, loading the kit when a page did not',
  C.includes('root.VeraOut.wire(this.shadowRoot, this._voHooks()); root.VeraOut.hydrate(layers.items);') && C.includes("ensureLib('/ui/elements/vera_output.js', '')"));
t('the item being streamed is drawn every paint (liveUpdate), held over older revisions until done', C.includes('liveUpdate(key, content) {') && C.includes("if (this._liveOv) blocks.forEach((b) => {") && C.includes('liveDone(key) {'));
t('a redraw keeps every scrolling part where it was; at its end it follows the new end', C.includes('el.scrollTop = s.end ? el.scrollHeight : s.top;'));
t('an item that grows is placed again', C.includes('this._cardRO = new ResizeObserver(') && C.includes('this._cardRaf = requestAnimationFrame(() => { this._cardRaf = 0; this._placeNow(); })'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

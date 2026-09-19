// The exploded scene draws a placed widget's OWN form (vera/chat/exploded_element.js; Notes/42 defect 37, the element
// side): widgetOf reads the record the WidgetConfig sheet lands first, the face is the registry's renderer per form (its
// own sample until it reads), so three forms on a plate are three different faces; a diagram card draws its mermaid
// through the estate's element. The pure parts, and the source strings the renders are held by.
//   node tests/test_exploded_place_form.cjs
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js');
const X = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
t('faceHtml and diagramHtml are exported (version 6)', typeof X.faceHtml === 'function' && typeof X.diagramHtml === 'function' && X.version >= 6);

// ── the root cause: one sample per SHAPE — every level the same dial, every set the same bars, the rest one block ──
const before = ['radial', 'counter', 'bar'].map((f) => JSON.stringify(X.widgetOf({ kind: 'widget', form: f }).data));
t('without the widget element, three level forms still share the shape\'s local sample (the old look, kept as the fallback)', before[0] === before[1] && before[1] === before[2]);

// ── the record first ──
const rec = { form: 'radial', title: 'GPU', source: 'obs.health', frame: { size: 'l' }, draw: { form: 'radial', palette: 'warm' }, data: { value: 71, max: 100 } };
const w = X.widgetOf({ n: 'GPU', d: 'canvas · placed', kind: 'widget', form: 'trace', record: rec });
t('a card carrying a record draws the record\'s form, size and data — not the card\'s form', w.form === 'radial' && w.size === 'l' && w.data.value === 71 && !w.sample && w.record === rec);
const w2 = X.widgetOf({ kind: 'widget', record: { form: 'table', frame: { size: 'm' } } });
t('a record without data draws the form\'s sample, marked', w2.form === 'table' && w2.sample && w2.data != null);
t('a record whose form sits in draw.form is read too', X.widgetOf({ record: { draw: { form: 'log', size: 's' } } }).form === 'log' && X.widgetOf({ record: { draw: { form: 'log', size: 's' } } }).size === 's');
t('a card with only a form is as before', X.widgetOf({ form: 'bars', data: { a: 1 } }).form === 'bars' && X.widgetOf({ kind: 'widget', d: 'widget · thermo' }).form === 'thermo');

// ── the face: the registry's renderer, per form, at the scene's size ──
t('without the widget element there is no face (the iso group / the widget card stand in)', X.faceHtml({ n: 'x' }, { form: 'radial', data: {} }, 'm') === '');
const calls = [];
globalThis.VeraWidget = { draw: (form, data, size, o) => { calls.push({ form, size, h: o.height, bare: o.bare, proj: o.proj, rec: !!o.record }); return '<i data-face="' + form + '">' + form + '</i>'; }, sample: (form) => ({ sampleFor: form }), normalise: (r) => Object.assign({ normalised: true }, r) };
const faces = ['radial', 'table', 'log'].map((f) => X.faceHtml({ n: f }, X.widgetOf({ kind: 'widget', record: { form: f, frame: { size: 'm' } } }), 'm'));
t('three forms placed on a plate are THREE different faces, each the form\'s own renderer', faces[0] !== faces[1] && faces[1] !== faces[2] && /data-face="radial"/.test(faces[0]) && /data-face="table"/.test(faces[1]) && /data-face="log"/.test(faces[2]));
t('the sample is the widget element\'s own, per form', calls.length === 3 && calls.every((c) => c.bare && c.proj === 'iso' && c.rec) && JSON.stringify(X.widgetOf({ kind: 'widget', form: 'radial' }).data) === '{"sampleFor":"radial"}');
t('the face is marked sample until it reads, and carries the form and the size', /class="xit-face sample" data-form="radial" data-size="m" style="--fh:70px"/.test(faces[0]) && !/ sample"/.test(X.faceHtml({ n: 'x' }, { form: 'radial', data: { value: 1 } }, 'm')));
t('the scene\'s size (S · M · L) sets the face; unset, the record\'s own size does', X.faceHtml({}, { form: 'bars', data: {} }, 's').includes('data-size="s"') && X.faceHtml({}, { form: 'bars', data: {} }, 'l').includes('--fh:110px') && X.faceHtml({}, { form: 'bars', data: {}, size: 'xl' }, '').includes('data-size="l"') && X.faceHtml({}, { form: 'bars', data: {}, size: 'xs' }, undefined).includes('data-size="s"'));
t('the record is normalised for the renderer when the element can', calls[0].rec && SRC.includes("if (rec && typeof root.VeraWidget.normalise === 'function') rec = root.VeraWidget.normalise(rec);"));
t('a renderer that throws or answers nothing gives no face (the fallback stands in)', (globalThis.VeraWidget.draw = () => { throw new Error('x'); }, X.faceHtml({}, { form: 'radial', data: {} }, 'm') === '') && (globalThis.VeraWidget.draw = () => '', X.faceHtml({}, { form: 'radial', data: {} }, 'm') === ''));
delete globalThis.VeraWidget;

// ── the layout: a card carrying a record is a widget on the plate ──
const turn = { mid: 'm1', who: 'you', t: '14:31', text: 'place three', read: [], say: [], made: [], land: [
  { n: 'GPU', d: 'canvas · placed', col: '#8fb87a', kind: 'widget', record: { form: 'radial', frame: { size: 'm' } } },
  { n: 'Jobs', d: 'canvas · placed', col: '#8fb87a', kind: 'widget', record: { form: 'table', frame: { size: 'm' } } },
  { n: 'Boot', d: 'canvas · placed', col: '#8fb87a', kind: 'widget', record: { form: 'log', frame: { size: 'm' } } },
  { n: 'Boot path', d: 'mermaid', col: '#a78bfa', kind: 'diagram', mermaid: 'graph TD\n A-->B' }] };
const o = X.layout({ turns: [turn], sel: 'm1' }, 'iso', 1200, 800, {});
const land = o.widgets.filter((x) => x.layer === 'land');
t('three placed records are three iso widgets, each its own form', land.filter((x) => x.draw === 'group').map((x) => x.form).join(',') === 'radial,table,log');
t('the diagram card is a card, not a widget', land.find((x) => x.card.kind === 'diagram') && land.find((x) => x.card.kind === 'diagram').draw === 'card');

// ── the diagram card ──
const dg = X.diagramHtml({ kind: 'diagram', mermaid: 'graph TD\n A-->B' });
t('a diagram card draws its mermaid through the estate\'s element, the source as its text', /^<span class="xf-diag"><vera-mermaid bare title="diagram">graph TD\n A--&gt;B<\/vera-mermaid><\/span>$/.test(dg));
t('a diagram card with the source in its body draws too; another kind\'s body does not', X.diagramHtml({ kind: 'diagram', body: 'pie\n "a": 1' }).includes('<vera-mermaid') && X.diagramHtml({ kind: 'code', body: 'graph TD' }) === '' && X.diagramHtml({ kind: 'diagram' }) === '');
const ib = X.isoBody({ kind: 'diagram', mermaid: 'graph TD\n A-->B' }, null);
t('on the iso card the diagram is on the card, its source behind the click', ib.on.includes('<vera-mermaid') && ib.x.includes('<pre>graph TD'));
t('the scene loads the mermaid element once when a card needs it', SRC.includes("function ensureMermaid(doc) {") && SRC.includes("if (h.indexOf('<vera-mermaid') >= 0) ensureMermaid(this.ownerDocument);") && SRC.includes("s.src = '/ui/elements/vera_mermaid.js'"));

// ── the renders draw the face: on the plate, in the cards, in the carousel; the group only without the element ──
t('a widget on the SESSION CANVAS plane stands on it as its own face, with the label beneath',
  SRC.includes("const xigHtml = (wg) => { const c = wg.card, open = S.open === wg.id; const onPlane = wg.layer === 'land';")
  && SRC.includes("if (face) return '<div class=\"xig xigf'")
  && SRC.includes("vera-exploded .xigf{width:var(--xw,190px);height:auto;transform-origin:50% 100%;"));
t('a widget on any OTHER plane - read, the exchange, produced - is the board\'s card, as it was',
  SRC.includes("if (face && !onPlane) return xitHtml(wg, face);"));
t('and the built object still stands in when there is no face to draw',
  SRC.includes("const g = face ? null : groupOf(wg, ISO, { tilt: S.tilt || 30, azim: S.azim || 45 }); const b = isoBody(c, null);"));

// the relation edges answer to the tier and to the switch (defect 83)
t('a relation run carries the two records it joins, into the DOM',
  SRC.includes("if (a && b) R.add(a, b, Rc[0], Rc[1], Rc[2], [String(r.from), String(r.to)]);")
  && SRC.includes("if (a && b) R.add(a, b, Rc[0], Rc[1], Rc[2], [String(r.from), String(r.to)], { rel: true });")
  && SRC.includes("if (r.joins) seg.joins = r.joins;")
  && SRC.includes("(e.joins ? ' data-a=\"' + esc(e.joins[0]) + '\" data-b=\"' + esc(e.joins[1]) + '\"' : '')"));
t('Full draws them all; Hover and Zen rest them and light what the pointer touches',
  SRC.includes('vera-exploded[data-den="hover"] .xp-e.rel,vera-exploded[data-den="zen"] .xp-e.rel{opacity:0;transition:opacity .13s ease}')
  && SRC.includes('vera-exploded[data-den="hover"] .xp-e.rel.hot,vera-exploded[data-den="zen"] .xp-e.rel.hot{opacity:.7}')
  && SRC.includes("_relHot(id) {"));
t('and the switch puts them away in every tier — only the relations, never the turn\'s own runs',
  SRC.includes('vera-exploded[data-rels="off"] .xp-e.rel{display:none}'));
t('and a rotation turns the scene on the spot rather than swinging it out of the frame',
  SRC.includes("this._schedule(); this._isoCentre(); return this._S.tilt;") && SRC.includes("this._schedule(); this._isoCentre(); return this._S.azim;")
  && SRC.includes("_isoCentre() {") && SRC.includes("p.x += (b.left + b.width / 2) - cx; p.y += (b.top + b.height / 2) - cy; p.auto = false;"));   // the iso group takes the view's angles since the PTZ controls; a face returns above this, so there is no face-or-body body left to name
t('the cards scene and the front carousel draw the face too', SRC.includes("const face = (card.form || card.record || String(card.kind || '').toLowerCase() === 'widget') ? faceHtml(card, wd, S.wsz) : ''; const b0 = isoBody(card, face ? null : wd);") && SRC.includes("const rcFace = (c) => (c.card && (c.card.form || c.card.record || String(c.card.kind || '').toLowerCase() === 'widget') ? faceHtml(c.card, widgetOf(c.card), S.wsz) : '');"));
t('a card carrying a record is a widget to the layout', SRC.includes("const isWidgetCard = (c) => !!(c && (c.tpl || c.form || (c.record && typeof c.record === 'object') || String(c.kind || '').toLowerCase() === 'widget'));") && SRC.includes("const isWidget = isWidgetCard;"));
t('the face is styled at the card, hidden with a tight card until hovered or opened', SRC.includes("vera-exploded .xit-face{display:block;margin-top:4px;min-height:var(--fh,70px)") && SRC.includes("vera-exploded .xit.tight .xit-face{display:none}") && SRC.includes("vera-exploded .xf-diag vera-mermaid{display:block;width:100%;height:110px"));

console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);

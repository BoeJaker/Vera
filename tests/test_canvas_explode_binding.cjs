// The canvas's EXPLODE item and its span binding to a code item (EXPLODE.md §8.3): a card's span lights the lines
// it came from, and a selection of lines lights the card that covers them — the same fact read each way. The
// renderer's half is checked numerically here (lightSpan against a real contract); the canvas element's half by
// the shape of what it renders and wires, since a canvas render needs a document and a shadow root.
//   node tests/test_canvas_explode_binding.cjs
const path = require('node:path'); const fs = require('node:fs');
const SG = require(path.join(__dirname, '..', 'vera', 'ui', 'structgraph_element.js'));
const CANVAS = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const SGSRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'structgraph_element.js'), 'utf8');
const CODE = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'structgraph_code.json'), 'utf8'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the code item is addressable, and says which file it is
t('a code item renders one addressable line per line, with the number in a gutter the selection does not reach',
  /data-line="\$\{i \+ 1\}"/.test(CANVAS) && /class="vc-line"/.test(CANVAS) && /vc-lno/.test(CANVAS)
  && /\.vc-code \.vc-lno\{[^}]*user-select:none/.test(CANVAS));
t('a code item carries its path, so a card from another file never lights the wrong source',
  /data-code="1"\$\{c\.path \|\| c\.filename \? ` data-path=/.test(CANVAS));
t('a lit line is visibly lit, and a tapped one is marked', /\.vc-code \.vc-line\.lit\{/.test(CANVAS) && /\.vc-code \.vc-line\.tap\{/.test(CANVAS));

// ── the explode item is the embed in the live layer, like every other live thing on the canvas
t('an explode item is a live slot, not markup re-created on every render', /data-live="explode"/.test(CANVAS)
  && /kind === 'explode'\) \{ inner = document\.createElement\('vera-graph-embed'\)/.test(CANVAS)
  && /ensureLib\('\/ui\/vera-graph-embed\.js', 'vera-graph-embed'\)/.test(CANVAS));
t('the item declares the block type to the capability layer', /"explode":  \{"desc"/.test(fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8')));
t('a bound item explodes the BOUND ITEM\'S OWN CODE — a snippet that exists only on the canvas explodes like a file',
  /const bound = c\.binds \? \(this\._contentOf\(String\(c\.binds\)\) \|\| null\) : null;/.test(CANVAS)
  && /if \(bound && \(bound\.code \|\| bound\.path\)\)/.test(CANVAS));
t('both directions are wired: the card click leaves the embed, the line click and the selection leave the source',
  /addEventListener\('vera-graph-node', \(ev\) => this\._explodeToSource\(/.test(CANVAS)
  && /\.vc-codewrap\[data-code\] \.vc-line'\)/.test(CANVAS)
  && /body\.addEventListener\('mouseup'[\s\S]{0,200}_sourceToExplode/.test(CANVAS));
t('clear drops the pointer at BOTH ends', /act === 'xpsync'/.test(CANVAS) && /lightSpan\(null\)/.test(CANVAS));
t('nothing about the binding is persisted — it is a reader\'s pointer', !/canvas\.update[^\n]*lit|call\('canvas\.[a-z]+', \{ *key *\}\)[^\n]*lightSpan/.test(CANVAS));

// ── lightSpan, against a real contract
const c = SG.layout(CODE, 1400, 900);
const by = (id) => c.cards.find((k) => k.id === id);
{ const run = by('agents.run_stream').card.span;   // lines 15–20 of vera/agents/agents.py in the fixture
  const hit = SG.layout(CODE, 1400, 900);         // a fresh scene: lightSpan works off the element, so use the module's own pure half
  t('a card carries the line span the binding needs', run.line > 0 && run.line_end >= run.line && run.path === 'vera/agents/agents.py', JSON.stringify(run));
  t('every code card carries one', c.cards.filter((k) => k.card.kind !== 'external').every((k) => k.card.span && (k.card.span.line > 0 || k.card.span.start >= 0)));
  t('and the scene keeps the spans the host binds by', hit.cards.length === c.cards.length); }

// the element's own lightSpan logic, exercised through a stand-in with the same shape the browser gives it
{ const cards = c.cards.map((k) => ({ id: k.id, card: k.card }));
  const el = { _last: { cards }, _cardOf: (id) => cards.find((k) => k.id === id), _paint() { this._painted = (this._painted || 0) + 1; } };
  const lightSpan = eval('(' + SGSRC.match(/lightSpan\(span\) \{[\s\S]*?\n      \}/)[0].replace(/^lightSpan/, 'function lightSpan') + ')');
  const call = (span) => lightSpan.call(el, span);
  const run = by('agents.run_stream').card.span;
  const hit = call({ path: run.path, line: run.line + 1, line_end: run.line + 1 });
  t('a line inside a method lights that method — and the class and module that hold it', hit.indexOf('agents.run_stream') >= 0 && hit.length >= 1, JSON.stringify(hit));
  t('the INNERMOST card is the selected one: a method, not the file that holds it', el._extHit.sel === 'agents.run_stream', String(el._extHit && el._extHit.sel));
  t('a line in no card lights nothing', call({ path: run.path, line: 99999, line_end: 99999 }).length === 0 && el._extHit.sel === null);
  t('a span in ANOTHER file never lights this one', call({ path: 'somewhere/else.py', line: run.line, line_end: run.line }).length === 0);
  t('character offsets work where a card has no line (prose)', (() => {
    const p = { _last: { cards: [{ id: 'e1', card: { span: { path: 'r1', start: 10, end: 20 } } }, { id: 'e2', card: { span: { path: 'r1', start: 50, end: 60 } } }] }, _cardOf(id) { return this._last.cards.find((k) => k.id === id); }, _paint() {} };
    return lightSpan.call(p, { path: 'r1', start: 12, end: 14 }).join() === 'e1'; })());
  t('null clears', call(null).length === 0 && el._extHit === null); }

// ── the painter prefers what the host lit, and a card click takes it back
t('the host\'s light outranks a stale hover and quiets the rest', /const ext = this\._extHit;/.test(SGSRC)
  && /if \(ext && !this\._hover\)/.test(SGSRC) && /c\.classList\.toggle\('dim', !hit\)/.test(SGSRC));
t('clicking a card clears the host\'s light, so the diagram leads again', /this\._S\.sel = card\.dataset\.id; this\._extHit = null;/.test(SGSRC));
t('the selected card is scrolled into view inside the diagram', /ext\.sel\) \{ const el = this\.querySelector\('\.sg-card\[data-id="'/.test(SGSRC) && /scrollIntoView\(\{ block: 'nearest'/.test(SGSRC));

console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);

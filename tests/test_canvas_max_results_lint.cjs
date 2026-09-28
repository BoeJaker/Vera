// The canvas round of 2026-09-27 (owner):
//   * "could the session canvas ... elements like the code and graphs have a button to maximise them in the canvas and fix them there"
//   * "it also needs full code linting in the canvas code block - just like the chat"
//   * "if i press ... the double arrow [it makes] the canvas scroll to the top ... new messages in the chat also make the canvas scroll to the top"
//   * "the canvas tracking of context seems a little broken" - the turn in view changing re-renders the column, which threw it to the top
//   * "as html is being written the canvas render flickers black/white ... it continues to flicker as the chat streams text after the html"
//   * "caps need to be able to return results to the canvas via the appropriate elements"
//   node tests/test_canvas_max_results_lint.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const PY = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the column keeps its scroll through a redraw ──────────────────────────────────────────────────────────────
t('a redraw on the stage puts the column back where it was',
  /if \(stage && !this\.strictAlign\(\) && body\.scrollTop !== keepTop\) body\.scrollTop = Math\.min\(keepTop,/.test(SRC));
t('the turn in view changing still redraws (it is the redraw that must not move the column)', /if \(fm !== this\._focusMid\) \{ this\._focusMid = fm;/.test(SRC));

// ── maximise ────────────────────────────────────────────────────────────────────────────────────────────────────
t('every item carries the maximise control, in both heads', (SRC.match(/data-act="max"/g) || []).length === 2);
t('the control reaches the dispatcher', /if \(act === 'max'\) return this\._toggleMax\(key\);/.test(SRC));
t('the choice is kept per canvas', /'vera:canvas:max:' \+ this\.canvasId/.test(SRC));
t('a maximised item is open', /const open = this\._open\.has\(key\) \|\| editing \|\| this\._max === key;/.test(SRC));
t('the placer leaves it out and puts it over the view', /filter\(\(c\) => c !== maxCard\)/.test(SRC) && /maxCard\.style\.top = mtop \+ 'px'/.test(SRC));
t('the column does not scroll under it', /:host\(\[data-maxed\]\) #body\{overflow:hidden!important\}/.test(SRC));
t('nothing else\'s live content is drawn over it', /if \(this\._max && k !== this\._max\) \{ L\[k\]\.style\.display = 'none'; return; \}/.test(SRC));

// ── the preview never blanks ────────────────────────────────────────────────────────────────────────────────────
t('an html item redraws from its html, not an empty code field', /this\._previewSwap\(el, previewDoc\(h\.dataset\.lang \|\| cc\.lang, cc\.code \|\| cc\.html \|\| ''\)\)/.test(SRC));
t('the first document is remembered, so an unchanged one is not reloaded', /inner\._doc = pd; inner\.srcdoc = pd;/.test(SRC));
t('the next document loads behind the one on screen', /nf\.className = 'vc-pframe vc-pnext'/.test(SRC) && /#live \.lv > iframe\.vc-pnext\{position:absolute;left:0;top:0;opacity:0/.test(SRC));
t('and a swap is at most every half second', /Math\.max\(0, 500 - \(Date\.now\(\) - t0\)\)/.test(SRC));

// ── code: the chat's colours and linter ─────────────────────────────────────────────────────────────────────────
{
  const L = V.splitHl('<span class="hl-com">/* a\nb */</span>\nx<span class="hl-str">"y"</span>');
  t('a comment across lines is coloured on each of them', L.length === 3 && L[0] === '<span class="hl-com">/* a</span>' && L[1] === '<span class="hl-com">b */</span>', JSON.stringify(L));
  t('and the lines after it are clean', L[2] === 'x<span class="hl-str">"y"</span>', L[2]);
  const g = globalThis; const was = g.VeraCode;
  g.VeraCode = { highlight: (c) => c.replace(/def/g, '<span class="hl-kw">def</span>'), lint: () => [{ sev: 'err', msg: 'Unclosed "("' }], summary: () => ({ cls: 'ok', parts: ['2 lines · 20 bytes'] }) };
  const html = V.BLOCK.code({ code: 'def f(\n  return 1', lang: 'python' }, 'm', '', null);
  t('a code item is coloured by the chat\'s highlighter', /<span class="hl-kw">def<\/span>/.test(html));
  t('and carries the chat\'s lint', /class="vc-lint err"/.test(html) && /Unclosed/.test(html) && /2 lines/.test(html), html.slice(-300));
  const el = { _doc: null, render() {} };
  V.BLOCK.code({ code: 'a(', lang: 'js' }, 'm', 'code:k', el);
  const streaming = V.BLOCK.code({ code: 'a(b', lang: 'js' }, 'm', 'code:k', el);
  t('code still being written is not flagged on every beat', /linting when it settles/.test(streaming) && !/Unclosed/.test(streaming));
  if (el._lintT) clearTimeout(el._lintT);
  g.VeraCode = was;
  const plain = V.BLOCK.code({ code: 'x = 1', lang: 'python' }, 'm', '', null);
  t('without the chat (the standalone panel) it is plain numbered lines, as before', /vc-lno/.test(plain) && !/vc-lint/.test(plain));
  t('the chat publishes one highlighter and one linter', /window\.VeraCode=\{ highlight:/.test(CHAT) && /function _codeSummary\(lang, code\)\{/.test(CHAT));
  t('and the code pane reads the same summary', /const r=_codeSummary\(lang, code\);/.test(CHAT));
}

// ── results, as the element that fits them ──────────────────────────────────────────────────────────────────────
{
  const G = V.genericView;
  t('without the chat a record is its fields', G({ host: 'a', up: true }).kind === 'kv');
  t('and anything nested is a tree', G({ a: { b: 1 } }).kind === 'json' && G([1, 2]).kind === 'json');
  t('a line of text is a note, a page of it markdown', G('ok').kind === 'note' && G('x'.repeat(300)).kind === 'markdown');
  const tree = V.jsonTree({ a: [1, 2, { b: 'c' }] }, 0);
  t('the tree opens its first levels', /<details class="j-node" open>/.test(tree) && /j-str/.test(tree));
  const g = globalThis; const was = g.VeraCanvasAdapt;
  g.VeraCanvasAdapt = (cap, res) => (res && res.hosts ? { kind: 'table', content: { columns: ['name'], rows: [['a']] } } : null);
  const r = V.BLOCK.result({ cap: 'docker.hosts', args: { all: true }, result: { hosts: [{ name: 'a' }] }, ok: true, ms: 1200 }, 'm', 'result:docker.hosts:x', null);
  t('a result is drawn as the element the chat\'s registry says it is', /data-as="table"/.test(r) && /<table class="vc-table">/.test(r), r.slice(0, 300));
  t('with the cap, its arguments and how long it took', /<code>docker\.hosts<\/code>/.test(r) && /all=true/.test(r) && /1\.2 s/.test(r));
  t('and a way to run it again', /data-act="rerun"/.test(r));
  const bad = V.BLOCK.result({ cap: 'x.y', ok: false, error: 'boom' }, 'm', 'result:x', null);
  t('a failed run says so', /vc-res-err/.test(bad) && /boom/.test(bad));
  g.VeraCanvasAdapt = was;
  t('run again goes through canvas.run with the same key', /this\.call\('canvas\.run', \{ cap: c\.cap, args: c\.args \|\| \{\}, key \}\)/.test(SRC));
  t('a result\'s live slot reads the element it is drawn as', /if \(b\.type === 'result'\) \{ const v = resultView\(b\.content \|\| \{\}\);/.test(SRC));
  t('the chat hands the element its registry', /window\.VeraCanvasAdapt=_cvAdapt;/.test(CHAT));
  t('the new elements exist', ['json', 'kv', 'chat', 'result', '_many'].every((k) => typeof V.BLOCK[k] === 'function'));
  const ex = V.BLOCK.chat({ messages: [{ role: 'user', text: 'why?' }, { role: 'assistant', name: 'aide', text: '**because**' }] });
  t('an exchange shows who asked and who answered', /vc-msg u/.test(ex) && /vc-msg a/.test(ex) && /aide/.test(ex));
}

// ── the server runs a cap onto the canvas ───────────────────────────────────────────────────────────────────────
t('canvas.run is a capability', /"canvas\.run", memory="off"/.test(PY) && /async def cap_canvas_run\(/.test(PY));
t('it refuses the canvas\'s own caps', /if name\.startswith\("canvas\."\):/.test(PY));
t('the item is keyed by the cap and its arguments', /k = str\(key or ""\)\.strip\(\) or f"result:\{name\}:\{_args_key\(a\)\}"/.test(PY));
t('running it again updates the item', /hit\["content"\] = content/.test(PY) && /resolved = "updated"/.test(PY));
t('an answer too big to keep is bounded, not dropped', /_RUN_MAX_CHARS = 300_000/.test(PY) && /"_truncated": True/.test(PY));
t('a slow capability times out, and a synchronous one runs off the event loop', /asyncio\.wait_for\(fn\(\*\*a, trace_id=trace_id\), timeout=limit\)/.test(PY) && /asyncio\.to_thread\(lambda: fn\(\*\*a, trace_id=trace_id\)\)/.test(PY));
t('the new kinds are declared', ['result', 'json', 'kv', 'chat'].every((k) => new RegExp('"' + k + '":\\s+\\{"desc"').test(PY)));
t('and the model is told it can do this', /A CAPABILITY\\'S ANSWER ONTO THE CANVAS/.test(CHAT) && /canvas\.run/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

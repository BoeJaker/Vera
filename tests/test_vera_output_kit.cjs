// 2026-10-03: window.VeraOut - one code card, one highlighter, one linter for the chat, the agent loop and the canvas
//   node tests/test_vera_output_kit.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera_output_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

// the smallest page the kit boots in: no markdown element, so render() uses its own fence pass
const noop = () => {}; const head = { querySelector: () => null, insertBefore: noop, appendChild: noop, firstChild: null };
global.window = {}; global.customElements = { get: () => null };
global.document = { readyState: 'complete', head, addEventListener: noop, querySelector: () => null, createElement: () => ({ setAttribute: noop }) };
new Function(SRC)();
const V = window.VeraOut;
t('the kit loads and says what it is', V && V.version >= 1 && typeof V.card === 'function');

// ── the card ──
const py = V.card('python', "print('x'", { filename: 'a.py' });
t('a card carries the chat\'s markup the harvesters read', /class="code-card" data-cb="cb\d+"/.test(py) && py.includes('<code class="language-python">') && py.includes('class="code-exec-out"'));
t('a python card: copy, edit, explode, run, save, canvas', ['copy', 'edit', 'explode', 'run', 'save', 'canvas'].every((a) => py.includes('data-act="' + a + '"')) && !py.includes('data-act="preview"'));
t('buttons carry data-act, never a host global', !/onclick=/.test(py) && !py.includes('CH.'));
const page = V.card('html', '<!doctype html><html><body>x</body></html>');
t('a whole page previews on sight; a page card has preview + pop out, no run', page.includes('data-auto-prev="1"') && page.includes('data-act="preview"') && page.includes('data-act="pop"') && !page.includes('data-act="run"'));
const frag = V.card('html', '<b>x</b>');
t('a fragment waits to be asked', !frag.includes('data-auto-prev'));
const live = V.card('python', 'def f(', { incomplete: true });
t('a block still being typed is marked, with no actions', live.includes('data-incomplete="1"') && live.includes('cb-stream') && !live.includes('data-act='));
const id = (py.match(/data-cb="(cb\d+)"/) || [])[1];
t('the registry holds the source', V.blocks[id] && V.blocks[id].code === "print('x'" && V.blocks[id].filename === 'a.py');
t('no canvas button where the host says so', !V.card('js', 'x', { canvas: false }).includes('data-act="canvas"'));

// ── highlight, lint ──
t('keywords, strings, comments are tokens', V.highlight('def f(): return "s" # c', 'python').includes('<span class="hl-kw">def</span>')
  && V.highlight('def f(): return "s" # c', 'python').includes('hl-str') && V.highlight('x # c', 'python').includes('hl-com'));
t('markup is escaped', !V.highlight('<script>alert(1)</script>', 'text').includes('<script>'));
t('lint: an unclosed bracket, bad JSON, mixed indentation', V.lintCode('python', 'print(1').some((d) => /Unclosed/.test(d.msg))
  && V.lintCode('json', '{"a":}').some((d) => /Invalid JSON/.test(d.msg)) && V.lintCode('python', 'if x:\n\tpass\n  y').some((d) => /Mixed/.test(d.msg)));
t('lint: clean code says nothing', V.lintCode('python', 'print(1)').length === 0 && V.lintCode('json', '{"a":1}').length === 0);

// ── markdown ──
const out = V.render('Hi\n\n```js file=app.js\nlet a = 1\n```\n\n```mermaid\ngraph TD; A-->B\n```');
t('render: fences become cards, with their file', out.includes('class="code-card"') && out.includes('app.js'));
t('render: a diagram fence is a diagram slot, not code', out.includes('class="vo-mm"'));
t('render: a host may draw a fence itself', V.render('```x\n1\n```', { fence: () => '<i>mine</i>' }).includes('<i>mine</i>'));
t('fence filenames: file=, path=, bare', V.fenceFilename('file=app.py') === 'app.py' && V.fenceFilename('path=src/x.ts') === 'src/x.ts' && V.fenceFilename('python main.py') === 'main.py');
t('a script previews in a page that reports its error', V.previewDoc('js', 'boom()').includes('catch(e)') && V.previewDoc('html', '<p>') === '<p>');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

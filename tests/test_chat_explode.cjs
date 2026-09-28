// Explode in the chat (EXPLODE-R2.md R6): a message action, a code fence's own button, and /explode <what>.
// The gate does not check panel JS, and chat_panel.html is one 1.4MB file, so this pulls the pieces out and runs
// them: the fence picker as a function, and the three entry points as wiring that actually resolves.
//   node tests/test_chat_explode.cjs
const path = require('node:path'), fs = require('node:fs');
const HTML = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const EMBED = fs.readFileSync(path.join(__dirname, '..', 'vera', 'graph_embed_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the fence picker, as a function ──────────────────────────────────────────────────────────────────────────
const src = HTML.match(/function _xpFences\(text\)\{[\s\S]*?\n  \}/);
t('the fence picker is there to be tested', !!src);
const _xpFences = src ? eval('(' + src[0].replace(/^function /, 'function ') + ')') : () => [];

{ const one = _xpFences('here you go:\n```python\ndef f():\n    return 1\n```\nand that is it');
  t('a fenced block is found, with its language and its size',
    one.length === 1 && one[0].lang === 'python' && one[0].lines === 3 && /def f/.test(one[0].code),
    JSON.stringify(one));
  const two = _xpFences('```js\nconst a=1;\n```\ntext\n```python\n' + Array.from({ length: 20 }, (_, i) => 'x = ' + i).join('\n') + '\n```');
  t('the biggest block leads — a message with code in it is usually about that code',
    two.length === 2 && two[0].lang === 'python' && two[0].lines > two[1].lines, JSON.stringify(two.map((f) => [f.lang, f.lines])));
  t('an empty fence is not a code block', _xpFences('```\n\n```').length === 0);
  t('a fence with a filename on its info line still reads its language',
    _xpFences('```python app.py\nx = 1\n```')[0].lang === 'python');
  t('prose alone has no fences', _xpFences('just a passage, no code at all').length === 0); }

// ── the three ways in ────────────────────────────────────────────────────────────────────────────────────────
t('a message carries an Explode action, beside Copy',
  /onclick="CH\._msgExplode\('\$\{mid\}',this\)"/.test(HTML) && /_msgExplode/.test(HTML.split('const api=')[0]));
t('a code fence carries its own Explode button, beside Copy',
  /onclick="CH\._codeExplode\('\$\{id\}',this\)"/.test(HTML));
t('/explode is a slash command, and says what it takes',
  /\{name:'explode',[^}]*run:_slashExplode\}/.test(HTML) && /path \| record \| node \| passage/.test(HTML));
t('both handlers are exported on CH, or the onclick resolves to nothing',
  /_msgExplode,_codeExplode,/.test(HTML));
t('the diagram is the shared embed, not a second renderer',
  /createElement\('vera-graph-embed'\)/.test(HTML) && /setAttribute\('renderer','struct'\)/.test(HTML));
t('the slash command asks the SERVER what a thing is, rather than guessing it is a record',
  /fetch\(BASE\+'\/explode\/target'/.test(HTML) && /tg\.why/.test(HTML));
t('a second Explode on the same thing closes the first, rather than stacking diagrams',
  (HTML.match(/const old=wrap\.querySelector\(':scope > \.xp-inline'\); if\(old\)\{ old\.remove\(\); return; \}/) || []).length === 1
  && /const old=card\.querySelector\(':scope > \.xp-inline'\); if\(old\)/.test(HTML));
t('the embed library is loaded once, on demand, not on every chat load',
  /_XP_LIB = new Promise/.test(HTML) && /ui\/vera-graph-embed\.js/.test(HTML));

// ── what the embed can be asked for ─────────────────────────────────────────────────────────────────────────
t('the embed can be asked for one function\'s flow', /var flow = this\.getAttribute\('flow'\); if \(flow\) body\.flow = flow;/.test(EMBED));
t('the embed can be asked for several records, as lanes', /body\.record_ids = this\.getAttribute\('records'\)/.test(EMBED));

console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);

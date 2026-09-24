// What a capability RESULT becomes on the session canvas (vera/chat/chat_panel.html).
//
// Every cap card landed on the canvas as `markdown` holding a fenced dump of its own result text, cut at 2000
// characters. A command's output, a list of docker hosts and a set of numbers all arrived as the same wall of
// text: nothing sortable, nothing chartable, nothing you could explore. That is the "it's not always going to
// have things it can represent" problem from the other end - it always had something, and always threw it away.
//
// Two things were wrong and only one of them was the drawing:
//
//   1. THE RESULT WAS NOT KEPT. executeCapInline turns `content` into pixels and drops it; the only trace left
//      in the DOM is the serialised text inside .cap-result, already truncated for display. So the harvest had
//      nothing to work from but that text. It is now kept on the card as el.__cap.
//   2. THE CHAT ALREADY KNEW BETTER. _widgetFormByShape picks a form for a result's shape, and the transcript
//      draws research reports and image galleries properly. The canvas simply never asked. These recognisers
//      reuse that rule rather than growing a second one beside it.
//
// A result that matches nothing still falls through to markdown - that is the honest representation of a thing
// with no shape, and the point of the fallback is that it is REACHED, not that it is never reached.
//
// This executes the recognisers the page actually ships, cut out of chat_panel.html by their markers.
//   node tests/test_canvas_cap_harvest.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the result is kept on the card in the first place ──────────────────────────────────────────────────────────
t('executeCapInline keeps the result on the card',
  /try\{ el\.__cap=\{ name:capName, args:coercedArgs, content, err:isErr \}; \}catch\(_\)\{\}/.test(src));
t('and the harvest reads it rather than the rendered text',
  /const cap = el\.__cap \|\| null, cres = cap && !cap\.err \? cap\.content : null;/.test(src));
t('a terminal lands big enough to read', /if\(k==='session'\) return 'm';/.test(src));

// ── cut the three recognisers out and run them ─────────────────────────────────────────────────────────────────
const A = "  const CV_ADAPTERS=[];", B = "  function _cvHarvest(body){";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the recognisers are present in the page', i0 >= 0 && i1 > i0);
if (i0 < 0 || i1 < i0) { console.log(fails + ' FAILED'); process.exit(1); }
const block = src.slice(i0, i1);

// the two helpers they lean on are the chat's own shape rule; the real ones are further up the same file, so the
// test gives them the same contract rather than a different one
const ctx = {
  _widgetData: (x, form) => {
    if (Array.isArray(x)) return x;
    if (x && typeof x === 'object') {
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'values', 'entries', 'results']) {
        if (Array.isArray(x[k])) return x[k];
      }
    }
    return x;
  },
  _widgetFormByShape: (x) => {
    if (Array.isArray(x) && x.length) {
      const o = x[0];
      if (o && typeof o === 'object') {
        if (('t' in o || 'ts' in o) && ('v' in o || 'value' in o)) return 'trace';
        return 'table';
      }
      if (x.every((v) => typeof v === 'number')) return 'trace';
    }
    if (x && typeof x === 'object') {
      if (typeof x.value === 'number' && ('min' in x || 'max' in x)) return 'radial';
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) {
        if (x[k] != null) return ctx._widgetFormByShape(x[k]);
      }
    }
    return null;
  },
  _widgetRecord: (o) => ({ name: o.name, form: o.form, draw: { form: o.form, size: 'm' }, reads: { cap: o.source } }),
  // the page has URL; a bare vm context does not, and the source recogniser reads a domain off the address with it
  URL,
  // the registry reads a cap's declared Output: out of the descriptions the chat already holds
  ALL_CAPS: [
    { name: 'exec.bash.run', description: 'Run a command. Output: {ok, rc, stdout, stderr, elapsed_ms, command}.' },
    { name: 'research.report', description: 'Write a report. Output: {report, sources}.' },
    { name: 'quiet.status', description: 'A status line. Output: {ok, text}.' },
  ],
};
vm.createContext(ctx);
vm.runInContext(block + '\nthis.T=_cvCapTerm; this.B=_cvCapTable; this.W=_cvCapWidget; this.S=_cvCapSources; this.AD=_cvAdapt; this.REG=CV_ADAPTERS;', ctx);
const { T, B: TBL, W, S: SRC, AD, REG } = ctx;

// ── a command and its output is a terminal ─────────────────────────────────────────────────────────────────────
{
  const r = T({ ok: true, rc: 0, command: 'docker ps --format "{{.Names}}"', stdout: 'vera-dev\nvera-redis\n', stderr: '', elapsed_ms: 42 });
  t('exec output becomes a terminal card, not prose', !!r && r.kind === 'session', JSON.stringify(r && r.kind));
  t('it carries the command and the output', !!r && r.content.command.startsWith('docker ps') && /vera-redis/.test(r.content.output));
  t('and says how it went', !!r && / rc 0/.test(r.d) && /42ms/.test(r.d), r && r.d);
  const err = T({ ok: false, rc: 1, command: 'ls /nope', stdout: '', stderr: 'No such file or directory' });
  t('a failing command keeps its stderr', !!err && /No such file/.test(err.content.output) && /rc 1/.test(err.d));
  t('something with no command is not a terminal', T({ ok: true, hosts: [] }) === null && T(null) === null);
  t('a command with no output at all is not a terminal card', T({ command: 'true', rc: 0 }) === null);
}

// ── rows of the same shape are a table ─────────────────────────────────────────────────────────────────────────
{
  const hosts = [{ name: 'gpu-250', cpu: 12, mem: '64G' }, { name: 'cpu-246', cpu: 40, mem: '32G' }];
  const r = TBL({ ok: true, hosts }, 'docker.hosts');
  t('a list of records becomes a table', !!r && r.kind === 'table', JSON.stringify(r && r.kind));
  t('with real columns, not a blob', !!r && r.content.columns.join(',') === 'name,cpu,mem', r && r.content.columns.join(','));
  t('and its rows in order', !!r && r.content.rows[0][0] === 'gpu-250' && r.content.rows[1][1] === '40');
  t('the row count is in the label', !!r && /2 rows/.test(r.d));
  // the guards: one column is a list, not a table; and nested objects are not cells
  t('a single-column list is not forced into a table', TBL({ items: [{ name: 'a' }, { name: 'b' }] }, 'x') === null);
  /* the envelope key is not in _widgetData's fixed list, and results are named for what they hold far more
     often than they are named `data` - this is the case that was falling through to the JSON dump */
  t('rows are found whatever the envelope calls them', !!TBL({ ok: true, containers: [{ id: 'a', state: 'up' }] }, 'x'));
  t('and the authored key still wins when there is one',
    TBL({ data: [{ a: 1, b: 2 }], other: [{ z: 9, y: 8 }] }, 'x').content.columns.join(',') === 'a,b');
  t('an empty result is not a table', TBL({ ok: true, hosts: [] }, 'x') === null && TBL(null, 'x') === null);
  // 80 rows is the cap, so a huge result cannot blow the canvas item up
  const many = Array.from({ length: 300 }, (_, i) => ({ id: i, v: i * 2 }));
  t('a huge result is capped', TBL({ rows: many }, 'x').content.rows.length === 80);
}

// ── a shape the widget rule recognises is a chart ──────────────────────────────────────────────────────────────
{
  const r = W({ value: 62, min: 0, max: 100 }, 'gpu.load', {});
  t('a gauge-shaped result becomes a widget', !!r && r.kind === 'widget' && r.content.form === 'radial', JSON.stringify(r && r.content && r.content.form));
  t('it carries its data', !!r && r.content.data && r.content.data.value === 62);
  t('and names the cap it came from', !!r && r.n === 'gpu.load');
  const series = W({ points: [{ t: 1, v: 3 }, { t: 2, v: 5 }] }, 'perf.trace', {});
  t('a time series becomes a trace', !!series && series.content.form === 'trace');
  /* a table-shaped result must NOT come back as a widget: the table recogniser above it builds real columns,
     and a widget would have swallowed it into a form with none */
  t('a table shape is left to the table recogniser', W({ hosts: [{ name: 'a', cpu: 1 }] }, 'x', {}) === null);
  t('a shapeless result is not a widget', W({ ok: true, note: 'done' }, 'x', {}) === null && W(null, 'x', {}) === null);
}

// ── and the fallback is still reachable, which is the point of it ──────────────────────────────────────────────
{
  const shapeless = { ok: true, note: 'nothing to draw here' };
  t('a result with no shape matches no recogniser', T(shapeless) === null && TBL(shapeless, 'x') === null && W(shapeless, 'x', {}) === null);
  t('so the harvest still falls through to markdown', /out\.push\(\{kind:'markdown', n:n\.split\(' '\)\[0\]/.test(src));
  t('and a card from a restored session, with no kept result, falls through too',
    /const cap = el\.__cap \|\| null/.test(src) && /if\(made\)\{ out\.push\(made\); return; \}/.test(src));
  t('a FAILED cap is never drawn as data', /cap && !cap\.err \? cap\.content : null/.test(src));
}

// ---- a web result is a list of PAGES, not a grid of truncated cells --------------------------------------------
{
  const res = { ok: true, results: [
    { url: 'https://example.com/a', title: 'The first page', snippet: 'about a thing' },
    { url: 'https://other.org/b', title: 'The second', description: 'about another' },
    { url: 'https://third.net/c', name: 'Third' }] };
  const r = SRC(res);
  t('a search result becomes source items', Array.isArray(r) && r.length === 3, JSON.stringify(r && r.length));
  t('each one is a source', !!r && r.every((x) => x.kind === 'source'));
  t('with its domain read off the address', !!r && r[0].content.domain === 'example.com' && r[1].content.domain === 'other.org');
  t('the snippet is taken from whichever field carried it', !!r && r[0].content.snippet === 'about a thing' && r[1].content.snippet === 'about another');
  /* keyed by the PAGE, not by where in the reply it was found - otherwise the same page lands once as the run
     announced it and again as the reply cited it, and again on every later turn that mentions it */
  t('keyed by the page', !!r && r[0].key === 'source:example.com/a');
  t('and the harvest honours an item that names its own key', /const key=m\.key\|\|\('turn:'\+mid/.test(src));
  // the guards
  t('one link is not a reading list', SRC({ results: [{ url: 'https://only.one/x', title: 'x' }] }) === null);
  t('rows with no addresses are left to the table', SRC({ rows: [{ name: 'a', cpu: 1 }, { name: 'b', cpu: 2 }] }) === null);
  t('a search is capped, the canvas is not a results page', SRC({ results: Array.from({ length: 40 }, (_, i) => ({ url: 'https://x.io/' + i, title: 't' + i })) }).length === 10);
  /* the chain of ifs this used to pin is a registry now - the claim is the same one, that a page with an address
     is a source before it is a row, and it is made by the ORDER of the table rather than by the order of ||s */
  t('sources are tried BEFORE the table', REG.map((a) => a.name).indexOf('sources') < REG.map((a) => a.name).indexOf('table'));
  t('and the harvest asks the registry rather than a chain', /made = _cvAdapt\(cname, cres, cap && cap\.args\)/.test(src));
  t('and a recogniser may answer with several items', /if\(Array\.isArray\(made\)\)\{ made\.forEach/.test(src));
}

// ---- the research run announces its pages as it reads them -----------------------------------------------------
{
  const card = fs.readFileSync(path.join(__dirname, '..', 'vera', 'research_card_element.js'), 'utf8');
  t('a crawled page is announced', /vera:research:source/.test(card) && /addPage\(url, domain, chars, failed\)/.test(card));
  t('and so is a cited one', (card.match(/vera:research:source/g) || []).length >= 2);
  t('the card asks rather than writing to a canvas it does not own', !/canvas\.add/.test(card));
  t('the chat listens and lands it', /document\.addEventListener\('vera:research:source'/.test(src));
  t('keyed by url so a re-run updates rather than doubles', /const key='source:'\+url\.replace/.test(src));
  t('and it lands WITHOUT the page body or picture', /content:\{ url, title:String\(d\.title\|\|''\), domain, chars:\+d\.chars\|\|0, failed:!!d\.failed \}/.test(src));
  /* ...but not as a bare headline. The crawl knows how much it read and passes on none of it, so the card was an
     address and a title until pressed - a reading list of headlines is a bibliography. The first few get a
     snippet; the rest wait to be asked, which is the bargain the screenshot makes too. */
  t('the first few sources arrive with something to read', /if\(!d\.failed\) _cvSnippet\(key, url\);/.test(src));
  t('bounded, because a deep run reads dozens', /if\(_cvSnipN>=3\) return; _cvSnipN\+\+;/.test(src));
  t('and short, because it is a preview and not the page', /max_chars:600/.test(src) && /txt\.slice\(0,400\)/.test(src));
  t('a page that would not load is not fetched for a preview of nothing', /if\(!d\.failed\)/.test(src));
  t('and one that already has a snippet is left alone', /if\(!b\|\|!b\.content\|\|b\.content\.snippet\) return;/.test(src));
}

// ---- the item fetches the page only when it is opened ----------------------------------------------------------
{
  const cv = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
  t('the canvas draws a source', /^    source: \(c, size, key, el\) => \{/m.test(cv));
  t('the page body is fetched on demand, not on landing', /browser\.content/.test(cv) && /_srcAct\(key, act\)/.test(cv));
  t('and so is the picture', /browser\.screenshot/.test(cv));
  /* the field name the cap actually answers with. It returns {ok, image_b64, url, title, load_ms}, and reading
     image/data_url/screenshot/png instead meant the picture was fetched every time and thrown away every time -
     the press did nothing, silently, which is the worst way for it to fail. Measured against the live cap. */
  t('the screenshot is read from the field the cap answers with', /r\.image_b64 \|\| r\.image/.test(cv));
  t('and the data url names the format the bytes actually are', /\/\^\\\/9j\\\/\/\.test\(shot\) \? 'image\/jpeg'/.test(cv));
  t('browser.content is asked with max_chars, which is its parameter', /browser\.content', \{ url: String\(c\.url\), max_chars: 40000 \}/.test(cv));
  /* the add bar is STICKY, so with no background the canvas scrolls under the buttons and they become
     unreadable. "blocks off" is a preference about ITEMS; it cannot be allowed to take the floor out from
     under a toolbar - and it bit because the server's appearance seed has blocks off for every new device. */
  /* THE BANNER LOSES ITS GROUND WITH BLOCKS OFF and keeps a blur instead (owner, 2026-09-24). It is sticky, so
   something must hold it readable over what scrolls beneath — but a plate is a block, and blocks-off means none. */
  t('the add bar has a ground by default, and a blur rather than one when blocks are off',
    /\.addbar\{position:sticky;[^}]*background:var\(--bg1/.test(cv) &&
    /:host\(\[blocks="off"\]\) \.addbar\{background:none;backdrop-filter:blur/.test(cv));
  t('what comes back is written into the item, so the second look is free', /canvas\.update', \{ key, content: Object\.assign\(\{\}, c, \{ text:/.test(cv));
  t('a second press is only the fold, not a second fetch', /if \(c\.text\) \{ this\._srcOpen\[key\] = !this\._srcOpen\[key\]/.test(cv));
  t('a source that would not load says so rather than being tidied away', /c\.failed \? `<span class="vc-src-n bad">did not load<\/span>`/.test(cv));
  t('the buttons reach the handler', /\[data-src-act\]/.test(cv));
  const py = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
  t('and the block type is declared server-side', /"source":\s*\{"desc"/.test(py) && /url:str, title\?:str/.test(py));
}

// ---- the registry: one ordered table, and adding a kind is appending to it ------------------------------------
{
  /* 2513 capabilities across 150 namespaces. Whatever decides what a result becomes cannot be a function per
     capability, and it cannot be a chain of ifs someone edits in the middle each time a cap is added. */
  t('the rules are a registry, in order', Array.isArray(REG) && REG.length >= 8, REG && REG.length);
  t('each one says what it recognises and what it builds',
    REG.every((a) => a.name && typeof a.when === 'function' && typeof a.build === 'function'));
  /* the order IS the behaviour, so it is pinned - a rule that moves above another changes what results become.
     calendar sits above table because the diary drawn as rows of id/title/start/end is the data and not the
     answer; prose sits last because almost every result carries some string. */
  t('the order is the one that matters', REG.map((a) => a.name).join(',') === 'terminal,sources,html,image,calendar,table,widget,prose',
    REG.map((a) => a.name).join(','));

  // and it routes, by shape alone, with no capability named anywhere
  const kind = (cap, res) => { const m = AD(cap, res, {}); return m ? (Array.isArray(m) ? m[0].kind + '[]' : m.kind) : null; };
  t('a command goes to a terminal', kind('exec.bash.run', { command: 'ls', stdout: 'a\nb', rc: 0 }) === 'session');
  t('rows go to a table', kind('docker.hosts', { hosts: [{ name: 'a', cpu: 1 }, { name: 'b', cpu: 2 }] }) === 'table');
  t('pages go to sources', kind('web.search', { results: [{ url: 'https://a.io', title: 'A' }, { url: 'https://b.io', title: 'B' }] }) === 'source[]');
  t('a gauge goes to a widget', kind('gpu.load', { value: 62, min: 0, max: 100 }) === 'widget');
  t('html goes to the html block', kind('render.html', { html: '<h1>hi</h1>' }) === 'html');
  t('an image goes to an image', kind('browser.screenshot', { image_b64: 'iVBORw0KG' }) === 'image');
  t('a written report goes to markdown, not a code fence', kind('research.report', { report: 'x'.repeat(400) }) === 'markdown');
  t('and a shapeless result matches nothing at all', kind('some.cap', { ok: true, note: 'done' }) === null);
}

// ---- the declaration is a hint, never the decision -------------------------------------------------------------
{
  /* 1680 of the 2513 caps declare Output: {...} in their own description, and the chat already has every one of
     them. It settles a tie the shape cannot: {ok, text} is a page from one cap and a status line from another. */
  const m = AD('exec.bash.run', { command: 'true', stdout: '', stderr: '', rc: 0 }, {});
  t('a cap that SAYS it returns stdout is a command even when it printed nothing', !!m && m.kind === 'session', JSON.stringify(m && m.kind));
  // ...and a short string from a cap that declares no report is not dressed up as one
  t('a short status line is not made into a document', AD('quiet.status', { ok: true, text: 'done' }, {}) === null);
  t('but a long one is the document it plainly is', (AD('quiet.status', { ok: true, text: 'y'.repeat(300) }, {}) || {}).kind === 'markdown');
  t('the declaration cannot overrule the bytes', (AD('research.report', { hosts: [{ a: 1, b: 2 }, { a: 3, b: 4 }] }, {}) || {}).kind === 'table');
}

// ---- the override, for the handful the rules read wrongly -------------------------------------------------------
{
  /* a canvas.* result describes what the canvas just did - drawing the receipt would put a receipt on the canvas,
     which is the one place it must not go */
  t('a namespace can be sent nowhere', AD('canvas.add', { ok: true, key: 'x', item: { id: 'b1' } }, {}) === null);
  t('and a single cap too', AD('ui.directive', { ok: true, row: { name: 'x' } }, {}) === null);
  t('the override is a table, one line per correction', /const CV_CAP_KIND=\{/.test(src) && /'canvas\.\*':''/.test(src));
  t('a namespace override is found from the cap name', /const ns=n\.split\('\.'\)\[0\]\+'\.\*'/.test(src));
}

// ---- and the sizes the new kinds land at ------------------------------------------------------------------------
{
  t('a rendered page is not put in a letterbox', /if\(k==='html'\) return 'm';/.test(src));
  t('a report is not a footnote to itself', /if\(k==='prose'\) return 'm';/.test(src));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

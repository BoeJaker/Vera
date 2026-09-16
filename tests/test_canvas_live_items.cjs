// The Canvas board's LIVE ITEMS (vera/canvas/canvas_element.js; A16 of Notes/42): a live terminal (<vera-terminal> over
// the estate's terminal WebSocket), a notebook cell (the notebook's own cell; Run through its exec; Open), a whole
// panel driven over the one bridge (query · dispatch · refresh · standalone; the add bar's panel picks from the
// panels open for the session) — the pure item bodies, the run-command mapping, and the source strings the live layer
// is held together by.   node tests/test_canvas_live_items.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const B = V.BLOCK;
t('the item bodies are exported (version 4 — the pickers of test_canvas_backing.cjs came with it)', !!B && typeof B.session === 'function' && typeof B.notebook === 'function' && typeof B.panel === 'function' && V.version >= 3);

// ── the live terminal ──
const ssh = B.session({ title: 'Terminal', host_id: 'ct126', shell: '' }, 'm', 'session:t1', { _live: {} });
t('a terminal with an ssh host is a live slot over the ssh terminal socket', /data-live="term"/.test(ssh) && /data-ws="\/remote\/ssh\/term\/ws\/ct126\?shell="/.test(ssh) && /data-key="session:t1"/.test(ssh));
t('its head: the host, the shell, detach, share', /<b>ct126<\/b>/.test(ssh) && /login shell/.test(ssh) && /data-act="tdetach"/.test(ssh) && /data-act="tshare"/.test(ssh));
const dk = B.session({ host_id: 'local', container: 'vera-mirror', shell: 'bash' }, 'm', 'session:t2', { _live: {} });
t('a container on a host is the docker terminal socket', /\/remote\/docker\/term\/ws\/local\/vera-mirror\?shell=bash/.test(dk) && /local \/ vera-mirror/.test(dk));
const none = B.session({ title: 'pytest', host: 'ct126', command: 'pytest -q', output: '20 passed' }, 'm', 'session:t3', { _live: {} });
t('a session with no host id keeps its results and offers the connect row (host prefilled), never a socket', !/data-live/.test(none) && /data-act="tconnect"/.test(none) && /value="ct126"/.test(none) && /20 passed/.test(none) && /pytest -q/.test(none));
const det = B.session({ host_id: 'ct126', attached: false }, 'm', 'session:t4', { _live: {} });
t('a detached terminal keeps its host and offers attach', !/data-live/.test(det) && /data-act="tattach"/.test(det));
t('the live dot: waiting until the element exists, on once it does', /dot wait/.test(ssh) && /dot on/.test(B.session({ host_id: 'ct126' }, 'm', 'session:t5', { _live: { 'session:t5': {} } })));
t('a session with no key (a plain block) draws no live slot', !/data-live/.test(B.session({ host_id: 'ct126' }, 'm', undefined, null)));

// ── the notebook cell ──
const code = B.notebook({ notebook_id: 'nb1abcdef00', cell_id: 'c1', cell_type: 'code', lang: 'python', content: 'print(1)', title: 'Sum', generated: '1' }, 'm', 'notebook:nb1:c1');
t('a code cell: its type and language, its source, its output, Run and Open', /code · python/.test(code) && /<b>Sum<\/b>/.test(code) && /print\(1\)/.test(code) && /data-act="nbrun"/.test(code) && /data-act="nbopen"/.test(code) && /vc-nbout"><code>1<\/code>/.test(code));
const mdc = B.notebook({ notebook_id: 'nb1', cell_id: 'c2', cell_type: 'markdown', content: '# Plan\n- one' }, 'm', 'notebook:nb1:c2');
t('a markdown cell renders and has no Run; an empty output stays hidden', /<h1>Plan<\/h1>/.test(mdc) && !/nbrun/.test(mdc) && /vc-nbout" hidden/.test(mdc));
t('the run command is the notebook page\'s own: a python heredoc, node, ruby, else the shell snippet', /^python3 - <<'VERA_NB_EOF_\w+'\nprint\(1\)\nVERA_NB_EOF_/.test(V.langRunCmd('python', 'print(1)')) && /^node - <</.test(V.langRunCmd('js', '1')) && V.langRunCmd('bash', 'ls -la') === 'ls -la' && V.langRunCmd('', 'echo x') === 'echo x');

// ── the whole panel ──
const pn = B.panel({ panel: 'workers', title: 'Workers' }, 'l', 'panel:workers', { _pq: {}, _pdOpen: {} });
t('a panel item frames the panel page (its route by default) and carries the bridge actions', /data-live="frame"/.test(pn) && /data-src="\/ui\/panels\/workers"/.test(pn) && /over the bridge/.test(pn) && ['pquery', 'pdispatch', 'prefresh', 'pstand'].every(a => pn.includes('data-act="' + a + '"')));
t('the dispatch row is hidden until asked; the readout hidden until there is one', /vc-bridge" hidden/.test(pn) && /vc-pq" hidden/.test(pn));
const pn2 = B.panel({ panel: 'dag', src: '/ui/panels/dag-workshop' }, 'l', 'panel:dag', { _pq: { 'panel:dag': { text: '{"ok":true}' } }, _pdOpen: { 'panel:dag': true } });
t('a resolved page wins over the route; an answered query shows; the dispatch row opens', /data-src="\/ui\/panels\/dag-workshop"/.test(pn2) && /vc-pq"><code>\{&quot;ok&quot;:true\}/.test(pn2) && /vc-bridge" data-w/.test(pn2));
t('a panel item with no id says so', /no panel id/.test(B.panel({}, 'm', 'panel:', {})));

// ── the vocabulary on the add bar and the glyphs ──
t('the add bar: the terminal seeds a host-less terminal, the panel picks from the open set', V.ADD_KINDS.some(k => k.n === 'terminal' && k.kind === 'session' && 'host_id' in k.content) && V.ADD_KINDS.some(k => k.n === 'panel' && k.kind === 'panel' && k.pick === true));
t('notebook and panel items carry their glyphs', V.KIND_GLYPH.notebook === 'NB' && V.KIND_GLYPH.panel === '▥');

// ── the live layer, in the source ──
t('the render writes the items layer, never the live layer', SRC.includes("this._layers(body).items.innerHTML = html;") && SRC.includes("live.id = 'live'") && !SRC.includes("      body.innerHTML = html;\n      if (!stage) body.scrollTop = keepTop;"));
t('the live elements are mounted after the placement and placed again after every placement, scroll and resize', SRC.includes("if (stage) this._placeNow();\n      this._mountLive(body);") && SRC.includes("this._placed = P;\n      this._liveLayout();") && SRC.includes("body.addEventListener('scroll', () => this._liveLayout())") && SRC.includes("this._liveRO = new ResizeObserver(() => this._liveLayout())"));
t('a live element is wrapped (the estate element keeps its own styles) and is never re-created by a render', SRC.includes("el = document.createElement('div'); el.className = 'lv'; el.dataset.kind = kind; el.dataset.key = key; el.appendChild(inner); L[key] = el; live.appendChild(el);") && SRC.includes("let el = L[key];"));
t('a folded item hides its live element, still connected', SRC.includes("if (!h || !h.getClientRects().length) { el.style.display = 'none'; return; }"));
t('the terminal element is loaded once from the page; the slow tick stops with the element', SRC.includes("ensureLib('/ui/vera-terminal.js', 'vera-terminal')") && SRC.includes("if (this._liveTick) { clearInterval(this._liveTick); this._liveTick = null; }"));
t('the bridge is asked through the same /mcp/call, its reply back on the item', SRC.includes("async callResult(name, args) {") && SRC.includes("name = 'panel.dispatch'; args = { session_id: this._sid(), panel: id, action, payload }") && SRC.includes("this.callResult('ui.panels.open', { session_id: this._sid() })"));
t('the cell runs through the notebook\'s exec SSE and its output lands on the cell and the item', SRC.includes("fetch(api + '/ide-api/exec/run'") && SRC.includes("'/cells/' + encodeURIComponent(c.cell_id), { method: 'PATCH'") && SRC.includes("return this.call('canvas.update', { key, content: Object.assign(c, { generated: text }) });"));
t('the /mcp/call envelope is opened either way: prod {type, tool_name, content}, a stand-in {result}', V.unwrap({ type: 'tool_result', tool_name: 'panel.query', content: { ok: true, panels: [1] } }).panels.length === 1 && V.unwrap({ result: { ok: 1 } }).ok === 1 && V.unwrap({ ok: true, panels: [] }).ok === true);
t('the host is asked for the panel\'s page after the item exists', SRC.includes("return this.call('canvas.add', args).then(() => { try { this.dispatchEvent(new CustomEvent('vera:canvas:panel-src'"));
t('nothing is placed under the sticky heads: the placement floors at the add bar + NOW head', SRC.includes("const pad = (bar ? bar.offsetHeight : 0) + (bh ? bh.offsetHeight : 0);") && /\{ columns: cols, gap, colWidth: w, pad, view: [^}]+\}\);/.test(SRC));
t('the host hears the live items', ['vera:canvas:live', 'vera:canvas:terminal', 'vera:canvas:cell', 'vera:canvas:panel', 'vera:canvas:panel-src', 'vera:canvas:open-notebook'].every(ev => SRC.includes(ev)));

// ── a hand-added item is yours (live defect 11): the add bar and the panel picker anchor no turn, so no run is drawn ──
t('the add bar and the panel picker anchor { origin: you, beside: the turn in view } — never the turn', SRC.split("args.anchor = { origin: 'you', beside: focusMid };").length === 3 && !SRC.includes("if (focusMid) args.anchor = { turn: focusMid, mid: focusMid };"));
t('the item carries where it sits (data-beside) and says it is yours; the placer levels it there', SRC.includes("data-beside=") && SRC.includes("added by you — it relates to no turn") && SRC.includes("const levelOf = (it) => it.mid || it.beside || '';"));
t('a taken suggestion keeps its turn (it is the reply\'s), a re-shown item its own anchors', SRC.includes("const mid = s.mid || focusMid; if (mid) args.anchor = { turn: mid, mid };") && SRC.includes("if (s.taken) return this.call('canvas.add', { key: s.key });"));

// ── the session's own sandbox is reachable from a canvas terminal (Notes/42 defect 64) ──
{ const rows = V.hostRowsOf({ ssh: [{ ssh_host_id: 'ct126', label: 'ct126' }] }, [], 'sess-abc');
  const first = rows[0] || {};
  t('the session\'s sandbox is the first row of the terminal picker, and only when there is a session',
    first.host_id === '@session' && first.g === 'session' && /sandbox/.test(first.n || '')
    && !V.hostRowsOf({ ssh: [{ ssh_host_id: 'ct126' }] }, [], '').some((r) => r.host_id === '@session'),
    JSON.stringify(first));
  // the estate's own enumerations never carry it: it belongs to the session
  t('it is not one of the estate rows', rows.filter((r) => r.host_id === '@session').length === 1);
  // picking it resolves through sandbox.session.terminal and leaves an ORDINARY terminal item behind
  t('picking it asks sandbox.session.terminal for this canvas\'s session', /this\.callResult\('sandbox\.session\.terminal', \{ session_id: sid, shell: 'bash' \}\)/.test(SRC));
  t('the answer becomes host, container, shell and socket on the item', /Object\.assign\(c2, \{ host_id: row\.host_id, container: row\.container, shell: row\.shell, ws: row\.ws, sandbox: true, attached: true \}\)/.test(SRC));
  t('and the item is persisted, so it survives a reload', /return this\.call\('canvas\.update', Object\.assign\(this\._ref\(key\), \{ content: c2 \}\)\);/.test(SRC));
  t('the add bar offers it too', /data-act="tsbx"/.test(SRC) && /if \(act === 'tsbx'\) return this\._sbxPick\(key, it, btn\);/.test(SRC));
  // a terminal item already carrying a socket draws through the ordinary path
  const sbx = B.session({ title: "this session's sandbox", host_id: 'local', container: 'vera-sbx-abc', shell: 'bash', ws: '/remote/docker/term/ws/local/vera-sbx-abc?shell=bash', sandbox: true, attached: true }, 'm', 'session:s1', { _live: {} });
  t('a resolved sandbox terminal is an ordinary live terminal item', /data-live="term"/.test(sbx) && /vera-sbx-abc/.test(sbx), sbx.slice(0, 160)); }

console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
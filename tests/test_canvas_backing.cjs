// The canvas backed (vera/canvas/canvas_element.js; Notes/42 defect 32): the terminal's connect form lists the estate's
// known hosts (conn.targets — the Exec panel's SSH hosts and the containers on every docker host — and conn.list, the
// saved connections), the panel picker lists EVERY registered panel by name (ui.panel.list, the registry the harness
// draws its tabs from) with the ones open for the session marked, and a diagram item is a live rendered diagram through
// the estate's own mermaid element. The pure rows, the item bodies, and the source strings the chain is held by.
//   node tests/test_canvas_backing.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const B = V.BLOCK;
t('the pure rows and the picker are exported (version 4)', typeof V.hostRowsOf === 'function' && typeof V.panelRowsOf === 'function' && typeof V.pickerHtml === 'function' && V.version === 4);

// ── the known hosts, as the remote subsystem answers them (conn.targets · conn.list, prod-shaped) ──
const targets = { docker: [{ docker_host_id: 'local', host_label: 'llm.int', id: 'abc123', container: 'vera-mirror', image: 'vera:latest', state: 'running' }, { docker_host_id: 'ct126', host_label: 'ct126 · gpu', container: 'ollama', image: 'ollama/ollama', state: 'running' }],
  ssh: [{ ssh_host_id: 'ct126', label: 'ct126 (gpu node)', host: '10.0.0.126', user: 'root', port: 22, tags: 'gpu' }, { ssh_host_id: 'pxstore', label: '', host: 'pxstore.int', user: 'boejaker', port: 2222 }], proxmox: [] };
const conns = { connections: [{ id: 'c1', kind: 'ssh', label: 'gpu shell', ssh_host_id: 'ct126', shell: 'bash' }, { id: 'c2', kind: 'docker', label: 'mirror sh', docker_host_id: 'local', container: 'vera-mirror', shell: 'sh' }, { id: 'c3', kind: 'proxmox', label: 'not enrolled' }], count: 3 };
const rows = V.hostRowsOf(targets, conns);
t('every host the estate knows is a row: 2 saved (the proxmox one has no host yet) · 2 ssh · 2 containers', rows.length === 6 && rows.filter(r => r.g === 'saved').length === 2 && rows.filter(r => r.g === 'ssh').length === 2 && rows.filter(r => r.g.startsWith('docker:')).length === 2, JSON.stringify(rows.map(r => r.g)));
const ssh = rows.find(r => r.g === 'ssh' && r.host_id === 'ct126'); t('an ssh host: its label, user@host, the tag; a login shell, no container', !!ssh && ssh.n === 'ct126 (gpu node)' && ssh.sub === 'root@10.0.0.126 · gpu' && ssh.container === '' && ssh.shell === '' && ssh.kind === 'ssh');
const px = rows.find(r => r.host_id === 'pxstore'); t('an ssh host with no label is named by its id; a port off 22 shows', !!px && px.n === 'pxstore' && px.sub === 'boejaker@pxstore.int:2222');
const dk = rows.find(r => r.container === 'ollama'); t('a container: its name, grouped on its host, the image and the state, sh', !!dk && dk.g === 'docker:ct126 · gpu' && dk.host_id === 'ct126' && dk.shell === 'sh' && /ollama\/ollama · running/.test(dk.sub) && dk.kind === 'docker');
const sv = rows.find(r => r.g === 'saved' && r.kind === 'docker'); t('a saved docker connection carries its host, container and shell', !!sv && sv.host_id === 'local' && sv.container === 'vera-mirror' && sv.shell === 'sh' && sv.n === 'mirror sh');
t('an empty or failed answer is no rows, never a throw', V.hostRowsOf(null, null).length === 0 && V.hostRowsOf({ ok: false, error: 'x' }, { ok: false }).length === 0 && V.hostRowsOf({ docker: 'nope' }, []).length === 0);

// ── every registered panel, the open ones marked ──
const list = { panels: [{ id: 'workers', label: 'Workers', icon: '⚙', mode: 'tab', tab_order: 10 }, { id: 'dag', label: 'DAG workshop', icon: '⋮', mode: 'tab', tab_order: 20 }, { id: 'memory-graph', label: 'Memory graph', icon: '◎', mode: 'element', tab_order: 100 }, { id: 'notebook', label: 'Notebook', icon: '▤', mode: 'inject', tab_order: 5 }, { id: 'dyn-1', label: 'My page', icon: '', mode: 'tab', tab_order: 200, dynamic: true }], count: 5 };
const open = { ok: true, panels: [{ id: 'dag', label: 'DAG workshop', origin: 'aide', placement: 'tab', host: 'harness' }, { id: 'ghost', label: 'A page by its route', origin: 'you', host: 'chat' }] };
const prow = V.panelRowsOf(list, open);
t('every registered panel is a row, plus an open one the registry does not list', prow.length === 6, String(prow.length));
t('the open ones come first and are marked with their host', prow[0].open && prow[1].open && prow.slice(2).every(r => !r.open) && prow.find(r => r.id === 'dag').host === 'harness' && prow.find(r => r.id === 'dag').g === 'open');
t('the rest in tab order, grouped as registered (inject · tab · element · dynamic)', prow.slice(2).map(r => r.id).join(',') === 'notebook,workers,memory-graph,dyn-1' && prow.find(r => r.id === 'notebook').g === 'inject' && prow.find(r => r.id === 'dyn-1').g === 'dynamic' && prow.find(r => r.id === 'memory-graph').mode === 'element');
t('the /ui/panels list (a bare array, the harness\'s) is accepted too', V.panelRowsOf([{ id: 'a', label: 'A', mode: 'tab' }], null).length === 1 && V.panelRowsOf({ ok: false }, { ok: false }).length === 0);

// ── the picker: a search, the groups, the rows ──
const html = V.pickerHtml(rows, { row: r => `<button class="pp" data-q="${r.n}">${r.n}</button>`, placeholder: 'find a host', labels: { saved: 'saved connections' } });
t('a picker is a search box that filters in place, the groups with their counts, the rows', /class="ti pk-q" placeholder="find a host"/.test(html) && /<div class="grp">saved connections · 2<\/div>/.test(html) && /<div class="grp">containers on ct126 · gpu · 1<\/div>/.test(html) && (html.match(/class="pp"/g) || []).length === 6);
t('the search hides the rows that do not carry the words, and a group left empty', SRC.includes("if (!q || !q.classList || !q.classList.contains('pk-q')) return;") && SRC.includes("const on = !s || (n.dataset.q || '').includes(s); n.hidden = !on; any = any || on;"));

// ── the terminal item: the hosts under the connect row ──
const none = B.session({ title: 'Terminal', host_id: '', container: '', shell: '' }, 'm', 'session:t1', { _live: {} });
t('the connect row keeps the typed host id · container · shell and adds the hosts button', /data-f="host_id"/.test(none) && /data-f="container"/.test(none) && /data-f="shell"/.test(none) && /data-act="thosts"/.test(none) && /data-act="tconnect"/.test(none) && !/vc-hosts/.test(none));
const withHosts = B.session({ host_id: '' }, 'm', 'session:t1', { _live: {}, _hostsOpen: { 'session:t1': true }, _hosts: rows });
t('asked for, the known hosts list under the row: grouped rows a pick fills the row with (host · container · shell)', /class="vc-hosts pk"/.test(withHosts) && /data-act="hpick" data-host="ct126" data-container="" data-shell=""/.test(withHosts) && /data-act="hpick" data-host="ct126" data-container="ollama" data-shell="sh"/.test(withHosts) && /ssh hosts · the Exec panel · 2/.test(withHosts));
t('while the estate is being asked the list says so; no host known says where they are kept', /asking the estate/.test(B.session({ host_id: '' }, 'm', 'session:t2', { _live: {}, _hostsOpen: { 'session:t2': true } })) && /no known host — the Exec panel keeps the SSH hosts/.test(B.session({ host_id: '' }, 'm', 'session:t3', { _live: {}, _hostsOpen: { 'session:t3': true }, _hosts: [] })));
t('the add bar\'s terminal opens the host picker; the blank terminal and the typed id stay', V.ADD_KINDS.some(k => k.n === 'terminal' && k.hosts === true && 'host_id' in k.content) && SRC.includes("if (k.hosts) return this._hostPick(btn);") && SRC.includes('data-act="tblank"') && SRC.includes('data-act="taddid"') && SRC.includes("return k ? this._addSeed(k) : undefined;"));
t('the hosts come from the remote subsystem\'s own enumerations, asked once and kept a while', SRC.includes("this.callResult('conn.targets', { include_proxmox: false }), this.callResult('conn.list', {})") && SRC.includes("now - this._hostsAt < 30000"));
t('a pick from the bar lands a terminal already on its host, yours; from the item it fills the row and connects', SRC.includes("const content = { title: String(row.n || (row.container ? row.container + ' @ ' + hid : hid)), host_id: hid, container: String(row.container || ''), shell: String(row.shell || ''), attached: true };") && SRC.includes("anchor: { origin: 'you', beside: focusMid } };   // picked by hand") && SRC.includes("Object.assign(c, { host_id: row.host_id, container: row.container, shell: row.shell, attached: true });"));

// ── the panel picker: every panel by name ──
t('the panel picker asks the registry and the open set together, the /ui/panels list when the light list is not there', SRC.includes("Promise.all([this.callResult('ui.panel.list', {}), this.callResult('ui.panels.open', { session_id: this._sid() })])") && SRC.includes("+ '/ui/panels', { headers: { Accept: 'application/json' } }") && SRC.includes("return panelRowsOf(L, open);"));
t('the typed panel id stays under the list', SRC.includes('<input class="ti" data-f="pid" placeholder="panel id" spellcheck="false"><button class="ib on" data-act="paddid">Add</button>'));
const ph = V.pickerHtml(prow, { row: r => `<button class="pp" data-act="padd" data-pid="${r.id}">${r.n}${r.open ? '<em class="tag">open</em>' : ''}</button>`, labels: { open: 'open for this session', tab: 'tabs' } });
t('the panel rows: open for this session first, then the registry\'s groups', /open for this session · 2/.test(ph) && ph.indexOf('open for this session') < ph.indexOf('tabs · 1') && /data-pid="memory-graph"/.test(ph));

// ── the diagram item ──
t('without a page a diagram block is its source', /vc-pre vc-dim"><code>graph TD/.test(B.diagram({ mermaid: 'graph TD\n A-->B' })));
global.document = {};   // a page: the estate's element draws
const dg = B.diagram({ mermaid: 'graph TD\n A-->B', title: 'Boot path' }, 'm', 'diagram:d1', { _live: {} });
t('on a page a keyed diagram is a live slot for the mermaid element with the board\'s actions: Open in the chat · copy · source', /data-live="mermaid" data-key="diagram:d1" data-title="Boot path"/.test(dg) && /data-act="dgopen"/.test(dg) && /data-act="dgcopy"/.test(dg) && /data-act="dgsrc"/.test(dg) && /vc-dgsrc" hidden><code>graph TD/.test(dg) && /mermaid · 2 lines/.test(dg) && /dot wait/.test(dg));
t('a plain diagram block holds the element inline, its source as its text', /^<vera-mermaid class="vc-mm" bare title="Diagram">graph TD\n A--&gt;B<\/vera-mermaid>$/.test(B.diagram({ mermaid: 'graph TD\n A->B' })) === false && /^<vera-mermaid class="vc-mm" bare title="Diagram">graph TD\n A--&gt;B<\/vera-mermaid>$/.test(B.diagram({ mermaid: 'graph TD\n A-->B' })));
t('a diagram with no source says so', /no diagram source yet/.test(B.diagram({}, 'm', 'diagram:d2', {})));
t('a caption stays', /vc-cap">the boot path/.test(B.diagram({ mermaid: 'graph TD\n A-->B', caption: 'the boot path' }, 'm', 'diagram:d3', {})));
delete global.document;
t('the live layer mounts the estate\'s element (bare, filling its slot) and hands it the source; a changed source redraws; it is fitted with its slot', SRC.includes("inner = document.createElement('vera-mermaid'); inner.setAttribute('bare', ''); inner.setAttribute('fill', '');") && SRC.includes("ensureLib('/ui/elements/vera_mermaid.js', 'vera-mermaid').then((ok) => {") && SRC.includes("if (!inner || inner._mmCode === code) return; inner._mmCode = code;") && SRC.includes("if (el.dataset.kind === 'mermaid') { const m = el.firstChild; try { m && m.fit && m.fit(); } catch (e) {} }"));
t('the host\'s window.mermaid still draws when the element cannot be had; the source shows when nothing can', SRC.includes("if (!ok && window.mermaid && typeof window.mermaid.render === 'function')") && SRC.includes("inner.textContent = code;") && SRC.includes("window.mermaid.run({ nodes: body.querySelectorAll('.mermaid') })"));
t('Open in the chat is the chat\'s own pop-out (vm:popout), after the host had its say (vera:canvas:diagram)', SRC.includes("new CustomEvent('vera:canvas:diagram', { bubbles: true, composed: true, cancelable: true, detail: { key, code, title } })") && SRC.includes("new CustomEvent('vm:popout', { bubbles: true, composed: true, detail: { code, title } })"));
t('the diagram glyph and the plain add path are untouched', V.KIND_GLYPH.diagram === '◇' && V.ADD_KINDS.length === 5 && V.ADD_KINDS.some(k => k.n === 'panel' && k.pick === true));

console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);

/* ── <vera-canvas> ─────────────────────────────────────────────────────────────
 * A live canvas, embeddable anywhere — most importantly INSIDE A CHAT MESSAGE.
 *
 *   <vera-canvas canvas-id="cv_abc123"></vera-canvas>
 *   <vera-canvas canvas-id="cv_abc123" compact rows="6"></vera-canvas>
 *
 * This element IS the Canvas board, wherever a canvas shows: the chat's canvas column (the session canvas), the
 * card canvas.show puts in the conversation, and the Canvas panel (/canvas/panel — this element with its rail, the
 * canvas from the route: ?canvas=<id>, ?session=<sid>, else the most recent). It polls /canvas/get and repaints
 * only when the document's revision actually changes.
 *
 *   <vera-canvas rail fill canvas-id="cv_abc123" session-id="…"></vera-canvas>   — the panel: the canvases on the
 *   left (the session's first, marked), + New (title · mode · topic), a delete behind a confirm; a row switches.
 *
 * An agent's canvas (canvas.create · canvas.append — keyless blocks) is drawn through the same projection as the
 * session canvas: every block a card with the same rail — edit · ↑ ↓ (canvas.move) · size · drop — the add bar at
 * the top adding through canvas.add on that canvas; the resolver's states (pin · park · in context) are the keyed
 * items'.
 *
 * On a SESSION canvas (the chat's canvas column) it is the Canvas board's column: the add bar at the top,
 * the NOW band (what this turn is waiting on — a decision with its answers, and what Vera can also do),
 * the items level with their turns, each a card that folds to its header line in Hover and Zen and opens
 * IN PLACE on a click (the column makes room), with a corner grip that sizes it and an edit rail beneath.
 *
 * Deliberately dependency-free: chat already carries enough weight, and a CDN
 * import inside a chat bubble is a failure waiting to happen. Markdown is a
 * small subset renderer; a diagram is drawn by the estate's own mermaid element (<vera-mermaid>, loaded once
 * from the page — the one implementation the chat draws its fences with), by the host page's window.mermaid when
 * that is all there is, and as its source otherwise.
 * ────────────────────────────────────────────────────────────────────────────*/
(function () {
  const root = typeof window !== 'undefined' ? window : globalThis;
  if (root.__veraCanvasElement) return;
  root.__veraCanvasElement = true;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* A deliberately small Markdown subset: headings, bold/italic, inline code,
     links, lists, and fenced code. Everything is escaped FIRST, so no canvas
     content can inject markup into the chat document. */
  function md(src) {
    const fences = [];
    let s = String(src || '').replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, body) => {
      fences.push(`<pre class="vc-pre"><code>${esc(body.replace(/\n$/, ''))}</code></pre>`);
      return `\u0000F${fences.length - 1}\u0000`;
    });
    s = esc(s);
    s = s.replace(/^###### (.*)$/gm, '<h6>$1</h6>')
         .replace(/^##### (.*)$/gm, '<h5>$1</h5>')
         .replace(/^#### (.*)$/gm, '<h4>$1</h4>')
         .replace(/^### (.*)$/gm, '<h3>$1</h3>')
         .replace(/^## (.*)$/gm, '<h2>$1</h2>')
         .replace(/^# (.*)$/gm, '<h1>$1</h1>');
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
         .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
         .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
                  '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/^\s*[-*] (.*)$/gm, '<li>$1</li>')
         .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>');
    s = s.split(/\n{2,}/).map(b =>
      /^\s*<(h\d|ul|pre|table|blockquote)/.test(b) ? b : `<p>${b.replace(/\n/g, '<br>')}</p>`
    ).join('');
    return s.replace(/\u0000F(\d+)\u0000/g, (_, i) => fences[+i] || '');
  }

  const BLOCK = {
    markdown: c => `<div class="vc-md">${md(c.md || c.text || '')}</div>`,

    /* a code item draws its source and, when the language is one a browser can simply show, the thing itself. The
       preview is drawn in the column's live layer exactly as a diagram is: a sandboxed iframe, srcdoc, no network. It
       is off until asked for, and the head carries the switch (Notes/42 defect 78 - the chat has had a Preview on its
       fences all along; a canvas item never had one). */
    code: (c, size, key, el) => {
      const prev = !!(key && el && el._prevOn && el._prevOn[key]) && PREVIEWABLE(c.lang);
      const head = `<div class="vc-codehead">${esc(c.filename || c.lang || 'code')}<span class="sp"></span>`
        + (PREVIEWABLE(c.lang) && key ? `<button class="ib${prev ? ' on' : ''}" data-act="cprev" title="${prev ? 'Show the source' : 'Render it here - a sandboxed frame, no network'}">${prev ? 'source' : 'preview'}</button>` : '')
        + '</div>';
      const bodyHtml = prev
        ? `<div class="vc-live vc-preview" data-live="preview" data-key="${esc(key)}" data-lang="${esc(String(c.lang || ''))}"><span class="vc-dim">rendering…</span></div>`
        : `<pre class="vc-pre"><code>${esc(c.code || '')}</code></pre>`;
      return `<div class="vc-codewrap">${head}${bodyHtml}</div>`;
    },

    /* a diagram item is a LIVE rendered diagram: the mermaid source drawn by the estate's own element (<vera-mermaid>,
       the one the chat draws its fences with — never a CDN pulled in to draw inside a chat bubble). A keyed item holds
       it in the column's live layer (its pan · zoom survive a render) with the board's actions — Open in the chat
       (the chat's own pop-out), copy, source; a plain block holds the element inline. The host's window.mermaid still
       draws when the element cannot be had; without a page at all, the source. */
    diagram: (c, size, key, el) => {
      const src = String(c.mermaid || c.code || c.source || '').trim();
      const cap = c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : '';
      if (!src) return '<div class="vc-dim">no diagram source yet</div>' + cap;
      if (typeof document === 'undefined') return `<pre class="vc-pre vc-dim"><code>${esc(src)}</code></pre>` + cap;
      const title = String(c.title || c.caption || 'Diagram');
      const live = !!(el && el._live && el._live[key]);
      if (key) return `<div class="vc-diag" data-w="canvas.diagram"><div class="vc-th"><i class="dot${live ? ' on' : ' wait'}"></i><b>${esc(title)}</b><span class="mono">mermaid · ${src.split('\n').length} lines</span><span class="sp"></span>`
        + '<button class="ib" data-act="dgopen" title="Open it in the chat — the chat\'s own diagram pop-out">Open in the chat ↗</button><button class="ib" data-act="dgcopy" title="Copy the mermaid source">copy</button><button class="ib" data-act="dgsrc" title="Show the source">source</button></div>'
        + `<div class="vc-live" data-live="mermaid" data-key="${esc(key)}" data-title="${esc(title)}"><span class="vc-dim">drawing…</span></div><pre class="vc-pre vc-dgsrc" hidden><code>${esc(src)}</code></pre></div>` + cap;
      return `<vera-mermaid class="vc-mm" bare title="${esc(title)}">${esc(src)}</vera-mermaid>` + cap;
    },

    image: c => `<figure class="vc-fig">
        <img src="${esc(c.url || '')}" alt="${esc(c.alt || '')}" loading="lazy">
        ${c.caption ? `<figcaption>${esc(c.caption)}</figcaption>` : ''}</figure>`,

    note: c => `<div class="vc-note">${esc(c.text || '')}
        ${c.author ? `<span class="vc-by">— ${esc(c.author)}</span>` : ''}</div>`,

    table: c => {
      const cols = c.columns || [];
      const rows = c.rows || [];
      return `<div class="vc-tablewrap"><table class="vc-table">
        ${cols.length ? `<thead><tr>${cols.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead>` : ''}
        <tbody>${rows.map(r => `<tr>${(r || []).map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}
        </tbody></table></div>`
        + (c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : '');
    },

    widget: (c, size, key) => {
      // a widget RECORD (name · draw · reads · source) draws through the one shared drawer when the page
      // has it; otherwise its record card, so the item still says what it is
      // a content with a form (or a draw block) IS a record — canvas.append's { form, title, data } as much as the sheet's
      // full record; the harvest's shape carries the record inside; the draw block is optional
      const isRec = (x) => !!(x && typeof x === 'object' && (x.draw || (typeof x.form === 'string' && x.form)));
      const rec = isRec(c) ? c : (c && isRec(c.record) ? Object.assign({}, c.record, c.title ? { title: c.title } : {}) : null);
      const form = rec ? String(rec.form || (rec.draw && rec.draw.form) || '') : '';
      // a record with a form is the live element (it reads its source itself, the sample face until it has one),
      // in the column's live layer over this slot — never re-created by a render
      if (rec && form && key && typeof customElements !== 'undefined' && customElements.get('vera-widget')) {
        const src = typeof rec.source === 'string' ? rec.source : ((rec.source && (rec.source.origin || rec.source.from)) || (rec.reads && rec.reads.cap) || '');
        return `<div class="vc-wid" data-w="canvas.widget"><div class="vc-live" data-live="widget" data-key="${esc(key)}" data-size="${esc(size || (rec.draw && rec.draw.size) || 'm')}"><span class="vc-dim">${esc(form)}…</span></div><div class="vc-cap mono">${esc(form)}${src ? ' · ' + esc(src) : ' · sample'}</div></div>`;
      }
      if (rec && window.VeraWidget && typeof window.VeraWidget.draw === 'function') {
        try {
          const out = window.VeraWidget.draw(form, rec.data, size || (rec.draw && rec.draw.size) || 'm');
          if (out != null) return typeof out === 'string' ? out : (out.outerHTML || '');
        } catch (e) { /* fall through to the record card */ }
      }
      if (rec) return `<div class="vc-rec"><b>${esc(rec.title || rec.name || (rec.reads && rec.reads.cap) || form || 'widget')}</b>
        <span class="vc-badge">${esc(form)}</span>
        <span class="vc-dim">${esc(typeof rec.source === 'string' ? rec.source : ((rec.source && (rec.source.origin || rec.source.from)) || (rec.reads && rec.reads.cap) || ''))}</span></div>`;
      return `<div class="vc-stub"><span class="vc-badge">widget</span>
        ${esc(c.title || c.widget || '')}</div>`;
    },

    /* ── the Canvas board's live items (A16 of Notes/42): a LIVE TERMINAL, a NOTEBOOK CELL, a WHOLE PANEL ─────────
       Each is a widget of the estate drawn as an item: the terminal is <vera-terminal> over the estate's terminal
       WebSocket (ssh host · docker container), the cell is the notebook's own cell (its source, its output, Run
       through the notebook's exec, Open the notebook), the panel is the panel page itself driven over the ONE
       bridge (panel.query · panel.dispatch · refresh · standalone). Live elements (the terminal, the panel's frame)
       are never re-created by a render: they live in the column's live layer, placed over their slot. ─────────── */
    session: (c, size, key, el) => {
      const hostId = String(c.host_id || '').trim(), shown = hostId || String(c.host || c.session_id || '').trim(), container = String(c.container || '').trim(), shell = String(c.shell || '').trim();
      const ws = c.ws || (hostId ? (container ? '/remote/docker/term/ws/' + encodeURIComponent(hostId) + '/' + encodeURIComponent(container) + '?shell=' + encodeURIComponent(shell || 'sh') : '/remote/ssh/term/ws/' + encodeURIComponent(hostId) + '?shell=' + encodeURIComponent(shell)) : '');
      const attached = !!key && !!ws && c.attached !== false;
      const live = !!(el && el._live && el._live[key]);
      // the known hosts, listed under the connect row once asked for (the list is state on the element, so a render keeps it)
      const hostsOpen = !!(key && el && el._hostsOpen && el._hostsOpen[key]);
      const hosts = hostsOpen ? `<div class="vc-hosts pk" data-w="canvas.terminal.hosts">${el._hosts ? hostListHtml(el._hosts) : '<span class="vc-dim">known hosts — asking the estate…</span>'}</div>` : '';
      const head = `<div class="vc-th"><i class="dot${attached ? (live ? ' on' : ' wait') : ''}"></i><b>${esc(shown ? (container ? shown + ' / ' + container : shown) : 'no host yet')}</b><span class="mono">${esc(shell || (container ? 'sh' : 'login shell'))}${c.command ? ' · ' + esc(c.command) : ''}</span><span class="sp"></span>`
        + (attached ? '<button class="ib" data-act="tdetach" title="Detach — the item keeps its host; Attach opens a new shell">detach</button>' : (ws ? '<button class="ib on" data-act="tattach" title="Open a shell on this host">attach</button>' : ''))
        + (ws ? '<button class="ib" data-act="tshare" title="Copy the terminal\'s address">share</button>' : '') + '</div>';
      const conn = attached ? `<div class="vc-live" data-live="term" data-key="${esc(key)}" data-ws="${esc(ws)}"><span class="vc-dim">connecting…</span></div>`
        : `<div class="vc-tconnect" data-w="canvas.terminal.connect"><input class="ti" data-f="host_id" placeholder="host id — an SSH host of the Exec panel" value="${esc(shown)}" spellcheck="false"><input class="ti" data-f="container" placeholder="container (docker) — optional" value="${esc(container)}" spellcheck="false"><input class="ti sm" data-f="shell" placeholder="shell" value="${esc(shell)}" spellcheck="false"><button class="ib${hostsOpen ? ' on' : ''}" data-act="thosts" title="The estate's known hosts — the Exec panel's SSH hosts, the containers on every docker host, your saved connections">hosts ▾</button><button class="ib on" data-act="tconnect">Connect</button></div>` + hosts;
      return `<div class="vc-term" data-w="canvas.terminal">${head}${conn}${c.output ? `<pre class="vc-pre vc-out"><code>${esc(String(c.output).slice(0, 4000))}</code></pre>` : ''}</div>`;
    },

    // a notebook cell as an item: the cell the notebook holds (source · output), Run through the notebook's exec, Open
    notebook: (c, size, key) => {
      const t = String(c.cell_type || 'markdown'), lang = String(c.lang || ''), src = String(c.content || c.source || '');
      const out = String(c.generated || c.output || '');
      const runnable = t === 'code' || t === 'exec';
      return `<div class="vc-nb" data-w="canvas.notebook"><div class="vc-th"><span class="vc-badge">${esc(t)}${lang && runnable ? ' · ' + esc(lang) : ''}</span><b>${esc(c.title || 'cell')}</b><span class="mono">${esc(String(c.notebook_id || '').slice(0, 8))}${c.cell_id ? ' · ' + esc(String(c.cell_id).slice(0, 8)) : ''}</span><span class="sp"></span>`
        + (runnable ? `<button class="ib on" data-act="nbrun" title="Run the cell through the notebook's exec">Run</button>` : '') + `<button class="ib" data-act="nbopen" title="Open the notebook at this cell">Open the notebook ↗</button></div>`
        + (t === 'markdown' ? `<div class="vc-md">${md(src)}</div>` : `<pre class="vc-pre"><code>${esc(src)}</code></pre>`)
        + `<pre class="vc-pre vc-out vc-nbout"${out ? '' : ' hidden'}><code>${esc(out.slice(0, 8000))}</code></pre></div>`;
    },

    // a whole panel as an item: the panel page in its frame, driven over the one bridge
    panel: (c, size, key, el) => {
      const id = String(c.panel || c.id || '').trim(); const src = c.src || (id ? '/ui/panels/' + encodeURIComponent(id) : '');
      const q = el && el._pq && el._pq[key];
      return `<div class="vc-panel" data-w="canvas.panel"><div class="vc-th"><i class="dot on"></i><b>${esc(c.title || c.label || id || 'panel')}</b><span class="mono">${esc(id)} · over the bridge</span><span class="sp"></span>`
        + `<button class="ib" data-act="pquery" title="panel.query — what the panel holds">query</button><button class="ib" data-act="pdispatch" title="panel.dispatch — drive it">dispatch</button><button class="ib" data-act="prefresh" title="Reload the panel">refresh</button><button class="ib" data-act="pstand" title="Open it standalone">standalone ↗</button></div>`
        + (src ? `<div class="vc-live vc-frame" data-live="frame" data-key="${esc(key)}" data-src="${esc(src)}"></div>` : '<div class="vc-dim">no panel id</div>')
        + `<div class="vc-bridge"${el && el._pdOpen && el._pdOpen[key] ? '' : ' hidden'} data-w="canvas.panel.bridge"><div class="row"><input class="ti" data-f="action" placeholder="action" spellcheck="false"><input class="ti" data-f="payload" placeholder="payload {…}" spellcheck="false"><button class="ib on" data-act="psend">Send</button></div></div>`
        + `<pre class="vc-pre vc-out vc-pq"${q ? '' : ' hidden'}><code>${esc(q ? (q.text || '') : '')}</code></pre></div>`;
    },

    // a run as an item (P7): its goal, its status, its steps — each the capability it ran, with its time
    loop: c => {
      const steps = Array.isArray(c.steps) ? c.steps : []; const done = steps.filter(s => s && s.status === 'ok').length;
      const st = String(c.status || 'running');
      return `<div class="vc-loop"><div class="vc-loop-h"><span class="vc-badge st-${esc(st)}">${esc(st)}</span><b>${esc(c.goal || c.title || 'agentic loop')}</b><span class="vc-dim">${done}/${steps.length} steps</span></div>
        <ol class="vc-steps">${steps.map(s => `<li class="${esc((s && s.status) || '')}"><i></i><span>${esc((s && s.n) || '')}</span>${s && s.cap ? `<code>${esc(s.cap)}</code>` : ''}${s && s.ms ? `<em>${esc(s.ms)}</em>` : ''}</li>`).join('')}</ol>
        ${c.run ? `<div class="vc-dim vc-run">run ${esc(c.run)}</div>` : ''}</div>`;
    },

    schedule: c => `<div class="vc-stub"><span class="vc-badge">when</span>
        ${esc(c.when || '')} — ${esc(c.what || '')}</div>`,

    // The escape hatch is the one place canvas content becomes live markup, and
    // only ever inside this element's shadow root.
    html: c => c.html || '',
  };

  const CSS = `
  :host{display:block;margin:6px 0;
    font:12.5px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    color:var(--fg,#dce1e8)}
  .wrap{border:1px solid var(--border,#2a2f37);border-radius:10px;
    background:var(--bg1,#15181d);overflow:hidden}
  /* Blocks off: the whole chat drops its surfaces and the session canvas has to go with it - the board turns the
     canvas column, its head and its items flat (Canvas.dc.html 655). A rule on <html> cannot cross a shadow root,
     so the host carries the tier and the element answers to it here (Notes/42 defect 85). The GROUNDS go; the
     structure - the lines that say where one item ends and the next begins - stays. */
  :host([blocks="off"]) .wrap{background:transparent;border-color:transparent}
  :host([blocks="off"]) .head{background:transparent;border-bottom-color:color-mix(in srgb,var(--border,#2a2f37) 55%,transparent)}
  :host([blocks="off"]) .it{background:transparent;box-shadow:none;border-color:color-mix(in srgb,var(--border,#2a2f37) 55%,transparent)}
  :host([blocks="off"]) .addbar{background:transparent}
  /* bare: the HOST draws the head. The tri-page column has its own title row - name, revision, the NOW count, the
   columns, the switches - and the element drawing a second "Session canvas" line inside it put two headers on top of
   each other and pushed the items down the column (Notes/42 defect 74). The widget element makes the same bargain. */
:host([bare]) .head{display:none}
.head{display:flex;align-items:center;gap:8px;padding:6px 10px;
    border-bottom:1px solid var(--border,#2a2f37);background:var(--bg2,#1c2026)}
  .title{font-weight:600;flex:1 1 auto;min-width:0;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .pill{flex:0 0 auto;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;
    color:var(--dim,#6b7480);border:1px solid var(--border,#2a2f37);
    border-radius:8px;padding:0 6px}
  .live{width:6px;height:6px;border-radius:50%;background:var(--acc,#5a9e8f);
    flex:0 0 auto;animation:vcp 2.2s infinite}
  @keyframes vcp{0%,100%{opacity:1}50%{opacity:.3}}
  @media (prefers-reduced-motion:reduce){.live,.nowbar i,.it.waiting{animation:none}}
  a.open{flex:0 0 auto;font-size:11px;color:var(--acc,#5a9e8f);text-decoration:none}
  a.open:hover{text-decoration:underline}
  .body{padding:8px 10px;max-height:var(--vc-max,420px);overflow:auto}
  /* fill mode — the element is the whole surface (a floating pop-out window),
     rather than a bounded card sitting in a transcript. */
  :host([fill]){height:100%}
  :host([fill]) .wrap{height:100%;display:flex;flex-direction:column}
  :host([fill]) .body{flex:1;min-height:0}
  /* the shell: the rail (the Canvas panel's canvases) beside the wrap */
  .shell{display:flex;align-items:stretch;min-height:0}:host([fill]) .shell{height:100%}
  .shell>.wrap{flex:1 1 auto;min-width:0}
  .rail{flex:0 0 clamp(150px,30%,210px);display:flex;flex-direction:column;min-height:0;border:1px solid var(--border,#2a2f37);border-right:0;border-radius:10px 0 0 10px;background:var(--bg2,#1c2026)}
  .rail[hidden]{display:none}
  :host([rail]) .shell>.wrap{border-radius:0 10px 10px 0}
  :host([rail]) a.open{display:none}
  .rail h2{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);margin:0;padding:8px 10px 6px;display:flex;align-items:center;gap:6px;font-weight:600}
  .rail h2 .ib{margin-left:auto}
  .rail .nf{display:flex;flex-direction:column;gap:5px;padding:4px 8px 8px;border-bottom:1px solid var(--border,#2a2f37);margin:0}
  .rail .nf[hidden]{display:none}.rail .nf .row{display:flex;gap:6px}
  .seg{display:inline-flex;border:1px solid var(--border,#2a2f37);border-radius:11px;overflow:hidden;align-self:flex-start}
  .seg button{font:inherit;font-size:10px;height:20px;padding:0 9px;border:0;background:none;color:var(--dim,#6b7480);cursor:pointer}
  .seg button.on{background:var(--acc,#5a9e8f);color:#06120f}
  .rail .list{flex:1 1 auto;overflow:auto;padding:4px 6px 8px;display:flex;flex-direction:column;gap:2px}
  .rail .cv{display:flex;flex-direction:column;gap:1px;padding:5px 8px;border-radius:7px;border:1px solid transparent;cursor:pointer;text-align:left;background:none;color:var(--fg,#dce1e8);font:inherit;width:100%}
  .rail .cv:hover{background:var(--bg1,#15181d)}.rail .cv.on{background:var(--bg1,#15181d);border-color:var(--border,#2a2f37)}
  .rail .cv b{font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
  .rail .cv span{display:flex;align-items:center;gap:6px;font-size:9.5px;color:var(--dim,#6b7480);font-family:ui-monospace,Consolas,monospace}
  .rail .cv span i{font-style:normal;white-space:nowrap}.rail .cv span em{font-style:normal;white-space:nowrap}
  .rail .cv span .sid{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rail .cv .x{margin-left:auto;opacity:0;cursor:pointer;padding:0 3px}.rail .cv:hover .x{opacity:1}.rail .cv .x:hover{color:var(--err,#f7768e)}
  .rail .cv.confirm{cursor:default;border-color:var(--err,#f7768e)}.rail .cv.confirm span{gap:4px;margin-top:3px}
  .mode-dynamic{color:var(--acc3,#c79a5a)}.mode-static{color:var(--acc4,#a07ec1)}.mode-session{color:var(--acc,#5a9e8f)}
  .pill.mode-dynamic{border-color:var(--acc3,#c79a5a)}
  /* the add bar's popovers open with what they add (defect 44) */
  .addpop .what{display:block;padding:2px 8px 6px;font-size:10px;border-bottom:1px solid var(--bd,var(--border,#2a2f37));margin-bottom:4px}
  .it-a button[data-act="up"],.it-a button[data-act="down"]{padding:0 4px}
  .blk{padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04)}
  .blk:last-child{border-bottom:none}
  .vc-md h1,.vc-md h2,.vc-md h3{margin:.3em 0;line-height:1.25;text-wrap:balance}
  .vc-md h1{font-size:1.35em}.vc-md h2{font-size:1.2em}.vc-md h3{font-size:1.08em}
  .vc-md p{margin:.35em 0}.vc-md ul{margin:.35em 0;padding-left:1.2em}
  code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.92em}
  .vc-pre{background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);
    border-radius:6px;padding:7px 9px;overflow-x:auto;margin:.3em 0}
  .vc-pre code{white-space:pre}
  .vc-dim{opacity:.75}
  .vc-out{max-height:200px;overflow:auto}
  .vc-codehead{font-size:9.5px;color:var(--dim,#6b7480);margin-bottom:2px;
    letter-spacing:.04em}
  .vc-note{border-left:2px solid var(--acc,#5a9e8f);padding:3px 0 3px 8px;
    color:var(--fg2,#c3cad4);white-space:pre-wrap}
  .vc-by{color:var(--dim,#6b7480);font-size:11px;margin-left:5px}
  .vc-fig{margin:.3em 0}.vc-fig img{max-width:100%;border-radius:6px;display:block}
  .vc-fig figcaption,.vc-cap{font-size:10.5px;color:var(--dim,#6b7480);margin-top:3px}
  .vc-tablewrap{overflow-x:auto}
  .vc-table{border-collapse:collapse;width:100%;font-size:11.5px;
    font-variant-numeric:tabular-nums}
  .vc-table th,.vc-table td{border:1px solid var(--border,#2a2f37);padding:3px 6px;
    text-align:left}
  .vc-table th{background:var(--bg2,#1c2026)}
  .vc-stub{display:flex;align-items:center;gap:6px;flex-wrap:wrap;
    color:var(--fg2,#c3cad4)}
  .vc-badge{font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;
    background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);
    border-radius:7px;padding:0 6px;color:var(--dim,#6b7480)}
  .empty{color:var(--dim,#6b7480);padding:14px;text-align:center}
  .err{color:var(--err,#f7768e);padding:10px}
  /* ── the SESSION projection (the Canvas board's column): the add bar at the top, pinned above, the NOW band,
        the items level with their turns, parked as a chip line at the bottom ── */
  .addbar{position:sticky;top:0;z-index:6;display:flex;align-items:center;gap:5px;flex-wrap:wrap;
    padding:6px 0 6px;margin-bottom:2px;background:var(--bg1,#15181d);border-bottom:1px solid var(--border,#2a2f37)}
  .addbar .lbl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);margin-right:2px}
  .add{display:inline-flex;align-items:center;gap:5px;height:21px;padding:0 9px;border-radius:11px;
    background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);font:inherit;font-size:9.5px;
    color:var(--fg2,#c3cad4);cursor:pointer;line-height:1}
  .add:hover{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
  .add b{font-family:ui-monospace,Consolas,monospace;font-size:9px;font-weight:400;color:var(--dim,#6b7480)}
  .hidwrap{position:relative;margin-left:auto}
  .nowbar{display:inline-flex;align-items:center;gap:5px;font-size:9.5px;color:var(--dim,#6b7480);
    border:1px solid var(--border,#2a2f37);border-radius:9px;padding:0 7px 0 5px;flex:0 0 auto;max-width:100%;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .nowbar i{width:6px;height:6px;border-radius:50%;background:var(--acc3,#e09a55);animation:nowp 2.2s infinite;flex:0 0 auto}
  .nowbar.ok i{background:var(--acc2,#5ec9a0);animation:none}
  .nowbar b{font-family:ui-monospace,Consolas,monospace;font-size:8.5px;letter-spacing:.12em}
  @keyframes nowp{0%,100%{box-shadow:0 0 0 0 rgba(224,154,85,.5)}60%{box-shadow:0 0 0 5px transparent}}
  .band{padding:4px 0}
  .band-h{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);
    display:flex;align-items:center;gap:6px;margin:4px 0;min-width:0}
  .band-h::after{content:"";flex:1;height:1px;background:var(--border,#2a2f37)}
  .it{border:1px solid var(--border,#2a2f37);border-radius:8px;background:var(--bg2,#1c2026);margin:6px 0;
    overflow:hidden;position:relative;display:flex;flex-direction:column;box-sizing:border-box}
  .it.now{box-shadow:0 0 0 1.5px rgba(224,154,85,.45)}
  .it.waiting{animation:waitring 2.2s ease-in-out infinite}
  @keyframes waitring{0%,100%{box-shadow:0 0 0 1.5px rgba(224,154,85,.45)}50%{box-shadow:0 0 0 3px rgba(224,154,85,.24)}}
  .it.dim{opacity:.5;transition:opacity .15s}.it.dim:hover{opacity:1}
  /* out of focus in Zen: a dimmed header line — the board folds a body in Zen, it never removes the item (defect 53) */
  .it.out{opacity:.45;transition:opacity .15s}.it.out:hover{opacity:1}
  /* an item from a turn well behind the one in focus folds to its header line — the conversation has moved on */
  .it.aged{opacity:.55}.it.aged:hover{opacity:1}
  /* what Vera can also do: a ghost until one of its suggestions is taken */
  .it.ghost{background:transparent;border:1px dashed var(--dim,#6b7480);box-shadow:none}
  /* compact: the header line only; the body, the rail and the grip held back */
  .it.compact .it-bd,.it.compact .it-ft,.it.compact .rz{display:none}
  .it.compact{transition:height .18s ease}
  /* opened in place: the item takes its own height, the column makes room */
  .it.openin{box-shadow:0 0 0 1.5px rgba(90,158,143,.6)}
  .it.openin .it-bd{max-height:none!important}
  .it.hovopen{z-index:5;box-shadow:0 0 0 1.5px var(--acc,#5a9e8f),0 12px 30px -10px rgba(0,0,0,.6)}
  .it.sized .rz{opacity:.45}
  .it.resizing{transition:none!important;user-select:none}
  /* the resize handle — a corner grip that shows on hover; a drop saves the item's size */
  .rz{position:absolute;right:3px;bottom:3px;width:14px;height:14px;z-index:5;cursor:nwse-resize;opacity:0;
    border-radius:0 0 5px 0;transition:opacity .18s;
    background:linear-gradient(135deg,transparent 52%,var(--dim,#6b7480) 52%,var(--dim,#6b7480) 60%,transparent 60%,
      transparent 74%,var(--dim,#6b7480) 74%,var(--dim,#6b7480) 82%,transparent 82%)}
  .it:hover .rz{opacity:.75}
  /* the stage: items placed level with their turns; the pinned band stays at the top, the parked chips at the bottom */
  .stage{position:relative;min-height:40px}
  .stage .it{position:absolute;margin:0;box-sizing:border-box;left:0;top:0;transition:top .32s cubic-bezier(.2,.7,.3,1),left .32s}
  .stage .it.resizing{transition:none}
  :host([stage]) .body{position:relative;padding-top:0}
  :host([stage]) .band.pinned{padding-top:6px}
  /* the NOW bar stays in view under the add bar while the stage scrolls with the transcript */
  :host([stage]) .band.now>.band-h{position:sticky;top:34px;z-index:5;background:var(--bg1,#15181d);margin:0;padding:3px 0}
  :host([stage]) .band.parked{position:sticky;bottom:0;z-index:5;background:var(--bg1,#15181d)}
  .band.hid{padding:4px 0;position:relative}
  .hidbtn{font:inherit;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);background:none;border:1px solid var(--border,#2a2f37);border-radius:9px;padding:1px 8px;cursor:pointer}
  .hidbtn:hover{color:var(--fg,#dce1e8)}
  .hidpop[hidden]{display:none}
  .hidpop{position:absolute;right:0;top:24px;z-index:6;background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);border-radius:8px;padding:6px 8px;display:flex;flex-wrap:wrap;gap:4px;max-width:260px;box-shadow:0 4px 14px rgba(0,0,0,.35)}
  .it .sc{font-family:ui-monospace,Consolas,monospace;font-size:8.5px;color:var(--dim,#6b7480);flex:0 0 auto}
  .it-hd{display:flex;align-items:center;gap:6px;padding:5px 8px 4px;cursor:pointer;flex:0 0 auto;min-height:18px}
  .it-hd .ic{width:15px;height:15px;border-radius:4px;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
    font-family:ui-monospace,Consolas,monospace;font-size:8px;color:#101317;background:var(--dim,#6b7480);padding:0;border:0;
    letter-spacing:0;text-transform:none;line-height:1}
  .it-hd .ic[data-kind="note"]{background:#8fb87a}.it-hd .ic[data-kind="markdown"]{background:#8fb87a}
  .it-hd .ic[data-kind="code"]{background:#5ec9a0}.it-hd .ic[data-kind="session"]{background:#4fb3bf}
  .it-hd .ic[data-kind="table"]{background:#6aa2e8}.it-hd .ic[data-kind="widget"]{background:#a78bfa}
  .it-hd .ic[data-kind="loop"]{background:var(--acc3,#e09a55)}.it-hd .ic[data-kind="diagram"]{background:#c58bd6}
  .it-hd .ic[data-kind="image"]{background:#d6a05f}.it-hd .ic[data-kind="schedule"]{background:#e0c55a}
  .it-hd .ic[data-kind="suggest"]{background:transparent;color:var(--dim,#6b7480);border:1px dashed var(--dim,#6b7480)}
  .it-hd .t{font-size:11px;font-weight:600;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap}
  .it-hd .src{font-family:ui-monospace,Consolas,monospace;font-size:8px;color:var(--dim,#6b7480);flex:0 0 auto}
  .it-hd .k{font-family:ui-monospace,Consolas,monospace;font-size:8.5px;color:var(--dim,#6b7480);
    max-width:30%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .it-hd .xp{font-size:10px;color:var(--dim,#6b7480);flex:0 0 auto;cursor:pointer;padding:0 2px}
  .it:hover .it-hd .xp{color:var(--fg,#dce1e8)}
  .it-a{display:inline-flex;gap:2px;flex:1 1 auto;min-width:0;align-items:center}
  .it-a button{font:inherit;font-size:9.5px;color:var(--dim,#6b7480);background:none;border:1px solid transparent;
    border-radius:5px;padding:0 5px;cursor:pointer;line-height:1.5}
  .it-a button:hover{color:var(--fg,#dce1e8);border-color:var(--border,#2a2f37)}
  .it-a button.on{color:var(--acc,#5a9e8f)}
  .it-a button.ctx{margin-left:auto}
  .it-a button.ctx.on{color:var(--acc2,#5ec9a0)}
  .it-bd{padding:2px 8px 6px;overflow:auto;flex:1 1 auto;min-height:0}
  .it[data-size="s"] .it-bd{max-height:72px}
  .it[data-size="m"] .it-bd{max-height:180px}
  .it[data-size="l"] .it-bd{max-height:340px}
  .it[data-size="xl"] .it-bd{max-height:none}
  /* the edit rail — the canvas is a document you can change */
  .it-ft{display:flex;align-items:center;gap:4px;padding:3px 6px 5px;margin-top:auto;border-top:1px solid rgba(255,255,255,.04);flex:0 0 auto}
  /* what this turn is waiting on: the decision, its answers */
  .askb{display:flex;flex-direction:column;gap:6px;padding:2px 0 4px}
  .askb .why,.sugb .why{font-size:9px;color:var(--dim,#6b7480);line-height:1.4}
  .askb .why b{color:var(--fg2,#c3cad4);font-weight:500}
  .askb .q{font-size:12px;color:var(--fg,#dce1e8);line-height:1.45}
  .askb .opts{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .askb .opts button{font:inherit;height:24px;padding:0 11px;border-radius:12px;background:var(--bg1,#15181d);
    border:1px solid var(--border,#2a2f37);color:var(--fg,#dce1e8);font-size:10.5px;cursor:pointer}
  .askb .opts button:hover{border-color:var(--acc3,#e09a55)}
  .askb .opts button.on{background:var(--acc3,#e09a55);color:#1a1408;font-weight:600;border-color:transparent}
  .askb .opts .or,.askb .st{font-size:9px;color:var(--dim,#6b7480)}
  .askb .st{font-family:ui-monospace,Consolas,monospace}
  /* what Vera can also do: each becomes an item when taken */
  .sugb{display:flex;flex-direction:column;gap:5px;padding:2px 0 4px}
  .sugb .sg{display:flex;align-items:center;gap:7px;height:24px;padding:0 9px;border-radius:6px;font:inherit;font-size:10px;
    color:var(--fg2,#c3cad4);background:transparent;border:1px dashed var(--dim,#6b7480);cursor:pointer;text-align:left;width:100%}
  .sugb .sg:hover{color:var(--fg,#dce1e8);border-color:var(--fg2,#c3cad4)}
  .sugb .sg i{width:6px;height:6px;border-radius:50%;background:var(--dim,#6b7480);flex:0 0 auto}
  .sugb .sg b{margin-left:auto;font-family:ui-monospace,Consolas,monospace;font-size:8.5px;font-weight:400;color:var(--dim,#6b7480)}
  .sugb .sg.on{border-style:solid;border-color:var(--border,#2a2f37);background:var(--bg2,#1c2026);color:var(--fg,#dce1e8)}
  .sugb .sg.on i{background:var(--acc2,#5ec9a0)}
  /* the editor: the item's own text, saved through canvas.update */
  .edit{display:flex;flex-direction:column;gap:4px}
  .edit textarea{font:inherit;font-size:11px;line-height:1.5;min-height:72px;width:100%;box-sizing:border-box;
    background:var(--bg1,#15181d);color:var(--fg,#dce1e8);border:1px solid var(--acc,#5a9e8f);border-radius:6px;padding:6px;resize:vertical}
  .edit textarea.code{font-family:ui-monospace,Consolas,monospace}
  .edit-a{display:flex;gap:4px;align-items:center}
  .ib{font:inherit;height:19px;padding:0 8px;border-radius:5px;font-size:9.5px;color:var(--fg2,#c3cad4);background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);cursor:pointer;line-height:1}
  .ib:hover{color:var(--fg,#dce1e8);border-color:var(--acc,#5a9e8f)}
  .edit-a .vc-dim{font-size:9px;margin-left:auto}
  .chips{display:flex;flex-wrap:wrap;gap:4px;padding:4px 0}
  .chip{font-size:9.5px;color:var(--dim,#6b7480);border:1px solid var(--border,#2a2f37);border-radius:9px;
    padding:1px 8px;cursor:pointer;background:var(--bg2,#1c2026);display:inline-flex;align-items:center;gap:5px}
  .chip:hover{color:var(--fg,#dce1e8);border-color:var(--acc,#5a9e8f)}
  .chip i{width:6px;height:6px;border-radius:2px;background:var(--acc,#5a9e8f)}
  .vc-loop-h{display:flex;align-items:center;gap:6px;font-size:11px}
  .vc-loop-h b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-badge.st-running{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}.vc-badge.st-ok{color:var(--acc2,#5ec9a0)}.vc-badge.st-fail{color:var(--err,#f7768e);border-color:var(--err,#f7768e)}
  .vc-badge.st-waiting{color:var(--acc3,#e09a55);border-color:var(--acc3,#e09a55)}
  .vc-steps{list-style:none;margin:4px 0 0;padding:0;display:flex;flex-direction:column;gap:2px;font-size:11px}
  .vc-steps li{display:flex;align-items:center;gap:6px;min-width:0}
  .vc-steps li i{width:6px;height:6px;border-radius:50%;background:var(--dim,#6b7480);flex:0 0 auto}
  .vc-steps li.ok i{background:var(--acc2,#5ec9a0)}.vc-steps li.running i{background:var(--acc,#5a9e8f);animation:vcp 1.2s infinite}.vc-steps li.fail i{background:var(--err,#f7768e)}
  .vc-steps li span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-steps li code{font-size:9.5px;color:var(--dim,#6b7480)}.vc-steps li em{margin-left:auto;font-style:normal;font-size:9.5px;color:var(--dim,#6b7480)}
  .vc-run{font-family:ui-monospace,Consolas,monospace;font-size:9px;margin-top:3px}
  /* the live items (A16): a terminal, a notebook cell, a whole panel — the board's item head, the live slot */
  .body{position:relative}
  #items{position:relative;min-height:1px}
  #live{position:absolute;left:0;top:0;width:0;height:0;overflow:visible;z-index:4}
  #live .lv{position:absolute;box-sizing:border-box;border-radius:6px;overflow:hidden;background:#000}
  #live .lv > *{display:block;width:100%;height:100%}
  #live iframe.vc-pframe{border:0;background:var(--s1,var(--bg1,#15181d))}
#live .lv[data-kind="widget"]{background:transparent}#live .lv vera-widget{display:block;width:100%;height:100%}
.vc-wid{display:flex;flex-direction:column;gap:4px}.vc-wid .vc-cap{font-size:9.5px;color:var(--t3,var(--dim,#6b7480))}
  .vc-th{display:flex;align-items:center;gap:8px;min-height:24px;padding:2px 0 6px;font-size:11px;color:var(--t1,var(--fg,#dce1e8))}
  .vc-th b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .vc-th .mono{font-family:var(--f-mono,ui-monospace,SFMono-Regular,Consolas,monospace);font-size:9.5px;color:var(--t3,var(--dim,#6b7480));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .vc-th .sp{flex:1}
  .vc-th .dot{width:7px;height:7px;border-radius:50%;background:var(--t3,var(--dim,#6b7480));flex:0 0 auto}
  .vc-th .dot.on{background:var(--ac2,var(--acc2,#8fb87a));box-shadow:0 0 0 3px color-mix(in srgb,var(--ac2,var(--acc2,#8fb87a)) 22%,transparent)}
  .vc-th .dot.wait{background:var(--ac3,var(--acc3,#c9955a));animation:vcp 1.4s infinite}
  .vc-th .ib{height:22px;padding:0 8px;font-size:10px}
  .ib.on{color:var(--ac,var(--acc,#5a9e8f));border-color:color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 45%,transparent)}
  .ti{height:24px;padding:0 8px;border:1px solid var(--bd,var(--border,#2a2f37));border-radius:var(--r-sm,6px);background:var(--s2,var(--bg2,#1c2026));color:var(--t1,var(--fg,#dce1e8));font:inherit;font-size:10.5px;min-width:0;flex:1 1 120px}
  .ti.sm{flex:0 1 84px}.ti:focus{outline:none;border-color:var(--ac,var(--acc,#5a9e8f))}
  .vc-term,.vc-panel,.vc-nb{display:flex;flex-direction:column;min-height:0;height:100%}
  .vc-tconnect,.vc-bridge .row,.addpop .row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:4px 0}
  .vc-live{flex:1 1 auto;min-height:40px;height:110px;border-radius:6px;background:var(--s3,var(--bg3,#0f1114));display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--t3,var(--dim,#6b7480))}
  .it[data-size="s"] .vc-live{height:40px}.it[data-size="l"] .vc-live{height:230px}.it[data-size="xl"] .vc-live{height:440px}
  .it.sized .vc-live{height:auto}
  .vc-nbout{margin-top:6px;border-left:2px solid var(--ac,var(--acc,#5a9e8f))}
  .vc-pq{margin-top:6px;max-height:160px}
  .addpop{position:absolute;left:0;top:100%;z-index:6;margin-top:4px;min-width:260px;max-width:min(92%,420px);padding:8px;border-radius:var(--ui-radius,8px);background:var(--s1,var(--bg1,#15181d));box-shadow:var(--elev,0 8px 24px -12px rgba(0,0,0,.6)),0 0 0 1px var(--bd,var(--border,#2a2f37));display:flex;flex-direction:column;gap:2px}
  .addpop .pp{display:flex;flex-direction:column;align-items:flex-start;gap:1px;padding:6px 8px;border:0;border-radius:var(--r-sm,6px);background:transparent;color:var(--t1,var(--fg,#dce1e8));font:inherit;font-size:11px;text-align:left;cursor:pointer}
  .addpop .pp:hover{background:var(--s2,var(--bg2,#1c2026))}
  .addpop .pp span{font-family:var(--f-mono,ui-monospace,monospace);font-size:9.5px;color:var(--t3,var(--dim,#6b7480))}
  .vc-rec{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .vc-rec b{font-size:11px}
  /* the pickers (defect 32): the add bar's popover for panels and hosts, the terminal's host list — a search that
     filters in place, the rows in their groups: icon · name · the line to find it by */
  .addpop.pk{min-width:300px;max-width:min(96%,460px)}
  .pk .row.q{padding:0 0 4px}.pk .pk-q{flex:1 1 auto}
  .pk .pk-list{max-height:300px;overflow:auto;display:flex;flex-direction:column;gap:1px}
  .pk .grp{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--t3,var(--dim,#6b7480));padding:6px 8px 2px;position:sticky;top:0;background:var(--s1,var(--bg1,#15181d))}
  .pk .pp{flex-direction:row;align-items:center;gap:7px;width:100%;padding:4px 8px}
  .pk .pp .ic{width:18px;text-align:center;flex:0 0 auto;font-family:var(--f-mono,ui-monospace,monospace);font-size:10px;color:var(--t2,var(--fg2,#c3cad4))}
  .pk .pp b{font-weight:500;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pk .pp span{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}
  .pk .pp .tag{font-family:var(--f-mono,ui-monospace,monospace);font-size:8.5px;font-style:normal;color:var(--ac2,var(--acc2,#8fb87a));border:1px solid currentColor;border-radius:8px;padding:0 5px;flex:0 0 auto}
  .pk .pp[hidden],.pk .grp[hidden]{display:none}
  .vc-hosts{display:flex;flex-direction:column;gap:2px;padding:4px 0 6px;margin-top:2px;border-top:1px dashed var(--bd,var(--border,#2a2f37))}
  .vc-hosts .pk-list{max-height:200px}.vc-hosts .grp{background:var(--s2,var(--bg2,#1c2026))}
  /* the diagram item: the estate's mermaid element in the live layer, the source behind a click */
  #live .lv[data-kind="mermaid"]{background:var(--s3,var(--bg3,#0f1114))}#live .lv vera-mermaid{display:block;width:100%;height:100%}
  .vc-diag{display:flex;flex-direction:column;min-height:0;height:100%}
  /* the diagram's slot takes the diagram's own height at the slot's width (--dh, set when it renders), up to the size's
     ceiling — s 120 · m 400 · l 640 · xl the diagram's own — never a fixed strip; a dragged size still wins */
  .vc-diag .vc-live{height:var(--dh,150px);max-height:400px}.it[data-size="s"] .vc-diag .vc-live{max-height:120px}.it[data-size="l"] .vc-diag .vc-live{max-height:640px}.it[data-size="xl"] .vc-diag .vc-live{max-height:none}
  .it[data-type="diagram"] .it-bd{max-height:none}
  .vc-dgsrc{margin-top:6px;max-height:160px}
  vera-mermaid.vc-mm{display:block;min-height:120px;margin:.3em 0}`;

  /* ── THE PLACER (Notes/38 §3.5), pure: items → a column and a top for each. An item sits level with the turn using it
     now (the turn's measured top in the transcript's scroll frame) — or, for an item added by hand, the turn it was
     added beside (its level, never a relation); items whose turn is not in view pack after; auto items never overlap —
     a column's next item starts at max(its turn's top, the column's bottom + gap); the column chosen is the one that
     lets it sit highest. ── */
  function place(items, turns, o) {
    o = o || {}; const cols = Math.max(1, Math.min(4, o.columns || 1)), gap = o.gap == null ? 10 : o.gap, cw = o.colWidth || 300, pad = o.pad || 0;
    const view = Math.max(0, o.view || 0);   // the top of the window in view (stage y): an item with nothing to stand beside sits where you are looking
    const T = turns || {}; const levelOf = (it) => it.mid || it.beside || ''; const known = (it) => { const m = levelOf(it); return !!(m && T[m] && typeof T[m].top === 'number'); };
    const order = (items || []).map((it, i) => ({ it, i })).sort((a, b) => { const ka = known(a.it), kb = known(b.it); if (ka && kb) return (T[levelOf(a.it)].top - T[levelOf(b.it)].top) || (a.i - b.i); if (ka) return -1; if (kb) return 1; return a.i - b.i; });
    const bottoms = new Array(cols).fill(pad), used = new Array(cols).fill(false); const out = []; let maxB = pad;
    order.forEach(({ it }) => { const ideal = known(it) ? Math.max(pad, T[levelOf(it)].top) : null; let best = 0, bestY = Infinity;
      for (let c = 0; c < cols; c++) { const floor = used[c] ? bottoms[c] + gap : bottoms[c]; const y = ideal == null ? Math.max(floor, view + pad) : Math.max(ideal, floor); if (y < bestY) { bestY = y; best = c; } }
      const h = Math.max(1, it.h || 1);
      out.push({ key: it.key, mid: it.mid || '', col: best, x: best * (cw + gap), y: bestY, h, level: ideal != null && bestY === ideal });
      bottoms[best] = bestY + h; used[best] = true; maxB = Math.max(maxB, bottoms[best]); });
    return { placements: out, height: maxB + pad + 8, columns: cols };
  }
  /* ── THE CHECKER (the design's numeric check for the router): an axis-aligned route must not pass through any
     card it does not start or end on, and must join its two ends on their edges. ── */
  function checkRoutes(routes, rects) {
    let crossings = 0, missed = 0; const eps = 1.5;
    const onEdge = (p, r) => !!r && ((Math.abs(p.x - r.x0) <= eps || Math.abs(p.x - r.x1) <= eps) && p.y >= r.y0 - eps && p.y <= r.y1 + eps || (Math.abs(p.y - r.y0) <= eps || Math.abs(p.y - r.y1) <= eps) && p.x >= r.x0 - eps && p.x <= r.x1 + eps);
    const through = (a, b, r) => { const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
      return x1 > r.x0 + eps && x0 < r.x1 - eps && y1 > r.y0 + eps && y0 < r.y1 - eps; };
    (routes || []).forEach((rt) => { const pts = rt.pts || []; if (pts.length < 2) { missed++; return; }
      if (!onEdge(pts[0], rt.from) || !onEdge(pts[pts.length - 1], rt.to)) missed++;
      for (let i = 1; i < pts.length; i++) (rects || []).forEach((r) => { if (r === rt.from || r === rt.to || (rt.from && r.key === rt.from.key) || (rt.to && r.key === rt.to.key)) return; if (through(pts[i - 1], pts[i], r)) crossings++; }); });
    return { crossings, missedJoins: missed, n: (routes || []).length };
  }

  /* ── THE COLUMN'S PARTS (the Canvas board), pure ──────────────────────────────────────────────────────────────── */
  const ITEM_SIZES = ['s', 'm', 'l', 'xl'];
  // a library of the estate loaded once from the page (the terminal element); the element that uses it draws when it lands
  // the picker row that means "this session's own sandbox" rather than a host of the estate; it never reaches an
  // item's content - picking it is resolved into a real host, container and socket first
  const SBX_PICK = '@session';
  // the languages a browser can simply show - the same set the chat offers a Preview on
  const PREVIEWABLE = (lang) => ['html', 'js', 'javascript', 'css', 'jsx', 'svg'].indexOf(String(lang || '').toLowerCase()) >= 0;
  // a code item's preview document: html and svg stand on their own, css dresses a small sample, js runs on a bare page
  function previewDoc(lang, code) {
    const L = String(lang || '').toLowerCase(), src = String(code || '');
    if (L === 'html' || L === 'svg') return src;
    const head = '<!doctype html><meta charset="utf-8"><style>body{margin:0;padding:10px;background:#101012;color:#d8dce4;font:12px system-ui,sans-serif}</style>';
    if (L === 'css') return head + '<style>' + src + '</style><h1>Heading</h1><p>A paragraph, a <a href="#">link</a> and a <button>button</button>, dressed by the sheet.</p>';
    return head + '<body><script>try{' + src + '}catch(e){document.body.innerHTML=\'<pre style="color:#c96b6b">\'+String(e&&e.message||e)+\'</pre>\';}<\/script>';
  }
  const _libs = {};
  function ensureLib(src, tag) {
    if (typeof document === 'undefined') return Promise.resolve(false);
    if (tag && typeof customElements !== 'undefined' && customElements.get(tag)) return Promise.resolve(true);
    if (_libs[src]) return _libs[src];
    return (_libs[src] = new Promise((res) => { const s = document.createElement('script'); s.src = src; s.async = true; s.onload = () => res(true); s.onerror = () => res(false); document.head.appendChild(s); }));
  }
  // the /mcp/call envelope, opened: the tool's own reply (prod answers {type, tool_name, content}; a stand-in {result})
  const unwrap = (j) => { if (!j || typeof j !== 'object') return j; if (j.result !== undefined && j.type === undefined) return j.result; if (j.content !== undefined && (j.type === 'tool_result' || j.tool_name)) return j.content; return j; };
  // the notebook page's own mapping of a cell's language to the command its exec runs (python · node · ruby · a shell snippet)
  function langRunCmd(lang, code) {
    const eof = 'VERA_NB_EOF_' + Math.random().toString(36).slice(2, 8);
    const heredoc = (i) => i + " - <<'" + eof + "'\n" + code + "\n" + eof;
    switch (String(lang || '').toLowerCase()) {
      case 'python': case 'python3': return heredoc('python3');
      case 'javascript': case 'js': case 'node': return heredoc('node');
      case 'ruby': return heredoc('ruby');
      default: return code;
    }
  }
  /* ── the estate's known hosts, as picker rows (pure): the saved connections (conn.list), the SSH hosts of the Exec
     panel and the running containers on every docker host (conn.targets — the remote subsystem's own enumeration).
     Each row is what the terminal needs — host_id · container · shell — with a name and a line to find it by. ── */
  function hostRowsOf(targets, conns, sid) {
    const T = targets && typeof targets === 'object' ? targets : {}; const out = [];
    // first, because it is the one the session means: the container this chat runs its commands in. The estate's
    // enumerations cannot list it - it belongs to the session, not the estate - so it is named here and resolved when
    // it is picked (Notes/42 defect 64).
    if (sid) out.push({ g: 'session', n: 'this session\'s sandbox', sub: 'the container this chat runs its commands in', host_id: SBX_PICK, container: '', shell: 'bash', kind: 'session', host: 'local' });
    const saved = Array.isArray(conns) ? conns : (conns && Array.isArray(conns.connections) ? conns.connections : []);
    saved.forEach((s) => { if (!s || typeof s !== 'object') return; const kind = String(s.kind || ''); const hostId = String(kind === 'docker' ? (s.docker_host_id || 'local') : (s.ssh_host_id || '')); if (!hostId) return;
      out.push({ g: 'saved', n: String(s.label || s.container || hostId), sub: kind + ' · ' + (kind === 'docker' && s.container ? s.container + ' @ ' + hostId : hostId), host_id: hostId, container: String(kind === 'docker' ? (s.container || '') : ''), shell: String(s.shell || ''), kind, id: String(s.id || '') }); });
    (Array.isArray(T.ssh) ? T.ssh : []).forEach((h) => { if (!h) return; const id = String(h.ssh_host_id || h.id || ''); if (!id) return;
      out.push({ g: 'ssh', n: String(h.label || id), sub: (h.user ? h.user + '@' : '') + String(h.host || id) + (h.port && +h.port !== 22 ? ':' + h.port : '') + (h.tags ? ' · ' + h.tags : ''), host_id: id, container: '', shell: '', kind: 'ssh' }); });
    (Array.isArray(T.docker) ? T.docker : []).forEach((d) => { if (!d) return; const name = String(d.container || d.name || ''); const hid = String(d.docker_host_id || d.host_id || 'local'); if (!name) return; const host = String(d.host_label || hid);
      out.push({ g: 'docker:' + host, n: name, sub: host + (d.image ? ' · ' + d.image : '') + (d.state ? ' · ' + d.state : ''), host_id: hid, container: name, shell: 'sh', kind: 'docker', host }); });
    return out;
  }
  /* ── every registered panel, as picker rows (pure): the registry (ui.panel.list — the same UI_PANELS the harness draws
     its tabs from, in tab order) grouped the way a panel is registered (tab · inject · mount · element · dynamic), the
     ones open for this session (ui.panels.open) first and marked — an open panel the registry does not list still shows. ── */
  function panelRowsOf(list, open) {
    const L = Array.isArray(list) ? list : (list && Array.isArray(list.panels) ? list.panels : []);
    const O = Array.isArray(open) ? open : (open && Array.isArray(open.panels) ? open.panels : []);
    const openBy = {}; O.forEach((p) => { if (p && p.id) openBy[String(p.id)] = p; });
    const rows = L.filter((p) => p && p.id).map((p) => { const id = String(p.id); const o = openBy[id]; return { id, n: String(p.label || id), icon: String(p.icon || ''), mode: String(p.dynamic ? 'dynamic' : (p.mode || 'inject')), g: o ? 'open' : String(p.dynamic ? 'dynamic' : (p.mode || 'inject')), order: +(p.tab_order == null ? 100 : p.tab_order) || 0, open: !!o, host: o ? String(o.host || '') : '', origin: o ? String(o.origin || '') : '' }; });
    O.forEach((p) => { if (p && p.id && !rows.some((r) => r.id === String(p.id))) rows.push({ id: String(p.id), n: String(p.label || p.id), icon: '', mode: '', g: 'open', order: 0, open: true, host: String(p.host || ''), origin: String(p.origin || '') }); });
    rows.sort((a, b) => (b.open - a.open) || (a.order - b.order) || a.n.localeCompare(b.n));
    return rows;
  }
  /* a picker: a search box that filters the rows in place, the rows in their groups (the add bar's popover, the
     terminal's host list — one construction) */
  function pickerHtml(rows, o) {
    o = o || {}; const groups = [], by = {};
    (rows || []).forEach((r) => { const g = String(r.g || ''); if (!by[g]) { by[g] = []; groups.push(g); } by[g].push(r); });
    const label = (g) => (o.labels && o.labels[g]) || (g.startsWith('docker:') ? 'containers on ' + g.slice(7) : g);
    return `<div class="row q"><input class="ti pk-q" placeholder="${esc(o.placeholder || 'search')}" spellcheck="false"><span class="vc-dim">${(rows || []).length}</span></div><div class="pk-list">`
      + groups.map((g) => `<div class="grp">${esc(label(g))} · ${by[g].length}</div>` + by[g].map(o.row).join('')).join('') + '</div>';
  }
  const HOST_GLYPH = { ssh: '>_', docker: '⬡', saved: '★' };
  const hostRow = (r) => `<button class="pp" data-act="hpick" data-host="${esc(r.host_id)}" data-container="${esc(r.container)}" data-shell="${esc(r.shell)}" data-n="${esc(r.n)}" data-q="${esc((r.n + ' ' + r.sub + ' ' + r.host_id + ' ' + r.kind).toLowerCase())}" title="${esc(r.kind === 'docker' ? 'a shell in the container ' + r.container + ' on ' + r.host_id : r.kind === 'ssh' ? 'a login shell on ' + r.host_id : 'the saved connection')}"><i class="ic">${esc(HOST_GLYPH[r.kind] || '>_')}</i><b>${esc(r.n)}</b><span>${esc(r.sub)}${r.shell ? ' · ' + esc(r.shell) : ''}</span></button>`;
  const hostListHtml = (rows) => (rows && rows.length ? pickerHtml(rows, { row: hostRow, placeholder: 'find a host · a container', labels: { saved: 'saved connections', ssh: 'ssh hosts · the Exec panel' } })
    : '<span class="vc-dim">no known host — the Exec panel keeps the SSH hosts, the Docker panel the hosts; type a host id</span>');
  const panelRow = (r) => `<button class="pp" data-act="padd" data-pid="${esc(r.id)}" data-plabel="${esc(r.n)}" data-q="${esc((r.n + ' ' + r.id + ' ' + r.mode).toLowerCase())}" title="${esc(r.id + (r.open ? ' — open for this session (' + (r.host || 'chat') + ')' : ' — ' + r.mode))}"><i class="ic">${esc(r.icon || '▥')}</i><b>${esc(r.n)}</b>${r.open ? '<em class="tag">open' + (r.host ? ' · ' + esc(r.host) : '') + '</em>' : ''}<span>${esc(r.id)}${r.mode ? ' · ' + esc(r.mode) : ''}</span></button>`;
  const panelListHtml = (rows) => pickerHtml(rows, { row: panelRow, placeholder: 'find a panel by name', labels: { open: 'open for this session', tab: 'tabs', inject: 'panels', mount: 'mounted', element: 'elements', dynamic: 'dynamic' } });
  /* the add bar: "+ note · terminal · panel · widget · chart" — each a real block type with its seed content; every
     one goes through canvas.add, the resolver's path, like anything an agent puts on the canvas */
  const ADD_KINDS = [
    { n: 'note', ik: '✎', kind: 'note', content: { title: 'Note', text: '' }, edit: true, menu: true },   // a menu: a blank note · a checklist · a decision · a link · from the clipboard
    { n: 'terminal', ik: '>_', kind: 'session', content: { title: 'Terminal', host_id: '', container: '', shell: '' }, hosts: true },   // the estate's known hosts pick the host (a blank terminal, a typed id too)
    { n: 'panel', ik: '▤', kind: 'panel', content: { panel: '', title: 'Panel' }, pick: true },
    { n: 'widget', ik: 'WG', kind: 'widget', content: { widget: '', title: 'Widget' }, sheet: true },   // the WidgetConfig sheet picks the record
    { n: 'chart', ik: 'CH', kind: 'widget', content: { name: 'chart', title: 'Chart', draw: { form: 'trace', size: 'm' }, data: [], source: { origin: 'you' } }, sheet: true, shape: 'series' },   // the WidgetConfig sheet on the series forms
  ];
  /* the note's menu (defect 44): no blank box on a click — each row says what it adds, and adds that */
  const NOTE_MENU = [
    { id: 'blank', n: 'a blank note', what: 'a note item, yours, open for typing', kind: 'note', content: { title: 'Note', text: '' }, edit: true },
    { id: 'checklist', n: 'a checklist', what: 'a markdown item with three boxes to tick', kind: 'markdown', content: { title: 'Checklist', md: '- [ ] first\n- [ ] second\n- [ ] third' }, edit: true },
    { id: 'decision', n: 'a decision', what: 'a question with its answers — the NOW band waits on it until one is picked', kind: 'note', content: { title: 'Decision', text: '', ask: { question: 'Which way?', options: ['yes', 'no'], why: 'you asked' } }, edit: true },
    { id: 'link', n: 'a link', what: 'a markdown item — its title and address', kind: 'markdown', content: { title: 'Link', md: '[title](https://)' }, edit: true },
    { id: 'clipboard', n: 'from the clipboard', what: 'a link, a diagram, code, a table or a note — by what the clipboard holds', clipboard: true },
  ];
  /* what the clipboard holds, as the item it becomes (pure): a URL is a link, mermaid its diagram, JSON rows a table,
     code by its shape, a longer text markdown, anything else a note */
  function fromClipboard(text) {
    const s = String(text == null ? '' : text).replace(/\r\n/g, '\n').trim();
    if (!s) return null;
    const first = s.split('\n')[0].trim();
    if (/^https?:\/\/\S+$/i.test(s)) { let host = s; try { host = new URL(s).host; } catch (e) {} return { kind: 'markdown', n: 'link', content: { title: host, md: '[' + host + '](' + s + ')' } }; }
    if (/^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(-v2)?|erDiagram|gantt|pie|journey|gitGraph|mindmap|timeline)\b/.test(first)) return { kind: 'diagram', n: 'diagram', content: { title: 'Diagram', mermaid: s } };
    if (/^[\[{]/.test(s)) {
      try { const j = JSON.parse(s); const rows = Array.isArray(j) ? j : (j && Array.isArray(j.rows) ? j.rows : null);
        if (rows && rows.length && rows.every((r) => r && typeof r === 'object' && !Array.isArray(r))) { const cols = Object.keys(rows[0]); return { kind: 'table', n: 'table', content: { title: 'Table', columns: cols, rows: rows.map((r) => cols.map((k) => r[k] == null ? '' : (typeof r[k] === 'object' ? JSON.stringify(r[k]) : String(r[k])))) } }; }
        if (rows && rows.length > 1 && rows.every(Array.isArray)) return { kind: 'table', n: 'table', content: { title: 'Table', columns: rows[0].map(String), rows: rows.slice(1).map((r) => r.map((x) => x == null ? '' : String(x))) } };
        return { kind: 'code', n: 'code', content: { title: 'JSON', lang: 'json', code: JSON.stringify(j, null, 2) } };
      } catch (e) { /* not JSON — on to code or a note */ }
    }
    const lines = s.split('\n');
    const codeish = lines.length > 1 && (/[{};]\s*$/m.test(s) || /^\s*(def|class|function|const|let|var|import|from|#include|SELECT|async|export|return|if|for|while)\b/m.test(s) || /^\$ /m.test(s));
    if (codeish) { const lang = /^\s*(def |class \w+:|import \w|from \w+ import)/m.test(s) ? 'python' : /^\s*(const|let|var|function|export|import .* from)\b/m.test(s) ? 'javascript' : /^\s*SELECT\b/im.test(s) ? 'sql' : /^\$ /m.test(s) ? 'sh' : ''; return { kind: 'code', n: 'code', content: { title: lang ? lang + ' snippet' : 'Snippet', lang, code: s } }; }
    if (lines.length > 3 || /^#{1,6} |^[-*] |\*\*/m.test(s)) return { kind: 'markdown', n: 'markdown', content: { title: first.replace(/^#+\s*/, '').slice(0, 60) || 'Pasted', md: s } };
    return { kind: 'note', n: 'note', content: { title: first.slice(0, 60) || 'Note', text: s } };
  }
  /* what each kind's popover opens with — the item it adds, before it adds it */
  const ADD_WHAT = { note: 'adds a note item — pick its shape', terminal: 'adds a terminal item — a live shell on the host you pick; a typed id or a blank one connects later', panel: 'adds a panel item — the panel\'s page in its frame, driven over the bridge', widget: 'adds a widget item — its form, source and size from the WidgetConfig sheet', chart: 'adds a chart — a series form (trace · bars · sparkline…) from the WidgetConfig sheet' };
  const seedName = (k) => k.n === 'chart' ? 'a trace chart' : k.n === 'widget' ? 'a widget frame' : 'a ' + k.n;
  const seedWhat = (k) => k.kind + ' item · ' + Object.keys(k.content || {}).filter((x) => x !== 'title').join(' · ');
  const KIND_GLYPH = { note: '✎', markdown: 'MD', code: '{}', session: '>_', table: 'TB', widget: 'WG', loop: '⟳', diagram: '◇', image: '▣', schedule: '⏰', html: '<>', suggest: '✦', notebook: 'NB', panel: '▥' };
  const glyphOf = t => KIND_GLYPH[t] || String(t || '?').slice(0, 2).toUpperCase();
  const hhmm = ts => { if (!ts) return ''; const d = new Date(ts); if (isNaN(d.getTime())) return String(ts).slice(0, 5); const p = n => (n < 10 ? '0' : '') + n; return p(d.getHours()) + ':' + p(d.getMinutes()); };
  /* the decision an item carries — what this turn is waiting on: content.ask {question, options, why, answer}, or a
     question with options on the content, or a loop whose status is waiting on the user. null when it carries none. */
  function decisionOf(b) {
    if (!b || typeof b !== 'object') return null;
    const c = b.content && typeof b.content === 'object' ? b.content : {};
    const a = c.ask && typeof c.ask === 'object' ? c.ask : null;
    const waitingLoop = b.type === 'loop' && /^(waiting|hitl|asking|paused|needs_input)$/i.test(String(c.status || ''));
    const q = a ? (a.question || a.q || a.prompt) : (c.question || c.q || (waitingLoop ? (c.prompt || c.ask_user || '') : ''));
    if (!q) return null;
    const raw = (a && (a.options || a.choices)) || c.options || c.choices || [];
    const options = (Array.isArray(raw) ? raw : []).map((o) => (o && typeof o === 'object') ? { n: String(o.n || o.label || o.name || o.v || o.value || ''), v: String(o.v != null ? o.v : (o.value != null ? o.value : (o.n || o.label || o.name || ''))) } : { n: String(o), v: String(o) }).filter((o) => o.n);
    const answer = String((a && a.answer) || c.answer || '');
    const why = String((a && a.why) || c.why || (b.type === 'loop' ? ('loop' + (c.goal ? ' · ' + c.goal : '') + ' is waiting on you') : 'this turn is waiting on you'));
    return { key: b.key, question: String(q), options, answer, why, since: (a && a.since) || c.asked || b.ts || '', answered: (a && a.answered) || c.answered || '', run: c.run || '' };
  }
  /* what Vera can also do: the document's suggestions and those any live item carries; each names the kind and content
     it becomes; one already on the canvas (its key exists) is 'taken' */
  function suggestionsOf(doc, blocks, focusMid) {
    const out = []; const have = new Set((blocks || []).filter(b => b && b.key).map(b => String(b.key)));
    const slug = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'item';
    const push = (s, mid) => { if (!s) return; const o = typeof s === 'string' ? { n: s } : s; const n = String(o.n || o.title || o.text || o.label || ''); if (!n) return;
      const kind = String(o.kind || o.type || 'note'); const key = String(o.key || (kind + ':' + slug(n)));
      out.push({ n, kind, key, content: o.content && typeof o.content === 'object' ? o.content : { title: n, text: n }, mid: String(o.mid || o.turn || mid || ''), taken: have.has(key) }); };
    const docS = doc && Array.isArray(doc.suggestions) ? doc.suggestions : [];
    docS.forEach(s => push(s, focusMid));
    (blocks || []).forEach((b) => { if (!b || !b.key || (b.state || 'now') === 'hidden') return; const c = b.content || {}; const arr = Array.isArray(c.suggestions) ? c.suggestions : Array.isArray(c.can_also) ? c.can_also : [];
      const a = b.anchor && typeof b.anchor === 'object' ? String(b.anchor.turn || b.anchor.mid || '') : ''; arr.forEach(s => push(s, a || focusMid)); });
    const seen = new Set(); return out.filter(s => !seen.has(s.key) && seen.add(s.key)).slice(0, 8);
  }
  /* the NOW bar's words: what this turn is waiting on, else what is live */
  function nowText(now, decision, suggs, inFocus) {
    const n = (now || []).length, ns = (suggs || []).length;
    if (decision && !decision.answer) return (hhmm(decision.since) ? hhmm(decision.since) + ' · ' : '') + 'waiting on you · 1 input' + (ns ? ' · ' + ns + ' suggested' : '');
    if (decision) return (hhmm(decision.answered) ? hhmm(decision.answered) + ' · ' : '') + 'answered · ' + n + ' now' + (ns ? ' · ' + ns + ' suggested' : '');
    if (!n && !ns) return 'nothing waiting on you';
    return n + ' now' + (inFocus != null ? ' · ' + inFocus + ' in focus' : '') + (ns ? ' · ' + ns + ' suggested' : '');
  }
  /* a dragged height, as the size record it becomes */
  function sizeOfHeight(h) { h = Number(h) || 0; return h <= 96 ? 's' : h <= 210 ? 'm' : h <= 380 ? 'l' : 'xl'; }
  /* the turns in order of their measured tops; an item is aged when its turn is more than `back` turns behind the focus */
  function turnOrder(turns) { const T = turns || {}; return Object.keys(T).filter(k => T[k] && typeof T[k].top === 'number').sort((a, b) => T[a].top - T[b].top); }
  function isAged(mid, focusMid, order, back) {
    back = back == null ? 2 : back; if (!mid || !focusMid || mid === focusMid) return false;
    const i = (order || []).indexOf(mid), f = (order || []).indexOf(focusMid); if (i < 0 || f < 0) return false; return i < f - back;
  }
  /* compact = a header line: Hover and Zen fold everything not opened; an aged item folds in every tier; the NOW
     items (the decision, the suggestions), a hovered one, the turn in view's own and anything the relevance pass holds
     IN FOCUS never fold — the band says "in focus", so the item is open (a placed widget folded away one turn later) */
  function foldOf(o) { o = o || {}; if (o.now || o.open || o.hovered || o.fresh || o.inFocus) return false; return !!(o.aged || (o.tier && o.tier !== 'full')); }
  const textFieldOf = t => t === 'markdown' ? 'md' : t === 'code' ? 'code' : t === 'html' ? 'html' : 'text';
  /* the rail's rows (pure): the session's own canvas first ("this session"), the named canvases by recency, then the
     other sessions' canvases ("session · <id>") — every session canvas is titled "Session canvas", so the id tells them apart */
  function railRows(rows, sessId) {
    const R = (rows || []).filter((c) => c && c.id).map((c) => { const isS = String(c.mode || '') === 'session' || /^cv_session_/.test(String(c.id)); const mine = !!sessId && c.id === sessId;
      return Object.assign({}, c, { session: mine, other: isS && !mine, sid: isS ? String(c.id).replace(/^cv_session_/, '') : '' }); });
    const rank = (c) => c.session ? 0 : c.other ? 2 : 1;
    return R.sort((a, b) => rank(a) - rank(b) || String(b.updated || '').localeCompare(String(a.updated || '')));
  }
  /* a block's title (pure): the content's own, else — a keyless block of an agent's canvas — its first line, else its kind */
  function blockTitle(b) {
    const c = (b && b.content) || {}; const own = c.title || c.name || c.goal || c.filename || c.caption || c.widget || c.panel;
    if (own) return String(own);
    if (b && b._bid) { const first = String(c[textFieldOf(b.type)] || '').split('\n').map(s => s.trim()).find(Boolean) || ''; const line = first.replace(/^#+\s*|^[-*]\s+\[.\]\s*|^[-*]\s+|\*\*/g, '').slice(0, 60); return line || ({ diagram: 'Diagram', table: 'Table', image: 'Image', code: 'Code', markdown: 'Text', note: 'Note', html: 'HTML' }[b.type] || String(b.type || 'block')); }
    return String(b && b.key ? String(b.key).split(':').slice(1).join(':') : '') || String((b && b.type) || 'block');
  }
  const EDITABLE = ['note', 'markdown', 'code', 'html'];

  class VeraCanvas extends (typeof HTMLElement !== 'undefined' ? HTMLElement : class {}) {
    static get observedAttributes() { return ['canvas-id', 'rows', 'compact', 'columns', 'rail', 'session-id', 'bare', 'blocks']; }

    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._rev = null;
      this._timer = null;
      this._open = new Set();     // items opened in place
      this._px = {};              // a dragged height per item, within its size record
      this._editKey = null;       // the item whose text is being edited
      this._rzT = 0;
    }

    connectedCallback() {
      this.shadowRoot.innerHTML = `<style>${CSS}</style>
        <div class="shell"><aside class="rail" id="rail" data-w="canvas.rail" hidden></aside>
        <div class="wrap">
          <div class="head">
            <span class="live" title="live — it follows the document"></span>
            <span class="title" id="title">Canvas</span>
            <span class="pill" id="mode" hidden></span>
            <span class="pill" id="count"></span>
            <a class="open" id="open" target="_blank" rel="noopener">open ↗</a>
          </div>
          <div class="body" id="body"><div class="empty">Loading…</div></div>
        </div></div>`;
      if (this.hasAttribute('rail')) this._railMount();
      if (this.hasAttribute('rows')) {
        const r = parseInt(this.getAttribute('rows'), 10);
        if (r > 0) this.style.setProperty('--vc-max', (r * 62) + 'px');
      }
      if (this.hasAttribute('compact')) this.style.setProperty('--vc-max', '240px');
      this.refresh();
      // Live-follow: cheap revision check, repaint only on real change.
      this._timer = setInterval(() => this.refresh(), 3000);
    }

    disconnectedCallback() {
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
      if (this._railTimer) { clearInterval(this._railTimer); this._railTimer = null; }
      if (this._liveTick) { clearInterval(this._liveTick); this._liveTick = null; }
    }

    attributeChangedCallback(name) {
      if (name === 'canvas-id' && this.shadowRoot.childElementCount) {
        this._rev = null;
        this.refresh();
      }
      if (name === 'columns' && this._doc) this.render(this._doc);
      if (name === 'rail' && this.shadowRoot.childElementCount) { if (this.hasAttribute('rail')) this._railMount(); else { const r = this.shadowRoot.getElementById('rail'); if (r) r.hidden = true; if (this._railTimer) { clearInterval(this._railTimer); this._railTimer = null; } } }
      if (name === 'session-id' && this._railTimer) this._railRefresh();
    }
    /* ── the stage (the column's split projection): the host hands the transcript's turn tops ({mid:{top,height}} in
       the transcript's scroll frame) and keeps the column's scroll in step; the element places and reports. ── */
    setTurns(turns, o) {
      this._turns = turns && typeof turns === 'object' ? turns : {}; this._turnsH = o && o.height > 0 ? o.height : 0;
      // the turn in focus (data-focus-mid, set by the host) decides which items have aged; a change repaints
      const fm = this.dataset.focusMid || '';
      if (fm !== this._focusMid) { this._focusMid = fm; if (this._doc && this._doc.mode === 'session') { this.render(this._doc); return; } }
      if (this.hasAttribute('stage')) this._placeNow();
    }
    syncScroll(msgsScrollTop, msgsTopClient) {
      const body = this.shadowRoot.getElementById('body'), st = this.shadowRoot.getElementById('stage'); if (!body || !st) return;
      if (this._rz) return;                                  // a resize in hand keeps the column still
      const br = body.getBoundingClientRect();
      // an item at stage y = its turn's top lands at the turn's own client top
      body.scrollTop = Math.max(0, Math.round(msgsScrollTop + st.offsetTop - (msgsTopClient - br.top)));
      const first = this._view == null; this._view = Math.max(0, body.scrollTop - st.offsetTop);   // where you are looking, in STAGE y (the stage sits below the heads in flow) — the placer's floor for a turn-less item
      // the first sync: an item with nothing to stand beside was placed before the view was known — once, it moves into view
      if (first && this._placed && this._placed.placements.some((p) => !p.mid && !p.level)) this._placeNow();
    }
    itemRects() {
      const out = []; const F = this._focus;
      this.shadowRoot.querySelectorAll('.it[data-key]').forEach((el) => { const r = el.getBoundingClientRect(); out.push({ key: el.dataset.key, mid: el.dataset.mid || '', col: +(el.dataset.col || 0), state: el.classList.contains('pinned') ? 'pinned' : 'now', inFocus: !F || F.has(el.dataset.key) || el.classList.contains('pinned'), out: el.classList.contains('out'), compact: el.classList.contains('compact'), open: el.classList.contains('openin'), ghost: el.classList.contains('ghost'), rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height } }); });
      return out;
    }
    _placeNow() {
      const st = this.shadowRoot.getElementById('stage'); if (!st) return;
      const cols = Math.max(1, Math.min(4, parseInt(this.getAttribute('columns') || '1', 10) || 1)); const W = st.clientWidth || 300, gap = 10; const w = Math.floor((W - gap * (cols - 1)) / cols);
      const cards = [...st.querySelectorAll('.it')]; cards.forEach((c) => { c.style.width = w + 'px'; });
      const items = cards.map((c) => ({ key: c.dataset.key, h: c.offsetHeight, mid: c.dataset.mid || '', beside: c.dataset.beside || '' }));
      const bar = this.shadowRoot.querySelector('.addbar'), bh = this.shadowRoot.querySelector('.band.now > .band-h');
      const pad = (bar ? bar.offsetHeight : 0) + (bh ? bh.offsetHeight : 0);   // the sticky heads overlay the stage's top: nothing is placed under them
      const body = this.shadowRoot.getElementById('body');
      const P = place(items, this._turns || {}, { columns: cols, gap, colWidth: w, pad, view: this._view != null ? this._view : (body ? body.scrollTop : 0) });
      P.placements.forEach((p) => { const c = cards.find((x) => x.dataset.key === p.key); if (!c) return; c.style.left = p.x + 'px'; c.style.top = p.y + 'px'; c.dataset.col = String(p.col); c.classList.toggle('level', !!p.level); });
      // the stage is at least as tall as the transcript's scroll height, so the column can scroll in step with it
      st.style.height = Math.max(P.height, (this._turnsH || 0) + 40) + 'px'; this._placed = P;
      this._liveLayout();
      try { this.dispatchEvent(new CustomEvent('vera:canvas:placed', { bubbles: true, detail: { n: P.placements.length, level: P.placements.filter((p) => p.level).length, columns: cols, height: P.height } })); } catch (e) { /* observers are optional */ }
    }

    get canvasId() { return this.getAttribute('canvas-id') || ''; }

    async refresh() {
      const id = this.canvasId;
      const body = this.shadowRoot.getElementById('body');
      if (!body) return;
      if (!id) { body.innerHTML = this.hasAttribute('rail') ? '<div class="empty">Pick a canvas on the left, or make one — the session\'s is the chat\'s own.</div>' : '<div class="err">No canvas-id given.</div>'; return; }
      let doc;
      try {
        const r = await fetch(`/canvas/get?id=${encodeURIComponent(id)}`,
                              { headers: { Accept: 'application/json' } });
        const j = await r.json();
        doc = (j && j.content !== undefined) ? j.content : j;
        if (doc && doc.canvas) doc = doc.canvas;
      } catch (e) {
        if (this._rev === null) body.innerHTML = '<div class="err">Canvas unreachable.</div>';
        return;                                   // keep the last good render
      }
      if (!doc || doc.error) {
        body.innerHTML = `<div class="err">${esc((doc && doc.error) || 'Not found')}</div>`;
        return;
      }
      const rev = doc.revision != null ? doc.revision : doc.rev != null ? doc.rev
        : (doc.updated_at || doc.updated || '') + ':' + ((doc.blocks || []).length);
      if (rev === this._rev) return;              // nothing changed — no repaint
      this._rev = rev;
      this._doc = doc;
      this.render(doc);
    }

    render(doc) {
      const root = this.shadowRoot;
      const blocks = doc.blocks || [];
      root.getElementById('title').textContent = doc.title || 'Canvas';
      root.getElementById('count').textContent =
        blocks.length + (blocks.length === 1 ? ' block' : ' blocks');
      const link = root.getElementById('open');
      if (link) link.href = `/canvas/panel?canvas=${encodeURIComponent(this.canvasId)}`;

      const body = root.getElementById('body');
      // one projection for every canvas: the session canvas as the board's column, an agent's canvas as the same
      // cards in the document's order (the Canvas panel, the card canvas.show puts in the chat)
      this.renderSession(doc, blocks, body);
      if (this.hasAttribute('rail')) this._railMark();
    }

    /* The relevance engine's answer (Notes/38 §3.3, P2): the keys in focus now, with their scores. The tier decides
       how the rest shows — Full greys them, Hover reveals on hover, Zen shows only the focus set (the tier is
       data-den on <html>, the appearance the whole UI shares). */
    setFocus(keys, scores) { this._focus = Array.isArray(keys) ? new Set(keys.map(String)) : null; this._scores = scores && typeof scores === 'object' ? scores : {}; if (this._doc) this.render(this._doc); }
    retier() { if (this._doc) this.render(this._doc); }
    tier() { const d = this.ownerDocument && this.ownerDocument.documentElement; const t = (this.getAttribute('tier') || (d && d.getAttribute('data-den')) || 'full').toLowerCase(); return t === 'hover' || t === 'zen' ? t : 'full'; }

    /* The session projection (Notes/38 P1; the Canvas board's canvas column): the add bar at the top, pinned items
       above the flow, the NOW band (what this turn is waiting on — the decision with its answers, what Vera can also
       do — then the live items, newest first), each item a card at its size that folds to a header line in Hover and
       Zen and opens in place on a click, with a corner grip and an edit rail (edit · pin · park · size · drop · in
       context); parked items a chip line (a click brings one back through the resolver), hidden items a popover on
       the add bar. Keyless blocks on a session canvas still render, as plain blocks after the bands. */
    renderSession(doc, blocks, body) {
      // an agent's or the panel's canvas (no session): every block a card in the document's order — a keyless block
      // under a view key (blk:<id>) so the same cards, rails and live layer serve it
      const plainDoc = doc.mode !== 'session' && !doc.session;
      const byOrder = arr => arr.slice().sort((a, b) => ((a.layout && a.layout.order) || 0) - ((b.layout && b.layout.order) || 0));
      const view = plainDoc ? byOrder(blocks.filter(Boolean)).map(b => b.key ? b : Object.assign({}, b, { key: 'blk:' + b.id, _bid: true, state: 'now', size: b.size || (b.meta && b.meta.size) || 'm' })) : blocks;
      const keyed = view.filter(b => b && b.key);
      const plain = view.filter(b => !(b && b.key));
      const by = st => keyed.filter(b => (b.state || 'now') === st);
      const newest = arr => plainDoc ? byOrder(arr) : arr.slice().sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
      const pinned = by('pinned'), now = newest(by('now')), parked = by('parked'), hidden = by('hidden');
      const head = this.shadowRoot.getElementById('count');
      if (head) head.textContent = plainDoc ? keyed.length + (keyed.length === 1 ? ' block' : ' blocks') : now.length + ' now · ' + pinned.length + ' pinned · ' + parked.length + ' parked';
      const modeEl = this.shadowRoot.getElementById('mode'); if (modeEl) { modeEl.hidden = !plainDoc; modeEl.textContent = String(doc.mode || 'static') + (doc.topic ? ' · ' + doc.topic : ''); modeEl.className = 'pill mode-' + esc(doc.mode || 'static'); modeEl.title = doc.mode === 'dynamic' ? 'dynamic — it tracks its topic; agents fill it in' : 'static — a working area'; }
      const tier = this.tier(), F = this._focus;
      const focusMid = this.dataset.focusMid || ''; this._focusMid = focusMid;
      const order = turnOrder(this._turns || {});
      const stage = this.hasAttribute('stage');
      // the NOW band's two parts: the decision this turn waits on (the first live item that carries one) and what
      // Vera can also do (the suggestions the document and the live items carry)
      const decisions = now.concat(pinned).map(b => ({ b, d: decisionOf(b) })).filter(x => x.d);
      const decision = (decisions.find(x => !x.d.answer) || decisions[0] || {}).d || null;
      const suggs = suggestionsOf(doc, keyed, focusMid); this._suggs = suggs;
      const inFocusN = F ? keyed.filter(b => F.has(String(b.key))).length : null;
      const nowTxt = plainDoc ? (now.length ? now.length + (now.length === 1 ? ' block' : ' blocks') : 'nothing yet') + ' · ' + (doc.mode || 'static') + (decision && !decision.answer ? ' · waiting on you' : '') : nowText(now, decision, suggs, inFocusN);
      const titleOf = b => blockTitle(b);
      const askHtml = d => `<div class="askb" data-w="canvas.decision">
            <span class="why">surfaced because <b>${esc(d.why)}</b></span>
            <span class="q">${esc(d.question)}</span>
            <div class="opts">${d.options.map(o => `<button data-act="answer" data-ans="${esc(o.v)}" class="${d.answer === o.v ? 'on' : ''}" title="Answer — it goes to the run">${esc(o.n)}</button>`).join('')}${d.answer ? '' : '<span class="or">or type below — it goes to the run, not into a new turn</span>'}</div>
            <span class="st">${d.answer ? 'answered' + (hhmm(d.answered) ? ' ' + hhmm(d.answered) : '') + ' · "' + esc(d.answer) + '" sent to the run' : 'waiting' + (hhmm(d.since) ? ' since ' + hhmm(d.since) : '') + ' · the composer answers this too'}</span></div>`;
      const editHtml = b => { const c = b.content || {}; const ask = c.ask && typeof c.ask === 'object' && !c[textFieldOf(b.type)]; const f = ask ? 'ask.question' : textFieldOf(b.type); const v = ask ? (c.ask.question || '') : (c[f] || '');   // a decision's editor edits its question
        return `<div class="edit" data-w="canvas.update"><textarea class="ta${b.type === 'code' || b.type === 'html' ? ' code' : ''}" data-field="${f}" spellcheck="false">${esc(v)}</textarea>
          <div class="edit-a"><button class="ib" data-act="save">Done</button><button class="ib" data-act="cancel">Cancel</button><span class="vc-dim">${esc(b.type)} · ${f}</span></div></div>`; };
      const card = b => {
        // out of focus: greyed in Full, revealed on hover in Hover, gone in Zen; a pinned item is never out
        const inF = !F || F.has(String(b.key)) || b.state === 'pinned';
        const sc = this._scores && this._scores[b.key]; const scoreTxt = sc && sc.score != null ? Number(sc.score).toFixed(2) : (b.score != null ? Number(b.score).toFixed(2) : '');
        const why = sc && sc.signals ? Object.keys(sc.signals).map(k => k + ' ' + Number(sc.signals[k]).toFixed(2)).join(' · ') : '';
        const fcls = inF ? (F ? ' inf' : '') : (tier === 'zen' ? ' out' : ' dim');
        const fn = BLOCK[b.type] || BLOCK.note;
        const c = b.content || {};
        const key = String(b.key);
        const dec = decisionOf(b); const isNow = !!dec && !dec.answer;
        const editing = this._editKey === key;
        let inner;
        if (editing) inner = editHtml(b);
        else {
          try { inner = fn(c, b.size, key, this); }
          catch (e) { inner = `<div class="err">Could not render a ${esc(b.type)} block.</div>`; }
          // a decision draws its ask above its own body; a bare question (a note that is only its question) is the ask alone
          if (dec) inner = askHtml(dec) + (b.type === 'loop' || c[textFieldOf(b.type)] ? inner : '');
        }
        const title = blockTitle(b);
        const size = ITEM_SIZES.includes(b.size) ? b.size : 'm';
        const a = b.anchor && typeof b.anchor === 'object' ? b.anchor : null; const mid = a ? String(a.turn || a.mid || '') : '';
        // an item added by hand: yours, level with the turn it was added beside, related to no turn (no run, never aged)
        const beside = a && !mid ? String(a.beside || '') : ''; const yours = !!(a && a.origin === 'you' && !mid);
        const open = this._open.has(key) || editing;
        const aged = isAged(mid, focusMid, order) && !(F && F.has(key)) && b.state !== 'pinned';
        const hovered = this._hovKey === key && (tier === 'hover' || aged);
        const fresh = !!mid && mid === focusMid;                                    // the turn in view produced it: open, in every tier
        const wouldFold = foldOf({ tier, aged, open, now: isNow, fresh, inFocus: !!F && F.has(key) });   // a header line, until opened
        const compact = wouldFold && !hovered;
        const px = this._px[key];
        const editable = EDITABLE.includes(b.type);
        const bid = b._bid ? String(b.id) : '';   // a keyless block: addressed by its id (canvas.update · canvas.remove · canvas.move)
        const cls = 'it ' + esc(b.state || 'now') + fcls + (fresh ? ' fresh' : '') + (wouldFold ? ' foldable' : '') + (compact ? ' compact' : '') + (wouldFold && hovered ? ' hovopen' : '') + (open ? ' openin' : '') + (aged ? ' aged' : '') + (dec ? ' now' : '') + (isNow ? ' waiting' : '') + (px ? ' sized' : '');
        return `<div class="${cls}" data-key="${esc(b.key)}" data-size="${size}" data-type="${esc(b.type)}"${mid ? ' data-mid="' + esc(mid) + '"' : ''}${beside ? ' data-beside="' + esc(beside) + '"' : ''}${scoreTxt ? ' data-score="' + esc(scoreTxt) + '"' : ''}${px && !compact ? ' style="height:' + Math.round(px) + 'px"' : ''}>
          <div class="it-hd"><span class="ic vc-badge" data-kind="${esc(b.type)}" title="${esc(b.type)}">${esc(glyphOf(b.type))}</span><span class="t" title="${esc(title)}">${esc(title)}</span>${scoreTxt ? '<span class="sc" title="' + esc('relevance ' + scoreTxt + (why ? ' — ' + why : '')) + '">' + esc(scoreTxt) + '</span>' : ''}
            ${mid ? '<span class="src" title="the turn using it">' + esc(mid) + '</span>' : yours ? '<span class="src" title="added by you — it relates to no turn">you</span>' : ''}<span class="k">${esc(bid ? b.type : b.key)}</span>
            <span class="xp" data-act="open" title="${open ? 'Fold it back' : 'Open in place — the column makes room'}">${open ? '⤡' : '⤢'}</span></div>
          <div class="it-bd">${inner}</div>
          <div class="it-ft" data-w="canvas.item.rail"><span class="it-a">
              ${editable ? `<button data-act="edit" class="${editing ? 'on' : ''}" title="Edit its text — saved through canvas.update">${editing ? 'Editing' : 'Edit'}</button>` : ''}
              ${plainDoc ? '<button data-act="up" title="Move it up — canvas.move">↑</button><button data-act="down" title="Move it down — canvas.move">↓</button>' : ''}
              ${bid ? '' : `<button data-act="pin" class="${b.state === 'pinned' ? 'on' : ''}" title="${b.state === 'pinned' ? 'Unpin — back into the flow' : 'Pin above the flow'}">${b.state === 'pinned' ? 'unpin' : 'pin'}</button>
              <button data-act="park" title="Park it below the flow">park</button>`}
              <button data-act="size" title="Size: ${size} — click for the next">${size}</button>
              <button data-act="remove" title="Take it off the canvas">drop</button>
              ${bid ? '' : `<button data-act="ctx" class="ctx ${inF && (F || b.state === 'pinned') ? 'on' : ''}" title="${b.state === 'pinned' ? 'Pinned — always in context; click to release it' : inF && F ? 'In focus now — pin it to keep it in context' : 'Put it in context — pins it, so it never leaves focus'}">${inF && (F || b.state === 'pinned') ? '✓ In context' : 'In context'}</button>`}
            </span></div>
          <div class="rz" data-w="canvas.size" title="Drag to resize — it is saved as the item's size"></div></div>`;
      };
      // what Vera can also do — a ghost item in the NOW band; each suggestion becomes an item here when taken
      const ghost = suggs.length ? `<div class="it now ghost" data-key="suggest:${esc(focusMid || 'now')}" data-type="suggest" data-size="m" data-w="canvas.suggest"${focusMid ? ' data-mid="' + esc(focusMid) + '"' : ''}>
          <div class="it-hd"><span class="ic" data-kind="suggest">✦</span><span class="t">Vera can also</span>${focusMid ? '<span class="src">' + esc(focusMid) + '</span>' : ''}<span class="k">${suggs.filter(s => s.taken).length}/${suggs.length} taken</span></div>
          <div class="it-bd"><div class="sugb"><span class="why">from this turn · each becomes an item here when you take it</span>
            ${suggs.map((s, i) => `<button class="sg ${s.taken ? 'on' : ''}" data-act="take" data-i="${i}" title="${s.taken ? 'On the canvas — click to bring it into the NOW band' : 'Take it — it becomes a ' + esc(s.kind) + ' item'}"><i></i>${esc(s.n)}${s.taken ? ' · added' : ''}<b>${esc(s.kind)}</b></button>`).join('')}
          </div></div></div>` : '';
      const chip = b => {
        const c = b.content || {};
        const title = c.title || c.name || c.filename || String(b.key).split(':').slice(1).join(':') || b.type;
        return `<span class="chip" data-key="${esc(b.key)}" title="Parked — click to bring it back"><i></i>${esc(title)}</span>`;
      };
      let html = '';
      // the add bar, fixed at the top of the column: each kind adds an item through canvas.add; hidden items are its popover
      html += `<div class="addbar" data-w="canvas.add"><span class="lbl">Add</span>${ADD_KINDS.map(k => `<button class="add" data-act="add" data-kind="${k.n}" title="Add a ${k.n} to the session canvas (a ${k.kind} item)"><b>${esc(k.ik)}</b>${k.n}</button>`).join('')}` +
        (hidden.length ? `<span class="hidwrap"><button class="hidbtn" data-act="hid" title="Hidden items — a click brings one back">hidden · ${hidden.length} ▾</button><div class="hidpop" hidden>${hidden.map(chip).join('')}</div></span>` : '') + '</div>';
      if (pinned.length) html += `<div class="band pinned"><div class="band-h">pinned · ${pinned.length}</div>${pinned.map(card).join('')}</div>`;
      // the NOW band: what this turn is waiting on first (the decision, then what Vera can also do), then the live items, newest
      // first; on the stage they are placed level with their turns (absolute, after a measure); in the flow they stack
      const nowOrder = now.slice().sort((x, y) => { const dx = decisionOf(x), dy = decisionOf(y); const wx = dx && !dx.answer ? 0 : dx ? 1 : 2, wy = dy && !dy.answer ? 0 : dy ? 1 : 2; return wx - wy; });
      const nowCards = (nowOrder.length ? nowOrder.slice(0, 1).map(card).join('') : '') + ghost + nowOrder.slice(1).map(card).join('');
      html += `<div class="band now"><div class="band-h"><span class="nowbar ${decision && !decision.answer ? 'wait' : 'ok'}" data-w="canvas.now" title="${plainDoc ? 'The canvas, in its order' : 'What this turn is waiting on'}"><i></i><b>${plainDoc ? 'BLOCKS' : 'NOW'}</b> ${esc(nowTxt)}</span></div>` +
        (nowCards ? (stage ? '<div class="stage" id="stage">' + nowCards + '</div>' : nowCards) : plainDoc ? '<div class="empty">Nothing on this canvas — add a block above, or let an agent fill it.</div>' : '<div class="empty">Nothing in the NOW band — nothing is waiting on you; items land here as the conversation uses them.</div>') + '</div>';
      if (parked.length) html += `<div class="band parked"><div class="band-h">parked · ${parked.length}</div><div class="chips">${parked.map(chip).join('')}</div></div>`;
      if (plain.length) html += plain.map(b => {
        const fn = BLOCK[b.type] || BLOCK.note; let inner;
        try { inner = fn(b.content || {}); } catch (e) { inner = `<div class="err">Could not render a ${esc(b.type)} block.</div>`; }
        return `<div class="blk" data-type="${esc(b.type)}">${inner}</div>`;
      }).join('');
      const keepTop = body.scrollTop;
      this._layers(body).items.innerHTML = html;
      if (!stage) body.scrollTop = keepTop;
      this._bind(body);
      if (stage) this._placeNow();
      this._mountLive(body);
      if (this._editKey) { const ta = body.querySelector('.it[data-key="' + this._editKey.replace(/"/g, '\\"') + '"] textarea'); if (ta && !this._editFocused) { this._editFocused = true; try { ta.focus(); } catch (e) {} } }
      if (body.querySelector('vera-mermaid')) ensureLib('/ui/elements/vera_mermaid.js', 'vera-mermaid');
      if (window.mermaid && body.querySelector('.mermaid')) {
        try { window.mermaid.run({ nodes: body.querySelectorAll('.mermaid') }); }
        catch (e) { /* leave the source visible */ }
      }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:rendered', { bubbles: true, detail: { id: this.canvasId, revision: doc.revision, now: now.length, pinned: pinned.length, parked: parked.length, hidden: hidden.length, focus: F ? F.size : null, tier, waiting: !!(decision && !decision.answer), suggested: suggs.length, nowText: nowTxt } })); } catch (e) { /* observers are optional */ }
    }

    /* ── the column's one set of listeners, bound once to the body (its markup is replaced on every render) ── */
    _bind(body) {
      if (this._bound) return; this._bound = true;
      body.addEventListener('click', (ev) => {
        const t = ev.target; if (!t || !t.closest) return;
        if (t.closest('textarea')) return;
        const act = t.closest('[data-act]');
        if (act && body.contains(act)) { ev.stopPropagation(); this._act(act, ev); return; }
        const ch = t.closest('.chip[data-key]'); if (ch) { ev.stopPropagation(); this.call('canvas.add', { key: ch.dataset.key }); return; }
        const hd = t.closest('.it-hd'); const it = hd && hd.closest('.it[data-key]');
        if (it && !it.classList.contains('ghost')) { ev.stopPropagation(); this._toggleOpen(it.dataset.key); }
      });
      // hover: the host hears which item is under the pointer (the runs light up); in the Hover tier a folded item
      // opens in the layout while the pointer is on it — the column makes room, like a click in Zen
      body.addEventListener('mouseover', (ev) => { const it = ev.target.closest && ev.target.closest('.it[data-key]'); const k = it ? it.dataset.key : null; if (k !== this._hovKey) { this._hovKey = k; this._hoverOpen(it); try { this.dispatchEvent(new CustomEvent('vera:canvas:hover', { bubbles: true, detail: { key: k } })); } catch (e) {} } });
      body.addEventListener('mouseleave', () => { if (this._hovKey) { this._hovKey = null; this._hoverOpen(null); try { this.dispatchEvent(new CustomEvent('vera:canvas:hover', { bubbles: true, detail: { key: null } })); } catch (e) {} } });
      // the corner grip: a drag sizes the item; the drop saves it as the item's size (s · m · l · xl)
      body.addEventListener('mousedown', (ev) => { const g = ev.target.closest && ev.target.closest('.rz'); if (!g) return; const it = g.closest('.it[data-key]'); if (!it) return;
        ev.preventDefault(); ev.stopPropagation(); it.classList.add('sized', 'resizing'); this._rz = { key: it.dataset.key, el: it, y0: ev.clientY, h0: it.offsetHeight, h: it.offsetHeight }; });
      const doc = this.ownerDocument || document;
      doc.addEventListener('mousemove', (ev) => { const r = this._rz; if (!r) return; const h = Math.max(40, r.h0 + (ev.clientY - r.y0)); r.h = h; r.el.style.height = h + 'px'; r.el.classList.add('openin');
        if (!r.raf) r.raf = requestAnimationFrame(() => { r.raf = 0; if (this.hasAttribute('stage')) this._placeNow(); }); });
      doc.addEventListener('mouseup', () => { const r = this._rz; if (!r) return; this._rz = null; r.el.classList.remove('resizing'); this._rzT = Date.now();
        const key = r.key; this._px[key] = r.h; this._open.add(key);
        const size = sizeOfHeight(r.h);
        try { this.dispatchEvent(new CustomEvent('vera:canvas:resized', { bubbles: true, detail: { key, height: r.h, size } })); } catch (e) {}
        if (size !== r.el.dataset.size) this._setSize(key, size); else if (this.hasAttribute('stage')) this._placeNow(); });
      // a picker's search box: the rows that do not carry the words are hidden, a group with none left with them
      body.addEventListener('input', (ev) => { const q = ev.target; if (!q || !q.classList || !q.classList.contains('pk-q')) return; const s = String(q.value || '').toLowerCase().trim(); const list = q.closest('.pk') && q.closest('.pk').querySelector('.pk-list'); if (!list) return;
        let grp = null, any = false; [...list.children].forEach((n) => { if (n.classList.contains('grp')) { if (grp) grp.hidden = !any; grp = n; any = false; return; } const on = !s || (n.dataset.q || '').includes(s); n.hidden = !on; any = any || on; }); if (grp) grp.hidden = !any; });
      body.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && this._editKey) { this._editKey = null; this._editFocused = false; if (this._doc) this.render(this._doc); } });
    }
    /* a folded item opens in the layout under the pointer — every folded item in the Hover tier, an aged one in any
       tier ("hover to read, click to open") — and folds back when the pointer leaves; one opened by a click stays */
    _hoverOpen(it) {
      const hoverTier = this.tier() === 'hover'; let moved = false;
      const prev = this._hovEl;
      if (prev && prev !== it && prev.classList.contains('hovopen')) { prev.classList.add('compact'); prev.classList.remove('hovopen'); moved = true; }
      this._hovEl = it || null;
      if (it && it.classList.contains('compact') && it.classList.contains('foldable') && (hoverTier || it.classList.contains('aged'))) { it.classList.remove('compact'); it.classList.add('hovopen'); moved = true; }
      if (moved && this.hasAttribute('stage')) this._placeNow();
    }
    _toggleOpen(key) {
      if (Date.now() - (this._rzT || 0) < 350) return;   // the click that ended a resize
      if (this._open.has(key)) this._open.delete(key); else this._open.add(key);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:open', { bubbles: true, detail: { key, open: this._open.has(key) } })); } catch (e) {}
      if (this._doc) this.render(this._doc);
    }
    _blockOf(key) { const bl = (this._doc && this._doc.blocks) || []; key = String(key); return bl.find(b => b && b.key != null && String(b.key) === key) || (key.startsWith('blk:') ? bl.find(b => b && !b.key && String(b.id) === key.slice(4)) : null) || null; }
    /* how a write names the block: the resolver's key, or — a keyless block of an agent's canvas — its block_id */
    _bidOf(key) { const b = this._blockOf(key); return b && !b.key ? String(b.id) : ''; }
    _ref(key) { const bid = this._bidOf(key); return bid ? { block_id: bid } : { key }; }
    /* every action on the column — the old per-item controls (pin · park · size · remove) and the board's new ones
       (add · open · edit · answer · take · in context) — one dispatcher */
    _act(btn, ev) {
      const act = btn.dataset.act; const it = btn.closest('.it[data-key]'); const key = it ? it.dataset.key : '';
      const focusMid = this.dataset.focusMid || '';
      if (act === 'pin' || act === 'ctx') return this.call(it && it.classList.contains('pinned') ? 'canvas.add' : 'canvas.pin', { key });
      if (act === 'park') return this.call('canvas.park', { key });
      if (act === 'remove') return this.call('canvas.remove', this._ref(key));
      if (act === 'size') { const i = ITEM_SIZES.indexOf(it.dataset.size); delete this._px[key]; return this._setSize(key, ITEM_SIZES[(i + 1) % ITEM_SIZES.length]); }
      if (act === 'up' || act === 'down') {   // a plain canvas keeps the document's order: canvas.move by block id
        const b = this._blockOf(key); if (!b) return; const bl = ((this._doc && this._doc.blocks) || []).filter(Boolean).slice().sort((x, y) => ((x.layout && x.layout.order) || 0) - ((y.layout && y.layout.order) || 0));
        const i = bl.indexOf(b), j = act === 'up' ? i - 1 : i + 1; if (i < 0 || j < 0 || j >= bl.length) return;
        return this.call('canvas.move', { block_id: String(b.id), order: j });
      }
      if (act === 'open') return this._toggleOpen(key);
      if (act === 'hid') { const pop = btn.parentElement && btn.parentElement.querySelector('.hidpop'); if (pop) pop.hidden = !pop.hidden; return; }
      if (act === 'edit') { this._editKey = this._editKey === key ? null : key; this._editFocused = false; if (this._doc) this.render(this._doc); return; }
      if (act === 'cancel') { this._editKey = null; this._editFocused = false; if (this._doc) this.render(this._doc); return; }
      if (act === 'save') {
        const ta = it && it.querySelector('textarea'); const b = this._blockOf(key); if (!ta || !b) return;
        const content = Object.assign({}, b.content || {}); const fld = ta.dataset.field || 'text';
        if (fld === 'ask.question') content.ask = Object.assign({}, content.ask || {}, { question: ta.value }); else content[fld] = ta.value;
        this._editKey = null; this._editFocused = false;
        try { this.dispatchEvent(new CustomEvent('vera:canvas:edited', { bubbles: true, detail: { key, field: ta.dataset.field || 'text' } })); } catch (e) {}
        return this.call('canvas.update', Object.assign(this._ref(key), { content }));
      }
      /* ── the live items' own actions (A16): the terminal, the cell, the panel over the bridge ── */
      if (act === 'tconnect' || act === 'tattach' || act === 'tdetach') {
        const c = this._contentOf(key); if (!c) return;
        if (act === 'tconnect') { const rd = (f) => { const i = it.querySelector('.vc-tconnect [data-f="' + f + '"]'); return i ? String(i.value || '').trim() : ''; }; const hostId = rd('host_id'); if (!hostId) { const i = it.querySelector('.vc-tconnect [data-f="host_id"]'); if (i) i.focus(); return; } Object.assign(c, { host_id: hostId, container: rd('container'), shell: rd('shell'), attached: true }); }
        else if (act === 'tattach') c.attached = true;
        else { c.attached = false; if (this._live && this._live[key]) { try { this._live[key].remove(); } catch (e) {} delete this._live[key]; } }
        this._open.add(key);
        try { this.dispatchEvent(new CustomEvent('vera:canvas:terminal', { bubbles: true, detail: { key, host_id: c.host_id || '', container: c.container || '', attached: !!c.attached } })); } catch (e) {}
        return this.call('canvas.update', Object.assign(this._ref(key), { content: c }));
      }
      /* the terminal's known hosts (defect 32): the list under the connect row; a pick fills the row and connects */
      if (act === 'thosts') { this._hostsOpen = this._hostsOpen || {}; this._hostsOpen[key] = !this._hostsOpen[key]; this._open.add(key); if (this._doc) this.render(this._doc); if (this._hostsOpen[key]) this._hostRows().then(() => { if (this._doc) this.render(this._doc); }); return; }
      if (act === 'hpick') {
        const row = { host_id: String(btn.dataset.host || ''), container: String(btn.dataset.container || ''), shell: String(btn.dataset.shell || ''), n: String(btn.dataset.n || '') }; if (!row.host_id) return;
        if (row.host_id === SBX_PICK) return this._sbxPick(key, it, btn);
        if (!it) return this._hostAdd(row);   // from the add bar: a terminal already on its host
        const c = this._contentOf(key); if (!c) return; Object.assign(c, { host_id: row.host_id, container: row.container, shell: row.shell, attached: true }); if (!c.title || c.title === 'Terminal') c.title = row.n || row.host_id;
        if (this._hostsOpen) delete this._hostsOpen[key]; this._open.add(key);
        try { this.dispatchEvent(new CustomEvent('vera:canvas:terminal', { bubbles: true, detail: { key, host_id: c.host_id, container: c.container || '', attached: true } })); } catch (e) {}
        return this.call('canvas.update', Object.assign(this._ref(key), { content: c }));
      }
      if (act === 'taddid') { const box = btn.closest('.row'); const rdf = (f) => { const i = box && box.querySelector('[data-f="' + f + '"]'); return i ? String(i.value || '').trim() : ''; }; const hid = rdf('hid'); if (!hid) { const i = box && box.querySelector('[data-f="hid"]'); if (i) i.focus(); return; } return this._hostAdd({ host_id: hid, container: rdf('hcont'), shell: '', n: hid }); }
      if (act === 'tblank') { const pop = btn.closest('.addpop'); if (pop) pop.remove(); const k = ADD_KINDS.find(x => x.n === 'terminal'); return k ? this._addSeed(k) : undefined; }
      if (act === 'tsbx') return this._sbxPick(key, it, btn);   // the add bar: a terminal straight onto this session's sandbox
      /* the diagram's actions: Open in the chat (the chat's own pop-out hears vm:popout), copy, source */
      if (act === 'dgopen') { const c = this._contentOf(key) || {}; const code = String(c.mermaid || c.code || c.source || ''); const title = String(c.title || c.caption || 'Diagram');
        const ev2 = new CustomEvent('vera:canvas:diagram', { bubbles: true, composed: true, cancelable: true, detail: { key, code, title } }); this.dispatchEvent(ev2); if (ev2.defaultPrevented) return;
        try { this.dispatchEvent(new CustomEvent('vm:popout', { bubbles: true, composed: true, detail: { code, title } })); } catch (e) {} return; }
      if (act === 'cprev') { this._prevOn = this._prevOn || {}; this._prevOn[key] = !this._prevOn[key]; this._open.add(key); if (this._doc) this.render(this._doc); return; }
      if (act === 'dgcopy') { const c = this._contentOf(key) || {}; try { navigator.clipboard.writeText(String(c.mermaid || c.code || c.source || '')); } catch (e) {} return; }
      if (act === 'dgsrc') { const p = it && it.querySelector('.vc-dgsrc'); if (p) p.hidden = !p.hidden; return; }
      if (act === 'tshare') { const h = it.querySelector('.vc-live[data-ws]'); const ws = h ? h.dataset.ws : ''; try { navigator.clipboard.writeText(location.origin + ws); } catch (e) {} return; }
      if (act === 'nbrun') return this._nbRun(key, it);
      if (act === 'nbopen') {
        const c = this._contentOf(key) || {}; const ev2 = new CustomEvent('vera:canvas:open-notebook', { bubbles: true, cancelable: true, detail: { key, notebook_id: c.notebook_id || '', cell_id: c.cell_id || '' } });
        this.dispatchEvent(ev2); if (ev2.defaultPrevented) return;
        try { window.open((window._veraBase || '') + '/notebook/panel?nb=' + encodeURIComponent(c.notebook_id || '') + '&focus=1', '_blank'); } catch (e) {} return;
      }
      if (act === 'pquery' || act === 'psend') {
        const c = this._contentOf(key) || {}; const id = String(c.panel || c.id || '');
        let name = 'panel.query', args = { session_id: this._sid(), panel: id };
        if (act === 'psend') { const rd = (f) => { const i = it.querySelector('.vc-bridge [data-f="' + f + '"]'); return i ? String(i.value || '') : ''; }; const action = rd('action').trim(); if (!action) return; let payload = {}; try { payload = rd('payload').trim() ? JSON.parse(rd('payload')) : {}; } catch (e) { return this._readout(key, 'payload is not JSON: ' + e.message); } name = 'panel.dispatch'; args = { session_id: this._sid(), panel: id, action, payload }; }
        return this._bridge(key, id, name, args);
      }
      if (act === 'pdispatch') { this._pdOpen = this._pdOpen || {}; this._pdOpen[key] = !this._pdOpen[key]; if (this._doc) this.render(this._doc); return; }
      if (act === 'prefresh') { const el = this._live && this._live[key]; const f = el && el.firstChild; if (f && f.tagName === 'IFRAME') { try { f.src = f.src; } catch (e) {} } return; }
      if (act === 'pstand') { const h = it.querySelector('.vc-live[data-src]'); if (h) { try { window.open(h.dataset.src, '_blank'); } catch (e) {} } return; }
      if (act === 'padd') return this._panelAdd(btn.dataset.pid, btn.dataset.plabel);
      if (act === 'paddid') { const i = btn.parentElement && btn.parentElement.querySelector('[data-f="pid"]'); return this._panelAdd(i ? i.value : '', ''); }
      if (act === 'add') {
        const k = ADD_KINDS.find(x => x.n === btn.dataset.kind); if (!k) return;
        if (k.hosts) return this._hostPick(btn);
        if (k.pick) return this._panelPick(btn);
        if (k.menu) return this._noteMenu(btn, k);
        if (k.sheet) return this._widgetSurface() ? this._widgetPick(btn, k) : this._sheetless(btn, k);
        return this._listed(btn, k);   // no kind adds on a click alone: it says what it adds, then adds it
      }
      if (act === 'nmenu') {
        const m = NOTE_MENU.find(x => x.id === btn.dataset.m); const pop = btn.closest('.addpop'); if (!m) return;
        if (!m.clipboard) { if (pop) pop.remove(); return this._addSeed(Object.assign({}, m, { n: m.id })); }   // the key names the row (note:decision-…)
        const paste = (text) => { const r = fromClipboard(text); if (!r) { if (pop) { const w = pop.querySelector('.what'); if (w) w.textContent = 'the clipboard holds no text — copy something first'; } return; } if (pop) pop.remove(); return this._addSeed({ n: r.n, kind: r.kind, content: r.content }); };
        try { return navigator.clipboard.readText().then(paste, () => { if (pop) { const w = pop.querySelector('.what'); if (w) w.textContent = 'the clipboard could not be read — paste into a blank note instead'; } }); } catch (e) { return paste(''); }
      }
      if (act === 'seedgo') { const k = ADD_KINDS.find(x => x.n === btn.dataset.kind); const pop = btn.closest('.addpop'); if (pop) pop.remove(); return k ? this._addSeed(k) : undefined; }
      if (act === 'answer') {
        const b = this._blockOf(key); const d = decisionOf(b); if (!b || !d) return;
        const ans = String(btn.dataset.ans || ''); const when = new Date().toISOString();
        const content = Object.assign({}, b.content || {});
        if (content.ask && typeof content.ask === 'object') content.ask = Object.assign({}, content.ask, { answer: ans, answered: when }); else { content.answer = ans; content.answered = when; }
        // the answer goes to the run: the host routes it (vera:canvas:answer); the item records it, so the band reads answered
        try { this.dispatchEvent(new CustomEvent('vera:canvas:answer', { bubbles: true, detail: { key, answer: ans, question: d.question, run: d.run } })); } catch (e) {}
        return this.call('canvas.update', Object.assign(this._ref(key), { content }));
      }
      if (act === 'take') {
        const s = (this._suggs || [])[+btn.dataset.i]; if (!s) return;
        try { this.dispatchEvent(new CustomEvent('vera:canvas:suggest', { bubbles: true, detail: { key: s.key, kind: s.kind, taken: s.taken } })); } catch (e) {}
        if (s.taken) return this.call('canvas.add', { key: s.key });
        const args = { kind: s.kind, key: s.key, content: s.content, at: 'now', size: 'm' }; const mid = s.mid || focusMid; if (mid) args.anchor = { turn: mid, mid };
        this._open.add(s.key);
        return this.call('canvas.add', args);
      }
    }

    /* ── the live layer (A16): the items' markup is replaced on every render, the live elements are not ────────────
       #items holds the rendered column; #live (a sibling, never re-rendered) holds the terminal elements and the
       panel frames, each placed over its slot (.vc-live) after every render, scroll and placement — hidden, still
       connected, while its item is folded. ─────────────────────────────────────────────────────────────────────── */
    _layers(body) {
      let items = body.querySelector(':scope > #items'), live = body.querySelector(':scope > #live');
      if (!items || !live) { body.innerHTML = ''; items = document.createElement('div'); items.id = 'items'; live = document.createElement('div'); live.id = 'live'; body.appendChild(items); body.appendChild(live);
        if (!this._liveBound) { this._liveBound = true; body.addEventListener('scroll', () => this._liveLayout()); try { this._liveRO = new ResizeObserver(() => this._liveLayout()); this._liveRO.observe(body); this._liveRO.observe(items); } catch (e) {} } }
      return { items, live };
    }
    _mountLive(body) {
      const L = this._live || (this._live = {}); const { live } = this._layers(body);
      body.querySelectorAll('#items .vc-live[data-live]').forEach((h) => {
        const key = h.dataset.key, kind = h.dataset.live; let el = L[key];
        if (el && el.dataset.kind !== kind) { try { el.remove(); } catch (e) {} el = null; }
        if (!el) {
          // the wrapper is the column's (placed, sized, hidden); the element inside is the estate's own and keeps its styles
          let inner;
          if (kind === 'term') { inner = document.createElement('vera-terminal'); inner.setAttribute('ws', h.dataset.ws || ''); ensureLib('/ui/vera-terminal.js', 'vera-terminal'); }
          else if (kind === 'mermaid') { inner = document.createElement('vera-mermaid'); inner.setAttribute('bare', ''); inner.setAttribute('fill', ''); inner.setAttribute('title', h.dataset.title || 'diagram'); h.textContent = '';
            inner.addEventListener('vm:rendered', () => this._diagramGrew(key, inner)); this._mermaidInto(inner, key); }
          else if (kind === 'widget') { inner = document.createElement('vera-widget'); inner.setAttribute('size', h.dataset.size || 'm'); const rc = this._contentOf(key); if (rc) { inner.record = rc.record || rc; try { inner._recJson = JSON.stringify(rc.record || rc); } catch (e) {} } h.textContent = ''; }
          else if (kind === 'preview') { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key);
          inner.setAttribute('sandbox', 'allow-scripts');   // no network, no cookies, no same-origin: it only draws
          h.textContent = ''; const cc = this._contentOf(key) || {}; inner.srcdoc = previewDoc(h.dataset.lang || cc.lang, cc.code || ''); }
        else { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key); inner.src = h.dataset.src || 'about:blank'; }
          el = document.createElement('div'); el.className = 'lv'; el.dataset.kind = kind; el.dataset.key = key; el.appendChild(inner); L[key] = el; live.appendChild(el);
          try { this.dispatchEvent(new CustomEvent('vera:canvas:live', { bubbles: true, detail: { key, kind, ws: h.dataset.ws || '', src: h.dataset.src || '' } })); } catch (e) {}
        } else if (kind === 'widget') { const inner = el.firstChild, rc = this._contentOf(key); const sz = h.dataset.size || 'm'; if (h.textContent) h.textContent = ''; if (inner && inner.getAttribute('size') !== sz) inner.setAttribute('size', sz); try { const j = JSON.stringify((rc && (rc.record || rc)) || null); if (inner && j && inner._recJson !== j) { inner._recJson = j; inner.record = rc.record || rc; } } catch (e) {}
        } else if (kind === 'mermaid') { if (h.textContent) h.textContent = ''; this._mermaidInto(el.firstChild, key); this._diagramGrew(key, el.firstChild);   // a re-rendered slot is new markup: the drawn diagram's height again
        } else if (kind === 'preview') { if (h.textContent) h.textContent = ''; const f = el.firstChild, cc = this._contentOf(key) || {};
          const doc = previewDoc(h.dataset.lang || cc.lang, cc.code || ''); if (f && f._doc !== doc) { f._doc = doc; f.srcdoc = doc; }
        } else if (kind === 'term' && h.dataset.ws) { const t = el.firstChild; if (t && t.getAttribute('ws') !== h.dataset.ws) { t.setAttribute('ws', h.dataset.ws); try { t.destroy && t.destroy(); t.connect(h.dataset.ws); } catch (e) {} } }
      });
      Object.keys(L).forEach((k) => { if (!body.querySelector('#items .vc-live[data-key="' + k.replace(/"/g, '\\"') + '"]')) { try { L[k].remove(); } catch (e) {} delete L[k]; } });
      // the slots move after a render (placement, fonts, a frame's load): every slot is observed, and a slow tick
      // catches what no observer reports, only while live elements exist
      try { if (this._liveRO) body.querySelectorAll('#items .vc-live[data-live], #items .it').forEach((n) => this._liveRO.observe(n)); } catch (e) {}
      const any = Object.keys(L).length > 0;
      if (any && !this._liveTick) this._liveTick = setInterval(() => this._liveLayout(), 500);
      if (!any && this._liveTick) { clearInterval(this._liveTick); this._liveTick = null; }
      this._liveLayout();
    }
    _liveLayout() {
      const L = this._live; if (!L) return; const body = this.shadowRoot.getElementById('body'); if (!body) return;
      const B = body.getBoundingClientRect();
      Object.keys(L).forEach((k) => {
        const el = L[k]; const h = body.querySelector('#items .vc-live[data-key="' + k.replace(/"/g, '\\"') + '"]');
        if (!h || !h.getClientRects().length) { el.style.display = 'none'; return; }
        const r = h.getBoundingClientRect(); const w = Math.max(0, Math.round(r.width)), ht = Math.max(0, Math.round(r.height));
        el.style.display = ''; el.style.left = Math.round(r.left - B.left + body.scrollLeft) + 'px'; el.style.top = Math.round(r.top - B.top + body.scrollTop) + 'px';
        if (el.style.width !== w + 'px' || el.style.height !== ht + 'px') { el.style.width = w + 'px'; el.style.height = ht + 'px'; if (el.dataset.kind === 'term') { const t = el.firstChild; try { t && t._doFit && t._doFit(); } catch (e) {} } if (el.dataset.kind === 'mermaid') { const m = el.firstChild; try { m && m.fit && m.fit(); } catch (e) {} } }
      });
    }
    /* the diagram rendered: its slot takes the diagram's height at the slot's width (the size's ceiling is the CSS's);
       the live layer follows, and the stage places again — the item grew */
    _diagramGrew(key, inner) {
      const body = this.shadowRoot.getElementById('body'); const h = body && body.querySelector('#items .vc-live[data-key="' + String(key).replace(/"/g, '\\"') + '"]'); if (!h || !inner || typeof inner.naturalHeight !== 'function') return;
      const w = h.getBoundingClientRect().width || 300; const nat = inner.naturalHeight(w); if (!nat) return;
      const dh = Math.max(60, Math.round(nat)) + 'px'; if (h.style.getPropertyValue('--dh') === dh) return;
      h.style.setProperty('--dh', dh); h.dataset.natural = String(Math.round(nat));
      requestAnimationFrame(() => { this._liveLayout(); if (this.hasAttribute('stage')) this._placeNow(); });
    }
    /* the diagram's source into the estate's mermaid element (loaded once from the page); a changed source redraws it;
       the host's own window.mermaid draws when the element cannot be had, the source shows when nothing can */
    _mermaidInto(inner, key) {
      const c = this._contentOf(key) || {}; const code = String(c.mermaid || c.code || c.source || '').trim(); if (!inner || inner._mmCode === code) return; inner._mmCode = code;
      ensureLib('/ui/elements/vera_mermaid.js', 'vera-mermaid').then((ok) => {
        if (!inner.isConnected) return;
        if (typeof inner.render !== 'function' && typeof customElements !== 'undefined' && customElements.get('vera-mermaid')) { try { customElements.upgrade(inner); } catch (e) {} }
        if (typeof inner.render === 'function') { try { inner.render(code); } catch (e) {} return; }
        if (!ok && window.mermaid && typeof window.mermaid.render === 'function') { try { Promise.resolve(window.mermaid.render('vc-mm-' + Math.random().toString(36).slice(2, 8), code)).then((r) => { inner.innerHTML = (r && r.svg) || ''; }).catch(() => { inner.textContent = code; }); } catch (e) { inner.textContent = code; } return; }
        inner.textContent = code;
      });
    }
    /* the bridge, asked for its answer (panel.query · panel.dispatch · ui.panels.open): the same /mcp/call, the reply back */
    async callResult(name, args) {
      const base = (window._veraBase || '').replace(/\/$/, '');
      try { const r = await fetch(base + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, arguments: args || {} }) }); const j = await r.json(); return unwrap(j); }
      catch (e) { return { ok: false, error: String(e && e.message || e) }; }
    }
    /* the bridge asked from an item: the readout on the item, the reply to the host */
    async _bridge(key, id, name, args) {
      this._readout(key, '… ' + name); const r = await this.callResult(name, args);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:panel', { bubbles: true, detail: { key, id, action: name, reply: r } })); } catch (e) {}
      return this._readout(key, r);
    }
    _sid() { return String((this._doc && this._doc.session) || this.getAttribute('session-id') || ''); }
    _contentOf(key) { const b = this._blockOf(key); return b ? Object.assign({}, b.content || {}) : null; }
    _readout(key, obj) { this._pq = this._pq || {}; let text = ''; try { text = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 1); } catch (e) { text = String(obj); } this._pq[key] = { text: String(text).slice(0, 4000) }; if (this._doc) this.render(this._doc); }
    /* the add bar's seed: the kind's own content as an item, yours, beside the turn in view */
    _addSeed(k) {
      const focusMid = this.dataset.focusMid || '';
      const nk = k.kind + ':' + k.n + '-' + Date.now().toString(36);
      const args = { kind: k.kind, key: nk, content: JSON.parse(JSON.stringify(k.content)), at: 'now', size: 'm' };
      // yours, not the turn's: it sits beside the turn in view and relates to no turn — no run is drawn to it
      args.anchor = { origin: 'you', beside: focusMid };
      this._open.add(nk); if (k.edit) { this._editKey = nk; this._editFocused = false; }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:add', { bubbles: true, detail: { key: nk, kind: k.kind, add: k.n } })); } catch (e) {}
      return this.call('canvas.add', args);
    }
    /* the add bar's panel: EVERY registered panel by name (ui.panel.list — the registry the harness draws its tabs from),
       the ones open for this session (ui.panels.open) first and marked, a search, and any id typed */
    async _panelPick(btn) {
      const pop = this._pop(btn, 'canvas.add.panel'); if (!pop) return; const what = `<span class="vc-dim what">${esc(ADD_WHAT.panel)}</span>`;
      pop.innerHTML = what + '<span class="vc-dim">every panel — asking the registry…</span>';
      const rows = await this._panelRows(); if (!pop.isConnected) return;
      pop.innerHTML = what + (rows.length ? panelListHtml(rows) : '<span class="vc-dim">the registry answered no panel — any panel by its id:</span>')
        + '<div class="row"><input class="ti" data-f="pid" placeholder="panel id" spellcheck="false"><button class="ib on" data-act="paddid">Add</button></div>';
      try { const q = pop.querySelector('.pk-q'); if (q) q.focus(); } catch (e) {}
    }
    /* the registry and the open set, asked together; the registry's light list first, the /ui/panels list the harness
       reads when the light list is not there */
    async _panelRows() {
      const [list, open] = await Promise.all([this.callResult('ui.panel.list', {}), this.callResult('ui.panels.open', { session_id: this._sid() })]);
      let L = list && Array.isArray(list.panels) ? list.panels : (Array.isArray(list) ? list : []);
      if (!L.length) { try { const r = await fetch((window._veraBase || '').replace(/\/$/, '') + '/ui/panels', { headers: { Accept: 'application/json' } }); const j = await r.json(); L = Array.isArray(j) ? j : (j && Array.isArray(j.panels) ? j.panels : []); } catch (e) { L = []; } }
      return panelRowsOf(L, open);
    }
    /* the add bar's terminal: the estate's known hosts (the picker), a typed host id, or a blank terminal */
    async _hostPick(btn) {
      const pop = this._pop(btn, 'canvas.add.terminal'); if (!pop) return; const what = `<span class="vc-dim what">${esc(ADD_WHAT.terminal)}</span>`;
      const typed = '<div class="row"><input class="ti" data-f="hid" placeholder="host id — an SSH host of the Exec panel" spellcheck="false"><input class="ti sm" data-f="hcont" placeholder="container" spellcheck="false"><button class="ib on" data-act="taddid">Add</button><button class="ib" data-act="tblank" title="A blank terminal — connect from the item">blank</button><button class="ib" data-act="tsbx" title="A terminal on the container this session runs its commands in">this session\'s sandbox</button></div>';
      pop.innerHTML = what + '<span class="vc-dim">known hosts — asking the estate…</span>' + typed;
      const rows = await this._hostRows(); if (!pop.isConnected) return;
      pop.innerHTML = what + hostListHtml(rows) + typed;
      try { const q = pop.querySelector('.pk-q'); if (q) q.focus(); } catch (e) {}
    }
    /* "this session's sandbox", picked: sandbox.session.terminal wakes (or creates) the container for THIS canvas's
       session and answers with the socket to it. The answer is written into the item - host, container, shell, ws - so
       from here it is an ordinary terminal item: it survives a reload, it draws through the same path as any other, and
       nothing downstream has to know where it came from (Notes/42 defect 64). */
    async _sbxPick(key, it, btn) {
      const sid = this._sid();
      if (!sid) { if (key) this._readout(key, 'this canvas has no session, so it has no sandbox'); return; }
      if (key) this._readout(key, '\u2026 waking this session\'s sandbox');
      let d = null;
      try { d = await this.callResult('sandbox.session.terminal', { session_id: sid, shell: 'bash' }); } catch (e) { d = null; }
      if (!d || !d.ws_path) { const why = (d && (d.error || d.reason)) || 'the sandbox did not answer';
        if (key) this._readout(key, 'the session sandbox could not be opened: ' + why); return; }
      const row = { host_id: String(d.docker_host_id || 'local'), container: String(d.container || ''), shell: String(d.shell || 'bash'), ws: String(d.ws_path), n: 'this session\'s sandbox' };
      if (!it) { const pop = btn && btn.closest('.addpop'); if (pop) pop.remove();
        const k = ADD_KINDS.find((x) => x.n === 'terminal');
        return this._addSeed(Object.assign({}, k, { content: { title: row.n, host_id: row.host_id, container: row.container, shell: row.shell, ws: row.ws, sandbox: true, attached: true } })); }
      const c2 = this._contentOf(key); if (!c2) return;
      Object.assign(c2, { host_id: row.host_id, container: row.container, shell: row.shell, ws: row.ws, sandbox: true, attached: true });
      if (!c2.title || c2.title === 'Terminal') c2.title = row.n;
      if (this._hostsOpen) delete this._hostsOpen[key]; this._open.add(key);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:terminal', { bubbles: true, detail: { key, host_id: c2.host_id, container: c2.container, attached: true, sandbox: true } })); } catch (e) {}
      return this.call('canvas.update', Object.assign(this._ref(key), { content: c2 }));
    }
    /* the known hosts, asked once and kept a while: conn.targets (the SSH hosts of the Exec panel, the running containers
       on every docker host) and conn.list (the saved connections) — the remote subsystem's own enumerations */
    async _hostRows() {
      const now = Date.now(); if (this._hosts && this._hostsAt && now - this._hostsAt < 30000) return this._hosts;
      const [t, s] = await Promise.all([this.callResult('conn.targets', { include_proxmox: false }), this.callResult('conn.list', {})]);
      this._hosts = hostRowsOf(t, s, this._sid()); this._hostsAt = now; return this._hosts;
    }
    /* a terminal already on its host: the pick (or the typed id) lands an attached session item, yours */
    _hostAdd(row) {
      const focusMid = this.dataset.focusMid || ''; const hid = String(row.host_id || '').trim(); if (!hid) return;
      const key = 'session:' + (row.container ? row.container : hid).replace(/[^a-zA-Z0-9_.-]/g, '') + '-' + Date.now().toString(36);
      const content = { title: String(row.n || (row.container ? row.container + ' @ ' + hid : hid)), host_id: hid, container: String(row.container || ''), shell: String(row.shell || ''), attached: true };
      const args = { kind: 'session', key, content, at: 'now', size: 'm', anchor: { origin: 'you', beside: focusMid } };   // picked by hand: beside the turn in view, related to no turn
      this._open.add(key);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:add', { bubbles: true, detail: { key, kind: 'session', add: 'terminal', host_id: hid, container: content.container } })); } catch (e) {}
      try { this.dispatchEvent(new CustomEvent('vera:canvas:terminal', { bubbles: true, detail: { key, host_id: hid, container: content.container, attached: true } })); } catch (e) {}
      return this.call('canvas.add', args);
    }
    /* one popover under the add bar at a time; the same button again closes it */
    _pop(btn, w) {
      const bar = btn.closest('.addbar'); if (!bar) return null; const old = bar.querySelector('.addpop');
      if (old) { const same = old.dataset.for === (btn.dataset.kind || ''); old.remove(); if (same) return null; }
      const pop = document.createElement('div'); pop.className = 'addpop pk'; pop.setAttribute('data-w', w); pop.dataset.for = btn.dataset.kind || ''; bar.appendChild(pop); return pop;
    }
    /* the note's menu (defect 44): a blank note · a checklist · a decision · a link · from the clipboard — each row
       says what it adds */
    _noteMenu(btn, k) {
      const pop = this._pop(btn, 'canvas.add.note'); if (!pop) return;
      pop.innerHTML = `<span class="vc-dim what">${esc(ADD_WHAT[k.n] || '')}</span>` + NOTE_MENU.map((m) => `<button class="pp" data-act="nmenu" data-m="${esc(m.id)}" title="${esc(m.what)}"><b>${esc(m.n)}</b><span>${esc(m.what)}</span></button>`).join('');
    }
    /* a kind with one option (its seed): listed, added on the click that reads it — never on the add bar's click alone */
    _listed(btn, k, note) {
      const pop = this._pop(btn, 'canvas.add.' + k.n); if (!pop) return;
      pop.innerHTML = `<span class="vc-dim what">${esc(ADD_WHAT[k.n] || 'adds a ' + k.n)}</span>` + (note ? `<span class="vc-dim">${esc(note)}</span>` : '') + `<button class="pp" data-act="seedgo" data-kind="${esc(k.n)}"><b>${esc(seedName(k))}</b><span>${esc(seedWhat(k))}</span></button>`;
    }
    _sheetless(btn, k) { return this._listed(btn, k, 'the WidgetConfig sheet is not on this page — the form is set from the item afterwards'); }
    /* a size: the resolver's canvas.size for a keyed item; a keyless block keeps it in its meta */
    _setSize(key, size) { const bid = this._bidOf(key); return bid ? this.call('canvas.update', { block_id: bid, meta: { size } }) : this.call('canvas.size', { key, size }); }
    /* the add bar's widget: the WidgetConfig sheet (widget_element.js — this window's, or the host's when the column is
       embedded); the record it resolves is the item's content, keyed by its form, yours (beside the turn, no run) */
    _widgetSurface() { try { if (window.VeraWidgetConfig && window.VeraWidgetConfig.open) return window.VeraWidgetConfig; } catch (e) {} try { const p = window.parent; if (p && p !== window && p.VeraWidgetConfig && p.VeraWidgetConfig.open) return p.VeraWidgetConfig; } catch (e) {} return null; }
    async _widgetPick(btn, k) {
      const S = this._widgetSurface(); if (!S) return; const focusMid = this.dataset.focusMid || '';
      // the chart is the sheet on the series forms (defect 44): the shape filter is the sheet's own
      let rec = null; try { rec = await S.open({ mode: 'add', into: 'canvas', anchor: btn, templates: true, title: k.shape === 'series' ? 'Add a chart — a series form' : 'Add a widget to the canvas', shape: k.shape || '', record: k.shape === 'series' ? { form: 'trace', title: 'Chart' } : undefined, sizes: ['s', 'm', 'l', 'xl'] }); } catch (e) { rec = null; }
      if (!rec) return;
      const form = String(rec.form || (rec.draw && rec.draw.form) || 'widget'); const size = String((rec.frame && rec.frame.size) || (rec.draw && rec.draw.size) || 'm');
      const nk = 'widget:' + form.replace(/[^a-zA-Z0-9_-]/g, '') + '-' + Date.now().toString(36);
      // the record as the item's content: the form and the size where the column's renderers read them too
      const content = Object.assign({}, rec, { widget: form, form, title: rec.title || form, draw: Object.assign({}, rec.draw || {}, { form, size }), record: rec });
      const args = { kind: 'widget', key: nk, content, at: 'now', size, anchor: { origin: 'you', beside: focusMid } };
      this._open.add(nk);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:add', { bubbles: true, detail: { key: nk, kind: 'widget', add: k.n, form } })); } catch (e) {}
      return this.call('canvas.add', args);
    }
    _panelAdd(id, label) {
      id = String(id || '').trim(); if (!id) return; const key = 'panel:' + id; const focusMid = this.dataset.focusMid || '';
      const args = { kind: 'panel', key, content: { panel: id, title: label || id }, at: 'now', size: 'l' }; args.anchor = { origin: 'you', beside: focusMid };   // picked by hand: beside the turn in view, related to no turn
      this._open.add(key);
      // the item first, then the host is asked for the panel's page (its registered page, alias or route) — the
      // update it answers with needs the item to exist
      return this.call('canvas.add', args).then(() => { try { this.dispatchEvent(new CustomEvent('vera:canvas:panel-src', { bubbles: true, detail: { key, id, content: args.content } })); } catch (e) {} });
    }
    /* the cell runs through the notebook's own exec (the SSE the notebook page uses); the output lands on the item
       and on the cell */
    async _nbRun(key, it) {
      const c = this._contentOf(key); if (!c) return; const api = (this.getAttribute('nb-api') || '').replace(/\/$/, '');
      const out = it && it.querySelector('.vc-nbout'); const paint = (t) => { if (out) { out.hidden = false; out.querySelector('code').textContent = t; } };
      const src = String(c.content || c.source || ''); const cmd = langRunCmd(c.lang, src); let text = '';
      paint('$ running…'); try { this.dispatchEvent(new CustomEvent('vera:canvas:cell', { bubbles: true, detail: { key, state: 'running' } })); } catch (e) {}
      try {
        const r = await fetch(api + '/ide-api/exec/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd, cwd: (c.props && c.props.cwd) || '', session_id: 'notebook:' + (c.notebook_id || '') }) });
        if (!r.body || !/text\/event-stream/.test(r.headers.get('content-type') || '')) { text = await r.text(); }
        else { const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
          while (true) { const { value, done } = await rd.read(); if (done) break; buf += dec.decode(value, { stream: true }); let i;
            while ((i = buf.indexOf('\n\n')) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2); let ev = 'message'; const data = [];
              chunk.split('\n').forEach((l) => { if (l.startsWith('event:')) ev = l.slice(6).trim(); else if (l.startsWith('data:')) data.push(l.slice(5).replace(/^ /, '')); });
              const d = data.join('\n'); if (ev === 'pid') continue; if (ev === 'exit' || ev === 'done') { text += (text && !text.endsWith('\n') ? '\n' : '') + '[' + ev + (d ? ' ' + d : '') + ']'; } else text += d + '\n'; paint(text); } } }
      } catch (e) { text += '\n[error ' + String(e && e.message || e) + ']'; }
      paint(text || '(no output)');
      try { if (c.notebook_id && c.cell_id) await fetch(api + '/api/notebooks/' + encodeURIComponent(c.notebook_id) + '/cells/' + encodeURIComponent(c.cell_id), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ generated: text }) }); } catch (e) {}
      try { this.dispatchEvent(new CustomEvent('vera:canvas:cell', { bubbles: true, detail: { key, state: 'done', output: text } })); } catch (e) {}
      return this.call('canvas.update', { key, content: Object.assign(c, { generated: text }) });
    }
    /* ── the rail (the Canvas panel's left column, defect 43): every canvas (canvas.list), the session's first and
       marked (canvas.session.resolve), + New (title · mode · topic → canvas.create), a delete behind a confirm
       (canvas.delete); a row switches the canvas-id. The old panel page's sidebar, as part of the element. ── */
    _railMount() {
      const r = this.shadowRoot.getElementById('rail'); if (!r) return; r.hidden = false;
      if (!r.childElementCount) {
        r.innerHTML = `<h2>Canvases <button class="ib on" data-ract="new" title="A new canvas — its title, mode and topic">+ New</button></h2>
          <form class="nf" data-w="canvas.create" hidden><input class="ti" data-f="title" placeholder="title" spellcheck="false">
            <span class="seg" data-f="mode"><button type="button" class="on" data-mode="static" title="a working area">static</button><button type="button" data-mode="dynamic" title="tracks a live topic — agents fill it in">dynamic</button></span>
            <input class="ti" data-f="topic" placeholder="topic it tracks" spellcheck="false" hidden>
            <span class="row"><button type="submit" class="ib on">Create</button><button type="button" class="ib" data-ract="newx">Cancel</button></span></form>
          <div class="list" id="raillist"><span class="vc-dim">asking…</span></div>`;
        r.addEventListener('click', (ev) => this._railAct(ev));
        r.addEventListener('submit', (ev) => { ev.preventDefault(); this._railCreate(); });
      }
      this._railRefresh();
      if (!this._railTimer) this._railTimer = setInterval(() => this._railRefresh(), 10000);
    }
    async _railRefresh() {
      const list = this.shadowRoot.getElementById('raillist'); if (!list) return;
      const sid = this.getAttribute('session-id') || '';
      const [L, S] = await Promise.all([this.callResult('canvas.list', { limit: 40 }), sid ? this.callResult('canvas.session.resolve', { session_id: sid }) : Promise.resolve(null)]);
      const rows = L && Array.isArray(L.canvases) ? L.canvases.slice() : [];
      const sessId = S && S.id ? String(S.id) : '';
      if (sessId && !rows.some((c) => c.id === sessId)) rows.unshift({ id: sessId, title: S.title || 'Session canvas', mode: 'session', blocks: S.count || 0 });
      this._rail = railRows(rows, sessId);
      this._railDraw();
    }
    _railDraw() {
      const list = this.shadowRoot.getElementById('raillist'); if (!list) return; const cur = this.canvasId; const rows = this._rail || [];
      list.innerHTML = rows.length ? rows.map((c) => this._railDel === c.id
          ? `<div class="cv confirm"><b>delete “${esc(c.title || 'Untitled')}”?</b><span><button class="ib" data-ract="delyes" data-cid="${esc(c.id)}">yes, delete</button><button class="ib" data-ract="delno">no</button></span></div>`
          : `<button class="cv${c.id === cur ? ' on' : ''}" data-ract="show" data-cid="${esc(c.id)}" title="${esc(c.id)}"><b>${esc(c.title || 'Untitled')}</b><span><em class="mode-${esc(c.other ? 'session' : c.session ? 'session' : c.mode || 'static')}">${esc(c.session ? 'this session' : c.other ? 'session' : c.mode || 'static')}</em>${c.other ? '<i class="sid" title="' + esc(c.id) + '">' + esc(c.sid) + '</i>' : ''}${c.blocks != null ? '<i>' + esc(c.blocks) + ' blk</i>' : ''}${c.updated ? '<i>' + esc(hhmm(c.updated)) + '</i>' : ''}${c.session ? '' : '<i class="x" data-ract="del" data-cid="' + esc(c.id) + '" title="Delete this canvas">✕</i>'}</span></button>`).join('')
        : '<span class="vc-dim">no canvas yet — + New makes one; an agent\'s canvas.create lands here too</span>';
    }
    _railMark() { const list = this.shadowRoot.getElementById('raillist'); if (!list) return; const cur = this.canvasId; list.querySelectorAll('.cv[data-cid]').forEach((b) => b.classList.toggle('on', b.dataset.cid === cur)); }
    _railAct(ev) {
      const form = this.shadowRoot.querySelector('.rail .nf');
      const seg = ev.target.closest && ev.target.closest('.nf [data-mode]');
      if (seg && form) { ev.stopPropagation(); form.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('on', b === seg)); const tp = form.querySelector('[data-f="topic"]'); if (tp) { tp.hidden = seg.dataset.mode !== 'dynamic'; if (!tp.hidden) { try { tp.focus(); } catch (e) {} } } return; }
      const t = ev.target.closest && ev.target.closest('[data-ract]'); if (!t) return; const a = t.dataset.ract; ev.stopPropagation();
      if (a === 'new' || a === 'newx') { if (form) { form.hidden = a === 'newx' ? true : !form.hidden; if (!form.hidden) { try { form.querySelector('[data-f="title"]').focus(); } catch (e) {} } } return; }
      if (a === 'show') { this._switch(t.dataset.cid); return; }
      if (a === 'del') { this._railDel = t.dataset.cid; this._railDraw(); return; }
      if (a === 'delno') { this._railDel = null; this._railDraw(); return; }
      if (a === 'delyes') { const id = t.dataset.cid; this._railDel = null; this.callResult('canvas.delete', { id }).then(() => { try { this.dispatchEvent(new CustomEvent('vera:canvas:deleted', { bubbles: true, detail: { id } })); } catch (e) {} if (id === this.canvasId) { const next = (this._rail || []).find((c) => c.id !== id); if (next) this._switch(next.id); else { this.removeAttribute('canvas-id'); this._rev = null; this.refresh(); } } return this._railRefresh(); }); return; }
    }
    async _railCreate() {
      const form = this.shadowRoot.querySelector('.rail .nf'); if (!form) return;
      const rd = (f) => { const i = form.querySelector('[data-f="' + f + '"]'); return i ? String(i.value || '').trim() : ''; }; const modeB = form.querySelector('[data-mode].on'); const mode = modeB ? modeB.dataset.mode : 'static';
      const title = rd('title') || 'Untitled canvas'; const topic = mode === 'dynamic' ? rd('topic') : '';
      const r = await this.callResult('canvas.create', { title, mode, topic }); if (!r || !r.id) return;
      form.hidden = true; try { form.querySelector('[data-f="title"]').value = ''; form.querySelector('[data-f="topic"]').value = ''; } catch (e) {}
      try { this.dispatchEvent(new CustomEvent('vera:canvas:created', { bubbles: true, detail: { id: r.id, title, mode, topic } })); } catch (e) {}
      this._switch(r.id); return this._railRefresh();
    }
    _switch(id) { id = String(id || ''); if (!id) return; this._open = new Set(); this._px = {}; this._editKey = null; this.setAttribute('canvas-id', id); this._railMark(); try { this.dispatchEvent(new CustomEvent('vera:canvas:switch', { bubbles: true, detail: { id } })); } catch (e) {} }
    /* Every per-item action is the capability the chat and the model use — one implementation — through the
       same /mcp/call the chat uses; the element only asks for a repaint afterwards. */
    async call(name, args) {
      const base = (window._veraBase || '').replace(/\/$/, '');
      try {
        await fetch(base + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, arguments: Object.assign({ id: this.canvasId }, args || {}) }) });
      } catch (e) { /* the poll shows whatever state the backend has */ }
      this._rev = null;
      this.refresh();
    }
  }

  const api = { place, checkRoutes, decisionOf, suggestionsOf, nowText, sizeOfHeight, turnOrder, isAged, foldOf, ADD_KINDS, NOTE_MENU, ADD_WHAT, fromClipboard, blockTitle, railRows, foldOf, ITEM_SIZES, KIND_GLYPH, BLOCK, langRunCmd, unwrap, hostRowsOf, panelRowsOf, pickerHtml, version: 6 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VeraCanvas = Object.assign(root.VeraCanvas || {}, api);
  if (typeof customElements !== 'undefined' && !customElements.get('vera-canvas')) {
    customElements.define('vera-canvas', VeraCanvas);
  }
})();

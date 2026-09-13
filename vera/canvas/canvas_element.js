/* ── <vera-canvas> ─────────────────────────────────────────────────────────────
 * A live canvas, embeddable anywhere — most importantly INSIDE A CHAT MESSAGE.
 *
 *   <vera-canvas canvas-id="cv_abc123"></vera-canvas>
 *   <vera-canvas canvas-id="cv_abc123" compact rows="6"></vera-canvas>
 *
 * The Canvas TAB is the full editor; this is the read-and-watch view that sits
 * in the conversation, so a canvas an agent is filling in updates in place
 * while you talk about it. It polls /canvas/get and repaints only when the
 * document's revision actually changes.
 *
 * On a SESSION canvas (the chat's canvas column) it is the Canvas board's column: the add bar at the top,
 * the NOW band (what this turn is waiting on — a decision with its answers, and what Vera can also do),
 * the items level with their turns, each a card that folds to its header line in Hover and Zen and opens
 * IN PLACE on a click (the column makes room), with a corner grip that sizes it and an edit rail beneath.
 *
 * Deliberately dependency-free: chat already carries enough weight, and a CDN
 * import inside a chat bubble is a failure waiting to happen. Markdown is a
 * small subset renderer; mermaid diagrams degrade to their source unless the
 * host page already provides window.mermaid.
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

    code: c => `<div class="vc-codewrap">
        <div class="vc-codehead">${esc(c.filename || c.lang || 'code')}</div>
        <pre class="vc-pre"><code>${esc(c.code || '')}</code></pre></div>`,

    diagram: c => {
      const src = c.mermaid || '';
      // Render properly only if the HOST page already provides mermaid; never
      // pull one in from a CDN just to draw inside a chat bubble.
      const body = window.mermaid
        ? `<pre class="mermaid">${esc(src)}</pre>`
        : `<pre class="vc-pre vc-dim"><code>${esc(src)}</code></pre>`;
      return body + (c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : '');
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
      const rec = c && c.draw ? c : null;
      const form = rec ? String(rec.form || rec.draw.form || '') : '';
      // a record with a form is the live element (it reads its source itself, the sample face until it has one),
      // in the column's live layer over this slot — never re-created by a render
      if (rec && form && key && typeof customElements !== 'undefined' && customElements.get('vera-widget')) {
        const src = typeof rec.source === 'string' ? rec.source : ((rec.source && (rec.source.origin || rec.source.from)) || (rec.reads && rec.reads.cap) || '');
        return `<div class="vc-wid" data-w="canvas.widget"><div class="vc-live" data-live="widget" data-key="${esc(key)}" data-size="${esc(size || rec.draw.size || 'm')}"><span class="vc-dim">${esc(form)}…</span></div><div class="vc-cap mono">${esc(form)}${src ? ' · ' + esc(src) : ' · sample'}</div></div>`;
      }
      if (rec && window.VeraWidget && typeof window.VeraWidget.draw === 'function') {
        try {
          const out = window.VeraWidget.draw(form, rec.data, size || rec.draw.size || 'm');
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
      const head = `<div class="vc-th"><i class="dot${attached ? (live ? ' on' : ' wait') : ''}"></i><b>${esc(shown ? (container ? shown + ' / ' + container : shown) : 'no host yet')}</b><span class="mono">${esc(shell || (container ? 'sh' : 'login shell'))}${c.command ? ' · ' + esc(c.command) : ''}</span><span class="sp"></span>`
        + (attached ? '<button class="ib" data-act="tdetach" title="Detach — the item keeps its host; Attach opens a new shell">detach</button>' : (ws ? '<button class="ib on" data-act="tattach" title="Open a shell on this host">attach</button>' : ''))
        + (ws ? '<button class="ib" data-act="tshare" title="Copy the terminal\'s address">share</button>' : '') + '</div>';
      const conn = attached ? `<div class="vc-live" data-live="term" data-key="${esc(key)}" data-ws="${esc(ws)}"><span class="vc-dim">connecting…</span></div>`
        : `<div class="vc-tconnect" data-w="canvas.terminal.connect"><input class="ti" data-f="host_id" placeholder="host id — an SSH host of the Exec panel" value="${esc(shown)}" spellcheck="false"><input class="ti" data-f="container" placeholder="container (docker) — optional" value="${esc(container)}" spellcheck="false"><input class="ti sm" data-f="shell" placeholder="shell" value="${esc(shell)}" spellcheck="false"><button class="ib on" data-act="tconnect">Connect</button></div>`;
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
  .addbar{position:sticky;top:0;z-index:4;display:flex;align-items:center;gap:5px;flex-wrap:wrap;
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
  .it.dim{opacity:.5;transition:opacity .15s}.it.dim:hover{opacity:1}.it.out{display:none}
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
  :host([stage]) .band.now>.band-h{position:sticky;top:34px;z-index:3;background:var(--bg1,#15181d);margin:0;padding:3px 0}
  :host([stage]) .band.parked{position:sticky;bottom:0;z-index:3;background:var(--bg1,#15181d)}
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
  .vc-rec b{font-size:11px}`;

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
  /* the add bar: "+ note · terminal · panel · widget · chart" — each a real block type with its seed content; every
     one goes through canvas.add, the resolver's path, like anything an agent puts on the canvas */
  const ADD_KINDS = [
    { n: 'note', ik: '✎', kind: 'note', content: { title: 'Note', text: '' }, edit: true },
    { n: 'terminal', ik: '>_', kind: 'session', content: { title: 'Terminal', host_id: '', container: '', shell: '' } },
    { n: 'panel', ik: '▤', kind: 'panel', content: { panel: '', title: 'Panel' }, pick: true },
    { n: 'widget', ik: 'WG', kind: 'widget', content: { widget: '', title: 'Widget' }, sheet: true },   // the WidgetConfig sheet picks the record
    { n: 'chart', ik: 'CH', kind: 'widget', content: { name: 'chart', title: 'Chart', draw: { form: 'trace', size: 'm' }, data: [], source: { origin: 'you' } } },
  ];
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
     items (the decision, the suggestions) and a hovered one never fold */
  function foldOf(o) { o = o || {}; if (o.now || o.open || o.hovered) return false; return !!(o.aged || (o.tier && o.tier !== 'full')); }
  const textFieldOf = t => t === 'markdown' ? 'md' : t === 'code' ? 'code' : t === 'html' ? 'html' : 'text';
  const EDITABLE = ['note', 'markdown', 'code', 'html'];

  class VeraCanvas extends (typeof HTMLElement !== 'undefined' ? HTMLElement : class {}) {
    static get observedAttributes() { return ['canvas-id', 'rows', 'compact', 'columns']; }

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
        <div class="wrap">
          <div class="head">
            <span class="live"></span>
            <span class="title" id="title">Canvas</span>
            <span class="pill" id="count"></span>
            <a class="open" id="open" target="_blank" rel="noopener">open ↗</a>
          </div>
          <div class="body" id="body"><div class="empty">Loading…</div></div>
        </div>`;
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
      if (this._liveTick) { clearInterval(this._liveTick); this._liveTick = null; }
    }

    attributeChangedCallback(name) {
      if (name === 'canvas-id' && this.shadowRoot.childElementCount) {
        this._rev = null;
        this.refresh();
      }
      if (name === 'columns' && this._doc) this.render(this._doc);
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
      if (!id) { body.innerHTML = '<div class="err">No canvas-id given.</div>'; return; }
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
      if (doc.mode === 'session' || blocks.some(b => b && b.key)) { this.renderSession(doc, blocks, body); return; }
      if (!blocks.length) {
        body.innerHTML = '<div class="empty">This canvas is empty — blocks appear here as they are added.</div>';
        return;
      }
      body.innerHTML = blocks.map(b => {
        const fn = BLOCK[b.type] || BLOCK.note;
        let inner;
        try { inner = fn(b.content || {}); }
        catch (e) { inner = `<div class="err">Could not render a ${esc(b.type)} block.</div>`; }
        return `<div class="blk" data-type="${esc(b.type)}">${inner}</div>`;
      }).join('');

      if (window.mermaid && body.querySelector('.mermaid')) {
        try { window.mermaid.run({ nodes: body.querySelectorAll('.mermaid') }); }
        catch (e) { /* leave the source visible */ }
      }
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
      const keyed = blocks.filter(b => b && b.key);
      const plain = blocks.filter(b => !(b && b.key));
      const by = st => keyed.filter(b => (b.state || 'now') === st);
      const newest = arr => arr.slice().sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
      const pinned = by('pinned'), now = newest(by('now')), parked = by('parked'), hidden = by('hidden');
      const head = this.shadowRoot.getElementById('count');
      if (head) head.textContent = now.length + ' now · ' + pinned.length + ' pinned · ' + parked.length + ' parked';
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
      const nowTxt = nowText(now, decision, suggs, inFocusN);
      const titleOf = b => { const c = b.content || {}; return c.title || c.name || c.goal || c.filename || c.caption || (c.widget) || String(b.key).split(':').slice(1).join(':') || b.type; };
      const askHtml = d => `<div class="askb" data-w="canvas.decision">
            <span class="why">surfaced because <b>${esc(d.why)}</b></span>
            <span class="q">${esc(d.question)}</span>
            <div class="opts">${d.options.map(o => `<button data-act="answer" data-ans="${esc(o.v)}" class="${d.answer === o.v ? 'on' : ''}" title="Answer — it goes to the run">${esc(o.n)}</button>`).join('')}${d.answer ? '' : '<span class="or">or type below — it goes to the run, not into a new turn</span>'}</div>
            <span class="st">${d.answer ? 'answered' + (hhmm(d.answered) ? ' ' + hhmm(d.answered) : '') + ' · "' + esc(d.answer) + '" sent to the run' : 'waiting' + (hhmm(d.since) ? ' since ' + hhmm(d.since) : '') + ' · the composer answers this too'}</span></div>`;
      const editHtml = b => { const c = b.content || {}; const f = textFieldOf(b.type);
        return `<div class="edit" data-w="canvas.update"><textarea class="ta${b.type === 'code' || b.type === 'html' ? ' code' : ''}" data-field="${f}" spellcheck="false">${esc(c[f] || '')}</textarea>
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
        const title = c.title || c.name || c.goal || c.filename || c.caption || (c.widget) || String(b.key).split(':').slice(1).join(':') || b.type;
        const size = ITEM_SIZES.includes(b.size) ? b.size : 'm';
        const a = b.anchor && typeof b.anchor === 'object' ? b.anchor : null; const mid = a ? String(a.turn || a.mid || '') : '';
        // an item added by hand: yours, level with the turn it was added beside, related to no turn (no run, never aged)
        const beside = a && !mid ? String(a.beside || '') : ''; const yours = !!(a && a.origin === 'you' && !mid);
        const open = this._open.has(key) || editing;
        const aged = isAged(mid, focusMid, order) && !(F && F.has(key)) && b.state !== 'pinned';
        const hovered = this._hovKey === key && (tier === 'hover' || aged);
        const wouldFold = foldOf({ tier, aged, open, now: isNow });        // a header line, until opened
        const compact = wouldFold && !hovered;
        const px = this._px[key];
        const editable = EDITABLE.includes(b.type);
        const cls = 'it ' + esc(b.state || 'now') + fcls + (wouldFold ? ' foldable' : '') + (compact ? ' compact' : '') + (wouldFold && hovered ? ' hovopen' : '') + (open ? ' openin' : '') + (aged ? ' aged' : '') + (dec ? ' now' : '') + (isNow ? ' waiting' : '') + (px ? ' sized' : '');
        return `<div class="${cls}" data-key="${esc(b.key)}" data-size="${size}" data-type="${esc(b.type)}"${mid ? ' data-mid="' + esc(mid) + '"' : ''}${beside ? ' data-beside="' + esc(beside) + '"' : ''}${scoreTxt ? ' data-score="' + esc(scoreTxt) + '"' : ''}${px && !compact ? ' style="height:' + Math.round(px) + 'px"' : ''}>
          <div class="it-hd"><span class="ic vc-badge" data-kind="${esc(b.type)}" title="${esc(b.type)}">${esc(glyphOf(b.type))}</span><span class="t" title="${esc(title)}">${esc(title)}</span>${scoreTxt ? '<span class="sc" title="' + esc('relevance ' + scoreTxt + (why ? ' — ' + why : '')) + '">' + esc(scoreTxt) + '</span>' : ''}
            ${mid ? '<span class="src" title="the turn using it">' + esc(mid) + '</span>' : yours ? '<span class="src" title="added by you — it relates to no turn">you</span>' : ''}<span class="k">${esc(b.key)}</span>
            <span class="xp" data-act="open" title="${open ? 'Fold it back' : 'Open in place — the column makes room'}">${open ? '⤡' : '⤢'}</span></div>
          <div class="it-bd">${inner}</div>
          <div class="it-ft" data-w="canvas.item.rail"><span class="it-a">
              ${editable ? `<button data-act="edit" class="${editing ? 'on' : ''}" title="Edit its text — saved through canvas.update">${editing ? 'Editing' : 'Edit'}</button>` : ''}
              <button data-act="pin" class="${b.state === 'pinned' ? 'on' : ''}" title="${b.state === 'pinned' ? 'Unpin — back into the flow' : 'Pin above the flow'}">${b.state === 'pinned' ? 'unpin' : 'pin'}</button>
              <button data-act="park" title="Park it below the flow">park</button>
              <button data-act="size" title="Size: ${size} — click for the next">${size}</button>
              <button data-act="remove" title="Take it off the canvas">drop</button>
              <button data-act="ctx" class="ctx ${inF && (F || b.state === 'pinned') ? 'on' : ''}" title="${b.state === 'pinned' ? 'Pinned — always in context; click to release it' : inF && F ? 'In focus now — pin it to keep it in context' : 'Put it in context — pins it, so it never leaves focus'}">${inF && (F || b.state === 'pinned') ? '✓ In context' : 'In context'}</button>
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
      html += `<div class="band now"><div class="band-h"><span class="nowbar ${decision && !decision.answer ? 'wait' : 'ok'}" data-w="canvas.now" title="What this turn is waiting on"><i></i><b>NOW</b> ${esc(nowTxt)}</span></div>` +
        (nowCards ? (stage ? '<div class="stage" id="stage">' + nowCards + '</div>' : nowCards) : '<div class="empty">Nothing in the NOW band — nothing is waiting on you; items land here as the conversation uses them.</div>') + '</div>';
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
        if (size !== r.el.dataset.size) this.call('canvas.size', { key, size }); else if (this.hasAttribute('stage')) this._placeNow(); });
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
    _blockOf(key) { const bl = (this._doc && this._doc.blocks) || []; return bl.find(b => b && String(b.key) === String(key)) || null; }
    /* every action on the column — the old per-item controls (pin · park · size · remove) and the board's new ones
       (add · open · edit · answer · take · in context) — one dispatcher */
    _act(btn, ev) {
      const act = btn.dataset.act; const it = btn.closest('.it[data-key]'); const key = it ? it.dataset.key : '';
      const focusMid = this.dataset.focusMid || '';
      if (act === 'pin' || act === 'ctx') return this.call(it && it.classList.contains('pinned') ? 'canvas.add' : 'canvas.pin', { key });
      if (act === 'park') return this.call('canvas.park', { key });
      if (act === 'remove') return this.call('canvas.remove', { key });
      if (act === 'size') { const i = ITEM_SIZES.indexOf(it.dataset.size); delete this._px[key]; return this.call('canvas.size', { key, size: ITEM_SIZES[(i + 1) % ITEM_SIZES.length] }); }
      if (act === 'open') return this._toggleOpen(key);
      if (act === 'hid') { const pop = btn.parentElement && btn.parentElement.querySelector('.hidpop'); if (pop) pop.hidden = !pop.hidden; return; }
      if (act === 'edit') { this._editKey = this._editKey === key ? null : key; this._editFocused = false; if (this._doc) this.render(this._doc); return; }
      if (act === 'cancel') { this._editKey = null; this._editFocused = false; if (this._doc) this.render(this._doc); return; }
      if (act === 'save') {
        const ta = it && it.querySelector('textarea'); const b = this._blockOf(key); if (!ta || !b) return;
        const content = Object.assign({}, b.content || {}); content[ta.dataset.field || 'text'] = ta.value;
        this._editKey = null; this._editFocused = false;
        try { this.dispatchEvent(new CustomEvent('vera:canvas:edited', { bubbles: true, detail: { key, field: ta.dataset.field || 'text' } })); } catch (e) {}
        return this.call('canvas.update', { key, content });
      }
      /* ── the live items' own actions (A16): the terminal, the cell, the panel over the bridge ── */
      if (act === 'tconnect' || act === 'tattach' || act === 'tdetach') {
        const c = this._contentOf(key); if (!c) return;
        if (act === 'tconnect') { const rd = (f) => { const i = it.querySelector('.vc-tconnect [data-f="' + f + '"]'); return i ? String(i.value || '').trim() : ''; }; const hostId = rd('host_id'); if (!hostId) { const i = it.querySelector('.vc-tconnect [data-f="host_id"]'); if (i) i.focus(); return; } Object.assign(c, { host_id: hostId, container: rd('container'), shell: rd('shell'), attached: true }); }
        else if (act === 'tattach') c.attached = true;
        else { c.attached = false; if (this._live && this._live[key]) { try { this._live[key].remove(); } catch (e) {} delete this._live[key]; } }
        this._open.add(key);
        try { this.dispatchEvent(new CustomEvent('vera:canvas:terminal', { bubbles: true, detail: { key, host_id: c.host_id || '', container: c.container || '', attached: !!c.attached } })); } catch (e) {}
        return this.call('canvas.update', { key, content: c });
      }
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
        if (k.pick) return this._panelPick(btn);
        if (k.sheet && this._widgetSurface()) return this._widgetPick(btn, k);
        const nk = k.kind + ':' + k.n + '-' + Date.now().toString(36);
        const args = { kind: k.kind, key: nk, content: JSON.parse(JSON.stringify(k.content)), at: 'now', size: 'm' };
        // yours, not the turn's: it sits beside the turn in view and relates to no turn — no run is drawn to it
        args.anchor = { origin: 'you', beside: focusMid };
        this._open.add(nk); if (k.edit) { this._editKey = nk; this._editFocused = false; }
        try { this.dispatchEvent(new CustomEvent('vera:canvas:add', { bubbles: true, detail: { key: nk, kind: k.kind, add: k.n } })); } catch (e) {}
        return this.call('canvas.add', args);
      }
      if (act === 'answer') {
        const b = this._blockOf(key); const d = decisionOf(b); if (!b || !d) return;
        const ans = String(btn.dataset.ans || ''); const when = new Date().toISOString();
        const content = Object.assign({}, b.content || {});
        if (content.ask && typeof content.ask === 'object') content.ask = Object.assign({}, content.ask, { answer: ans, answered: when }); else { content.answer = ans; content.answered = when; }
        // the answer goes to the run: the host routes it (vera:canvas:answer); the item records it, so the band reads answered
        try { this.dispatchEvent(new CustomEvent('vera:canvas:answer', { bubbles: true, detail: { key, answer: ans, question: d.question, run: d.run } })); } catch (e) {}
        return this.call('canvas.update', { key, content });
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
          else if (kind === 'widget') { inner = document.createElement('vera-widget'); inner.setAttribute('size', h.dataset.size || 'm'); const rc = this._contentOf(key); if (rc) { inner.record = rc.record || rc; try { inner._recJson = JSON.stringify(rc.record || rc); } catch (e) {} } h.textContent = ''; }
          else { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key); inner.src = h.dataset.src || 'about:blank'; }
          el = document.createElement('div'); el.className = 'lv'; el.dataset.kind = kind; el.dataset.key = key; el.appendChild(inner); L[key] = el; live.appendChild(el);
          try { this.dispatchEvent(new CustomEvent('vera:canvas:live', { bubbles: true, detail: { key, kind, ws: h.dataset.ws || '', src: h.dataset.src || '' } })); } catch (e) {}
        } else if (kind === 'widget') { const inner = el.firstChild, rc = this._contentOf(key); const sz = h.dataset.size || 'm'; if (h.textContent) h.textContent = ''; if (inner && inner.getAttribute('size') !== sz) inner.setAttribute('size', sz); try { const j = JSON.stringify((rc && (rc.record || rc)) || null); if (inner && j && inner._recJson !== j) { inner._recJson = j; inner.record = rc.record || rc; } } catch (e) {}
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
        if (el.style.width !== w + 'px' || el.style.height !== ht + 'px') { el.style.width = w + 'px'; el.style.height = ht + 'px'; if (el.dataset.kind === 'term') { const t = el.firstChild; try { t && t._doFit && t._doFit(); } catch (e) {} } }
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
    /* the add bar's panel: the panels open for this session (ui.panels.open), or any id */
    async _panelPick(btn) {
      const bar = btn.closest('.addbar'); if (!bar) return; let pop = bar.querySelector('.addpop'); if (pop) { pop.remove(); return; }
      pop = document.createElement('div'); pop.className = 'addpop'; pop.setAttribute('data-w', 'canvas.add.panel'); pop.innerHTML = '<span class="vc-dim">open panels — asking the bridge…</span>'; bar.appendChild(pop);
      const r = await this.callResult('ui.panels.open', { session_id: this._sid() }); const rows = (r && Array.isArray(r.panels)) ? r.panels : [];
      pop.innerHTML = (rows.length ? rows.map((p) => `<button class="pp" data-act="padd" data-pid="${esc(p.id)}" data-plabel="${esc(p.label || p.id)}"><b>${esc(p.label || p.id)}</b><span>${esc(p.host || '')}${p.origin ? ' · ' + esc(p.origin) : ''}</span></button>`).join('') : '<span class="vc-dim">no panel open for this session — any panel by its id:</span>')
        + '<div class="row"><input class="ti" data-f="pid" placeholder="panel id" spellcheck="false"><button class="ib on" data-act="paddid">Add</button></div>';
    }
    /* the add bar's widget: the WidgetConfig sheet (widget_element.js — this window's, or the host's when the column is
       embedded); the record it resolves is the item's content, keyed by its form, yours (beside the turn, no run) */
    _widgetSurface() { try { if (window.VeraWidgetConfig && window.VeraWidgetConfig.open) return window.VeraWidgetConfig; } catch (e) {} try { const p = window.parent; if (p && p !== window && p.VeraWidgetConfig && p.VeraWidgetConfig.open) return p.VeraWidgetConfig; } catch (e) {} return null; }
    async _widgetPick(btn, k) {
      const S = this._widgetSurface(); if (!S) return; const focusMid = this.dataset.focusMid || '';
      let rec = null; try { rec = await S.open({ mode: 'add', into: 'canvas', anchor: btn, templates: true, title: 'Add a widget to the canvas', sizes: ['s', 'm', 'l', 'xl'] }); } catch (e) { rec = null; }
      if (!rec) return;
      const form = String(rec.form || (rec.draw && rec.draw.form) || 'widget'); const size = String((rec.frame && rec.frame.size) || (rec.draw && rec.draw.size) || 'm');
      const nk = 'widget:' + form.replace(/[^a-zA-Z0-9_-]/g, '') + '-' + Date.now().toString(36);
      // the record as the item's content: the form and the size where the column's renderers read them too
      const content = Object.assign({}, rec, { widget: form, form, title: rec.title || form, draw: Object.assign({}, rec.draw || {}, { form, size }), record: rec });
      const args = { kind: 'widget', key: nk, content, at: 'now', size, anchor: { origin: 'you', beside: focusMid } };
      this._open.add(nk);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:add', { bubbles: true, detail: { key: nk, kind: 'widget', add: 'widget', form } })); } catch (e) {}
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

  const api = { place, checkRoutes, decisionOf, suggestionsOf, nowText, sizeOfHeight, turnOrder, isAged, foldOf, ADD_KINDS, ITEM_SIZES, KIND_GLYPH, BLOCK, langRunCmd, unwrap, version: 3 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VeraCanvas = Object.assign(root.VeraCanvas || {}, api);
  if (typeof customElements !== 'undefined' && !customElements.get('vera-canvas')) {
    customElements.define('vera-canvas', VeraCanvas);
  }
})();

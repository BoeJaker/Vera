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

  /* READER MARKDOWN - what browser.reader writes (reader_extract.js) drawn as the article it is: headings, paragraphs,
     nested and numbered lists, quotes, tables, figures, rules, code. The subset above (md) was built for notes and
     turns a table into pipes and a figure into a link. Same rule as md: everything is escaped FIRST, so a page's text
     can never become markup in the chat document; only the constructs below are drawn. */
  function mdx(src) {
    const fences = [];
    let s = String(src || '').replace(/```([\w-]*)\n([\s\S]*?)```/g, (_, lang, body) => {
      fences.push(`<pre class="vc-pre"><code>${esc(body.replace(/\n$/, ''))}</code></pre>`);
      return `\n\u0000F${fences.length - 1}\u0000\n`;
    });
    const inl = (t) => {
      const keep = [];
      let x = String(t).replace(/\\([*_`\[\]\\|])/g, (_, ch) => { keep.push(ch); return '\u0001' + (keep.length - 1) + '\u0001'; });
      x = esc(x)
        .replace(/`([^`\n]+)`/g, '<code>$1</code>')
        .replace(/!\[([^\]]*)\]\((https?:[^)\s]+)\)/g, '<img class="vc-rd-img" src="$2" alt="$1" loading="lazy" referrerpolicy="no-referrer">')
        .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
      return x.replace(/\u0001(\d+)\u0001/g, (_, i) => esc(keep[+i] || ''));
    };
    const lines = s.split('\n'); const out = []; let para = [];
    const flush = () => { if (para.length) { out.push('<p>' + para.map(inl).join('<br>') + '</p>'); para = []; } };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      if (/^\u0000F\d+\u0000$/.test(ln.trim())) { flush(); out.push(ln.trim()); continue; }
      if (!ln.trim()) { flush(); continue; }
      let m = /^(#{1,6})\s+(.*)$/.exec(ln);
      if (m) { flush(); out.push(`<h${m[1].length}>${inl(m[2])}</h${m[1].length}>`); continue; }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(ln)) { flush(); out.push('<hr>'); continue; }
      m = /^\s*!\[([^\]]*)\]\((https?:[^)\s]+)\)\s*$/.exec(ln);
      if (m) { flush(); out.push(`<figure><img class="vc-rd-img" src="${esc(m[2])}" alt="${esc(m[1])}" loading="lazy" referrerpolicy="no-referrer"></figure>`); continue; }
      if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        flush(); const cells = (r) => r.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((c) => inl(c.trim()));
        const head = cells(ln); i++; const body = [];
        while (i + 1 < lines.length && /^\s*\|/.test(lines[i + 1])) { i++; body.push(cells(lines[i])); }
        out.push('<div class="vc-tablewrap"><table class="vc-table"><thead><tr>' + head.map((c) => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
          + body.map((r) => '<tr>' + r.map((c) => `<td>${c}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>');
        continue;
      }
      if (/^\s*>\s?/.test(ln)) { flush(); const q = []; i--; while (i + 1 < lines.length && /^\s*>\s?/.test(lines[i + 1])) { i++; q.push(lines[i].replace(/^\s*>\s?/, '')); }
        out.push('<blockquote>' + q.map(inl).join('<br>') + '</blockquote>'); continue; }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(ln)) {
        flush(); const items = []; i--;
        while (i + 1 < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i + 1])) { i++; const mm = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]); items.push({ d: Math.floor(mm[1].length / 2), ol: /\d/.test(mm[2]), t: mm[3] }); }
        let html = ''; const stack = [];
        items.forEach((it) => {
          while (stack.length > it.d + 1) html += '</li></' + stack.pop() + '>';
          if (stack.length === it.d + 1) html += '</li>';
          while (stack.length < it.d + 1) { const tag = it.ol ? 'ol' : 'ul'; html += '<' + tag + '>'; stack.push(tag); }
          html += '<li>' + inl(it.t);
        });
        while (stack.length) html += '</li></' + stack.pop() + '>';
        out.push(html); continue;
      }
      para.push(ln.trim());
    }
    flush();
    return out.join('').replace(/\u0000F(\d+)\u0000/g, (_, i) => fences[+i] || '');
  }
  /* a page read in reader mode (browser.reader's answer): its title, who and when, how long, a note when the body was
     stitched from a fragmented page, and the article */
  /* ONE PAGE VIEW (owner, 2026-10-10: "the autogen web results dont have all the readermode features the other web search
     elements have can you make sure the canvas elements that show web results or sources or similar all use the same
     system"). A page read once carries who wrote it, how long it is, what it names and where it points; a source item drew
     all of that, a page opened in a records list (a search, the news, an enrich answer, a research run) drew none of it.
     Both draw these parts, from the same reading. */
  const PAGE_ENT = /^(PERSON|ORG|GPE|LOC|PRODUCT|EVENT|NORP|WORK_OF_ART|LAW|TICKER)$/;
  function pageExtras(rdr, host) {
    if (!rdr || typeof rdr !== 'object') return '';
    const meta = [rdr.site, rdr.byline, rdr.published ? String(rdr.published).slice(0, 10) : '', rdr.minutes ? rdr.minutes + ' min read' : ''].filter(Boolean).map(String);
    const ents = Array.isArray(rdr.entities) ? rdr.entities.filter((e) => e && PAGE_ENT.test(String(e.type || ''))).slice(0, 10) : [];
    const outs = []; if (rdr.markdown) { const seen = new Set([host || '']); const rx = /\]\((https?:\/\/[^)\s]+)\)/g; let m; while ((m = rx.exec(String(rdr.markdown))) && outs.length < 6) { let h = ''; try { h = new URL(m[1]).host; } catch (e) { continue; } if (!h || seen.has(h)) continue; seen.add(h); outs.push([h, m[1]]); } }
    return (meta.length ? `<div class="vc-src-m">${esc(meta.join(' · '))}</div>` : '')
      + (ents.length ? `<div class="vc-src-ents">${ents.map((e) => `<button class="vc-chip" data-act="srcent" data-ent-t="${esc(String(e.text))}" data-ent-y="${esc(String(e.type))}" title="${esc(e.type + ' - look it up beside this')}">${esc(String(e.text))}${e.count > 1 ? `<i>${e.count}</i>` : ''}</button>`).join('')}</div>` : '')
      + (outs.length ? `<div class="vc-src-out"><span>points to</span>${outs.map(([h, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener" title="${esc(u)}">${esc(h)}</a>`).join('')}</div>` : '');
  }
  /* browser.screenshot answers base64 under image_b64 (and not always PNG): the picture as a src */
  function shotSrc(r) {
    const shot = String((r && (r.image_b64 || r.image || r.data_url || r.screenshot || r.png)) || ''); if (!shot) return '';
    const mime = /^\/9j\//.test(shot) ? 'image/jpeg' : /^R0lGOD/.test(shot) ? 'image/gif' : /^UklGR/.test(shot) ? 'image/webp' : 'image/png';
    return /^data:|^https?:/.test(shot) ? shot : 'data:' + mime + ';base64,' + shot;
  }
  function readerHtml(rd) {
    const when = rd.published ? String(rd.published).slice(0, 10) : '';
    const bits = [rd.site, rd.byline, when, rd.minutes ? rd.minutes + ' min read' : ''].filter(Boolean);
    return `<article class="vc-reader">`
      + (rd.title ? `<h1 class="vc-rd-t">${esc(rd.title)}</h1>` : '')
      + (bits.length ? `<div class="vc-rd-by">${bits.map(esc).join(' · ')}</div>` : '')
      + (rd.composite ? `<div class="vc-rd-note">stitched from ${esc(rd.parts || 'several')} parts of a fragmented page</div>` : '')
      + (rd.image && /^https?:/.test(String(rd.image)) ? `<img class="vc-rd-hero" src="${esc(rd.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '')
      + `<div class="vc-rd-body">${mdx(String(rd.markdown || ''))}</div></article>`;
  }
  /* THE FABRIC'S ANSWER AS A GRAPH (owner, 2026-09-28: "the fabric one just seems to return one line results - itd be
     better if it displayed a vera graph .js graph by default but the list can be an option"). The query at the centre,
     the datasets that hold the records, the records sized by how close they are, and the tags two or more of them
     share - a tag every record carries links everything and says nothing, so it is left out. Built from the item's own
     records: no second read. */
  /* a page's names as graph node types (the Vera graph colours by type) */
  const ENT_NODE = { PERSON: 'Person', ORG: 'Organisation', GPE: 'Place', LOC: 'Place', FAC: 'Place', NORP: 'Group', PRODUCT: 'Product',
                     EVENT: 'Event', WORK_OF_ART: 'Work', LAW: 'Law', TICKER: 'Ticker' };
  /* MORE THINGS AS A GRAPH (owner, 2026-10-10: "id like to be able to open more things as a graph in the element style
     selector alongside cards and list"). Each kind says how its content is a graph; the records item's graph slot (still,
     fitted, filling its card) draws it. Ids avoid the records graph's own (r: / e:), so a pick never browses a list. */
  const tableCats = (c) => { const cols = c.columns || [], rows = (c.rows || []).filter(Array.isArray); const out = [];
    for (let j = 1; j < cols.length; j++) { const vals = rows.map((r) => String(r[j] == null ? '' : r[j]).trim()).filter((v) => v && v.length <= 40);
      const uniq = new Set(vals); if (vals.length >= 2 && uniq.size >= 1 && uniq.size < vals.length) out.push(j); } return out; };
  function graphOf(b) {
    const c = (b && b.content) || {}; const nodes = [], edges = [], seen = {};
    const node = (n) => { if (seen[n.id]) return; seen[n.id] = 1; nodes.push(n); };
    if (b.type === 'table') {
      const cols = c.columns || [], rows = (c.rows || []).filter(Array.isArray).slice(0, 80), cats = tableCats(c);
      node({ id: 'root', label: String(c.caption || 'table').slice(0, 40), type: 'Query', r: 15, props: { title: String(c.caption || '') } });
      rows.forEach((r, i) => { const rid = 'row:' + i; node({ id: rid, label: String(r[0] == null ? 'row ' + (i + 1) : r[0]).slice(0, 40), type: 'FabricRecord', r: 9,
        props: Object.fromEntries(cols.map((h, j) => [String(h || 'col ' + j), r[j]])) });
        let linked = false;
        cats.forEach((j) => { const v = String(r[j] == null ? '' : r[j]).trim(); if (!v || v.length > 40) return; const vid = 'v:' + j + ':' + v.toLowerCase();
          node({ id: vid, label: (cols[j] ? cols[j] + ': ' : '') + v, type: 'Tag', r: 11, props: { name: v, column: cols[j] } }); edges.push({ from: rid, to: vid, rel: String(cols[j] || '') }); linked = true; });
        if (!linked) edges.push({ from: 'root', to: rid, rel: 'row' }); });
      cats.forEach((j) => { nodes.filter((n) => n.id.indexOf('v:' + j + ':') === 0).forEach((n) => edges.push({ from: 'root', to: n.id, rel: String(cols[j] || ''), dashed: true })); });
    } else if (b.type === 'entities') {
      node({ id: 'root', label: String(c.title || 'Mentioned').slice(0, 40), type: 'Query', r: 15, props: { title: String(c.title || '') } });
      (Array.isArray(c.groups) ? c.groups : []).forEach((g, gi) => { const gid = 'g:' + gi; node({ id: gid, label: String(g.group || 'names'), type: 'Dataset', r: 12, props: { name: g.group } }); edges.push({ from: 'root', to: gid, rel: 'names' });
        (Array.isArray(g.entities) ? g.entities : []).forEach((e, ei) => { const nid = 'n:' + gi + ':' + ei; node({ id: nid, label: String(e.text || ''), type: 'Entity', r: Math.min(16, 6 + 2 * (e.count || 1)), props: { name: e.text, type: e.type, count: e.count, text: (e.sentences || []).join(' ') } }); edges.push({ from: gid, to: nid, rel: String(e.type || '') }); }); });
    } else if (b.type === 'kv') {
      const f = c.fields || c.data || {}; const rows = Array.isArray(f) ? f : Object.keys(f).map((k) => [k, f[k]]);
      node({ id: 'root', label: String(c.title || 'record').slice(0, 40), type: 'Query', r: 15, props: { title: String(c.title || '') } });
      rows.slice(0, 40).forEach((r, i) => { const v = r && r[1]; const s = v == null ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
        node({ id: 'f:' + i, label: (String(r && r[0]) + ': ' + s).slice(0, 44), type: 'Tag', r: 8, props: { name: String(r && r[0]), value: s.slice(0, 400) } }); edges.push({ from: 'root', to: 'f:' + i, rel: 'field' }); });
    }
    return { nodes, edges };
  }
  const graphSlot = (key) => `<div class="vc-tv-graph"><div class="vc-live" data-live="rgraph" data-key="${esc(key || '')}"><span class="vc-dim">drawing the graph…</span></div></div>`;
  function recGraph(c, bodies, ents) {
    const items = (Array.isArray(c.items) ? c.items : []).filter((r) => r && typeof r === 'object').slice(0, 80);
    const nodes = [], edges = [], seen = {};
    const node = (n) => { if (seen[n.id]) return; seen[n.id] = 1; nodes.push(n); };
    const q = String(c.query || c.title || 'query');
    node({ id: 'q', label: q.slice(0, 40), type: 'Query', r: 16, props: { title: q } });
    const tagN = {}; items.forEach((r) => (Array.isArray(r.tags) ? r.tags : []).forEach((t) => { tagN[t] = (tagN[t] || 0) + 1; }));
    items.forEach((r) => {
      const rid = 'r:' + r.id;
      const ds = String((r.meta && (r.meta.dataset_id || r.meta.source)) || r.domain || '');
      const sc = r.score != null && isFinite(+r.score) ? Math.max(0, Math.min(1, +r.score)) : 0.5;
      node({ id: rid, label: String(r.title || r.id).slice(0, 48), type: 'FabricRecord', r: Math.round(6 + 8 * sc),
             props: { title: String(r.title || ''), text: String(r.snippet || '').slice(0, 600), score: r.score, dataset_id: ds, record_id: String(r.id) } });
      if (ds) { node({ id: 'd:' + ds, label: ds, type: 'Dataset', r: 13, props: { name: ds } }); edges.push({ from: 'q', to: 'd:' + ds, rel: 'in' }, { from: 'd:' + ds, to: rid, rel: 'holds' }); }
      else edges.push({ from: 'q', to: rid, rel: 'found' });
      (Array.isArray(r.tags) ? r.tags : []).forEach((t) => { if (tagN[t] < 2 || tagN[t] === items.length) return;
        node({ id: 't:' + t, label: '#' + t, type: 'Tag', r: 7, props: { name: t } }); edges.push({ from: rid, to: 't:' + t, rel: 'tag', dashed: true }); });
    });
    // the NEIGHBOURS a record was asked for (its 'neighbours' in the list): drawn off it, dashed - 'similar', not 'holds'
    let nbN = 0;
    items.forEach((r) => { const nb = bodies && bodies[r.id] && bodies[r.id].nb; if (!Array.isArray(nb)) return;
      nb.slice(0, 8).forEach((m) => { if (!m || !m.id) return; const nid = 'r:' + m.id; const ds = String(m.dataset_id || '');
        if (!seen[nid]) { nbN++; node({ id: nid, label: String(m.snippet || m.id).replace(/\s+/g, ' ').slice(0, 40), type: 'FabricRecord', r: 6,
          props: { title: String(m.snippet || '').slice(0, 120), text: String(m.snippet || ''), dataset_id: ds, record_id: String(m.id), neighbour: true } });
          if (ds) { node({ id: 'd:' + ds, label: ds, type: 'Dataset', r: 11, props: { name: ds } }); edges.push({ from: 'd:' + ds, to: nid, rel: 'holds' }); } }
        edges.push({ from: 'r:' + r.id, to: nid, rel: 'similar', dashed: true }); }); });
    /* WHAT THE PAGES NAME (a research answer's pages, from the reader cache): a name two or more pages share is a node
       linked to each page that names it - the people and organisations the run keeps meeting, and which pages meet them */
    let entN = 0;
    if (ents) {
      const n = {}; items.forEach((r) => (ents[String(r.id)] || []).forEach((e) => { n[e[0]] = (n[e[0]] || 0) + 1; }));
      const keep = Object.keys(n).filter((k) => n[k] >= 2).sort((a, b) => n[b] - n[a]).slice(0, 24); const on = new Set(keep);
      items.forEach((r) => (ents[String(r.id)] || []).forEach((e) => { if (!on.has(e[0])) return;
        if (!seen['e:' + e[0]]) entN++;
        node({ id: 'e:' + e[0], label: e[0].slice(0, 32), type: ENT_NODE[e[1]] || 'Entity', r: Math.min(16, 7 + 2 * n[e[0]]), props: { name: e[0], kind: e[1], pages: n[e[0]] } });
        edges.push({ from: 'r:' + r.id, to: 'e:' + e[0], rel: 'names' }); }));
    }
    return { nodes, edges, caption: items.length + ' record' + (items.length === 1 ? '' : 's') + (nbN ? ' \u00b7 ' + nbN + ' neighbour' + (nbN === 1 ? '' : 's') : '') + (entN ? ' \u00b7 ' + entN + ' name' + (entN === 1 ? '' : 's') + ' they share' : '') };
  }


  /* ── CODE AS THE CHAT DRAWS IT: the chat's own highlighter and linter (window.VeraCode, published by the chat page),
     so a code item and a code fence are coloured and checked by ONE implementation. Where the page has none (the
     standalone Canvas panel) the item draws plain numbered lines, as it always did. ── */
  // highlighted markup split into lines, closing every span open at a newline and reopening it on the next line, so a
  // comment or a string that runs across lines is still coloured on each of them
  function splitHl(html) {
    const out = []; const open = []; let cur = '';
    const re = /(<span\b[^>]*>)|(<\/span>)|(\n)|([^<\n]+)|(<)/g; let m;
    while ((m = re.exec(String(html || '')))) {
      if (m[1]) { open.push(m[1]); cur += m[1]; }
      else if (m[2]) { open.pop(); cur += m[2]; }
      else if (m[3]) { cur += '</span>'.repeat(open.length); out.push(cur); cur = open.join(''); }
      else cur += m[0];
    }
    out.push(cur); return out;
  }
  /* the lint strip: always the summary (lines · bytes · a raw fence or an envelope that should not be there · JSON
     validity), and the diagnostics once the code has SETTLED. A code item still being streamed is half a brace, and
     flagging it on every beat is noise - the chat lints only closed fences for the same reason. */
  function codeLintHtml(c, key, el) {
    // the chat's VeraCode gives the summary line and the lint; on a page without the chat, the shared kit's linter
    const VO = root.VeraOut;
    const VC = (root.VeraCode && typeof root.VeraCode.summary === 'function') ? root.VeraCode
      : (VO && typeof VO.lintCode === 'function') ? { summary: (l, code) => ({ parts: [String(code).split('\n').length + ' lines'], cls: 'ok' }), lint: VO.lintCode } : null;
    if (!VC) return '';
    const code = String(c.code || ''); const lang = String(c.lang || '');
    if (!code.trim()) return '';
    let settled = true;
    if (key && el) {
      el._codeAt = el._codeAt || {}; const prev = el._codeAt[key]; const now = Date.now();
      if (!prev) el._codeAt[key] = { code, t: 0 };                       // first sight: a finished item, not a stream
      else if (prev.code !== code) { el._codeAt[key] = { code, t: now }; settled = false; }
      else settled = !prev.t || now - prev.t >= 1400;
      if (!settled && !el._lintT) el._lintT = setTimeout(() => { el._lintT = null; if (el._doc) el.render(el._doc); }, 1600);
    }
    let sum = null; try { sum = VC.summary(lang, code); } catch (e) { sum = null; }
    let diags = []; if (settled && typeof VC.lint === 'function') { try { diags = VC.lint(lang, code) || []; } catch (e) { diags = []; } }
    const cls = diags.some((d) => d && d.sev === 'err') || (sum && sum.cls === 'err') ? 'err' : (diags.length || (sum && sum.cls === 'warn') ? 'warn' : 'ok');
    const parts = (sum && Array.isArray(sum.parts) ? sum.parts : []).map((p) => esc(String(p)));
    return '<div class="vc-lint ' + cls + '" data-w="canvas.code.lint">' + parts.join(' <b>\u00b7</b> ') + (settled ? '' : ' <i>\u00b7 linting when it settles</i>') + '</div>'
      + diags.slice(0, 8).map((d) => '<div class="vc-lint-d ' + (d.sev === 'err' ? 'err' : 'warn') + '">' + (d.sev === 'err' ? '\u26a0 ' : '\u25cf ') + esc((d.line ? 'L' + d.line + ': ' : '') + String(d.msg || '')) + '</div>').join('');
  }
  /* ── A CAPABILITY'S RESULT, AS THE ELEMENT THAT FITS IT. The chat publishes its adapter registry (terminal · sources
     · html · image · code · table · widget · chat · prose · a record's fields · a JSON tree …) as
     window.VeraCanvasAdapt; a result item asks it what the answer IS and draws that. Without the chat, the element's
     own reading: a record's fields, a JSON tree, text. Cached by the answer, because a render is not a new answer. ── */
  const _RV = new Map();
  function genericView(res) {
    if (res == null) return { kind: 'note', content: { text: '(no result)' } };
    if (typeof res === 'string') return res.length > 200 || res.indexOf('\n') >= 0 ? { kind: 'markdown', content: { md: res } } : { kind: 'note', content: { text: res } };
    if (typeof res !== 'object') return { kind: 'note', content: { text: String(res) } };
    const ks = Array.isArray(res) ? [] : Object.keys(res);
    const flat = !Array.isArray(res) && ks.length > 0 && ks.length <= 24 && ks.every((k) => res[k] == null || typeof res[k] !== 'object');
    return flat ? { kind: 'kv', content: { fields: res } } : { kind: 'json', content: { data: res } };
  }
  function resultView(c) {
    c = c || {}; const res = c.result; let sig = '';
    try { sig = String(c.cap || '') + '\u0000' + JSON.stringify(res === undefined ? null : res); } catch (e) { sig = ''; }
    if (sig && _RV.has(sig)) return _RV.get(sig);
    let v = null; const A = root.VeraCanvasAdapt;
    if (typeof A === 'function') {
      try { const m = A(String(c.cap || ''), res, c.args || {});
        if (Array.isArray(m)) v = m.length ? { kind: '_many', content: { items: m } } : null;
        else if (m && m.kind && m.kind !== 'result') v = { kind: String(m.kind), content: m.content || {} };
      } catch (e) { v = null; }
    }
    if (!v) v = genericView(res);
    if (sig) { if (_RV.size > 80) _RV.clear(); _RV.set(sig, v); }
    return v;
  }
  // a JSON value as a tree you can open: the first two levels open, long strings cut, wide arrays counted
  function jsonTree(v, d) {
    if (v === null || v === undefined) return '<span class="j-null">null</span>';
    if (typeof v === 'string') return '<span class="j-str">"' + esc(v.length > 400 ? v.slice(0, 400) + '\u2026' : v) + '"</span>';
    if (typeof v !== 'object') return '<span class="j-num">' + esc(String(v)) + '</span>';
    const arr = Array.isArray(v); const keys = arr ? v.map((_, i) => i) : Object.keys(v);
    if (!keys.length) return '<span class="j-null">' + (arr ? '[]' : '{}') + '</span>';
    if (d > 6) return '<span class="j-more">' + (arr ? '[\u2026 ' + keys.length + ']' : '{\u2026}') + '</span>';
    const shown = keys.slice(0, 200);
    const rows = shown.map((k) => '<div class="j-row"><span class="j-k">' + esc(String(k)) + '</span>' + jsonTree(v[k], d + 1) + '</div>').join('')
      + (keys.length > shown.length ? '<div class="j-more">\u2026 ' + (keys.length - shown.length) + ' more</div>' : '');
    return '<details class="j-node"' + (d < 2 ? ' open' : '') + '><summary>' + (arr ? '[' + keys.length + ']' : '{' + keys.length + '}') + '</summary>' + rows + '</details>';
  }

  /* a records item's browser state (page, filter, sort, view, facet, what is open, what was fetched): the element's,
     per item key, never written to the canvas - paging is not an edit. A renderer called with no element (a
     result drawn inside another) keeps it here instead. */
  const _recFallback = {};
  const recState = (el, key) => { const box = el ? (el._recUi = el._recUi || {}) : _recFallback; const k = String(key || '');
    return box[k] || (box[k] = { page: 0, q: '', sort: 'rank', view: 'list', dom: '', ent: '', open: {}, body: {} }); };
  /* A PANEL'S OWN PAGE (WSU-67, owner 2026-10-11: "ui panels opened on the desk seem to lock up and can't be interacted
     with"). An item with no `src` drew /ui/panels/<id> - a route that does not exist (404 for every panel, measured) - so
     the frame was an empty page that took no input. Only the chat page resolved the real address (its listener for
     vera:canvas:panel-src); a desk opened anywhere else, or restored, kept the dead frame. The canvas now resolves it
     itself, the way the chat does: the iframe src in the panel's registered html, the alias table, a dedicated
     /ui/panels/<id> route, then file-name guesses under /ui/panels/file/. Cached per panel; '' when nothing answers. */
  const PANEL_ALIASES = { 'agents-skills-ontologies': '/ui/panels/agents-skills-ontologies', 'skills-editor': '/ui/panels/file/skills_panel.html',
    'ontologies-browser': '/ui/panels/file/ontologies_panel.html', 'agents-editor': '/ui/panels/file/agent_panel.html', 'memory-graph': '/ui/panels/file/memory_graph_panel.html',
    'cap_tracking': '/ui/panels/file/cap_tracking_panel.html', 'workers': '/ui/panels/file/worker_panel.html', 'workers-ollama': '/ui/panels/workers-ollama',
    'model-routing': '/ui/panels/model-routing', 'chat2': '/chat_panel' };
  const PANEL_SRC = {}; let PANEL_REG = null;
  /* the widest a widget card of a short-content form is on the desk (WSU-69): lists, checklists, fields, notices and
     single figures wrap short; wider was an empty band. Charts, tables, maps, graphs and calendars are not capped. */
  const DESK_FORM_MAXW = { checklist: 420, list: 440, links: 440, notices: 460, people: 460, kv: 520, pills: 480, string: 460, announcement: 460,
    counter: 380, hero: 400, ring: 360, radial: 360, dial: 360, gauge: 420, meter: 420, level: 380, glance: 460, numbers: 520, status: 480, stepper: 640, files: 520 };
  async function panelSrc(id) {
    id = String(id || ''); if (!id) return ''; if (PANEL_SRC[id] != null) return PANEL_SRC[id];
    const base = (root._veraBase || '').replace(/\/$/, '');
    const ok = async (path) => { try { const r = await fetch(base + path, { method: 'HEAD' }); return r.ok; } catch (e) { return false; } };
    if (!PANEL_REG) PANEL_REG = fetch(base + '/ui/panels', { headers: { Accept: 'application/json' } }).then((r) => r.json()).then((j) => (Array.isArray(j) ? j : (j && j.panels) || [])).catch(() => []);
    const reg = await PANEL_REG; const p = reg.find((x) => x && (x.id === id || x.label === id)) || null;
    const m = p && String(p.html || '').match(/<iframe[^>]+src="([^"]+)"/i);
    let src = m ? m[1] : (PANEL_ALIASES[id] || '');
    if (!src && await ok('/ui/panels/' + encodeURIComponent(id))) src = '/ui/panels/' + encodeURIComponent(id);
    if (!src) { const g = [id.replace(/-/g, '_') + '_panel.html', id.replace(/-panel$/, '').replace(/-/g, '_') + '_panel.html', id.replace(/-/g, '_') + '.html', id + '.html'];
      for (const f of g.filter((x, i) => g.indexOf(x) === i)) { if (await ok('/ui/panels/file/' + f)) { src = '/ui/panels/file/' + f; break; } } }
    PANEL_SRC[id] = src; return src;
  }

  const BLOCK = {
    /* a document item draws with the reader's renderer (mdx): a research report's tables, numbered lists, quotes and
       figures were pipes and bare lines under the note subset */
    // a reading item through the shared kit when it is on the page: its fences are the same cards as the chat's and the
    // loop's (Copy · Edit · Explode · Preview · Run · Save); the canvas's own mdx where it is not
    /* ...and under it the same row a chat reply carries (owner, 2026-10-03: "prose sent to the canvas also needs the same
       options as code (like the menu under responses) so it can be converted into other formats sent to other areas or
       saved or edited"): Copy, then the item's own Edit (kept with the item) and Explode (the bound explode item), then
       "Send to" - an HTML report, a podcast, a notebook, a file, a project, any channel with a style. */
    markdown: (c, size, key, el) => {
      const VO = root.VeraOut; const md = String(c.md || c.text || '');
      /* the card's header names the document: a first heading that only says the same again is not drawn twice */
      const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
      const h1 = md.match(/^\s*#{1,2}\s+(.+?)\s*(\n|$)/);
      const shown = h1 && c.title && norm(h1[1]) === norm(c.title) ? md.slice(h1[0].length) : md;
      const body = VO && typeof VO.render === 'function' ? VO.render(shown, { canvas: false }) : mdx(shown);
      let row = '';
      if (VO && typeof VO.proseMenu === 'function' && key) {
        const own = (act, ico, label, tip) => `<button class="mact" data-act="${act}" title="${esc(tip)}">${(VO.icons && VO.icons[ico]) || ''}<span>${label}</span></button>`;
        row = VO.proseMenu('cvp-' + key, { md, canvas: false, explode: false,
          extra: own('edit', 'edit', 'Edit', 'Edit this text - the item keeps your version') + own('explode', 'explode', 'Explode', 'A structured diagram of this text, beside it') });
      }
      return `<div class="vc-md">${body}</div>` + (row ? `<div class="vc-prose-row">${row}</div>` : '');
    },

    /* A MONTH, WITH WHAT IS ON IT. Backed by the diary rather than by a copy of it: the month buttons and a day
       press go back to cal.events.list, so the item is a VIEW of the calendar and not a screenshot taken once.
       An event written anywhere - by the aide, by the panel, by Google's sync - shows here on the next refresh.

       The week starts Monday. Days carry a dot per event up to three and then a count, because a grid where a
       busy day and a quiet one look alike is a decoration; and the selected day's events are listed underneath
       with their times, since that is the question a calendar is actually asked. */
    calendar: (c, size, key, el) => {
      const evs = Array.isArray(c.events) ? c.events : [];
      const pad = (n) => (n < 10 ? '0' : '') + n;
      const today = new Date();
      const todayKey = today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate());
      const month = /^\d{4}-\d{2}$/.test(String(c.month || '')) ? String(c.month) : todayKey.slice(0, 7);
      const [Y, M] = month.split('-').map(Number);
      const first = new Date(Y, M - 1, 1);
      const days = new Date(Y, M, 0).getDate();
      // Monday-first: JS makes Sunday 0, and a week that starts on Sunday is not the week this is read in
      const lead = (first.getDay() + 6) % 7;
      const byDay = {};
      evs.forEach((e) => { const d = String(e && e.start || '').slice(0, 10); if (!d) return; (byDay[d] = byDay[d] || []).push(e); });
      const sel = /^\d{4}-\d{2}-\d{2}$/.test(String(c.selected || '')) ? String(c.selected) : '';
      const MON = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
      let cells = '';
      for (let i = 0; i < lead; i++) cells += '<span class="vc-cal-d out"></span>';
      for (let d = 1; d <= days; d++) {
        const k = month + '-' + pad(d);
        const on = byDay[k] || [];
        const dots = on.slice(0, 3).map((e) => '<i style="background:' + esc(String(e.color || 'var(--acc,#5a9e8f)')) + '"></i>').join('');
        cells += '<button class="vc-cal-d' + (k === todayKey ? ' today' : '') + (k === sel ? ' on' : '') + (on.length ? ' has' : '')
          + '" data-cal-day="' + k + '" data-cal-key="' + esc(key || '') + '"'
          + ' title="' + esc(k + (on.length ? ' \u00b7 ' + on.length + ' event' + (on.length === 1 ? '' : 's') : '')) + '">'
          + '<b>' + d + '</b><span class="vc-cal-dots">' + dots + (on.length > 3 ? '<em>+' + (on.length - 3) + '</em>' : '') + '</span></button>';
      }
      const listFor = sel ? (byDay[sel] || []) : evs.slice().sort((a, b) => String(a.start || '').localeCompare(String(b.start || ''))).slice(0, 8);
      const timeOf = (e) => { const s = String(e.start || ''); if (e.all_day || s.length <= 10) return 'all day'; const t = s.slice(11, 16); return t || ''; };
      const rows = listFor.map((e) => '<div class="vc-cal-e">'
        + '<span class="vc-cal-t">' + esc(timeOf(e)) + '</span>'
        + '<span class="vc-cal-n" style="border-color:' + esc(String(e.color || 'var(--acc,#5a9e8f)')) + '">' + esc(String(e.title || 'event'))
        + (e.location ? ' <em>' + esc(String(e.location)) + '</em>' : '') + '</span></div>').join('')
        || '<div class="vc-cal-none">' + (sel ? 'Nothing on this day.' : 'Nothing in this month.') + '</div>';
      return '<div class="vc-cal"><div class="vc-cal-in">'
        + '<div class="vc-cal-hd">'
        + '<button class="vc-cal-b" data-cal-mv="-1" data-cal-key="' + esc(key || '') + '" title="The month before">\u2039</button>'
        + '<span class="vc-cal-m">' + esc(MON[M - 1] + ' ' + Y) + '</span>'
        + '<button class="vc-cal-b" data-cal-mv="1" data-cal-key="' + esc(key || '') + '" title="The month after">\u203a</button>'
        + '<span class="vc-cal-sp"></span>'
        + '<button class="vc-cal-b" data-cal-mv="0" data-cal-key="' + esc(key || '') + '" title="Back to this month">today</button>'
        + '</div>'
        + '<div class="vc-cal-w">' + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((d) => '<span>' + d + '</span>').join('') + '</div>'
        + '<div class="vc-cal-g">' + cells + '</div>'
        + '<div class="vc-cal-l">' + rows + '</div>'
        + '</div></div>';
    },

    /* DATED EVENTS IN ORDER. `when` is whatever precision the prose had - a year, a month, a day - and it is
       shown as it was read rather than padded out to a fake day so the axis looks tidy. A run of events in the
       same year is grouped under it, because a timeline of forty rows each labelled with the same four digits
       is a list wearing an axis. Each event keeps the page it came from, so the timeline is a way back into the
       sources rather than a summary that has left them behind. */
    timeline: (c) => {
      const ev = (Array.isArray(c.events) ? c.events : []).filter((e) => e && e.when);
      if (!ev.length) return `<div class="vc-tl-empty">No dated events were found.</div>`;
      const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const yearOf = (w) => String(w || '').slice(0, 4);
      const pos = (w) => { const s = String(w || ''); const y = +s.slice(0, 4) || 0, m = +s.slice(5, 7) || 0, d = +s.slice(8, 10) || 0; return y + (m ? (m - 1) / 12 : 0) + (d ? (d - 1) / 366 : 0); };
      const pretty = (w) => { const s = String(w || ''); const m = +s.slice(5, 7); if (s.length >= 10) return (+s.slice(8, 10)) + ' ' + (MON[m - 1] || ''); if (s.length >= 7) return MON[m - 1] || ''; return ''; };
      const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      // the date is on the axis and on the row: a label that OPENS with it ('In 1984, ...') loses it
      const labelOf = (e) => { let l = String(e.label || e.text || '');
        if (e.lead && e.raw) { const s = l.replace(new RegExp('^\\W*(?:(?:in|on|by|since|from|during|circa|early|late|mid|around)\\s+)?' + reEsc(e.raw) + '\\s*[,:\\u2013\\u2014-]?\\s*', 'i'), ''); if (s.length > 8) l = s.charAt(0).toUpperCase() + s.slice(1); }
        return l; };
      const xs = ev.map((e) => pos(e.when)); const lo = Math.min.apply(null, xs), hi = Math.max.apply(null, xs), span = Math.max(1e-6, hi - lo);
      const pct = (x) => (4 + 92 * (x - lo) / span).toFixed(2);
      const y0 = Math.floor(lo), y1 = Math.floor(hi);
      const ticks = [y0]; if (y1 - y0 >= 4) { const step = (y1 - y0) / 4; for (let k = 1; k < 4; k++) ticks.push(Math.round(y0 + step * k)); } if (y1 !== y0) ticks.push(y1);
      const axis = `<div class="vc-tl-ax"><div class="vc-tl-ln"></div>`
        + ev.map((e, i) => `<button class="vc-tl-dot" style="left:${pct(xs[i])}%" data-tl-go="${i}" title="${esc(e.when + ' \u2014 ' + labelOf(e).slice(0, 90))}"></button>`).join('')
        /* a year starts before its first event: its tick is kept on the axis, not drawn off its left end ("324" for 2024) */
        + [...new Set(ticks)].map((y, i, a) => `<span class="vc-tl-tick${i === a.length - 1 && a.length > 1 ? ' end' : ''}" style="left:${Math.min(100, Math.max(0, +pct(y)))}%">${y}</span>`).join('')
        + `</div>`;
      let out = '', year = '';
      ev.forEach((e, i) => {
        const y = yearOf(e.when);
        if (y !== year) {
          if (year && +y - +year > 1) out += `<div class="vc-tl-gap">\u2026 ${+y - +year} years</div>`;
          year = y; out += `<div class="vc-tl-y">${esc(y)}</div>`;
        }
        out += `<div class="vc-tl-e" data-i="${i}">`
          + `<span class="vc-tl-w">${esc(pretty(e.when))}</span>`
          + `<span class="vc-tl-t">${esc(labelOf(e))}`
          + (e.url ? `<a class="vc-tl-a" href="${esc(e.url)}" target="_blank" rel="noopener">source \u2197</a>` : '')
          + `</span></div>`;
      });
      const years = y1 > y0 ? y0 + '\u2013' + y1 : String(y0);
      // the card's own header carries the title; the body says how much and over what span
      return `<div class="vc-tl"><div class="vc-tl-h"><small>${ev.length} event${ev.length === 1 ? '' : 's'} \u00b7 ${esc(years)}</small></div>`
        + (ev.length > 1 ? axis : '') + `<div class="vc-tl-list">${out}</div></div>`;
    },
    /* A PAGE THE RESEARCH READ. The run already knows every page it fetched - the research card has listed them
       beside the report all along - but the canvas only ever got the finished prose, so the thing you could not
       do was go back to what it was BUILT from. This is that: where it came from, what it said, and what it
       looked like.

       The body and the screenshot are NOT fetched when the item lands. A deep run reads dozens of pages, and
       forty screenshots taken to be thumbnails nobody opens is forty browser sessions. They are fetched when the
       item is opened, by the host, and written back into the item - so the second look is instant and the first
       costs one page. `failed` is kept rather than hidden: a source that would not load is a fact about the
       research, not a blank to tidy away. */
    source: (c, size, key, el) => {
      const url = String(c.url || ''); let host = String(c.domain || '');
      if (!host && url) { try { host = new URL(url).host; } catch (e) { host = url.slice(0, 40); } }
      const open = !!(key && el && el._srcOpen && el._srcOpen[key]);
      const title = String(c.title || host || url || 'source');
      const chars = c.chars ? (c.chars > 1000 ? (c.chars / 1000).toFixed(1) + 'k' : String(c.chars)) + ' chars' : '';
      const shot = String(c.shot || '');
      const text = String(c.text || '');
      const rdr = c.reader && typeof c.reader === 'object' ? c.reader : null;
      /* A PAGE, DEEPER (canvas-enrichment slice 6 - owner, 2026-10-03: "the canvas elements mentioned here need to be much
         better more engaging and informative, interactive, deep"). A page read once is in the reader cache, so the item
         carries its reading at once (the canvas asks the cache, never the web, for every source it draws): who wrote it,
         how long it is, the people, organisations and places it names - each a chip that looks the name up beside it -
         the other sites it points to, and an Explode of the page itself. */
      return `<div class="vc-src${c.failed ? ' bad' : ''}${open ? ' open' : ''}">`
        + `<div class="vc-src-hd">`
        + `<span class="vc-src-dom">${esc(host)}</span>`
        + (chars ? `<span class="vc-src-n">${esc(chars)}</span>` : '')
        + (c.failed ? `<span class="vc-src-n bad">did not load</span>` : '')
        + `</div>`
        + `<a class="vc-src-t" href="${esc(url)}" target="_blank" rel="noopener" title="${esc(url)}">${esc(title)}</a>`
        + (c.snippet ? `<div class="vc-src-s">${esc(String(c.snippet).slice(0, 400))}</div>` : '')
        + pageExtras(rdr, host)
        + `<div class="vc-src-act">`
        + `<button class="vc-src-b" data-src-act="read" data-src-key="${esc(key || '')}">${open ? 'less' : (text || rdr ? 'read' : 'reader mode')}</button>`
        + `<button class="vc-src-b" data-src-act="shot" data-src-key="${esc(key || '')}">${shot ? 'hide the picture' : 'see the page'}</button>`
        + (rdr && rdr.markdown ? `<button class="vc-src-b" data-src-act="explode" data-src-key="${esc(key || '')}" title="A structured diagram of the page - its sections, what it names, how they connect">explode</button>` : '')
        + `</div>`
        + (open && rdr ? `<div class="vc-src-body rd">${readerHtml(rdr)}</div>` : open && text ? `<div class="vc-src-body">${md(text.slice(0, 20000))}</div>` : '')
        + (shot ? `<img class="vc-src-shot" src="${esc(shot)}" alt="${esc(title)}">` : '')
        + `</div>`;
    },

    /* a code item draws its source and, when the language is one a browser can simply show, the thing itself. The
       preview is drawn in the column's live layer exactly as a diagram is: a sandboxed iframe, srcdoc, no network. It
       is off until asked for, and the head carries the switch (Notes/42 defect 78 - the chat has had a Preview on its
       fences all along; a canvas item never had one). */
    code: (c, size, key, el) => {
      // a whole page starts drawn; anything else starts as source until the toggle says otherwise. What it
      // actually settled on is remembered, so the toggle flips what you can SEE rather than an unset flag.
      const pset = key && el && el._prevOn && Object.prototype.hasOwnProperty.call(el._prevOn, key);
      const pdef = !el || typeof el.previewOn !== 'function' || el.previewOn();   // the canvas's Preview setting
      const prev = PREVIEWABLE(c.lang) && (pset ? !!el._prevOn[key] : (pdef && WHOLE_PAGE(c.lang, c.code)));
      if (key && el) { el._prevSeen = el._prevSeen || {}; el._prevSeen[key] = prev; }
      /* THE SAME CARD AS THE CHAT'S AND THE LOOP'S (owner, 2026-10-03: "all rendering of output types needs to be
         consistent and use the same element"): the shared kit's bar - Copy, Run, Save, Pop out, in its look - with the
         canvas's own actions beside them in the same style: Edit (the item's editor, kept with the item), Preview (the
         column's live layer, which survives a redraw). The body stays the canvas's: line-addressable, so an explode item
         bound to this one still lights its spans. */
      const VO = root.VeraOut;
      const vid = key ? 'cv-' + key : '';
      /* THE KIT'S CARD, ENTIRE (owner, 2026-10-03: "the code renderer in the canvas does not have the same options as the
         one in the chat - it should - and it should be using the same underlying code"): VeraOut.card draws it - the
         same bar, every action (Copy · Edit · Explode · Outline · Preview · Pop out · Run · Save), the numbered lines an
         explode item binds to, the reader's helpers - with only what the canvas does its own way plugged in: Preview
         draws in the column's live layer (it survives a redraw), Explode makes the bound explode item, an edit is kept
         with the item. While the chat is still streaming into it, it is the kit's streaming card. */
      /* A DECK, A SHEET, A PAGE (```slides / ```sheet ```csv ```tsv / ```document) is drawn as what it is - the kit's file
         card - not as its source: a long one streams out of the chat onto the canvas as a code item (owner, 2026-10-04:
         a 4-slide deck landed here as 55 lines of markdown). Drawn as it streams in; its Edit is kept with the item. */
      const fk = VO && vid && typeof VO.fileKind === 'function' ? VO.fileKind(c.lang) : '';
      if (fk && typeof VO.fileCard === 'function') {
        const streaming = !!(el && el._liveOv && el._liveOv[key]);
        return `<div class="vc-codewrap vc-filewrap">${VO.fileCard(fk, String(c.lang || ''), String(c.code || ''), { id: vid, canvas: false, cls: 'vc-kit', incomplete: streaming, title: String(c.title || c.filename || '') })}</div>`;
      }
      if (VO && vid && typeof VO.card === 'function') {
        const streaming = !!(el && el._liveOv && el._liveOv[key]);
        const live = prev ? `<div class="vc-live vc-preview" data-live="preview" data-key="${esc(key)}" data-lang="${esc(String(c.lang || ''))}"><span class="vc-dim">rendering…</span></div>` : null;
        // autoPrev off: the canvas decides what is drawn - a page it switched to source stays source (owner, 2026-10-03:
        // "the source button on html in the canvas doent open the source": the kit drew the page straight back over it)
        const cardHtml = VO.card(String(c.lang || ''), String(c.code || ''), { id: vid, filename: String(c.filename || c.path || ''), canvas: false, body: live, prevOn: prev, cls: 'vc-kit', incomplete: streaming, autoPrev: false });
        return `<div class="vc-codewrap" data-code="1"${c.path || c.filename ? ` data-path="${esc(c.path || c.filename)}"` : ''}>${cardHtml}</div>`;
      }
      // (without the kit on the page: the canvas's own numbered body and Preview switch)
      const head = `<div class="vc-codehead">${esc(c.filename || c.lang || 'code')}<span class="sp"></span>`
        + (PREVIEWABLE(c.lang) && key ? `<button class="ib${prev ? ' on' : ''}" data-act="cprev" title="${prev ? 'Show the source' : 'Render it here - a sandboxed frame, no network'}">${prev ? 'source' : 'preview'}</button>` : '')
        + '</div>';
      const lines = String(c.code || '').replace(/\n$/, '').split('\n');
      let hl = null; const VC = root.VeraCode;
      if (!prev && VC && typeof VC.highlight === 'function') { try { hl = splitHl(VC.highlight(String(c.code || '').replace(/\n$/, ''), c.lang)); if (hl.length !== lines.length) hl = null; } catch (e) { hl = null; } }
      const bodyHtml = prev
        ? `<div class="vc-live vc-preview" data-live="preview" data-key="${esc(key)}" data-lang="${esc(String(c.lang || ''))}"><span class="vc-dim">rendering…</span></div>`
        : `<pre class="vc-pre vc-code"><code>${lines.map((l, i) => `<span class="vc-line" data-line="${i + 1}"><i class="vc-lno">${i + 1}</i>${hl ? (hl[i] || ' ') : (esc(l) || ' ')}</span>`).join('')}</code></pre>`;
      return `<div class="vc-codewrap" data-code="1"${c.path || c.filename ? ` data-path="${esc(c.path || c.filename)}"` : ''}>${head}${bodyHtml}${codeLintHtml(c, key, el)}</div>`;
    },
    /* AN EXPLODE ITEM — the structured diagram of something, in the canvas beside what it is a diagram OF
       (EXPLODE.md §8.3). content:
         {binds?: the KEY of a code item in this canvas — its code is what gets exploded, and the two are bound
                  by span BOTH ways: click a card, the code scrolls and lights; select lines, the covering card lights;
          path/paths/depth · record/ranges/mode/layers · text+lang · assess · height · title}
       The diagram itself is <vera-graph-embed renderer="struct">, mounted in the column's live layer the way a
       diagram, a widget and a preview are — so its pan, zoom and selection survive a render of the column. */
    explode: (c, size, key) => {
      const src = c.binds ? 'bound to ' + String(c.binds) : (c.record ? 'record ' + String(c.record) : (c.path || (c.paths && [].concat(c.paths).join(', ')) || (c.text ? 'a passage' : (c.code ? 'a snippet' : 'nothing yet'))));
      const head = `<div class="vc-th"><i class="dot on"></i><b>${esc(c.title || 'Explode')}</b><span class="mono">${esc(String(src).slice(0, 60))}</span><span class="sp"></span>`
        + (c.binds ? '<button class="ib" data-act="xpsync" title="Light the whole of the bound source again">clear</button>' : '') + '</div>';
      /* The slot takes what the ITEM can give it. Forcing the asked-for height here made the slot taller than
         the item that holds it (320 in a 242 box, measured in the page), and an item is `overflow:hidden` -- so
         the bottom of every diagram was cut off. An item's height is its SIZE, which is the canvas's own model
         and what a reader drags; `height` on the content is a hint that picks that size when the item is made. */
      return `<div class="vc-xp" data-w="canvas.explode"${c.binds ? ` data-binds="${esc(c.binds)}"` : ''}>${head}`
        + `<div class="vc-live" data-live="explode" data-key="${esc(key)}">`
        + `<span class="vc-dim">exploding…</span></div></div>`;
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
      /* THE DIAGRAM CARD (owner, 2026-10-03: "mermaid diagrams rendered in the chat and canvas need the unified menu
         style and the relevant options for along the top same as the rendered code output"): the kit's bar - Copy, Edit,
         Source, Pop out, Save, Send to - over the column's live drawing; Pop out is the canvas's own "open in the chat" */
      const VO = root.VeraOut;
      if (key && VO && typeof VO.diagram === 'function') return `<div class="vc-codewrap vc-diagwrap" data-w="canvas.diagram">`
        + VO.diagram(src, { id: 'cvd-' + key, title, canvas: false, cls: 'vc-kit',
          body: `<div class="vc-live" data-live="mermaid" data-key="${esc(key)}" data-title="${esc(title)}"><span class="vc-dim">drawing…</span></div>` }) + `</div>` + cap;
      if (key) return `<div class="vc-diag" data-w="canvas.diagram"><div class="vc-th"><i class="dot${live ? ' on' : ' wait'}"></i><b>${esc(title)}</b><span class="mono">mermaid · ${src.split('\n').length} lines</span><span class="sp"></span>`
        + '<button class="ib" data-act="dgopen" title="Open it in the chat — the chat\'s own diagram pop-out">Open in the chat ↗</button><button class="ib" data-act="dgcopy" title="Copy the mermaid source">copy</button><button class="ib" data-act="dgsrc" title="Show the source">source</button></div>'
        + `<div class="vc-live" data-live="mermaid" data-key="${esc(key)}" data-title="${esc(title)}"><span class="vc-dim">drawing…</span></div><pre class="vc-pre vc-dgsrc" hidden><code>${esc(src)}</code></pre></div>` + cap;
      return `<vera-mermaid class="vc-mm" bare title="${esc(title)}">${esc(src)}</vera-mermaid>` + cap;
    },

    /* the caption is the card's title when the image has no title of its own (blockTitle): then it is not drawn twice */
    image: c => `<figure class="vc-fig">
        <img src="${esc(c.url || '')}" alt="${esc(c.alt || '')}" loading="lazy">
        ${c.caption && c.title && c.caption !== c.title ? `<figcaption>${esc(c.caption)}</figcaption>` : ''}</figure>`,

    note: c => `<div class="vc-note">${esc(c.text || '')}
        ${c.author ? `<span class="vc-by">— ${esc(c.author)}</span>` : ''}</div>`,

    /* MANY RECORDS, TO LOOK THROUGH. A web search, the news, a research run's sources, what the fabric holds -
       each is a list you browse, not a page you read, and landing them as a dozen separate items buried the
       canvas while cutting the list at ten. This is one item that holds the whole list and lets you move in it:
       a filter box, the domains as facets, sort (as ranked · newest · title), three views (list · cards · table),
       pages sized to the item, and a record that OPENS in place - a page's text through browser.content, a
       fabric record's text through memory.read in pages - or lands as its own source item. The browser's state
       (page, filter, sort, view, what is open) is the element's, per item: it does not write the canvas on every
       click; what is fetched is kept for the session's look. */
    records: (c, size, key, el) => {
      const all = Array.isArray(c.items) ? c.items.filter((r) => r && typeof r === 'object') : [];
      const st = recState(el, key);
      // the view the item asks for (the fabric's: graph) is where it starts; the reader's own choice wins after that
      if (!st._v0) { st._v0 = true; if (c.view) st.view = String(c.view); }
      /* WHAT THE PAGES NAME: each row's page's entities, from the reader cache (_srcPrefetch fills them, in memory - never
         the web), as facets beside the sites - the people, organisations and places two or more of the pages name.
         Choosing one shows the pages that name it; each row carries its own top three; and the shared names make a
         graph of the pages and what links them. */
      const ents = (el && el._recEnts && el._recEnts[String(key)]) || {};
      const entsOf = (r) => ents[String(r.id)] || [];
      const entN = {}; all.forEach((r) => entsOf(r).forEach((e) => { entN[e[0]] = (entN[e[0]] || 0) + 1; }));
      const entList = Object.keys(entN).filter((n) => entN[n] >= 2).sort((a, b) => entN[b] - entN[a]).slice(0, size === 'xl' ? 12 : 6);
      const canGraph = c.kind === 'memory' || c.view === 'graph' || entList.length > 0;
      if (st.view === 'graph' && !canGraph) st.view = 'list';
      const q = String(st.q || '').toLowerCase().trim();
      let rows = all.filter((r) => (!st.ent || entsOf(r).some((e) => e[0] === st.ent)) && (!st.dom || r.domain === st.dom) && (!q || (String(r.title || '') + ' ' + String(r.snippet || '') + ' ' + String(r.domain || '') + ' ' + JSON.stringify(r.meta || {})).toLowerCase().includes(q)));
      if (st.sort === 'newest') rows = rows.slice().sort((a, b) => (Date.parse(b.when) || 0) - (Date.parse(a.when) || 0));
      else if (st.sort === 'title') rows = rows.slice().sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
      const per = st.view === 'cards' ? ({ s: 4, m: 6, l: 9, xl: 18 }[size] || 6) : ({ s: 4, m: 6, l: 10, xl: 20 }[size] || 8);
      const pages = Math.max(1, Math.ceil(rows.length / per)); const page = Math.min(Math.max(0, st.page | 0), pages - 1);
      const shown = rows.slice(page * per, page * per + per);
      const K = esc(key || '');
      const icon = { web: '\u{1F310}', news: '\u{1F4F0}', memory: '◈', research: '\u{1F50E}', evidence: '\u{1F50E}' }[c.kind] || '☰';
      const doms = {}; all.forEach((r) => { if (r.domain) doms[r.domain] = (doms[r.domain] || 0) + 1; });
      const domList = Object.keys(doms).sort((a, b) => doms[b] - doms[a]).slice(0, size === 'xl' ? 12 : 6);
      const hue = (s) => { let h = 0; for (const ch of String(s || '')) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; };
      /* a source's badge in one of the theme's category colours (--dv1..7), the same site always the same colour */
      /* a lookup's evidence carries the number the reply cites it by: that is its badge (see the evidence strip below) */
      const badge = (r) => c.kind === 'evidence' && r.n ? `<span class="vc-rbx-fav vc-rbx-num${r.kind === 'intel' ? ' intel' : ''}" title="cited as [${esc(r.n)}]${r.kind === 'intel' ? ' - from a tracked topic' : ''}">${esc(r.n)}</span>` : badge0(r);
      const badge0 = (r) => { const d = r.domain || (r.meta && (r.meta.dataset_id || r.meta.source)) || c.kind || '?'; const h = hue(d); return `<span class="vc-rbx-fav" style="--dvc:var(--dv${1 + (h % 7)},hsl(${h} 45% 60%))">${esc(String(d).replace(/^www\./, '').charAt(0).toUpperCase())}</span>`; };
      const ago = (t) => { const x = Date.parse(t); if (!isFinite(x)) return String(t || '').slice(0, 10); const s = (Date.now() - x) / 1000; return s < 3600 ? Math.max(1, Math.round(s / 60)) + 'm' : s < 172800 ? Math.round(s / 3600) + 'h' : Math.round(s / 86400) + 'd'; };
      const actBtn = (act, id, label, t) => `<button class="vc-rbx-b" data-rec-act="${act}" data-rec-key="${K}" data-rec-arg="${esc(id)}" title="${esc(t || '')}">${label}</button>`;
      /* A RECORD, OPENED, WITH ITS DEPTH (owner, 2026-09-29: the web results need "better depth and rendering of results &
         their text"; the fabric's "doesnt show enough depth on each record i.e. its neighbors, meta data"). Its facts -
         where it came from, when, how relevant, its tags, its fields; the page's own text when the answer carried it
         (web.research reads its pages); a page in reader mode; a fabric record read in full and its NEIGHBOURS - the
         records nearest it across every dataset, each readable in place, and drawn into the graph. */
      const paras = (s) => String(s || '').replace(/\r/g, '').replace(/[ \t]+\n/g, '\n').replace(/\n{1,}/g, '\n\n').trim();
      const opened = (r) => {
        const b = (st.body || {})[r.id] || {};
        const isRec = !!(r.ref && r.ref.record_id);
        /* a page in the list does everything a source item does (see ONE PAGE VIEW): reader mode, its picture, its explode,
           to the canvas, open - and once read, who wrote it, what it names, where it points */
        const acts = `<button class="vc-rbx-b rb-back" data-rec-act="open" data-rec-key="${K}" data-rec-arg="${esc(r.id)}" title="close the page - the card goes back where it was">← back</button>` + (r.url ? actBtn('read', r.id, b.reader || b.text ? 'read again' : 'reader mode', 'the page as the article it is - its body (or a composite of its parts) formatted (browser.reader)')
            + actBtn('shot', r.id, b.shot ? 'hide the picture' : 'see the page', 'a picture of the page as it looks (browser.screenshot)')
            + ((b.reader && b.reader.markdown) || b.text ? actBtn('xpl', r.id, 'explode', 'a structured diagram of the page - its sections, what it names, how they connect') : '')
            + actBtn('land', r.id, 'to the canvas', 'land it as its own source item') + `<a class="vc-rbx-b" href="${esc(r.url)}" target="_blank" rel="noopener">open ↗</a>` : '')
          + (isRec ? actBtn('rec', r.id, b.text ? 'read again' : 'read the record', 'the record in full, in pages (fabric.record.get)')
                   + actBtn('nb', r.id, b.nb ? 'neighbours \u21bb' : 'neighbours', 'the records nearest this one, across every dataset (fabric.loom.record_match)') : '');
        const facts = []; const fact = (k, vHtml) => { if (vHtml != null && vHtml !== '') facts.push(`<span><i>${esc(k)}</i>${vHtml}</span>`); };
        if (r.domain) fact('site', esc(r.domain));
        if (r.when) fact('when', esc(String(r.when).slice(0, 19).replace('T', ' ')));
        if (r.score != null && isFinite(+r.score) && +r.score > 0) fact('relevance', esc(Math.round(+r.score * 100) + '%'));   // no score is not a score of 0
        Object.keys(r.meta || {}).forEach((k) => fact(k, esc(r.meta[k])));
        const rec = b.rec || null;
        if (rec) { const s = rec.source || {}; if (s.url) fact('source', `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label || s.url)}</a>`); else if (s.label) fact('source', esc(s.label));
          if (rec.created_at) fact('written', esc(String(rec.created_at).slice(0, 19))); if (rec.total_chars) fact('length', esc(rec.total_chars > 1000 ? (rec.total_chars / 1000).toFixed(1) + 'k chars' : rec.total_chars + ' chars')); }
        const tags = (Array.isArray(r.tags) && r.tags.length ? r.tags : (rec && rec.tags) || []).slice(0, 12);
        const fields = rec && rec.data && typeof rec.data === 'object' ? Object.keys(rec.data) : [];
        /* the same chunk three times over (a document indexed in overlapping pieces) is ONE neighbour, counted */
        const nbList = []; if (Array.isArray(b.nb)) { const bySig = {}; b.nb.forEach((m) => { const sg = String(m.snippet || m.id).replace(/\s+/g, ' ').trim().slice(0, 80).toLowerCase(); if (bySig[sg]) { bySig[sg].n++; return; } const x = Object.assign({ n: 1 }, m); bySig[sg] = x; nbList.push(x); }); }
        const nbHtml = Array.isArray(b.nb) ? `<div class="vc-rbx-nb"><h5>nearest records \u00b7 ${nbList.length}</h5>` + (nbList.length ? nbList.map((m) => {
            const on = !!(b.nbOpen || {})[m.id]; const nt = (b.nbText || {})[m.id];
            const head = String(m.snippet || m.id).replace(/\s+/g, ' ').trim();
            return `<div class="vc-rbx-nbr${on ? ' on' : ''}" data-rec-act="nbopen" data-rec-key="${K}" data-rec-arg="${esc(r.id + '|' + m.id)}">`
              + `<div class="vc-rbx-nbh"><b>${esc(head.slice(0, 90))}</b>${m.n > 1 ? `<small title="the same text in ${m.n} records">\u00d7${m.n}</small>` : ''}<small>${esc(m.dataset_id || '')}</small></div>`
              + (on ? (nt ? `<div class="vc-rbx-nbt">${mdx(paras(String(nt).slice(0, 6000)))}</div>` : `<div class="vc-rbx-busy">\u2026 reading</div>`) : '')
              + `</div>`; }).join('') : '<div class="vc-rbx-empty">nothing near it</div>') + `</div>` : '';
        return `<div class="vc-rbx-open">${r.snippet && !r.text ? `<div class="vc-rbx-full">${esc(r.snippet)}</div>` : ''}`
          + (facts.length ? `<div class="vc-rbx-meta">${facts.join('')}</div>` : '')
          + (tags.length ? `<div class="vc-rbx-tags">${tags.map((x) => `<span>#${esc(x)}</span>`).join('')}</div>` : '')
          + (fields.length ? `<details class="vc-rbx-fields"><summary>fields \u00b7 ${fields.length}</summary><div class="vc-rbx-kv">${fields.map((k) => `<span class="k">${esc(k)}</span><span class="v">${esc(rec.data[k] == null ? '' : String(rec.data[k]))}</span>`).join('')}</div></details>` : '')
          + (b.reader ? pageExtras(b.reader, r.domain || '') : '')   /* who wrote it, what it names, where it points - as a source item shows */
          + `<div class="vc-rbx-acts">${acts}</div>`
          + (b.shot ? `<img class="vc-src-shot" src="${esc(b.shot)}" alt="${esc(r.title || r.url || '')}">` : '')
          + (b.busy ? `<div class="vc-rbx-busy">… ${esc(b.busy)}</div>` : '')
          + (b.err ? `<div class="vc-rbx-err">${esc(b.err)}</div>` : '')
          + (b.reader ? `<div class="vc-rbx-body rd">${readerHtml(b.reader)}</div>`
             : r.text ? `<div class="vc-rbx-body rd"><div class="vc-reader"><div class="vc-rd-body">${mdx(paras(String(r.text).slice(0, 20000)))}</div></div></div>` : '')
          + (b.text ? `<div class="vc-rbx-body rd"><div class="vc-reader"><div class="vc-rd-body">${mdx(paras(String(b.text).slice(0, 60000)))}</div></div></div>` + (b.next != null ? actBtn('more', r.id, 'more of it ↓', 'the next page of the record') : '') : '')
          + nbHtml
          + `</div>`;
      };      const title = (r) => r.url ? `<a class="vc-rbx-t" href="${esc(r.url)}" target="_blank" rel="noopener" data-rec-stop="1">${esc(r.title || r.url)}</a>` : `<span class="vc-rbx-t">${esc(r.title || r.id)}</span>`;
      const score = (r) => r.score != null ? `<span class="vc-rbx-sc" title="relevance ${esc(r.score)}"><i style="width:${Math.round(Math.max(0, Math.min(1, +r.score)) * 100)}%"></i></span>` : '';
      let body = '';
      if (!all.length) body = `<div class="vc-rbx-empty">${c.running ? 'searching - the pages appear here as they are found' : 'nothing in it'}</div>`;
      else if (!rows.length) body = `<div class="vc-rbx-empty">nothing matches “${esc(st.q || st.dom)}”</div>`;
      else if (st.view === 'graph') body = `<div class="vc-rbx-graph"><div class="vc-live" data-live="rgraph" data-key="${K}"><span class="vc-dim">drawing the graph…</span></div><div class="vc-rbx-gnote">a record's node opens it in the list; a name, the pages that name it</div></div>`;
      else if (st.view === 'table') {
        body = `<div class="vc-tablewrap"><table class="vc-table vc-rbx-tbl"><thead><tr><th>title</th><th>source</th><th>when</th>${all.some((r) => r.score != null) ? '<th>score</th>' : ''}</tr></thead><tbody>`
          + shown.map((r) => `<tr class="${(st.open || {})[r.id] ? 'on' : ''}" data-rec-act="open" data-rec-key="${K}" data-rec-arg="${esc(r.id)}"><td>${title(r)}</td><td class="mono">${esc(r.domain || (r.meta && (r.meta.dataset_id || r.meta.engine)) || '')}</td><td class="mono">${esc(r.when ? ago(r.when) : '')}</td>${all.some((x) => x.score != null) ? `<td>${score(r)}</td>` : ''}</tr>` + ((st.open || {})[r.id] ? `<tr class="vc-rbx-tr-open"><td colspan="4">${opened(r)}</td></tr>` : '')).join('')
          + `</tbody></table></div>`;
      } else {
        body = `<div class="vc-rbx-${st.view === 'cards' ? 'cards' : 'list'}">` + shown.map((r) => {
          const on = !!(st.open || {})[r.id];
          return `<div class="vc-rbx-row${on ? ' on' : ''}" data-rec-act="open" data-rec-key="${K}" data-rec-arg="${esc(r.id)}">`
            + `<div class="vc-rbx-hd">${badge(r)}<div class="vc-rbx-tt">${title(r)}${entsOf(r).length ? `<div class="vc-rbx-ents">${entsOf(r).slice(0, 3).map((e) => `<span title="${esc(e[1])}">${esc(e[0])}</span>`).join('')}</div>` : ''}<div class="vc-rbx-sub">${r.domain ? `<span>${esc(r.domain)}</span>` : ''}${r.when ? `<span>${esc(ago(r.when))}</span>` : ''}${r.meta && r.meta.engine ? `<span>${esc(r.meta.engine)}</span>` : ''}${r.meta && r.meta.dataset_id ? `<span>${esc(r.meta.dataset_id)}</span>` : ''}${score(r)}</div></div></div>`
            + (!on && r.snippet ? `<div class="vc-rbx-s">${esc(String(r.snippet).slice(0, 400))}</div>` : '')
            + (on ? opened(r) : '') + `</div>`;
        }).join('') + `</div>`;
      }
      /* A RESEARCH RUN AT A GLANCE (a web or research answer): where its pages came from - one bar, each site its share,
         a site's part choosing it - and, when the pages carry dates, when they were written, each a dot that opens its page */
      let glance = '';
      /* A LOOKUP'S EVIDENCE (info.seek): what was asked and how - the need, since when, whether it could be answered, the
         topic it tracks - over the pages; what it checked and the gaps it could not fill under them */
      const ev = c.kind === 'evidence' && c.evidence && typeof c.evidence === 'object' ? c.evidence : null;
      const evChip = (k, v, cls) => `<span class="vc-ev-chip${cls ? ' ' + cls : ''}"><i>${esc(k)}</i>${esc(v)}</span>`;
      /* still looking (the chat's lookup item, before the answer): what it is doing now, how long, a moving line */
      const secs = ev && ev.started ? Math.max(0, Math.round((Date.now() - +ev.started) / 1000)) : 0;
      const evRun = ev && c.running ? `<div class="vc-ev-run"><span class="vc-rr-dot"></span><b>${esc(ev.stage || 'looking it up')}</b><span class="t">${secs ? secs + ' s' : ''}</span></div><div class="vc-rr-bar"><i></i></div>` : '';
      const evHead = ev ? `<div class="vc-ev${c.running ? ' running' : ''}">${evRun}${ev.question && ev.question !== ev.subject ? `<div class="vc-ev-q">${esc(ev.question.slice(0, 200))}</div>` : ''}<div class="vc-ev-chips">`
        + (ev.need ? evChip('need', ev.need) : '') + (ev.since ? evChip('since', String(ev.since).slice(0, 10)) : '')
        + (ev.tracked ? evChip('tracked', ev.tracked) : '') + (c.running ? (ev.cap ? evChip('', ev.cap) : '') : evChip('', ev.answerable ? 'answerable' : 'not answered', ev.answerable ? 'ok' : 'bad'))
        + (ev.ms ? evChip('took', (ev.ms / 1000).toFixed(ev.ms < 10000 ? 1 : 0) + ' s') : '') + (ev.job ? evChip('report', 'job ' + ev.job) : '') + `</div></div>` : '';
      const evFoot = ev && ((ev.checked || []).length || (ev.gaps || []).length) ? `<details class="vc-ev-foot"${(ev.gaps || []).length ? ' open' : ''}><summary>checked ${(ev.checked || []).length}${(ev.gaps || []).length ? ' · <b>' + ev.gaps.length + ' gap' + (ev.gaps.length === 1 ? '' : 's') + '</b>' : ''}</summary>`
        + ((ev.gaps || []).length ? `<ul class="vc-ev-gaps">${ev.gaps.map((g) => `<li>${esc(g)}</li>`).join('')}</ul>` : '')
        + `<ul class="vc-ev-chk">${(ev.checked || []).map((g) => `<li>${esc(g)}</li>`).join('')}</ul></details>` : '';
      if ((c.kind === 'research' || c.kind === 'web' || c.kind === 'evidence') && all.length > 2) {
        const sites = Object.keys(doms).sort((a, b) => doms[b] - doms[a]); const top = sites.slice(0, 10); const rest = all.length - top.reduce((s, d) => s + doms[d], 0);
        const map = sites.length > 1 ? `<div class="vc-rbx-map" title="where the pages came from">${top.map((d) => `<button class="${st.dom === d ? 'on' : ''}" style="flex:${doms[d]};--h:${hue(d)}" data-rec-act="dom" data-rec-key="${K}" data-rec-arg="${esc(d)}" title="${esc(d)} · ${doms[d]}"><span>${esc(d.replace(/^www\./, ''))}</span></button>`).join('')}${rest > 0 ? `<i style="flex:${rest}" title="${rest} more"></i>` : ''}</div>` : '';
        const dated = all.map((r) => [r, Date.parse(r.when)]).filter((x) => isFinite(x[1]));
        let tl = '';
        if (dated.length >= 2) {
          const t0 = Math.min(...dated.map((x) => x[1])), t1 = Math.max(...dated.map((x) => x[1])), span = Math.max(1, t1 - t0);
          const day = (t) => new Date(t).toISOString().slice(0, 10);
          tl = `<div class="vc-rbx-tl" title="when the pages were written"><span>${day(t0)}</span><div class="vc-rbx-tlb">${dated.map(([r, t]) => `<button style="left:${(100 * (t - t0) / span).toFixed(1)}%;--h:${hue(r.domain || '')}" data-rec-act="goto" data-rec-key="${K}" data-rec-arg="${esc(r.id)}" title="${esc(day(t) + ' · ' + (r.title || r.url || ''))}"></button>`).join('')}</div><span>${day(t1)}</span></div>`;
        }
        if (map || tl) glance = `<div class="vc-rbx-glance">${map}${tl}</div>`;
      }
      const seg = (name, opts) => `<span class="vc-rbx-seg seg-${name}">${opts.map((o) => `<button class="${(st[name] || opts[0][0]) === o[0] ? 'on' : ''}" data-rec-act="${name}" data-rec-key="${K}" data-rec-arg="${o[0]}" title="${esc(o[2] || '')}">${o[1]}</button>`).join('')}</span>`;
      const from = rows.length ? page * per + 1 : 0, to = Math.min(rows.length, page * per + per);
      const dots = pages > 1 ? Array.from({ length: Math.min(pages, 9) }, (_, i) => { const n = pages <= 9 ? i : Math.round(i * (pages - 1) / 8); return `<button class="vc-rbx-dot${n === page ? ' on' : ''}" data-rec-act="page" data-rec-key="${K}" data-rec-arg="${n}" title="page ${n + 1}"></button>`; }).join('') : '';
      return `<div class="vc-rbx k-${esc(c.kind || 'rows')} s-${esc(size || 'm')}">`
        + `<div class="vc-rbx-top"><span class="vc-rbx-ic">${icon}</span><div class="vc-rbx-h"><b>${esc(c.title || 'Records')}</b><small>${all.length} ${all.length === 1 ? 'record' : 'records'}${c.source ? ' · ' + esc(c.source) : ''}${c.why ? ' · ' + esc(c.why) : ''}</small></div></div>`
        + evHead + glance
        + `<div class="vc-rbx-tools"><input class="vc-rbx-q" data-rec-q="${K}" placeholder="filter ${all.length}…" value="${esc(st.q || '')}">`
        /* WHAT THE PAGES NAME AND WHERE THEY CAME FROM, BEHIND A MENU (owner, 2026-10-11: "it has a list of names and counts
           ... this could be displayed as a widget and in a menu rather than upfront as pills"): one button, and under it two
           ranked-bar widgets (the widget system's own drawing) - the names the pages share and the sites - a bar choosing
           that filter. The choice in force shows on the button. */
        + ((entList.length || domList.length > 1) ? `<button class="vc-rbx-b vc-rbx-nbtn${st.namesOpen ? ' on' : ''}${st.ent || st.dom ? ' set' : ''}" data-rec-act="names" data-rec-key="${K}" data-rec-arg="" title="the names the pages share and the sites they came from - a choice filters the list">${esc(st.ent || st.dom || 'names · sites')}${st.ent || st.dom ? ' ✕' : ' ▾'}</button>` : '')
        + `${seg('sort', [['rank', 'ranked', 'as the source ranked them'], ['newest', 'newest'], ['title', 'A–Z']])}${seg('view', (canGraph ? [['graph', '◈', 'graph - the query, the datasets, the records, the tags and names they share']] : []).concat([['list', '☰', 'list'], ['cards', '▦', 'cards'], ['table', '☷', 'table']]))}</div>`
        + (st.namesOpen ? (() => { const wjson = (title, rows) => esc(JSON.stringify({ form: 'ranked', title, data: rows }));
            return `<div class="vc-rbx-names" data-rec-key="${K}" data-w="canvas.records.names">`
              + (entList.length ? `<div class="vc-rbx-nw" data-pick="ent"><h5>names the pages share</h5><vera-widget bare size="m" record="${wjson('Names', entList.map((n) => ({ name: n, value: entN[n] })))}"></vera-widget></div>` : '')
              + (domList.length > 1 ? `<div class="vc-rbx-nw" data-pick="dom"><h5>sites</h5><vera-widget bare size="m" record="${wjson('Sites', domList.map((d) => ({ name: d, value: doms[d] })))}"></vera-widget></div>` : '')
              + `</div>`; })() : '')
        + (entList.length ? `<div class="vc-rbx-facets vc-rbx-efacets"><span class="vc-rbx-fl">names</span>${entList.map((n) => `<button class="${st.ent === n ? 'on' : ''}" data-rec-act="ent" data-rec-key="${K}" data-rec-arg="${esc(n)}">${esc(n)}<i>${entN[n]}</i></button>`).join('')}</div>` : '')
        + (domList.length > 1 ? `<div class="vc-rbx-facets">${domList.map((d) => `<button class="${st.dom === d ? 'on' : ''}" data-rec-act="dom" data-rec-key="${K}" data-rec-arg="${esc(d)}">${esc(d)}<i>${doms[d]}</i></button>`).join('')}</div>` : '')
        + body + evFoot
        + `<div class="vc-rbx-foot"><span>${from}–${to} of ${rows.length}${rows.length !== all.length ? ' (of ' + all.length + ')' : ''}</span><span class="vc-rbx-pg">${pages > 1 ? `<button data-rec-act="page" data-rec-key="${K}" data-rec-arg="prev" ${page ? '' : 'disabled'}>‹</button>${dots}<button data-rec-act="page" data-rec-key="${K}" data-rec-arg="next" ${page < pages - 1 ? '' : 'disabled'}>›</button>` : ''}</span>${c.next && c.next.cap ? actBtn('loadmore', '', 'more from ' + esc(c.next.cap), 'fetch the next page from the source') : ''}</div>`
        + `</div>`;
    },

    table: (c, size, key, el) => {
      const cols = c.columns || [];
      const rows = c.rows || [];
      /* the table's other views (see VIEWS): each row a card (its first cell the title, the rest named), a list (the first
         cell, the rest on a line under it), or a chart of its first column of numbers */
      const view = rows.length ? viewOf(el, key, tableDefault(c, el)) : 'table';
      if (view === 'graph' && tableCats(c).length) return graphSlot(key) + (c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : '');
      const cell = (v) => esc(v == null ? '' : String(v));
      if (view === 'cards' || view === 'list') {
        return `<div class="vc-tv vc-tv-${view}">` + rows.filter(Array.isArray).map((r) => `<div class="vc-tv-r"><b class="vc-tv-t">${cell(r[0])}</b>`
          + (view === 'cards' ? `<dl>${r.slice(1).map((v, j) => `<dt>${cell(cols[j + 1] || '')}</dt><dd>${cell(v)}</dd>`).join('')}</dl>` : `<span class="vc-tv-s">${r.slice(1).map((v, j) => (cols[j + 1] ? `<i>${cell(cols[j + 1])}</i> ` : '') + cell(v)).join(' · ')}</span>`)
          + `</div>`).join('') + `</div>` + (c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : '');
      }
      if (view === 'chart') { const j = numCol(c);
        if (j > 0) { const pts = rows.filter(Array.isArray).map((r) => [String(r[0] == null ? '' : r[0]), parseFloat(String(r[j]).replace(/[,%$£€]/g, ''))]).filter((p) => isFinite(p[1]));
          const mx = Math.max(...pts.map((p) => Math.abs(p[1])), 1e-9);
          return `<div class="vc-tv vc-tv-chart"><div class="vc-tv-ch">${esc(cols[j] || '')}</div>` + pts.map((p) => `<div class="vc-tv-bar"><span class="l" title="${esc(p[0])}">${esc(p[0])}</span><span class="b"><i style="width:${(100 * Math.abs(p[1]) / mx).toFixed(1)}%"></i></span><span class="v">${esc(String(p[1]))}</span></div>`).join('') + `</div>`
            + (c.caption ? `<div class="vc-cap">${esc(c.caption)}</div>` : ''); } }
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
        /* A WIDGET IS DRIVABLE, the way a panel is. A panel item has had query and dispatch over the bridge all
           along; a widget was a picture of a capability's answer at the moment it landed, and a working area
           made of stale pictures is a scrapbook. Its record says which cap it reads and with what, so it can
           read it again - and the answer is written into the item, so the refresh survives a reload the way the
           source's page body and the calendar's month do. The button appears only when there IS a source: a
           widget drawn from inline data has nothing to go back to. */
        const rcap = (rec.reads && rec.reads.cap) ? String(rec.reads.cap) : '';
        return `<div class="vc-wid" data-w="canvas.widget"><div class="vc-live" data-live="widget" data-key="${esc(key)}" data-size="${esc(size || (rec.draw && rec.draw.size) || 'm')}"><span class="vc-dim">${esc(form)}…</span></div><div class="vc-cap mono">${esc(form)}${src ? ' · ' + esc(src) : ' · sample'}${rcap ? `<button class="vc-wid-b" data-wid-act="refresh" data-wid-key="${esc(key)}" title="Read ${esc(rcap)} again and redraw">refresh</button>` : ''}</div></div>`;
      }
      /* `root` rather than a bare `window`: the module is loaded as a script in the page AND required directly by
         the tests, and the branch above already guards `typeof customElements` for the same reason. A bare window
         here made the widget path the one drawing that could not be tested at all. */
      if (rec && root.VeraWidget && typeof root.VeraWidget.draw === 'function') {
        try {
          const out = root.VeraWidget.draw(form, rec.data, size || (rec.draw && rec.draw.size) || 'm');
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
      /* A COMMAND THAT RAN (a capability's command and its output, no shell open): what was run, where, and what it said -
         not a connect form over it. The form is for opening a shell; "open a shell" still does that, on the same host. */
      if (!attached && c.command && c.output != null) return `<div class="vc-term vc-ran" data-w="canvas.terminal">`
        + `<div class="vc-ran-cmd"><span class="ps">$</span><code>${esc(String(c.command))}</code>${shown ? `<span class="host">${esc(shown)}</span>` : ''}`
        + (ws ? '<button class="ib" data-act="tattach" title="Open a shell on this host">open a shell</button>' : '') + `</div>`
        + `<pre class="vc-pre vc-out"><code>${esc(String(c.output).slice(0, 8000)) || '<span class="vc-dim">(no output)</span>'}</code></pre></div>`;
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
      const id = String(c.panel || c.id || '').trim();
      /* the item's own address, else the one resolved for its panel (panelSrc above); while it resolves, a line saying so */
      const got = id && PANEL_SRC[id] != null ? PANEL_SRC[id] : null; const src = c.src || got || '';
      if (!c.src && id && got == null && el && el._panelResolve) el._panelResolve(id);
      const q = el && el._pq && el._pq[key];
      /* behind / front live in the panel's own head, always drawn: in the item's footer rail they were hidden until hover
         and, on a panel sent behind, under the other items (owner, 2026-10-07: "i dont see the behind or front buttons") */
      const lay = el && el.deskAlign && el.deskAlign() ? ((el._layer || {})[String(key)] || '') : null;
      const layBtns = lay === null ? '' : `<button class="ib${lay === 'behind' ? ' on' : ''}" data-act="layer-behind" title="Behind the desk: the panel fills the free part of the desk, under the other items - press again to put it back among them">behind</button><button class="ib${lay === 'front' ? ' on' : ''}" data-act="layer-front" title="In front: the panel fills the free part of the desk, over the other items (never over the chat) - press again to put it back among them">front</button>`;
      return `<div class="vc-panel" data-w="canvas.panel"><div class="vc-th"><i class="dot on"></i><b>${esc(c.title || c.label || id || 'panel')}</b><span class="mono">${esc(id)} · over the bridge</span><span class="sp"></span>`
        + layBtns + `<button class="ib" data-act="pquery" title="panel.query — what the panel holds">query</button><button class="ib" data-act="pdispatch" title="panel.dispatch — drive it">dispatch</button><button class="ib" data-act="prefresh" title="Reload the panel">refresh</button><button class="ib" data-act="pstand" title="Open it standalone">standalone ↗</button></div>`
        + (src ? `<div class="vc-live vc-frame" data-live="frame" data-key="${esc(key)}" data-src="${esc(src)}"></div>`
          : !id ? '<div class="vc-dim">no panel id</div>'
          : got == null ? `<div class="vc-dim vc-pwait">opening ${esc(c.title || id)}…</div>`
          : `<div class="vc-dim vc-pwait">${esc(c.title || id)} has no page that can open here.</div>`)
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
    /* WHAT A TURN IS DOING (owner, 2026-10-10: "need more useful desk elements for caps to display output or status as they
       run"): the capabilities the chat runs for a turn, each as it runs - a spinner while it works, then done or failed,
       how long it took and a line of what it answered. The chat writes it as the calls start and end. */
    activity: (c) => {
      const calls = Array.isArray(c.calls) ? c.calls.filter((x) => x && x.cap) : [];
      const run = calls.filter((x) => x.status === 'running').length, bad = calls.filter((x) => x.status === 'error').length;
      const st = run ? 'running' : bad ? 'error' : 'done';
      const when = (t) => { const d = new Date(+t); return isFinite(d) ? d.toTimeString().slice(0, 8) : ''; };
      const dur = (ms) => ms == null ? '' : ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + ' s';
      /* the card's header names it: the body opens with how it stands, not the title a second time */
      return `<div class="vc-act st-${st}" data-w="canvas.activity"><div class="vc-act-h"><span class="vc-act-dot"></span><b>${run ? 'working' : bad ? 'finished, with failures' : 'finished'}</b>`
        + `<span class="vc-act-n">${run ? run + ' running · ' : ''}${calls.length - run} of ${calls.length} done${bad ? ' · ' + bad + ' failed' : ''}</span></div>`
        + `<ol class="vc-act-l">${calls.map((x) => `<li class="${esc(x.status || 'running')}"><span class="ic"></span><div class="tx"><div class="ln"><code>${esc(x.cap)}</code>`
          + `<span class="t">${x.status === 'running' ? 'since ' + esc(when(x.started)) : esc(dur(x.ms))}</span></div>`
          + (x.args ? `<div class="ar">${esc(String(x.args).slice(0, 120))}</div>` : '') + (x.peek ? `<div class="pk">${esc(String(x.peek).slice(0, 200))}</div>` : '') + `</div></li>`).join('')}</ol></div>`;
    },

    /* A RESEARCH RUN, WHOLE (owner, 2026-10-10: "research.run on the desk is missing all of the detail it has in chat").
       The chat's research card shows how a run stands, its method (depth, style, the questions it will answer, its
       writing brief), every page it reads, the sources it cites and the report as it is written; the desk got a list of
       the pages and, at the very end, the report as a separate document. One item now holds the run: a status line with
       its step, then Report · Method · Pages · Sources. On a wide card the report is the main pane and the others sit
       beside it. The pages are the records browser (reader mode, the picture, explode, the sites and names it read).
       Written by the chat from the card's own state as the run streams, and kept, so a reload shows the run as it ended. */
    research: (c, size, key, el) => {
      const st = recState(el, key);
      const items = Array.isArray(c.items) ? c.items.filter((r) => r && typeof r === 'object') : [];
      const cites = (Array.isArray(c.cites) ? c.cites : []).filter((x) => x && x.url);
      const plan = c.plan && typeof c.plan === 'object' ? c.plan : null;
      const state = c.status === 'done' ? 'done' : c.status === 'error' ? 'error' : 'running';
      const K = esc(key || '');
      const secs = +c.secs || (c.started ? Math.max(0, Math.round((Date.now() - +c.started) / 1000)) : 0);
      const dur = secs ? (secs < 90 ? secs + ' s' : Math.floor(secs / 60) + ' min' + (secs % 60 ? ' ' + (secs % 60) + ' s' : '')) : '';
      const read = items.filter((r) => !(r.meta && r.meta.status)).length, failed = items.length - read;
      const report = String(c.report || ''), live = String(c.live || '');
      /* what it is doing, in words: the step it is on while it runs; how it ended once it has */
      const head = state === 'done' ? 'Research complete' : state === 'error' ? 'Research failed' : (c.writing ? 'Writing the report' : 'Researching');
      const VO = root.VeraOut; const draw = (s) => (VO && typeof VO.render === 'function' ? VO.render(s, { canvas: false }) : mdx(s));
      const chip = (k, v) => `<span class="vc-rr-chip"><i>${esc(k)}</i>${esc(v)}</span>`;
      const methodHtml = () => {
        if (!plan) return `<div class="vc-rr-wait">${state === 'running' ? 'planning the research…' : 'The run gave no method.'}</div>`;
        const qs = plan.sub_questions || plan.key_questions || [];
        let h = '<div class="vc-rr-chips">';
        if (plan.depth) h += chip('depth', plan.depth);
        if (plan.style || plan.output_style) h += chip('style', plan.style || plan.output_style);
        if (plan.focus || plan.scope_focus) h += chip('focus', plan.focus || plan.scope_focus);
        if (plan.needs_recency != null) h += chip('recency', plan.needs_recency ? 'required' : 'not required');
        (plan.source_priority || []).slice(0, 6).forEach((s) => { h += chip('source', s); });
        (plan.nlp_tools || []).slice(0, 8).forEach((s) => { h += chip('nlp', s); });
        h += '</div>';
        if (qs.length) h += `<h5>Questions it answers</h5><ol class="vc-rr-qs">${qs.slice(0, 16).map((q) => `<li>${esc(q)}</li>`).join('')}</ol>`;
        if (plan.writer_instructions) h += `<h5>Writing brief</h5><div class="vc-rr-brief">${esc(plan.writer_instructions)}</div>`;
        if ((plan.table_topics || []).length) h += `<h5>Tables</h5><div class="vc-rr-brief">${plan.table_topics.map(esc).join(' · ')}</div>`;
        return h;
      };
      const reportHtml = () => report ? `<div class="vc-rr-doc">${draw(report)}</div>`
        /* streamed formatted, as the chat draws it (owner, 2026-10-11: "the research streams in to the canvas / desk element
           but its unformatted") - the kit's renderer, not the canvas's small markdown subset */
        : live ? `<div class="vc-rr-doc vc-rr-live">${draw(live)}</div>`
        : `<div class="vc-rr-wait">${state === 'error' ? esc(c.error || 'The run stopped before it wrote a report.') : c.writing ? 'writing the report…' : 'gathering - the report is written once the pages are read'}</div>`;
      const sourcesHtml = () => cites.length
        ? `<ol class="vc-rr-src">${cites.map((s) => { let host = ''; try { host = new URL(s.url).host.replace(/^www\./, ''); } catch (e) { host = ''; }
            return `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || host || s.url)}</a><small>${esc(host || s.url)}</small></li>`; }).join('')}</ol>`
        : `<div class="vc-rr-wait">${state === 'running' ? 'the sources are named once the report cites them' : 'The report cites no sources.'}</div>`;
      const pagesHtml = () => items.length ? BLOCK.records(Object.assign({}, c, { kind: 'research', items }), size, key, el)
        : `<div class="vc-rr-wait">${state === 'running' ? 'no page read yet' : 'No page was read.'}</div>`;
      /* narrow: one pane at a time, by its tab. Wide (the container rule): the report is the main pane and the chosen one
         of the others sits beside it - the method while it runs, its pages once it is done */
      const tab = st.tab || 'report'; const side = st.side || (state === 'running' ? 'method' : 'pages');
      const T = [['report', 'Report'], ['method', 'Method'], ['pages', 'Pages', items.length], ['sources', 'Sources', cites.length]];
      /* WHERE THE SECOND PANE GOES (owner, 2026-10-11: "could it have the option to be in either of the divs underneath the
         main or the one to the right"): beside the report, or under it (then on a narrow card too, rather than one tab at a
         time). One choice for every run, kept in this browser. */
      let lay = 'right'; try { lay = localStorage.getItem('vera:canvas:rrlay') === 'below' ? 'below' : 'right'; } catch (e) { /* default */ }
      const layBtn = `<button class="vc-rr-lay" data-rec-act="rrlay" data-rec-key="${K}" data-rec-arg="${lay === 'below' ? 'right' : 'below'}" title="${lay === 'below' ? 'Put the second pane beside the report' : 'Put the second pane under the report'}">${lay === 'below' ? '⇥ beside' : '⤓ below'}</button>`;
      const tabs = `<div class="vc-rr-tabs">${T.map(([id, label, n]) => `<button class="vc-rr-tab${tab === id ? ' on' : ''}${side === id ? ' side' : ''}" data-rr-tab="${id}" data-rec-act="rrtab" data-rec-key="${K}" data-rec-arg="${id}">${label}${n != null ? `<i>${n}</i>` : ''}</button>`).join('')}<span class="vc-rr-sp"></span>${layBtn}</div>`;
      const pane = (id, html) => `<div class="vc-rr-p${tab === id ? ' on' : ''}${id === 'report' ? ' main' : side === id ? ' side' : ''}" data-p="${id}">${html}</div>`;
      return `<div class="vc-rr st-${state} lay-${lay}" data-w="canvas.research">`
        + `<div class="vc-rr-hd"><span class="vc-rr-dot"></span><b>${head}</b>`
        + (state === 'running' && c.step ? `<span class="vc-rr-step">${esc(String(c.step).slice(0, 90))}</span>` : '')
        + `<span class="vc-rr-n">${items.length ? read + ' page' + (read === 1 ? '' : 's') + ' read' + (failed ? ' · ' + failed + ' failed' : '') : ''}${cites.length ? ' · ' + cites.length + ' cited' : ''}${dur ? ' · ' + dur : ''}</span></div>`
        + (state === 'running' ? '<div class="vc-rr-bar"><i></i></div>' : '')
        + (c.query ? `<div class="vc-rr-q">${esc(String(c.query).slice(0, 240))}</div>` : '')
        + tabs
        + `<div class="vc-rr-body">${pane('report', reportHtml())}${pane('method', methodHtml())}${pane('pages', pagesHtml())}${pane('sources', sourcesHtml())}</div>`
        + (c.job ? `<div class="vc-rr-ft">job ${esc(String(c.job))}</div>` : '')
        + `</div>`;
    },


    /* WHAT A TURN NAMES (canvas-enrichment slice 1 - owner, 2026-10-03: "the nlp enrichment is still few and far
       between"). turn.understand's answer: the people, organisations, places, products, tickers, money and dates
       the reply named, as chips in their groups with how often each was named. Pressed, a chip opens under its
       group: the sentences it was named in, and "look it up" - the fabric's records and a web search for it,
       landed beside this item as browsers (turn.entity.look). */
    entities: (c, size, key, el) => {
      const groups = Array.isArray(c && c.groups) ? c.groups : [];
      if (!groups.length) return '<div class="vc-dim">nothing named</div>';
      const open = (el && el._entOpen && el._entOpen[key]) || '';
      /* a name as it reads: the markdown round it in the reply (`Phi`, **Qwen**, quotes) is not part of it */
      const entTxt = (t) => String(t || '').replace(/^[`'"*_\s]+|[`'"*_\s]+$/g, '') || String(t || '');
      if (viewOf(el, key, 'chips') === 'graph') return graphSlot(key);
      /* the names as a list (see VIEWS): one per row - the name, what it is, how often - each opening as its chip does */
      if (viewOf(el, key, 'chips') === 'list') return '<div class="vc-ent vc-ent-list" data-w="canvas.entities">' + groups.map((g, gi) => `<div class="vc-ent-gl">${esc(g.group || '')}</div>`
        + (Array.isArray(g.entities) ? g.entities : []).map((e, ei) => `<button class="vc-ent-row${open === gi + ':' + ei ? ' on' : ''}" data-act="entopen" data-ent="${gi}:${ei}"><b>${esc(e.text)}</b><span>${esc(e.type || '')}</span>${e.count > 1 ? `<i>${e.count}×</i>` : ''}</button>`).join('')).join('') + '</div>';
      return '<div class="vc-ent" data-w="canvas.entities">' + groups.map((g, gi) => {
        const ents = Array.isArray(g.entities) ? g.entities : [];
        const chips = ents.map((e, ei) => { const id = gi + ':' + ei;
          return `<button class="vc-chip${open === id ? ' on' : ''}" data-act="entopen" data-ent="${id}" title="${esc(e.type + (e.count > 1 ? ' · named ' + e.count + ' times' : ''))}">${esc(entTxt(e.text))}${e.count > 1 ? `<i>${e.count}</i>` : ''}</button>`; }).join('');
        const [ogi, oei] = open.split(':').map(Number); const oe = (ogi === gi && ents[oei]) || null;
        const detail = oe ? `<div class="vc-ent-d"><div class="vc-ent-h"><b>${esc(oe.text)}</b><span class="mono">${esc(oe.type)}</span><span class="sp"></span>`
          + `<button class="ib" data-act="entlook" data-ent="${gi}:${oei}" title="The fabric's records and a web search for it, beside this item">look it up</button></div>`
          + (Array.isArray(oe.sentences) && oe.sentences.length ? oe.sentences.map((s) => `<p>${esc(s)}</p>`).join('') : '') + '</div>' : '';
        return `<div class="vc-ent-g"><div class="vc-ent-gl">${esc(g.group || '')}</div><div class="vc-ent-cs">${chips}</div>${detail}</div>`;
      }).join('') + '</div>';
    },
    /* ── elements for what a capability answers with (canvas.run, and the chat's adapter) ────────────────────── */
    // a nested answer as a tree you can open and close, rather than a wall of braces in a code fence
    json: (c) => {
      const data = c && c.data !== undefined ? c.data : c;
      return '<div class="vc-json" data-w="canvas.json">' + (c && c.title ? '<div class="vc-json-t">' + esc(c.title) + '</div>' : '') + jsonTree(data, 0) + '</div>';
    },
    // a record: its fields, name beside value
    kv: (c, size, key, el) => {
      const f = (c && (c.fields || c.data)) || {};
      const rows = Array.isArray(f) ? f : Object.keys(f).map((k) => [k, f[k]]);
      if (rows.length >= 3 && viewOf(el, key, 'fields') === 'graph') return graphSlot(key);
      /* the card's header names it, so the body does not again; a field is named in words ("vram_used" is "Vram used"),
         its raw name kept as the tip. Narrow: name beside value. Wide (the container rule): tiles, the value large. */
      const say = (k) => { const ws = String(k).replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').split(/\s+/).filter(Boolean)
          .map((w) => (w.length > 1 && /^[A-Z0-9]+$/.test(w) ? w : w.toLowerCase())); const s = ws.join(' ');   /* an acronym (GPU, URL) stays as it is */
        return s ? s.charAt(0).toUpperCase() + s.slice(1) : String(k); };
      return '<div class="vc-kv" data-w="canvas.kv"><div class="vc-kv-g">'
        + rows.slice(0, 80).map((r) => { const v = r && r[1]; const s = v == null ? '\u2014' : (typeof v === 'object' ? JSON.stringify(v) : String(v)); const k = String(r && r[0]);
          return '<div class="kv-r' + (s.length > 48 ? ' long' : '') + '"><span class="kv-k" title="' + esc(k) + '">' + esc(say(k)) + '</span><span class="kv-v' + (v === true ? ' yes' : v === false ? ' no' : '') + (typeof v === 'number' ? ' num' : '') + '">' + esc(s.slice(0, 600)) + '</span></div>'; }).join('')
        + '</div></div>';
    },
    // an exchange: who asked, who answered - a model's or an agent's answer is a conversation, not a paragraph
    chat: (c) => {
      const ms = Array.isArray(c && c.messages) ? c.messages : [];
      return '<div class="vc-chat" data-w="canvas.chat">' + (c && c.title ? '<div class="vc-json-t">' + esc(c.title) + '</div>' : '')
        + (ms.slice(-40).map((m) => { const r = String((m && m.role) || 'assistant'); const t = String((m && (m.text || m.content)) || '');
          return '<div class="vc-msg ' + (r === 'user' ? 'u' : 'a') + '"><span class="vc-who">' + esc(String((m && m.name) || r)) + '</span><div class="vc-md">' + md(t) + '</div></div>'; }).join('')
          || '<div class="vc-dim">no messages</div>') + '</div>';
    },
    // several items from one answer (a search's pages): each drawn as its own kind, without per-item state
    _many: (c, size, key, el) => (((c && c.items) || []).map((m) => { const fn = BLOCK[m && m.kind] || BLOCK.note;
      try { return '<div class="vc-many">' + fn((m && m.content) || {}, 's', '', el) + '</div>'; } catch (e) { return ''; } }).join('')),
    /* A RESULT: the capability that ran, with what, how it went - and the answer drawn as the element that fits it.
       Keyed by the cap and its arguments, so "run again" updates this item instead of landing a second one. */
    result: (c, size, key, el) => {
      c = c || {}; const cap = String(c.cap || 'capability'); const ok = c.ok !== false && !c.error;
      const a = c.args && typeof c.args === 'object' ? c.args : {};
      const args = Object.keys(a).slice(0, 4).map((k) => k + '=' + String(typeof a[k] === 'object' ? JSON.stringify(a[k]) : a[k]).slice(0, 40)).join(' \u00b7 ');
      const ms = typeof c.ms === 'number' ? (c.ms >= 1000 ? (c.ms / 1000).toFixed(1) + ' s' : c.ms + ' ms') : '';
      const v = ok ? resultView(c) : null;
      const head = '<div class="vc-res-h"><i class="dot' + (ok ? ' on' : ' bad') + '"></i><code>' + esc(cap) + '</code><span class="mono">' + esc(args) + '</span><span class="sp"></span>'
        + (v ? '<span class="vc-badge">' + esc(v.kind === '_many' ? 'items' : v.kind) + '</span>' : '') + (ms ? '<span class="mono">' + esc(ms) + '</span>' : '')
        + (key ? '<button class="ib" data-act="rerun" title="Run it again - this item updates in place">run again</button>' : '') + '</div>';
      let inner = '';
      if (!ok) inner = '<div class="vc-res-err">' + esc(String(c.error || 'the capability failed')) + '</div>';
      else { const fn = BLOCK[v.kind]; try { inner = fn ? fn(v.content || {}, size, key, el) : jsonTree(c.result, 0); } catch (e) { inner = '<div class="vc-json">' + jsonTree(c.result, 0) + '</div>'; } }
      return '<div class="vc-res" data-as="' + esc(v ? v.kind : 'error') + '" data-w="canvas.result">' + head + inner + '</div>';
    },

    schedule: c => `<div class="vc-stub"><span class="vc-badge">when</span>
        ${esc(c.when || '')} — ${esc(c.what || '')}</div>`,

    /* AN HTML ITEM DRAWS, IN A SANDBOXED FRAME (owner, 2026-09-23: "html items preview by default"). It used to be
       injected straight into this element's shadow root as live markup — which renders, but inside the page's own
       origin, where an inline handler or an <img onerror> in something a capability answered with is running in the
       chat. The frame the code items already preview into is sandboxed with allow-scripts alone: no network, no
       cookies, no same-origin. Same drawing, none of that reach.
       The head carries the switch, and the Preview setting decides which way it starts. */
    html: (c, size, key, el) => {
      const src = String(c.html || '');
      /* A DELIVERED REPORT (owner, 2026-10-02: "html delivery to be able to go to the canvas instead of automatically
         opening a new tab"): report.canvas puts the report's address here, not its markup - the document Vera serves,
         drawn in a sandboxed frame (scripts for its diagrams; no same-origin, so it cannot reach the chat), with the
         tab and the download one press away. */
      const url = String(c.url || '');
      if (!src && url && /^\/[^/]/.test(url)) {
        const head = `<div class="vc-codehead">${esc(c.title || 'report')}<span class="sp"></span>`
          + `<a class="ib" href="${esc(url)}" target="_blank" rel="noopener" title="Open the report in its own tab">open</a>`
          + (c.download_url ? `<a class="ib" href="${esc(c.download_url)}" download title="Download the report">download</a>` : '') + '</div>';
        const body = key
          ? `<div class="vc-live vc-preview vc-report" data-live="report" data-key="${esc(key)}" data-src="${esc(url)}"><span class="vc-dim">opening the report…</span></div>`
          : `<div class="vc-dim"><a href="${esc(url)}" target="_blank" rel="noopener">open the report</a></div>`;
        return `<div class="vc-codewrap">${head}${body}</div>`;
      }
      if (!src) return '<div class="vc-dim">nothing to draw yet</div>';
      const pset = key && el && el._prevOn && Object.prototype.hasOwnProperty.call(el._prevOn, key);
      const pdef = !el || typeof el.previewOn !== 'function' || el.previewOn();
      const prev = key ? (pset ? !!el._prevOn[key] : pdef) : pdef;
      if (key && el) { el._prevSeen = el._prevSeen || {}; el._prevSeen[key] = prev; }
      // ...and Send to, as every card has it (owner, 2026-10-03: "the 'send to' menu ... integrated into the code and
      // markdown and html rendering")
      const VO = root.VeraOut;
      const send = key && VO && typeof VO.sendMenu === 'function'
        ? VO.sendMenu('cvh-' + key, { getMd: () => '```html\n' + src.replace(/\n$/, '') + '\n```', raw: () => src, title: String(c.title || 'page'), skip: ['podcast'] }) : '';
      const head = `<div class="vc-codehead">${esc(c.title || 'html')}<span class="sp"></span>`
        + (key ? `<button class="ib${prev ? ' on' : ''}" data-act="cprev" title="${prev ? 'Show the source' : 'Draw it here - a sandboxed frame, no network'}">${prev ? 'source' : 'preview'}</button>` : '')
        + send + '</div>';
      const body = prev && key
        ? `<div class="vc-live vc-preview" data-live="preview" data-key="${esc(key)}" data-lang="html"><span class="vc-dim">drawing…</span></div>`
        : `<pre class="vc-pre vc-code"><code>${esc(src)}</code></pre>`;
      return `<div class="vc-codewrap">${head}${body}</div>`;
    },
  };

  const CSS = `
  :host{display:block;margin:6px 0;
    font:12.5px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    color:var(--fg,#dce1e8)}
  /* the UI's own scrollbars (design.css) stop at a shadow root: the canvas's items scrolled with the browser's wide grey
     ones (owner, 2026-10-09: "minimalist in-theme scrollbars like the rest of the ui"). The same rules, in here. */
  *{scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--text,#d8dce4) 22%,transparent) transparent}
  ::-webkit-scrollbar{width:9px;height:9px}
  ::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--text,#d8dce4) 18%,transparent);border-radius:99px;border:2px solid transparent;background-clip:padding-box}
  ::-webkit-scrollbar-track{background:transparent}
  .wrap{border:1px solid var(--border,#2a2f37);border-radius:10px;
    background:var(--bg1,#15181d);overflow:hidden}
  /* Blocks off: the whole chat drops its surfaces and the session canvas has to go with it - the board turns the
     canvas column, its head and its items flat (Canvas.dc.html 655). A rule on <html> cannot cross a shadow root,
     so the host carries the tier and the element answers to it here (Notes/42 defect 85).
     AN ITEM LOSES ITS CONTAINER ENTIRELY (the canvas's final form §4.1; owner, 2026-09-22: "if blocks is off, they
     could loose their containers"). This used to keep a 55%-opacity border - the grounds went, the boxes stayed -
     and a column of empty rectangles is the thing blocks-off is meant to get rid of. What says where an item ends
     is now its own HEADER LINE, and its rail appears when you are on it. A hairline BETWEEN items is not available
     to us: on the stage items are absolutely placed and moved, so there is no "next item" to draw a line against. */
  :host([blocks="off"]) .wrap{background:transparent;border-color:transparent}
  :host([blocks="off"]) .head{background:transparent;border-bottom-color:color-mix(in srgb,var(--border,#2a2f37) 55%,transparent)}
  :host([blocks="off"]) .it{background:transparent;box-shadow:none;border-color:transparent}
  :host([blocks="off"]) .it > .it-hd{border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 45%,transparent)}
  :host([blocks="off"]) .it > .it-ft{opacity:0;transition:opacity .15s}
  :host([blocks="off"]) .it:is(:hover,.hov) > .it-ft,:host([blocks="off"]) .it:focus-within > .it-ft{opacity:1}
  /* the states that MEAN something keep their ring: blocks off is about grounds, not about hiding that this turn is
     waiting on you, that you opened an item, or that one is being dragged to a size */
  /* (no ring for the NOW band here either — see the note on .it.now below) */
  /* (no ring for an opened item here either — see .it.openin below) */
  :host([blocks="off"]) .it.hovopen{box-shadow:0 0 0 1.5px var(--acc,#5a9e8f),0 12px 30px -10px rgba(0,0,0,.6)}
  :host([blocks="off"]) .it.ghost{border:1px dashed color-mix(in srgb,var(--dim,#6b7480) 70%,transparent)}
  /* THE BANNER LOSES ITS GROUND TOO (owner, 2026-09-24: "the top session canvas header/control bar should loose
     its background if blocks mode is off"). It is sticky, so something has to keep it readable over whatever
     scrolls beneath — that is a blur rather than a plate: the colour underneath still shows, the text does not
     fight it. (The earlier note here argued for keeping the ground; the owner's call is the ground goes.) */
  :host([blocks="off"]) .addbar{background:none;backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);
    border-bottom-color:color-mix(in srgb,var(--border,#2a2f37) 40%,transparent)}
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
  .vc-md ol{margin:.35em 0;padding-left:1.5em}.vc-md li{margin:.12em 0}
  .vc-md blockquote{margin:.4em 0;padding:.1em 0 .1em .9em;border-left:3px solid var(--acc,#5a9e8f);color:var(--dim2,#8a7e70)}
  .vc-md hr{border:0;border-top:1px solid var(--border,#3a3530);margin:.9em 0}
  .vc-md figure{margin:.4em 0}.vc-md img{max-width:100%;height:auto;border-radius:6px}
  .vc-md a{color:var(--acc,#5a9e8f)}.vc-md .vc-tablewrap{margin:.4em 0}
  code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.92em}
  /* the parts an item draws for itself carry the same rule as the item: content, not chrome. Code keeps a ground
     because a monospace block IS a surface — but a hairline of one, not a boxed card inside a boxed card. */
  .vc-pre{background:color-mix(in srgb,var(--bg2,#1c2026) 55%,transparent);border:0;
    border-left:2px solid color-mix(in srgb,var(--border,#2a2f37) 80%,transparent);
    border-radius:0;padding:6px 9px;overflow-x:auto;margin:.3em 0}
  .vc-pre code{white-space:pre}
  /* a line of a code item: addressable, and lit when an explode card's span covers it */
  .vc-code .vc-line{display:block;padding-left:2px;border-left:2px solid transparent;transition:background .12s}
  .vc-code .vc-lno{display:inline-block;width:2.4em;margin-right:.5em;text-align:right;font-style:normal;
    color:var(--dim,#6b7480);opacity:.5;user-select:none;-webkit-user-select:none}
  .vc-code .vc-line.lit{background:color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent);
    border-left-color:var(--acc,#5a9e8f)}
  .vc-code .vc-line.lit .vc-lno{opacity:1;color:var(--acc,#5a9e8f)}
  .vc-code .vc-line.tap{background:color-mix(in srgb,var(--acc,#5a9e8f) 9%,transparent)}
  .vc-xp{display:flex;flex-direction:column;min-height:0}
  .vc-xp{flex:1 1 auto}
  .vc-xp .vc-live{flex:1 1 auto;height:auto;min-height:140px}
  .vc-dim{opacity:.75}
  .vc-out{max-height:200px;overflow:auto}
  .vc-codehead{font-size:9.5px;color:var(--dim,#6b7480);margin-bottom:2px;
    letter-spacing:.04em}
  /* a code item drawn as the shared kit's card: the item is already the frame, so the card is not a box inside it;
     its bar sits flush on the code, and the canvas's own line-numbered body keeps its look */
  .vc-codewrap > .code-card.vc-kit{margin:0;border:none;border-radius:0;background:transparent;overflow:visible}
  .vc-codewrap > .code-card.vc-kit > .code-bar{margin:0 0 4px;border-radius:var(--r-sm,5px);border-bottom:none}
  .vc-codewrap > .code-card.vc-kit > pre.vc-pre{max-height:none}
  /* the reading item's row (the shared prose row) under its text */
  .vc-prose-row{margin-top:6px;padding-top:4px;border-top:1px solid color-mix(in srgb,var(--border,#2a2f37) 60%,transparent)}
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
  /* THE ADD BAR'S MENUS OVER THE DESK (owner, 2026-10-10: "the canvas panel menu appears behind the desk elements and the
     note and terminal menus are see through"): a desk card stacks at 10-200 and the graph drawn over it with it, and the
     bar - its menus inside it - at 6, so cards covered the panel and terminal menus and a graph showed through the note
     menu. On the desk the bar, and so its menus, are over every card. */
  :host([align="desk"]:not([desk-narrow])) .addbar{z-index:240}
  .addbar .lbl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);margin-right:2px}
  /* THE ONE BANNER (§4.2): what this canvas is, the kinds you can add, then the host's own controls at the far end.
     The host's nodes are slotted, so they are styled by the host's stylesheet - only their PLACE is ours. */
  ::slotted([slot="banner-end"]){margin-left:auto}
  ::slotted([slot="banner-start"]){display:inline-flex;align-items:center;gap:6px;min-width:0;max-width:52%}
  /* ONE thing may claim the free space, and it is the controls at the end. The hidden-items button used to take it,
     which in a banner leaves it floating in the middle of the row; it belongs beside the kinds it restores. */
  .addbar .hidwrap{margin-left:6px}
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
  /* ⛔ AN ITEM IS ITS CONTENT, NOT A TILE (owner, 2026-09-24: "every item in the canvas is still displayed inside a
     box - id like the elements to feel more homogeneous instead of a rigid tile layout", "its still a rigid grid").
     No border, no ground, no radius, no shadow — BY DEFAULT, not when a setting says so. What separates one item
     from the next is its own caption line and the space around it, the way paragraphs are separated on a page. The
     rings that MEAN something (this turn is waiting on you · you opened it · it is a suggestion) still draw, and an
     item you are pointing at lifts a little; everything else is just what it holds. */
  .it{border:0;border-radius:0;background:none;margin:2px 0 10px;
    overflow:hidden;position:relative;display:flex;flex-direction:column;box-sizing:border-box;
    container-type:inline-size}   /* its own width is a query: a fused pair stacks when the card is too narrow to halve */
  .it:is(:hover,.hov){background:color-mix(in srgb,var(--bg2,#1c2026) 42%,transparent)}
  .it.pinned{box-shadow:inset 2px 0 0 0 color-mix(in srgb,var(--acc,#5a9e8f) 70%,transparent)}
  /* ⛔ "now" IS A BAND, NOT A STATE. Every live item carries it (the class is the item's state: now · pinned ·
     parked), so a ring on .it.now drew an amber box around EVERY item on the canvas — the "border" the owner kept
     reporting after the tile was already gone, and the reason this still read as a grid of cards. What is actually
     worth a ring is an item WAITING on you, and that is .it.waiting, which pulses one below. */
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
  /* A HEADER BELONGS TO FULL. In Hover and Zen an item is its content and nothing else; its name, its key and its
     controls come back when you are on it (owner, 2026-09-24: "they should only be displayed in the full view mode
     the hover and zen should act accordingly"). A FOLDED item is exempt: its header line is the whole item. */
  /* ...and held back WITHOUT leaving the layout (owner, 2026-10-01: the canvas "flicker ... still there if the mouse is
     between elements"). They were display:none until :hover, so pointing at an item near its top or its foot made it
     GROW by its header or its rail: the edge moved off the pointer, they went, the item shrank back under it - an
     oscillation at every item edge. Now each is an overlay - the header over the top, the rail over the foot - that fades
     in when you are on the item; an item's height never changes because you pointed at it. */
  /* (an item is already position:relative - absolute on the stage - so the overlays anchor to it either way) */
  :host([data-tier="hover"]) .it:not(.compact):not(.overfold) > .it-hd,:host([data-tier="zen"]) .it:not(.compact):not(.overfold) > .it-hd{position:absolute;left:0;right:0;top:0;z-index:4;opacity:0;pointer-events:none;transition:opacity .15s;background:var(--bg1,#1f1d1a);border-radius:var(--r,8px) var(--r,8px) 0 0}
  :host([data-tier="hover"]) .it:not(.compact):not(.overfold) > .it-ft,:host([data-tier="zen"]) .it:not(.compact):not(.overfold) > .it-ft{position:absolute;left:0;right:0;bottom:0;z-index:4;opacity:0;pointer-events:none;transition:opacity .15s;background:var(--bg1,#1f1d1a);border-radius:0 0 var(--r,8px) var(--r,8px)}
  :host([data-tier="hover"]) .it:not(.compact):not(.overfold):is(:hover,.hov,:focus-within) > :is(.it-hd,.it-ft),:host([data-tier="zen"]) .it:not(.compact):not(.overfold):is(:hover,.hov,:focus-within) > :is(.it-hd,.it-ft){opacity:1;pointer-events:auto}
  /* ...in bands of their own (owner, 2026-10-04: "the top and bottom bar appear over the top of the element instead of
     above and below - sometimes it can block entire parts of the elements"): an overlay at the item's edge covered its
     first and last lines - a short note's header and rail met in the middle and hid it whole. The item keeps their room
     all the time, so they fade in above and below its content, over nothing - and its height still never changes
     because you pointed at it (the reason they are overlays). */
  :host([data-tier="hover"]) .it:not(.compact):not(.overfold):has(> .it-hd:not(.own-fold)),:host([data-tier="zen"]) .it:not(.compact):not(.overfold):has(> .it-hd:not(.own-fold)){padding-top:var(--vc-hd-band,29px)}
  :host([data-tier="hover"]) .it:not(.compact):not(.overfold):has(> .it-ft),:host([data-tier="zen"]) .it:not(.compact):not(.overfold):has(> .it-ft){padding-bottom:var(--vc-ft-band,28px)}
  /* ONE BAR PER ITEM, ON HOVER (owner, 2026-10-04: "the top bar that appear on mouseover ... makes the static title at
     the top of each element pretty much redundant and those actions could be integrated so the static title is merged
     into the on-hover element and only appears on-hover. i think the canvas should better respect the full hover and zen
     modes too"). Full: every head, rail and control drawn. Hover and Zen: an item has ONE head and it comes with the
     pointer - the card's header for most items (their body's own title goes: a records item's heading, a widget's - the
     widget is drawn bare, the header carries its name); for the kinds that draw their own head (code, a diagram, a page,
     an explode, a terminal, a notebook cell, a panel) that head is the bar, holding its title and its actions, faded in
     with the fold and maximise controls. Each keeps its room, so nothing moves under the pointer. The canvas's own
     toolbar follows the tier too: Hover shows it while the pointer is on the canvas, Zen only while it is on the toolbar. */
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead > .it-bd > .vc-codewrap > .code-card > .code-bar,
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead > .it-bd > .vc-codewrap > .vc-codehead,
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead > .it-bd > :is(.vc-xp,.vc-term,.vc-nb,.vc-panel) > :is(.vc-codehead,.vc-th):first-child{opacity:0;transition:opacity .15s}
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead:is(:hover,.hov,:focus-within) > .it-bd > .vc-codewrap > .code-card > .code-bar,
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead:is(:hover,.hov,:focus-within) > .it-bd > .vc-codewrap > .vc-codehead,
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it.ownhead:is(:hover,.hov,:focus-within) > .it-bd > :is(.vc-xp,.vc-term,.vc-nb,.vc-panel) > :is(.vc-codehead,.vc-th):first-child{opacity:1}
  :host(:is([data-tier="hover"],[data-tier="zen"])) .it:not(.compact) .vc-rbx > .vc-rbx-top{display:none}
  /* the item's fold / maximise / open controls sit at the head's right end: the head leaves them room (they lay over
     its last button, "Send to") */
  .it.ownhead > .it-bd > .vc-codewrap > .code-card > .code-bar,.it.ownhead > .it-bd > .vc-codewrap > .vc-codehead,
  .it.ownhead > .it-bd > :is(.vc-xp,.vc-term,.vc-nb,.vc-panel) > :is(.vc-codehead,.vc-th):first-child{padding-right:58px}
  :host([data-tier="full"]) .it:not(.ufold) > .it-hd .fdn{display:none}
  :host([data-tier="full"]) .it > :is(.fd,.mx,.xp).solo{opacity:.8}
  :host([data-tier="hover"]) .addbar,:host([data-tier="zen"]) .addbar{opacity:0;transition:opacity .2s}
  :host([data-tier="hover"]:hover) .addbar,:host([data-tier="zen"]) .addbar:is(:hover,:focus-within){opacity:1}
  /* the solo expand of an item whose drawer draws its own head: a corner mark, not a row of its own */
  .xp.solo{position:absolute;right:4px;top:3px;z-index:3;font-size:10px;color:var(--dim,#6b7480);cursor:pointer;opacity:0;transition:opacity .15s}
  .it:is(:hover,.hov) > .xp.solo,.it:focus-within > .xp.solo{opacity:.8}
  .it.compact{transition:height .18s ease}
  /* ⛔ OPENED IN PLACE DRAWS NO RING EITHER. A resize marks the item open (it has an explicit height now), so the
     ring appeared the moment you finished dragging and stayed until you clicked the item — "if i resize a canvas
     element the boarder comes back and does not go unless i click the element after the resize" (owner,
     2026-09-24). An item being open is visible from the fact that it is open: it is showing you its content. */
  .it.openin{}
  .it.openin .it-bd{max-height:none!important}
  /* ⛔ WHAT YOU DRAGGED IS WHAT THE CONTENT FILLS. The live kinds — a widget, a diagram, a preview, a terminal, an
     explode — are NOT inside the card: they are mounted in the column's overlay, and _liveLayout sizes them to the
     rect of a PLACEHOLDER slot left in the card. So the slot is what has to grow. The old rule (sized → height auto)
     did the opposite: the placeholder is empty (its content lives in the overlay), so auto collapsed it to nothing
     and the overlay was sized to nothing — the content SHRANK as you dragged, which is exactly what the owner
     reported twice (2026-09-24). Dragging an item must make its slot take the room the card gained. */
  .it.sized .it-bd,.it.openin .it-bd{max-height:none!important;display:flex;flex-direction:column;min-height:0}
  .it.sized .vc-live,.it.openin .vc-live{flex:1 1 auto;min-height:60px;height:auto}
  .it.sized .vc-codewrap,.it.openin .vc-codewrap,.it.sized .vc-diag,.it.openin .vc-diag,
  .it.sized .vc-xp,.it.openin .vc-xp,.it.sized .vc-term,.it.openin .vc-term{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  .it.sized .vc-pre,.it.openin .vc-pre{flex:1 1 auto;min-height:0;max-height:none}
  /* ...and every other drawing takes the room you gave it (owner, 2026-10-04: "i can re-size elements in the canvas but
     they dont fill the space of the resized container"): the body grew, but a widget's slot (in .vc-wid), a records
     list, a table and a deck / sheet / page card kept their own height - measured on the mirror, a widget dragged
     260 px taller filled 8% of its body. Each wrapper is a column now and its working part grows: the widget's slot
     (the widget follows it), the list or table or graph (scrolling inside), the card's view. */
  .it:is(.sized,.openin,.maxed) > .it-bd > :is(.vc-wid,.vc-rbx,.vc-filewrap){flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-wid > .vc-live{flex:1 1 auto;height:auto;min-height:60px}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-rbx > :is(.vc-rbx-list,.vc-rbx-cards,.vc-tablewrap,.vc-rbx-graph){flex:1 1 auto;min-height:0;overflow:auto}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-rbx > .vc-rbx-graph{display:flex;flex-direction:column}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-rbx > .vc-rbx-graph > .vc-live{flex:1 1 auto;height:auto!important;min-height:160px}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-tablewrap{flex:1 1 auto;min-height:0;overflow:auto}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap > .code-card{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;margin:0}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap > .code-card > .vo-fview{flex:1 1 auto;min-height:0;overflow:auto;display:flex;flex-direction:column}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap .vo-fview > :is(.vo-sheet,.vo-doc){flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap .vo-fview > .vo-deck{flex:1 1 auto}   /* block: its centred 16:9 stage keeps its width */
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap .vo-stage{max-height:none}   /* a slide keeps 16:9 - it grows with the width */
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-filewrap .vo-grid-w{flex:1 1 auto;max-height:none}
  .it.hovopen{z-index:5;box-shadow:0 0 0 1.5px var(--acc,#5a9e8f),0 12px 30px -10px rgba(0,0,0,.6)}
  /* the preview of a folded item: over the column, its own scroll past a screenful, no slide (it does not move) */
  .stage .it.peek{z-index:8;max-height:min(70vh,640px);overflow:auto;transition:none;background:var(--bg1,#16181d);scrollbar-width:thin}
  .stage .it.peek .it-bd{max-height:none}
  /* the header line of an item that draws its own head: hidden while that head shows, the chip when the COLUMN folds it */
  .it > .it-hd.own-fold{display:none}
  .stage .it.overfold > .it-hd.own-fold{display:flex}
  .stage .it.overfold > .solo{display:none}
  .stage .it.overfold.peek .it-bd{display:block}
  /* A FOLDED CHIP SAYS WHAT IT IS (owner, 2026-10-04: "when they are collapsed its hard to tell what each chip is"): its
     line held the kind, the title, the turn, the item's key, a score and two buttons, so the title - the one thing that
     says what it is - was cut to "ht…". Folded, it is the kind and the title, in full where it fits; the rest is in its
     preview's header a moment away. */
  .it.compact > .it-hd :is(.k,.src,.sc,.mx,.xp,.fu-w){display:none}
  .it.compact > .it-hd .t{flex:1 1 auto;min-width:0;max-width:none;white-space:normal;line-height:1.25;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;overflow-wrap:anywhere}
  .it.sized .rz{opacity:.45}
  .it.resizing{transition:none!important;user-select:none}
  /* the resize handle — a corner grip that shows on hover; a drop saves the item's size */
  .rz{position:absolute;right:3px;bottom:3px;width:14px;height:14px;z-index:5;cursor:nwse-resize;opacity:0;
    border-radius:0 0 5px 0;transition:opacity .18s;
    background:linear-gradient(135deg,transparent 52%,var(--dim,#6b7480) 52%,var(--dim,#6b7480) 60%,transparent 60%,
      transparent 74%,var(--dim,#6b7480) 74%,var(--dim,#6b7480) 82%,transparent 82%)}
  .it:is(:hover,.hov) .rz{opacity:.75}
  /* the stage: items placed level with their turns; the pinned band stays at the top, the parked chips at the bottom */
  .stage{position:relative;min-height:40px}
  .stage .it{position:absolute;margin:0;box-sizing:border-box;left:0;top:0;transition:top .32s cubic-bezier(.2,.7,.3,1),left .32s}
  /* While the transcript is scrolling, a new top is not a move - it is a correction that should already have been
     there - so it is taken instantly. Animating it makes the item chase the scroll and land a third of a second
     late, which reads as the column resetting and dropping (Notes/42 defect 89). A drag already does this. */
  .stage[data-scrolling] .it{transition:none}
  .stage .it.resizing{transition:none}
  /* HELD LAYOUT (the canvas's final form §2): an item taller than the column keeps its head and its rail and scrolls
     its OWN body. It used to run off the end of a stage nobody could scroll, so the part of it you wanted — the
     bottom of the last turn's item — could not be reached at all. */
  /* the desk (align="desk", owner approved 2026-10-06): sized by the distance to the chat on the column's left edge */
  :host([align="desk"]:not([desk-narrow])) .stage .it{transition:left .45s cubic-bezier(.2,.8,.2,1), top .45s cubic-bezier(.2,.8,.2,1), width .35s, height .35s, opacity .3s; overflow:hidden!important; contain:paint; box-sizing:border-box; min-height:0}
  /* a composite card (see _deskGroups): its members behind the front one are not drawn; the front one carries the tabs
     under its header, and as a pill says how many it holds */
  :host([align="desk"]:not([desk-narrow])) .stage .it.cmp-behind:not(.cmp-front){display:none!important}   /* stronger than a pill's display:flex */
  .cmp-tabs{display:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it.cmp-front:not(.desk-chip) > .cmp-tabs{display:flex;gap:2px;flex:none;overflow-x:auto;scrollbar-width:none;padding:0 6px;margin:0 0 4px;border-bottom:1px solid var(--border,#2a2f37)}
  .cmp-tabs::-webkit-scrollbar{display:none}
  .cmp-tab{flex:none;max-width:220px;display:inline-flex;align-items:center;gap:6px;border:0;background:none;font:inherit;font-size:11.5px;color:var(--dim,#8b93a1);padding:5px 9px;border-bottom:2px solid transparent;margin-bottom:-1px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cmp-tab:hover{color:var(--fg,#dce1e8)}
  .cmp-tab.on{color:var(--fg,#dce1e8);border-bottom-color:var(--acc,#5a9e8f)}
  .cmp-ty{font-style:normal;font-size:8.5px;font-weight:700;letter-spacing:.04em;padding:1px 4px;border-radius:4px;color:var(--dim,#8b93a1);background:color-mix(in srgb,var(--fg,#dce1e8) 8%,transparent)}
  .cmp-tab.on .cmp-ty{color:var(--acc,#5a9e8f);background:color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent)}
  :host([align="desk"]:not([desk-narrow])) .stage .it.cmp-front.desk-chip::after{content:attr(data-cmp-n);flex:none;margin-left:6px;font-size:10px;font-weight:600;padding:0 6px;border-radius:8px;color:var(--acc,#5a9e8f);background:color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent)}
  /* on the desk the distance is the fold: a card or full-size face shows its body even if it was folded in held */
  :host([align="desk"]:not([desk-narrow])) .stage .it.compact:not(.desk-chip) > .it-bd{display:block}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip{border-radius:17px;padding:0 10px!important;display:flex!important;flex-direction:row!important;align-items:center;justify-content:flex-start}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd{position:static!important;margin:0!important;flex:1 1 0;width:100%;max-width:100%;min-width:0;display:flex;align-items:center;justify-content:flex-start;gap:6px;overflow:hidden}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > *{flex:0 0 auto}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > .t{flex:1 1 auto;min-width:0;display:block;direction:ltr;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:none!important;transform:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > .t *{animation:none!important;transform:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > .fd,
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > .src,
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > :not(.ic):not(.t):not(.ly):not(.dp){display:none}
  /* a card placed by hand carries its way back to the arrangement (the double click is redrawn away by the first click) */
  .dp{display:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-placed > .it-hd > .dp{display:inline-flex;flex:0 0 auto;cursor:pointer;color:var(--acc,#5a9e8f);font-size:11px;padding:0 3px}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-placed > .dp.solo{display:block;position:absolute;right:36px;top:3px;z-index:3;cursor:pointer;color:var(--acc,#5a9e8f);font-size:11px;opacity:.85}
  /* a chip IS its header line: the hover and zen tiers' hidden, unclickable header left a chip blank until hovered */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd{opacity:1!important;pointer-events:auto!important;background:none}
  /* a panel's chip keeps its two layer controls: a chip has no body to hold them */
  .it-hd > .ly{display:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip:not(.desk-icon) > .it-hd > .ly{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;width:18px;height:18px;border-radius:5px;cursor:pointer;color:var(--dim,#8b93a1);border:1px solid var(--border,#2a2f37)}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > .it-hd > .ly:hover{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip > :not(.it-hd){display:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip .it-hd{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-wrap:nowrap}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip .it-hd *{white-space:nowrap}
  /* an icon: a chip the border had no room for - its icon alone, its title on hover */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-icon{padding:0!important;border-radius:9px;justify-content:center}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-icon > .it-hd{justify-content:center;gap:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-icon > .it-hd > :not(.ic){display:none!important}
  /* a panel item sent behind the desk or brought in front: it fills its region the way a maximised item fills the column */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face^="layer-"]{display:flex!important;flex-direction:column;border-radius:10px;background:var(--bg1,#15181d)!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face="layer-front"]{box-shadow:0 18px 48px rgba(0,0,0,.45)}
  .stage > .lay-pill{position:absolute;z-index:190;display:flex;align-items:center;gap:6px;max-width:60%;padding:3px 4px 3px 9px;border-radius:14px;font-size:11px;
    background:var(--bg2,#1d2128);border:1px solid var(--border2,#3a3f48);box-shadow:0 6px 18px rgba(0,0,0,.4);pointer-events:auto}
  .stage > .lay-pill > b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .stage > .lay-pill > .m{color:var(--dim,#8b93a1);white-space:nowrap}
  .stage > .lay-pill > .ic{color:var(--acc,#5a9e8f)}
  :host(:not([align="desk"])) .stage > .lay-pill{display:none}
  :host([desk-narrow]) .stage > .lay-pill{display:none}
  /* on the desk a live element stacks WITH its card (each .lv carries its card's z-index): the live layer as one plane
     at z 4 put a panel sent behind over the items, and hid a panel brought in front under its own card's ground */
  :host([align="desk"]:not([desk-narrow])) #live{z-index:auto}
  /* NO GROUND ON A BARE DESK (owner, 2026-10-09: "id like the elements to have no background (if there is no panel behind
     them, else keep the background)"): with nothing behind them the items sit on the desk itself. One that rides over the
     others - dragged, peeking open, maximised - keeps its ground, or its text would run into theirs. */
  :host([align="desk"]:not([desk-narrow]):not([layered])) .stage .it:not([data-desk-face^="layer-"]):not(.desk-dragging):not(.resizing):not(.peek):not(.hovopen):not(.maxed):not(.chip-peek){background:transparent!important;border-color:transparent!important}   /* its marks (waiting, pinned, a run's end) are rings, kept */
  /* a pill opened to its whole title under the pointer: over its neighbours, with a ground, its title unclipped */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip.chip-peek{background:var(--bg2,#1d2128)!important;box-shadow:0 6px 18px rgba(0,0,0,.4);border-radius:17px}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-chip.chip-peek > .it-hd > .t{overflow:visible;text-overflow:clip}
  /* a pill opened under the pointer (see _chipPeek): a card over its neighbours, with its ground */
  :host([align="desk"]:not([desk-narrow])) .stage .it.pill-open{background:var(--bg1,#15181d)!important;border:1px solid var(--border2,#3a3f48)!important;border-radius:10px;box-shadow:0 14px 36px rgba(0,0,0,.5);overflow:hidden}
  /* a card being carried or sized rides over the others with its ground, so it never mixes with what it passes over */
  :host([align="desk"]:not([desk-narrow])) .stage .it:is(.desk-dragging,.resizing){background:var(--bg1,#15181d)!important;border-radius:8px}
  /* a panel behind the items: they keep a ground over it, or their text runs into the panel's */
  :host([align="desk"]:not([desk-narrow])[layered]) .stage .it:not([data-desk-face^="layer-"]){background:var(--bg1,#15181d)!important;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.35)}
  /* an older item fades by opacity, which let the panel show through it: over a panel it dims instead */
  :host([align="desk"]:not([desk-narrow])[layered]) .stage .it.aged:not(:hover){opacity:1;filter:brightness(.62) saturate(.7)}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face^="layer-"] > .it-bd{display:flex!important;flex-direction:column;flex:1 1 auto;min-height:0;max-height:none!important;overflow:hidden}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face^="layer-"] > .it-bd > *{flex:1 1 auto;min-height:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face^="layer-"] .vc-panel{display:flex;flex-direction:column;flex:1 1 auto;min-height:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face^="layer-"] .vc-live{flex:1 1 auto;height:auto!important;min-height:120px}
  /* A DESK CARD IS FILLED BY WHAT IT HOLDS (owner, 2026-10-09: "the vera graphs displayed on the desk flicker", "news feeds
     dont take up the entire space in its frame"). The desk gives every card a box, but the rules that let a drawing take
     its box were only for a sized, opened or maximised card: on the desk a records card's graph kept its 320 px slot
     under its toolbar inside a 171 px card - drawn below the card and cut away whole, showing only as the card moved -
     and a widget or a list filled 60% of its card. A desk card with a body is a column whose working part grows. */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd{display:flex!important;flex-direction:column;flex:1 1 auto;min-height:0;max-height:none!important;overflow:hidden}
  /* ...but a card that holds several things side by side (a code item and its exploded diagram, fused "beside") keeps its
     row: the column above stacked the diagram under the code, past the card's foot, and Explode showed nothing (owner,
     2026-10-10: "pressing explode on a piece of code on the desk ... cant see the exploded view") - each pane scrolls on its
     own and the diagram's slot takes the pane's height. Wherever the card is (a desk face or filling the desk). */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd.fu-beside{flex-direction:row!important;display:flex!important;flex:1 1 auto;min-height:0;overflow:hidden}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd.fu-beside > *{flex:1 1 0;min-width:0;min-height:0;overflow:auto;display:flex;flex-direction:column}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd.fu-beside > .fu-p > .fu-b{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd.fu-beside .vc-live[data-live="explode"]{flex:1 1 auto;height:auto!important;min-height:160px}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd.fu-over{flex-direction:column!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > :is(.vc-wid,.vc-rbx,.vc-filewrap,.vc-codewrap,.vc-diag,.vc-xp,.vc-term,.vc-tablewrap,.vc-act,.vc-rr){flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  /* CODE SCROLLS IN ITS CARD (owner, 2026-10-11: "code on the desk isnt scrollable in normal view - it is in edit mode"):
     the kit's card (.code-card: its bar, then pre.vo-lines) was not a column that passes the card's height down, so the
     body - overflow hidden - cut the lines off and nothing inside scrolled (measured: 391 px shown of 1801). The card is a
     column; its lines take the height left and scroll. */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd .vc-codewrap > .code-card{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;margin:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd .vc-codewrap > .code-card > pre{flex:1 1 auto;min-height:0;max-height:none!important;overflow:auto!important;margin:0}
  /* an image is as large as its card lets it be, whole (never cropped), its caption under it */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd > .vc-fig{flex:1 1 auto;min-height:0;margin:0;display:flex;flex-direction:column;align-items:center}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd > .vc-fig > img{flex:1 1 0;min-height:0;width:100%;height:100%;max-width:none;object-fit:contain}
  /* a research run fills its card: its panes take the height there is and scroll inside it, not the 60vh cap */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd > .vc-rr .vc-rr-p{max-height:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd > .vc-rr .vc-rr-body{flex:1 1 0;min-height:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd > .vc-rr .vc-rr-p.on,
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd > .vc-rr .vc-rr-p.main,
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face] > .it-bd > .vc-rr .vc-rr-p.side{height:100%}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > .vc-tablewrap{display:block;overflow:auto}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > :is(.vc-wid,.vc-diag,.vc-xp,.vc-term) > .vc-live{flex:1 1 auto;height:auto!important;min-height:60px}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > .vc-live{flex:1 1 auto;height:auto!important;min-height:60px}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) .vc-pre{flex:1 1 auto;min-height:0;max-height:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > .vc-rbx > :is(.vc-rbx-list,.vc-rbx-cards,.vc-tablewrap){flex:1 1 auto;min-height:0;overflow:auto}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > .vc-rbx > .vc-rbx-graph{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd:not(.fu) > .vc-rbx > .vc-rbx-graph > .vc-live{flex:1 1 auto;height:auto!important;min-height:60px}
  /* ...and a small card is its content, not its controls: a records card's tool row (views, search, filters) wraps to 84 px
     at a desk card's width and, with its title and foot, took the whole of a 171 px card - its graph got 0 px. Under 380 px
     tall the tool row, the title line (the card's header already names it), a graph's pager and its caption go; a taller
     card, or the card maximised, has them. */
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip):not([data-desk-face^="layer-"]) > .it-bd{container:deskcard / size}
  @container deskcard (max-height: 380px) { .vc-rbx > :is(.vc-rbx-top,.vc-rbx-tools,.vc-rbx-glance,.vc-rbx-facets), .vc-rbx:has(> .vc-rbx-graph) > .vc-rbx-foot, .vc-rbx-gnote { display:none!important } }   /* the site bar and the site filters too: on a 171 px card they left the list 0 px */
  /* a desk card that is not a chip keeps its corner grip: a compact card's rule hid it, so no desk card could be resized */
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip):not([data-desk-face^="layer-"]) > .rz{display:block}
  /* a card is moved by its header on the desk; while it moves it follows the pointer exactly and rides above the rest */
  :host([align="desk"]:not([desk-narrow])) .stage .it:not([data-desk-face^="layer-"]) > :is(.it-hd,.it-bd > .vc-panel > .vc-th){cursor:grab}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-dragging{transition:width .12s,height .12s!important;cursor:grabbing;box-shadow:0 18px 40px rgba(0,0,0,.5);outline:1px solid var(--acc,#5a9e8f)}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-dragging *{cursor:grabbing!important;user-select:none}
  /* while it moves, a card shows what it is, not its controls: the footer rail and the grip popped in under the pointer */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-dragging > :is(.it-ft,.rz,.mx,.xp,.dp){display:none!important}
  @keyframes vcDeskFlash{0%,100%{box-shadow:0 0 0 0 transparent}40%{box-shadow:0 0 0 3px var(--acc,#5a9e8f),0 0 26px color-mix(in srgb,var(--acc,#5a9e8f) 55%,transparent)}}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-flash{animation:vcDeskFlash .8s ease-in-out 2}
  /* the end of a chat run on the desk: the run stops at the desk's edge and its item carries the theme's mark */
  :host([align="desk"]:not([desk-narrow])) .stage .it.run-target{box-shadow:inset 3px 0 0 var(--acc,#9b8dfa), 0 0 0 1px color-mix(in srgb, var(--acc,#9b8dfa) 45%, transparent)}
  /* EVERY DESK CARD SHOWS ITS NAME (owner, 2026-10-09: "some elements added by the chat are not draggable on the desk"):
     the Hover and Zen tiers hide a card's header until it is hovered and make it unclickable - on the desk that left
     widget and table cards nameless, with nothing to take hold of. On the desk the header is always there, in the card's
     flow, and it is the handle. */
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip):not(.ownhead) > .it-hd{position:relative!important;inset:auto!important;opacity:1!important;pointer-events:auto!important;visibility:visible!important}
  /* ONE ROW OF CONTROLS, AT THE TOP (see the render's .hda): on the desk the footer rail, the header's own corner icons,
     the item's key and turn label, and a panel's second bar (its title again and six buttons) are not drawn. A panel
     card shows the shared header like every other card; the bands the hover tier keeps for a header and a rail that
     float over the body are given back to the body. */
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > :is(.it-ft,.solo){display:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .it-hd > :is(.dp,.mx,.xp,.k,.src,.fdn){display:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .it-bd > .vc-panel > .vc-th{display:none!important}
  /* ONE CONTAINER FOR EVERY DESK CARD (owner, 2026-10-10: "code on the desk doesnt act like the other elements and cant be
     dragged its menu options need integrating into the canonical element container"). Kinds that draw a head of their own
     (code, a page, a terminal, a notebook cell, a diagram, an explode, a panel) showed only that head on the desk - in the
     hover tier unseen until hovered, never a handle, with its own buttons: on the desk they take the shared header (the
     handle, the title, the one row of controls) and their own head is not drawn; its buttons are in the card's ⋯ menu. */
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-hd.own-fold{display:flex!important;position:relative!important;inset:auto!important;opacity:1!important;pointer-events:auto!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > .vc-codewrap > .code-card > .code-bar,
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > .vc-codewrap > .vc-codehead,
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > :is(.vc-xp,.vc-term,.vc-nb,.vc-panel,.vc-diag) > :is(.vc-codehead,.vc-th):first-child{display:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip){padding-top:0!important;padding-bottom:0!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .it-hd,
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > :first-child > :is(.vc-codehead,.vc-th,.code-bar):first-child,
  :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > .vc-codewrap > .code-card > .code-bar{padding-right:250px!important}
  @container (max-width: 320px) {
    :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .hda > .hb:is([data-act="ctx"],[data-act="explode"],[data-act="edit"]){display:none}
    :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .it-hd,
    :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > :first-child > :is(.vc-codehead,.vc-th,.code-bar):first-child,
    :host([align="desk"]:not([desk-narrow])) .stage .it.ownhead:not(.desk-chip) > .it-bd > .vc-codewrap > .code-card > .code-bar{padding-right:168px!important} }   /* view · lock · fill · more · drop */
  .hda{display:none}
  :host([align="desk"]:not([desk-narrow])) .stage .it:not(.desk-chip) > .hda{display:flex;position:absolute;top:4px;right:6px;z-index:6;gap:1px;align-items:center;opacity:.55;transition:opacity .15s}
  :host([align="desk"]:not([desk-narrow])) .stage .it:is(:hover,.hov,:focus-within) > .hda{opacity:1}
  /* the card's own controls, big enough to hit (owner, 2026-10-11: "the controls of the desk items on the top bar need to be a bit bigger") */
  .hda > .hb{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:6px;cursor:pointer;color:var(--dim,#8b93a1)}
  .hda > .hb > svg{width:16px;height:16px}
  .hda > .hb:hover{color:var(--fg,#dce1e8);background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent)}
  .hda > .hb.on{color:var(--acc,#5a9e8f)}
  .hda > .hb[data-act="remove"]:hover{color:var(--err,#e06c6c)}
  /* locked: no grip, no handle; its lock shows lit */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-locked > .rz{display:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-locked > :is(.it-hd,.it-bd > .vc-panel > .vc-th){cursor:default!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-locked > .hda > .hb[data-act="lock"]{opacity:1}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-locked > .hda{opacity:1}
  .stage > .hd-pop{position:absolute;z-index:230;display:flex;flex-direction:column;min-width:170px;padding:4px;border-radius:9px;
    background:var(--bg2,#1d2128);border:1px solid var(--border2,#3a3f48);box-shadow:0 10px 28px rgba(0,0,0,.45)}
  .stage > .hd-pop > .ib{display:block;width:100%;text-align:left;border:0;background:none;color:var(--fg,#dce1e8);padding:5px 9px;border-radius:6px;font:inherit;font-size:11.5px;cursor:pointer}
  .stage > .hd-pop > .ib:hover{background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent)}
  .stage > .hd-pop > .ib.on{color:var(--acc,#5a9e8f)}
  .stage > .hd-pop > hr{border:0;border-top:1px solid var(--border,#2a2f37);margin:4px 2px}
  .stage > .hd-pop > .ib.vrow{display:flex;align-items:center;gap:8px}.stage > .hd-pop > .ib.vrow .vic{display:inline-flex;color:var(--dim,#8b93a1)}
  .stage > .hd-pop > .ib.vrow.on .vic{color:var(--acc,#5a9e8f)}
  .stage > .hd-pop{min-width:0}.stage > .hd-pop:not([data-kind="view"]){min-width:170px}
  /* the header's View button is the desk's one view switch: the records item's own switch would be a second */
  :host([align="desk"]:not([desk-narrow])) .vc-rbx-seg.seg-view{display:none}
  /* resizing follows the hand: no size animation while the grip is held */
  :host([align="desk"]:not([desk-narrow])) .stage .it.resizing{transition:none!important}
  /* a redrawn card held at its last place (no animation), and a new one unseen until the desk has placed it */
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-hold{transition:none!important}
  :host([align="desk"]:not([desk-narrow])) .stage .it.desk-new{opacity:0;transition:none!important}  @media (prefers-reduced-motion: reduce){:host([align="desk"]:not([desk-narrow])) .stage .it{transition:none}}
  /* THE BACK LAYER (the desk's graph, owner-approved 2026-10-06): whatever the page puts in the "back" slot is drawn
     under the cards, dimmed until the pointer is on it. The desk's own ground goes so the layer shows through, and
     the empty desk lets the pointer reach the layer; the cards and the head still take it. */
  .back{display:none}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .wrap{position:relative}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .back{display:block;position:absolute;inset:0;z-index:0;opacity:.38;transition:opacity .25s}
  /* a card over the layer keeps a ground in every blocks tier, or its text runs into the graph's */
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .stage .it{background:color-mix(in srgb, var(--bg1,#15181d) 90%, transparent)!important;border-radius:8px}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .back:hover{opacity:.9}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .head,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body{position:relative;z-index:1}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .stage,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .band{background:transparent;pointer-events:none}
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .it,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .band-h > *,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .chips > *,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body .addbar,
  :host([align="desk"]:not([desk-narrow])[back="behind"]) .body :is(button,a,input,select,textarea){pointer-events:auto}
  .stage .it.capped{overflow:hidden}
  .stage .it.capped .it-bd{max-height:none;overflow:auto}
  /* folded because the COLUMN ran out of room, not because the tier folds it: the header line alone, and it opens
     again the moment there is room. The weakest hold folds first, so what you are reading stays whole. */
  .stage .it.overfold .it-bd,.stage .it.overfold .it-ft,.stage .it.overfold .rz{display:none}
  .stage .it.overfold{cursor:pointer}
  :host([stage]) .body{position:relative;padding-top:0}
  :host([stage]) .band.pinned{padding-top:6px}
  /* the NOW bar stays in view under the add bar while the stage scrolls with the transcript */
  :host([stage]) .band.now>.band-h{position:sticky;top:34px;z-index:5;background:var(--bg1,#15181d);margin:0;padding:3px 0}
  :host([stage]) .band.parked{position:sticky;bottom:0;z-index:5;background:var(--bg1,#15181d)}
  .band.hid{padding:4px 0;position:relative}
  .hidbtn{font:inherit;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim,#6b7480);background:none;border:1px solid var(--border,#2a2f37);border-radius:9px;padding:1px 8px;cursor:pointer}
  .hidbtn:hover{color:var(--fg,#dce1e8)}
  .hidpop[hidden]{display:none}
  :host([chrome="none"]) .addbar{display:none!important}   /* the host draws these controls elsewhere (WSU-72) */
  .hidpop .chip.rmv{cursor:pointer;border:0;font:inherit}
  .vc-toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:400;display:flex;gap:12px;align-items:center;padding:8px 10px 8px 14px;border-radius:10px;background:var(--bg2,#1c2026);border:1px solid var(--border2,rgba(255,255,255,.14));color:var(--text,#d8dce4);font:12.5px var(--ui,system-ui,sans-serif);box-shadow:0 6px 22px color-mix(in srgb,var(--bg0,#0e0f12) 70%,transparent)}
  .vc-toast button{border:1px solid var(--accent,#5a9e8f);background:transparent;color:var(--accent,#5a9e8f);border-radius:6px;padding:3px 10px;font:600 12px var(--ui,system-ui,sans-serif);cursor:pointer}
  .hidpop{position:absolute;right:0;top:24px;z-index:6;background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);border-radius:8px;padding:6px 8px;display:flex;flex-wrap:wrap;gap:4px;max-width:260px;box-shadow:0 4px 14px rgba(0,0,0,.35)}
  .it .sc{font-family:ui-monospace,Consolas,monospace;font-size:8.5px;color:var(--dim,#6b7480);flex:0 0 auto}
  .it-hd{display:flex;align-items:center;gap:6px;padding:5px 8px 4px;cursor:pointer;flex:0 0 auto;min-height:18px}
  .it-hd .ic{width:15px;height:15px;border-radius:4px;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
    font-family:ui-monospace,Consolas,monospace;font-size:8px;color:#101317;background:var(--dim,#6b7480);padding:0;border:0;
    letter-spacing:0;text-transform:none;line-height:1}
  .it-hd .ic[data-kind="note"]{background:#8fb87a}.it-hd .ic[data-kind="markdown"]{background:#8fb87a}
  .it-hd .ic[data-kind="source"]{background:#7aa2d6}
  .it-hd .ic[data-kind="timeline"]{background:#c9955a}
  .it-hd .ic[data-kind="calendar"]{background:#9fe1e7}
  .vc-cal{display:flex;flex-direction:column;gap:6px}
  .vc-cal-hd{display:flex;align-items:center;gap:5px}
  .vc-cal-m{font-size:11px;font-weight:600;color:var(--fg,#ddd)}
  .vc-cal-sp{flex:1}
  .vc-cal-b{font:inherit;font-size:9.5px;min-width:19px;height:19px;padding:0 7px;border:1px solid var(--border,#3a3530);
    border-radius:9px;background:var(--bg2,#272421);color:var(--dim2,#8a7e70);cursor:pointer;line-height:1}
  .vc-cal-b:hover{color:var(--fg,#ddd);border-color:var(--acc,#5a9e8f)}
  .vc-cal-w{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;font-family:var(--mono,monospace);font-size:8.5px;
    color:var(--dim2,#8a7e70);text-align:center}
  .vc-cal-g{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
  .vc-cal-d{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;gap:1px;
    min-height:30px;padding:2px 0 3px;border:1px solid transparent;border-radius:5px;background:none;font:inherit;
    color:var(--dim2,#8a7e70);cursor:pointer}
  .vc-cal-d.out{visibility:hidden;cursor:default}
  .vc-cal-d b{font-size:10px;font-weight:500;line-height:1}
  .vc-cal-d.has b{color:var(--fg,#ddd)}
  .vc-cal-d:hover{background:var(--bg2,#272421)}
  .vc-cal-d.today b{color:var(--acc,#5a9e8f);font-weight:700}
  .vc-cal-d.on{border-color:var(--acc,#5a9e8f);background:var(--bg2,#272421)}
  .vc-cal-dots{display:flex;align-items:center;gap:1.5px;min-height:4px}
  .vc-cal-dots i{width:3.5px;height:3.5px;border-radius:50%;display:block}
  .vc-cal-dots em{font-style:normal;font-size:7px;color:var(--dim2,#8a7e70)}
  .vc-cal-l{display:flex;flex-direction:column;gap:2px;border-top:1px solid var(--border,#3a3530);padding-top:5px}
  .vc-cal-e{display:grid;grid-template-columns:44px 1fr;gap:6px;align-items:baseline}
  .vc-cal-t{font-family:var(--mono,monospace);font-size:8.5px;color:var(--dim2,#8a7e70)}
  .vc-cal-n{font-size:10.5px;color:var(--fg,#ddd);border-left:2px solid var(--acc,#5a9e8f);padding-left:6px}
  .vc-cal-n em{font-style:normal;font-size:9px;color:var(--dim2,#8a7e70)}
  .vc-cal-none{font-size:10px;color:var(--dim2,#8a7e70);font-style:italic}
  /* readable at a desk card's size, and wide: the month on the left, the day's (or the month's) list beside it, so the
     list is never pushed under the grid and cut off */
  .vc-cal{container:vccal / inline-size}.vc-cal-in{display:flex;flex-direction:column;gap:6px}
  .vc-cal-m{font-size:12.5px}.vc-cal-w{font-size:10px}.vc-cal-d b{font-size:11.5px}.vc-cal-t{font-size:10.5px}.vc-cal-n{font-size:12px}.vc-cal-none{font-size:11.5px}
  @container vccal (min-width: 470px) {
    .vc-cal-in{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(150px,1fr);grid-template-rows:auto auto 1fr;column-gap:14px;row-gap:6px;
      grid-template-areas:"hd l" "w l" "g l"}
    .vc-cal-hd{grid-area:hd}.vc-cal-w{grid-area:w}.vc-cal-g{grid-area:g;align-content:start}
    .vc-cal-l{grid-area:l;border-top:0;border-left:1px solid var(--border,#3a3530);padding:0 0 0 12px;overflow:auto;min-height:0}
  }
  .vc-wid-b{font:inherit;font-size:8.5px;height:16px;padding:0 7px;margin-left:6px;border:1px solid var(--border,#3a3530);
    border-radius:8px;background:var(--bg2,#272421);color:var(--dim2,#8a7e70);cursor:pointer;vertical-align:middle}
  .vc-wid-b:hover{color:var(--fg,#ddd);border-color:var(--acc,#5a9e8f)}
  /* A TIMELINE: the span at a glance on a proportional axis (a dot per event - press one to go to it), then the spine,
     the years standing on it, a dot per event, a gap said where years pass with nothing in them */
  .vc-tl{display:flex;flex-direction:column;gap:2px;position:relative;min-width:0}
  .vc-tl-h{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
  .vc-tl-h b{font-size:11.5px;font-weight:600;color:var(--fg,#ddd);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-tl-h small{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#8a7e70);white-space:nowrap}
  .vc-tl-ax{position:relative;height:30px;margin:4px 8px 8px}
  .vc-tl-ln{position:absolute;left:0;right:0;top:11px;height:2px;border-radius:1px;background:var(--border,#3a3530)}
  .vc-tl-dot{position:absolute;top:7px;width:10px;height:10px;margin-left:-5px;padding:0;border:0;border-radius:50%;background:var(--acc3,#c9955a);opacity:.85;cursor:pointer;box-shadow:0 0 0 2px var(--bg0,#171513);transition:transform .12s}
  .vc-tl-dot:hover{transform:scale(1.45);opacity:1}
  .vc-tl-tick{position:absolute;top:19px;transform:translateX(-50%);font-family:var(--mono,monospace);font-size:9px;color:var(--dim2,#8a7e70);white-space:nowrap}
  .vc-tl-tick:first-of-type{transform:none}.vc-tl-tick.end{transform:translateX(-100%)}
  .vc-tl-list{position:relative;padding-left:16px}
  .vc-tl-list::before{content:"";position:absolute;left:5px;top:6px;bottom:6px;width:2px;border-radius:1px;background:var(--border,#3a3530)}
  .vc-tl-y{position:relative;font-family:var(--mono,monospace);font-size:10.5px;font-weight:700;color:var(--acc3,#c9955a);margin:9px 0 3px}
  .vc-tl-y::before{content:"";position:absolute;left:-15px;top:3px;width:10px;height:10px;border-radius:3px;background:var(--acc3,#c9955a)}
  .vc-tl-gap{font-size:9.5px;color:var(--dim,#6b7480);font-style:italic;margin:4px 0}
  .vc-tl-e{position:relative;display:grid;grid-template-columns:44px 1fr;gap:8px;padding:4px 6px 4px 0;border-radius:6px;transition:background .3s}
  .vc-tl-e::before{content:"";position:absolute;left:-13px;top:10px;width:6px;height:6px;border-radius:50%;background:var(--fg,#ddd);opacity:.55}
  .vc-tl-e.flash{background:color-mix(in srgb,var(--acc3,#c9955a) 20%,transparent)}
  .vc-tl-w{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#8a7e70);padding-top:2px;text-align:right;white-space:nowrap}
  .vc-tl-t{font-size:11px;line-height:1.55;color:var(--fg,#ddd)}
  .vc-tl-a{margin-left:6px;font-size:9px;color:var(--dim2,#8a7e70);text-decoration:none;white-space:nowrap}
  .vc-tl-a:hover{color:var(--acc,#5a9e8f);text-decoration:underline}
  .vc-tl-empty{font-size:10.5px;color:var(--dim2,#8a7e70);font-style:italic}
  /* a source reads as a page, not as a row: the domain small above it, the title the thing you click */
  .vc-src{display:flex;flex-direction:column;gap:5px;padding:2px 0}
  .vc-src-hd{display:flex;align-items:center;gap:7px;font-size:9px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace)}
  .vc-src-dom{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
  .vc-src-n{opacity:.75}.vc-src-n.bad{color:var(--err,#c96b6b);opacity:1}
  .vc-src.bad .vc-src-t{opacity:.6;text-decoration:line-through}
  .vc-src-t{font-size:12px;line-height:1.4;color:var(--fg,#ddd);text-decoration:none;font-weight:500}
  .vc-src-t:hover{text-decoration:underline}
  .vc-src-s{font-size:10.5px;line-height:1.55;color:var(--dim2,#8a7e70);
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  .vc-src-act{display:flex;gap:5px;margin-top:1px}
  .vc-src-b{font:inherit;font-size:9px;height:19px;padding:0 8px;border:1px solid var(--border,#3a3530);
    border-radius:9px;background:var(--bg2,#272421);color:var(--dim2,#8a7e70);cursor:pointer}
  .vc-src-b:hover{color:var(--fg,#ddd);border-color:var(--acc,#5a9e8f)}
  .vc-src-b[disabled]{opacity:.5;cursor:default}
  /* a records item: a browser, not a dump - the list the thing you move through, one record opening in place */
  .vc-rbx{display:flex;flex-direction:column;gap:7px;min-width:0}
  .vc-rbx-top{display:flex;align-items:center;gap:9px}
  .vc-rbx-ic{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:13px;background:color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent);flex:none}
  .vc-rbx.k-news .vc-rbx-ic{background:color-mix(in srgb,var(--warn,#c9a35a) 18%,transparent)}.vc-rbx.k-memory .vc-rbx-ic{background:color-mix(in srgb,#9e8fa0 22%,transparent)}
  .vc-rbx-h{display:flex;flex-direction:column;min-width:0}.vc-rbx-h b{font-size:12.5px;font-weight:600;color:var(--fg,#ddd);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .vc-rbx-h small{font-size:9.5px;color:var(--dim2,#8a7e70);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .vc-rbx-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
  .vc-rbx-q{flex:1;min-width:120px;height:24px;padding:0 9px;border-radius:12px;border:1px solid var(--border,#3a3530);background:var(--bg2,#272421);color:var(--fg,#ddd);font:inherit;font-size:11px}
  .vc-rbx-q:focus{outline:none;border-color:var(--acc,#5a9e8f)}
  .vc-rbx-seg{display:inline-flex;border:1px solid var(--border,#3a3530);border-radius:12px;overflow:hidden}
  .vc-rbx-seg button{font:inherit;font-size:10px;height:22px;padding:0 8px;border:0;background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}
  .vc-rbx-seg button+button{border-left:1px solid var(--border,#3a3530)}.vc-rbx-seg button.on{background:var(--bg3,#302c29);color:var(--fg,#ddd)}
  .vc-rbx-facets{display:flex;gap:4px;flex-wrap:wrap}
  .vc-rbx-facets button{font:inherit;font-size:9.5px;height:20px;padding:0 8px;border-radius:10px;border:1px solid var(--border,#3a3530);background:transparent;color:var(--dim2,#8a7e70);cursor:pointer;display:inline-flex;gap:5px;align-items:center}
  .vc-rbx-facets button i{font-style:normal;opacity:.6}.vc-rbx-facets button.on{border-color:var(--acc,#5a9e8f);color:var(--fg,#ddd);background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent)}
  /* a turn's activity: one line per capability call, the state as its mark */
  .vc-act{display:flex;flex-direction:column;gap:8px;min-width:0}
  .vc-act-h{display:flex;align-items:center;gap:8px;min-width:0}.vc-act-h b{font-size:12.5px;font-weight:600}
  .vc-act-n{margin-left:auto;font-size:10.5px;color:var(--dim,#8b93a1);white-space:nowrap}
  .vc-act-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--ok,#5fb98a)}
  .vc-act.st-running .vc-act-dot{background:var(--acc,#5a9e8f);animation:vcActPulse 1.2s ease-in-out infinite}.vc-act.st-error .vc-act-dot{background:var(--err,#e06c6c)}
  @keyframes vcActPulse{50%{opacity:.35}}
  .vc-act-l{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;overflow:auto;min-height:0}
  .vc-act-l li{display:flex;gap:8px;align-items:flex-start;padding:6px 2px;border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 50%,transparent)}
  .vc-act-l li:last-child{border-bottom:0}
  .vc-act-l .ic{width:14px;height:14px;flex:none;margin-top:1px;border-radius:50%;border:2px solid var(--ok,#5fb98a)}
  .vc-act-l li.ok .ic{background:color-mix(in srgb,var(--ok,#5fb98a) 35%,transparent)}
  .vc-act-l li.error .ic{border-color:var(--err,#e06c6c);background:color-mix(in srgb,var(--err,#e06c6c) 35%,transparent)}
  .vc-act-l li.running .ic{border-color:color-mix(in srgb,var(--acc,#5a9e8f) 30%,transparent);border-top-color:var(--acc,#5a9e8f);animation:vcActSpin .8s linear infinite}
  @keyframes vcActSpin{to{transform:rotate(360deg)}}
  .vc-act-l .tx{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
  .vc-act-l .ln{display:flex;gap:8px;align-items:baseline;min-width:0}
  .vc-act-l code{font-family:var(--mono,monospace);font-size:11.5px;color:var(--fg,#dce1e8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-act-l .t{margin-left:auto;font-size:10px;color:var(--dim,#8b93a1);white-space:nowrap}
  .vc-act-l .ar{font-family:var(--mono,monospace);font-size:10px;color:var(--dim,#8b93a1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-act-l .pk{font-size:11px;line-height:1.45;color:color-mix(in srgb,var(--fg,#dce1e8) 72%,transparent);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  @media (prefers-reduced-motion: reduce){ .vc-act-l li.running .ic,.vc-act.st-running .vc-act-dot{animation:none} }
  /* a research run (see the renderer): status, the question, the tabs, then the panes - one at a time, or on a wide card
     the report with one other beside it. Each pane scrolls on its own. */
  .vc-rr{display:flex;flex-direction:column;gap:6px;min-width:0;container:vcrr / inline-size}
  .vc-rr-hd{display:flex;align-items:center;gap:8px;min-width:0}
  .vc-rr-hd b{font-size:12.5px;font-weight:600;white-space:nowrap}
  .vc-rr-step{font-size:11px;color:var(--dim,#8b93a1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .vc-rr-n{margin-left:auto;font-size:10.5px;color:var(--dim,#8b93a1);white-space:nowrap}
  .vc-rr-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--ok,#5fb98a)}
  .vc-rr.st-running .vc-rr-dot{background:var(--acc,#5a9e8f);animation:vcActPulse 1.2s ease-in-out infinite}
  .vc-rr.st-error .vc-rr-dot{background:var(--err,#e06c6c)}
  .vc-rr-bar{height:2px;border-radius:2px;overflow:hidden;background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent)}
  .vc-rr-bar > i{display:block;width:30%;height:100%;background:var(--acc,#5a9e8f);animation:vcRrSlide 1.6s ease-in-out infinite}
  @keyframes vcRrSlide{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}
  .vc-rr-q{font-size:12px;line-height:1.45;color:color-mix(in srgb,var(--fg,#dce1e8) 80%,transparent);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .vc-rr-tabs{display:flex;gap:2px;border-bottom:1px solid var(--border,#2a2f37);flex:none}
  .vc-rr-tab{border:0;background:none;color:var(--dim,#8b93a1);font:inherit;font-size:11.5px;padding:5px 10px;border-bottom:2px solid transparent;margin-bottom:-1px;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
  .vc-rr-tab i{font-style:normal;font-size:10px;padding:0 6px;border-radius:8px;background:color-mix(in srgb,var(--fg,#dce1e8) 8%,transparent)}
  .vc-rr-tab:hover{color:var(--fg,#dce1e8)}
  .vc-rr-tab.on{color:var(--fg,#dce1e8);border-bottom-color:var(--acc,#5a9e8f)}
  .vc-rr-body{display:flex;flex-direction:column;min-height:0;flex:1 1 auto}
  .vc-rr-p{display:none;min-width:0;min-height:0;overflow:auto;max-height:60vh}
  .vc-rr-p.on{display:block}
  .vc-latest{position:sticky;bottom:8px;float:right;margin:-34px 8px 0 0;z-index:3;padding:4px 10px;border-radius:999px;border:1px solid var(--accent,#5a9e8f);background:var(--bg1,#16181d);color:var(--accent,#5a9e8f);font:600 11px var(--ui,system-ui,sans-serif);cursor:pointer;box-shadow:0 2px 10px color-mix(in srgb,var(--bg0,#0e0f12) 70%,transparent)}
  .vc-latest:hover{background:color-mix(in srgb,var(--accent,#5a9e8f) 14%,var(--bg1,#16181d))}
  .vc-rr-p > .vc-rbx{max-height:none}
  .vc-rr-doc{font-size:13px;line-height:1.6}
  .vc-rr-live{color:color-mix(in srgb,var(--fg,#dce1e8) 82%,transparent)}
  .vc-rr-wait{font-size:12px;color:var(--dim,#8b93a1);font-style:italic;padding:10px 2px}
  .vc-rr-chips{display:flex;flex-wrap:wrap;gap:4px}
  .vc-rr-chip{font-size:11px;border:1px solid var(--border,#2a2f37);border-radius:10px;padding:2px 8px;color:var(--fg,#dce1e8)}
  .vc-rr-chip i{font-style:normal;color:var(--dim,#8b93a1);margin-right:5px}
  .vc-rr-p h5{margin:12px 0 4px;font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--dim,#8b93a1)}
  .vc-rr-qs{margin:0;padding-left:20px;font-size:12.5px;line-height:1.55}
  .vc-rr-qs li{margin:3px 0}
  .vc-rr-brief{font-size:12px;line-height:1.5;color:color-mix(in srgb,var(--fg,#dce1e8) 78%,transparent)}
  .vc-rr-src{margin:0;padding-left:22px;font-size:12.5px;line-height:1.45}
  .vc-rr-src li{margin:5px 0;overflow-wrap:anywhere}
  .vc-rr-src a{color:var(--acc,#5a9e8f);text-decoration:none}.vc-rr-src a:hover{text-decoration:underline}
  .vc-rr-src small{display:block;font-family:var(--mono,monospace);font-size:10px;color:var(--dim,#8b93a1)}
  .vc-rr-ft{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim,#8b93a1);flex:none}
  /* WSU-65 (owner 2026-10-11: "the research plan doesn't render into the right hand panel reliably"): beside the report
     only from 760 px, so a desk card sized narrower showed one pane at a time - the plan (the side pane while it runs)
     was gone from view. Beside from 560 px now; narrower, the side pane goes under the report rather than away. */
  @container vcrr (max-width: 559.98px) {
    .vc-rr.lay-right .vc-rr-body{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1.5fr) minmax(0,1fr);gap:10px}
    .vc-rr.lay-right .vc-rr-p{display:none}
    .vc-rr.lay-right .vc-rr-p.main,.vc-rr.lay-right .vc-rr-p.side{display:block}
    .vc-rr.lay-right .vc-rr-p.main{grid-row:1}
    .vc-rr.lay-right .vc-rr-p.side{grid-row:2;border-top:1px solid var(--border,#2a2f37);padding-top:10px}
    .vc-rr.lay-right .vc-rr-tab[data-rr-tab="report"]{display:none}
    .vc-rr.lay-right .vc-rr-tab.on:not(.side){color:var(--dim,#8b93a1);border-bottom-color:transparent}
    .vc-rr.lay-right .vc-rr-tab.side{color:var(--fg,#dce1e8);border-bottom-color:var(--acc,#5a9e8f)}
  }
  /* wide: the report beside one other pane; its own tab is not needed there */
  @container vcrr (min-width: 560px) {
    .vc-rr-body{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(250px,1fr);gap:14px}
    .vc-rr-p{display:none}
    .vc-rr-p.main,.vc-rr-p.side{display:block}
    .vc-rr-p.main{grid-column:1;grid-row:1}.vc-rr-p.side{grid-column:2;grid-row:1;border-left:1px solid var(--border,#2a2f37);padding-left:12px}
    .vc-rr-tab[data-rr-tab="report"]{display:none}
    .vc-rr-tab.on:not(.side){color:var(--dim,#8b93a1);border-bottom-color:transparent}
    .vc-rr-tab.side{color:var(--fg,#dce1e8);border-bottom-color:var(--acc,#5a9e8f)}
  }
  @media (prefers-reduced-motion: reduce){ .vc-rr.st-running .vc-rr-dot,.vc-rr-bar > i{animation:none} }
  .vc-rr-sp{flex:1}
  .vc-rr-lay{border:0;background:none;color:var(--dim,#8b93a1);font:inherit;font-size:11px;padding:5px 8px;cursor:pointer;white-space:nowrap}
  .vc-rr-lay:hover{color:var(--fg,#dce1e8)}
  /* the second pane under the report, at any width: the report, then the pane chosen in the tabs (not one at a time) */
  .vc-rr.lay-below .vc-rr-body{display:grid!important;grid-template-columns:minmax(0,1fr)!important;grid-template-rows:minmax(0,1.5fr) minmax(0,1fr);gap:10px}
  .vc-rr.lay-below .vc-rr-p{display:none!important}
  .vc-rr.lay-below .vc-rr-p.main,.vc-rr.lay-below .vc-rr-p.side{display:block!important;grid-column:1!important}
  .vc-rr.lay-below .vc-rr-p.main{grid-row:1!important}
  .vc-rr.lay-below .vc-rr-p.side{grid-row:2!important;border-left:0!important;padding-left:0!important;border-top:1px solid var(--border,#2a2f37);padding-top:10px}
  .vc-rr.lay-below .vc-rr-tab[data-rr-tab="report"]{display:none}
  .vc-rr.lay-below .vc-rr-tab.on:not(.side){color:var(--dim,#8b93a1);border-bottom-color:transparent}
  .vc-rr.lay-below .vc-rr-tab.side{color:var(--fg,#dce1e8);border-bottom-color:var(--acc,#5a9e8f)}
  /* another kind seen as a graph (graphOf): the records graph's slot, filling what it is given */
  .vc-tv-graph{display:flex;flex-direction:column;gap:3px;min-height:0}
  .vc-tv-graph > .vc-live{height:300px}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-tv-graph{flex:1 1 auto;min-height:0}
  .it:is(.sized,.openin,.maxed) > .it-bd > .vc-tv-graph > .vc-live{flex:1 1 auto;height:auto!important;min-height:160px}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd:not(.fu) > .vc-tv-graph{flex:1 1 auto;min-height:0}
  :host([align="desk"]:not([desk-narrow])) .stage .it[data-desk-face]:not(.desk-chip) > .it-bd:not(.fu) > .vc-tv-graph > .vc-live{flex:1 1 auto;height:auto!important;min-height:60px}
  /* the table's other views, and the names as a list */
  .vc-tv-list{display:flex;flex-direction:column}
  .vc-tv-list .vc-tv-r{display:flex;flex-direction:column;gap:2px;padding:7px 4px;border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 60%,transparent)}
  .vc-tv-t{font-weight:600;font-size:12px;color:var(--fg,#dce1e8)}
  .vc-tv-s{font-size:11px;line-height:1.5;color:color-mix(in srgb,var(--fg,#dce1e8) 70%,transparent)}.vc-tv-s i{font-style:normal;color:var(--dim,#8b93a1)}
  .vc-tv-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
  .vc-tv-cards .vc-tv-r{padding:9px 10px;border-radius:8px;border:1px solid color-mix(in srgb,var(--border,#2a2f37) 70%,transparent);background:color-mix(in srgb,var(--bg2,#1c2026) 55%,transparent)}
  .vc-tv-cards dl{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;margin:6px 0 0;font-size:11px}
  .vc-tv-cards dt{color:var(--dim,#8b93a1)}.vc-tv-cards dd{margin:0;color:var(--fg,#dce1e8);overflow-wrap:anywhere}
  .vc-tv-chart{display:flex;flex-direction:column;gap:5px;font-size:11px}.vc-tv-ch{font-size:10px;color:var(--dim,#8b93a1);text-transform:uppercase;letter-spacing:.06em}
  .vc-tv-bar{display:grid;grid-template-columns:minmax(60px,30%) 1fr auto;gap:8px;align-items:center}
  .vc-tv-bar .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg,#dce1e8)}
  .vc-tv-bar .b{height:10px;border-radius:5px;background:color-mix(in srgb,var(--border,#2a2f37) 50%,transparent);overflow:hidden}
  .vc-tv-bar .b i{display:block;height:100%;border-radius:5px;background:var(--acc,#5a9e8f)}.vc-tv-bar .v{font-family:var(--mono,monospace);color:var(--dim,#8b93a1)}
  .vc-ent-list{display:flex;flex-direction:column}
  .vc-ent-row{display:flex;align-items:baseline;gap:8px;padding:5px 4px;border:0;border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 60%,transparent);background:none;color:inherit;font:inherit;text-align:left;cursor:pointer}
  .vc-ent-row b{font-weight:600;font-size:12px;color:var(--fg,#dce1e8)}.vc-ent-row span{font-size:10.5px;color:var(--dim,#8b93a1)}.vc-ent-row i{margin-left:auto;font-style:normal;font-size:10px;color:var(--dim,#8b93a1)}
  .vc-ent-row:hover,.vc-ent-row.on{background:color-mix(in srgb,var(--acc,#5a9e8f) 10%,transparent)}
  .vc-rbx-list{display:flex;flex-direction:column;gap:0}
  .vc-rbx-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
  .vc-rbx-row{padding:8px 8px;border-radius:8px;cursor:pointer;display:flex;flex-direction:column;gap:4px;border:1px solid transparent;min-width:0}
  /* A LIST YOU CAN READ (owner, 2026-10-10: "web and research sources doesnt look great its hard to read the lists"): rows
     ruled apart, the title the strongest line, the source and age in the reading face rather than 9 px mono, the
     snippet bright enough to read */
  .vc-rbx-list > .vc-rbx-row{border-radius:0;border-width:0 0 1px;border-bottom-color:color-mix(in srgb,var(--border,#2a2f37) 55%,transparent)}
  .vc-rbx-list > .vc-rbx-row:last-child{border-bottom-color:transparent}
  .vc-rbx-row:hover{background:var(--bg2,#272421)}.vc-rbx-row.on{background:var(--bg2,#272421);border-color:var(--border,#3a3530)}
  .vc-rbx-cards .vc-rbx-row{background:var(--bg2,#272421);border-color:var(--border,#3a3530)}.vc-rbx-cards .vc-rbx-row.on{grid-column:1/-1}
  .vc-rbx-hd{display:flex;gap:8px;align-items:flex-start;min-width:0}
  .vc-rbx-fav{width:20px;height:20px;border-radius:6px;flex:none;display:grid;place-items:center;font:700 10px var(--mono,monospace);color:var(--dvc,var(--acc,#5a9e8f));background:color-mix(in srgb,var(--dvc,var(--acc,#5a9e8f)) 18%,transparent)}
  .vc-rbx-tt{min-width:0;flex:1;display:flex;flex-direction:column;gap:2px}
  .vc-rbx-t{font-size:12.5px;font-weight:600;line-height:1.4;color:var(--fg,#ddd);text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  a.vc-rbx-t:hover{text-decoration:underline;color:var(--acc,#5a9e8f)}
  .vc-rbx-sub{display:flex;gap:0;align-items:center;font-size:10.5px;color:var(--dim,#8b93a1);min-width:0}
  .vc-rbx-sub span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40%}
  .vc-rbx-sub > span + span::before{content:" · ";white-space:pre;opacity:.7}
  .vc-rbx-sc{display:inline-block;width:38px;height:4px;border-radius:2px;background:var(--bg3,#302c29);overflow:hidden}.vc-rbx-sc i{display:block;height:100%;background:var(--acc,#5a9e8f)}
  .vc-rbx-s{font-size:11.5px;line-height:1.55;color:color-mix(in srgb,var(--fg,#dce1e8) 72%,transparent);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;padding-left:28px}
  .vc-rbx-cards .vc-rbx-s{padding-left:0;-webkit-line-clamp:4}
  .vc-rbx-open{display:flex;flex-direction:column;gap:6px;padding:4px 0 2px 28px;cursor:default}
  .vc-rbx-full{font-size:11px;line-height:1.55;color:var(--fg,#ddd);white-space:pre-wrap}
  .vc-rbx-meta{display:flex;gap:10px;flex-wrap:wrap;font:9.5px var(--mono,monospace);color:var(--dim2,#8a7e70)}.vc-rbx-meta i{font-style:normal;opacity:.6;margin-right:4px}
  .vc-rbx-acts{display:flex;gap:5px;flex-wrap:wrap}
  .vc-rbx-b{font:inherit;font-size:9.5px;height:21px;padding:0 9px;border:1px solid var(--border,#3a3530);border-radius:10px;background:var(--bg1,#1f1d1a);color:var(--dim2,#8a7e70);cursor:pointer;display:inline-flex;align-items:center;text-decoration:none}
  .vc-rbx-b:hover{color:var(--fg,#ddd);border-color:var(--acc,#5a9e8f)}
  .vc-rbx-body{max-height:420px;overflow:auto;font-size:11.5px;line-height:1.6;padding:8px 10px;border-radius:8px;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530)}
  /* a record's depth: its tags, its fields, its neighbours */
  .vc-rbx-meta a{color:var(--acc,#5a9e8f);text-decoration:none}.vc-rbx-meta a:hover{text-decoration:underline}
  .vc-rbx-tags{display:flex;flex-wrap:wrap;gap:4px}.vc-rbx-tags span{font-family:var(--mono,monospace);font-size:9.5px;color:var(--acc2,var(--acc,#5a9e8f));background:color-mix(in srgb,var(--acc2,var(--acc,#5a9e8f)) 12%,transparent);border-radius:8px;padding:1px 7px}
  .vc-rbx-fields summary{cursor:pointer;font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#8a7e70);letter-spacing:.04em}
  .vc-rbx-kv{display:grid;grid-template-columns:minmax(80px,30%) 1fr;gap:2px 10px;margin-top:4px;font-size:10.5px}
  .vc-rbx-kv .k{color:var(--dim2,#8a7e70)}.vc-rbx-kv .v{font-family:var(--mono,monospace);font-size:10px;overflow-wrap:anywhere}
  .vc-rbx-nb{display:flex;flex-direction:column;gap:4px;margin-top:2px}
  .vc-rbx-nb h5{margin:4px 0 0;font-size:9.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--dim2,#8a7e70)}
  .vc-rbx-nbr{padding:6px 9px;border-radius:7px;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);cursor:pointer}
  .vc-rbx-nbr:hover,.vc-rbx-nbr.on{border-color:color-mix(in srgb,var(--acc,#5a9e8f) 55%,var(--border,#3a3530))}
  .vc-rbx-nbh{display:flex;gap:8px;align-items:baseline}.vc-rbx-nbh b{flex:1;min-width:0;font-weight:500;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vc-rbx-nbh small{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#8a7e70);white-space:nowrap}
  .vc-rbx-nbt{margin-top:6px;font-size:11px;line-height:1.6;max-height:260px;overflow:auto;cursor:text}
  .vc-rbx-busy{font-size:10px;color:var(--acc,#5a9e8f)}.vc-rbx-err{font-size:10px;color:var(--err,#c96b6b)}
  .vc-rbx-empty{padding:14px;text-align:center;font-size:11px;color:var(--dim2,#8a7e70);font-style:italic}
  .vc-rbx-foot{display:flex;align-items:center;gap:10px;font:9.5px var(--mono,monospace);color:var(--dim2,#8a7e70)}
  .vc-rbx-pg{display:inline-flex;align-items:center;gap:4px;margin-left:auto}
  .vc-rbx-pg>button{font:inherit;font-size:12px;width:22px;height:20px;border-radius:6px;border:1px solid var(--border,#3a3530);background:transparent;color:var(--fg,#ddd);cursor:pointer}
  .vc-rbx-pg>button[disabled]{opacity:.35;cursor:default}
  .vc-rbx-dot{width:7px!important;height:7px!important;padding:0;border-radius:50%!important;border:0!important;background:var(--bg3,#302c29)!important;cursor:pointer}.vc-rbx-dot.on{background:var(--acc,#5a9e8f)!important}
  .vc-rbx-tbl tr{cursor:pointer}.vc-rbx-tbl tr.on td{background:var(--bg2,#272421)}.vc-rbx-tbl td.mono{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,#8a7e70);white-space:nowrap}
  .vc-rbx-tr-open td{padding:0 6px 8px}.vc-rbx-tr-open .vc-rbx-open{padding-left:0}
  .vc-src-body{font-size:11px;line-height:1.6;max-height:340px;overflow:auto;
    border-top:1px solid var(--border,#3a3530);padding-top:6px;margin-top:2px}
  .vc-src-shot{width:100%;border-radius:6px;border:1px solid var(--border,#3a3530);margin-top:2px}
  /* the fold you make: ▾ / ▸ at the head of an item, and what a folded list holds beside its name */
  .it-hd .fd{flex:0 0 auto;width:14px;text-align:center;font-size:10px;color:var(--dim,#6b7480);cursor:pointer;user-select:none}
  .it-hd .fd:hover{color:var(--fg,#dce1e8)}
  .it-hd .fdn{flex:0 0 auto;font-family:ui-monospace,Consolas,monospace;font-size:9px;color:var(--dim,#6b7480);background:var(--bg2,#272421);border-radius:8px;padding:0 6px}
  .fd.solo{position:absolute;right:36px;top:3px;z-index:3;font-size:10px;color:var(--dim,#6b7480);cursor:pointer;opacity:0;transition:opacity .15s}
  .it:is(:hover,.hov) > .fd.solo,.it:focus-within > .fd.solo{opacity:.8}
  /* a records item as a GRAPH: the slot the live layer draws the Vera graph over, sized with the item */
  .vc-rbx-graph{display:flex;flex-direction:column;gap:3px}
  .vc-rbx-graph .vc-live{height:300px}
  .vc-rbx.s-s .vc-rbx-graph .vc-live{height:200px}.vc-rbx.s-m .vc-rbx-graph .vc-live{height:260px}
  .vc-rbx.s-l .vc-rbx-graph .vc-live{height:320px}.vc-rbx.s-xl .vc-rbx-graph .vc-live{height:480px}
  .vc-rbx-gnote{font-size:9.5px;color:var(--dim2,#8a7e70)}
  /* a records graph is a glance: the graph's workbench drawer (terminal · table · content · chat) is not drawn here */
  .lv[data-kind="rgraph"] .vg-bottom-area{display:none!important}
  /* READER MODE: a page read as its article - a measure you can read at, the prose face, the page's own shape */
  .vc-rbx-body.rd,.vc-src-body.rd{max-height:620px;background:var(--bg0,#171513);padding:10px 14px}
  .vc-reader{max-width:68ch;margin:0 auto;font-family:var(--f-prose,Georgia,'Iowan Old Style','Times New Roman',serif);font-size:13.5px;line-height:1.7;color:var(--fg,#dce1e8)}
  .vc-rd-t{font-size:19px;line-height:1.25;margin:2px 0 6px;font-weight:650;letter-spacing:-.01em}
  .vc-rd-by{font-family:var(--f-ui,system-ui,sans-serif);font-size:10.5px;color:var(--dim2,#8a7e70);margin-bottom:10px}
  .vc-rd-note{display:inline-block;font-family:var(--f-ui,system-ui,sans-serif);font-size:10px;color:var(--acc2,#c9955a);border:1px dashed currentColor;border-radius:6px;padding:2px 8px;margin:0 0 10px}
  .vc-rd-hero{display:block;width:100%;max-height:220px;object-fit:cover;border-radius:8px;margin:0 0 12px}
  .vc-rd-body h1,.vc-rd-body h2{font-size:16px;line-height:1.3;margin:1.3em 0 .4em}
  .vc-rd-body h3,.vc-rd-body h4,.vc-rd-body h5,.vc-rd-body h6{font-size:14px;margin:1.1em 0 .3em}
  .vc-rd-body p{margin:0 0 .9em}
  .vc-rd-body a{color:var(--acc,#5a9e8f);text-decoration:underline;text-underline-offset:2px}
  .vc-rd-body ul,.vc-rd-body ol{margin:0 0 .9em;padding-left:1.4em}.vc-rd-body li{margin:.2em 0}
  .vc-rd-body blockquote{margin:0 0 .9em;padding:.1em 0 .1em 1em;border-left:3px solid var(--acc,#5a9e8f);color:var(--dim2,#8a7e70);font-style:italic}
  .vc-rd-body hr{border:0;border-top:1px solid var(--border,#3a3530);margin:1.4em 0}
  .vc-rd-body figure{margin:0 0 1em}.vc-rd-img{max-width:100%;height:auto;border-radius:6px}
  .vc-rd-body code{font-size:.86em;background:var(--bg2,#272421);padding:1px 4px;border-radius:4px}
  .vc-rd-body pre{font-size:11.5px;margin:0 0 1em}
  .vc-rd-body .vc-tablewrap{margin:0 0 1em;font-family:var(--f-ui,system-ui,sans-serif)}
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
  .it:is(:hover,.hov) .it-hd .xp{color:var(--fg,#dce1e8)}
  .it-a{display:inline-flex;gap:2px;flex:1 1 auto;min-width:0;align-items:center}
  .it-a button{font:inherit;font-size:9.5px;color:var(--dim,#6b7480);background:none;border:1px solid transparent;
    border-radius:5px;padding:0 5px;cursor:pointer;line-height:1.5}
  .it-a button:hover{color:var(--fg,#dce1e8);border-color:var(--border,#2a2f37)}
  .it-a button.on{color:var(--acc,#5a9e8f)}
  .it-a button.ctx{margin-left:auto}
  .it-a button.ctx.on{color:var(--acc2,#5ec9a0)}
  /* A SIZE IS A BOUND, AND THE CONTENT DECIDES INSIDE IT (the canvas's final form §3.2). An item has always taken
     only the height its content needs; what was fixed was the CEILING — 72 · 180 · 340 flat pixels, whatever the
     column. In a tall column that made every item a letterbox with its own scrollbar while the column below it sat
     empty, and in a short one an "l" item filled the whole thing. The ceiling is a share of the column's own
     viewport now (--vc-vh, measured and set by _placeNow), held between a floor and a cap so a very short or very
     tall column still reads: a small item is a glance, a medium one the working face, a large one the whole of a
     diagram or a page. --vc-vh has a fallback, so an element that never measures (the panel, a test) keeps
     sensible numbers.
     The ceilings are a TRANSITION, so growing or shrinking an item glides rather than jumping (§4.3). */
  .it-bd{padding:2px 8px 6px;overflow:auto;flex:1 1 auto;min-height:0;transition:max-height .24s cubic-bezier(.2,.7,.3,1)}
  .it[data-size="s"] .it-bd{max-height:clamp(56px,calc(.16 * var(--vc-vh,460px)),170px)}
  .it[data-size="m"] .it-bd{max-height:clamp(120px,calc(.34 * var(--vc-vh,460px)),430px)}
  .it[data-size="l"] .it-bd{max-height:clamp(210px,calc(.58 * var(--vc-vh,460px)),780px)}
  .it[data-size="xl"] .it-bd{max-height:none}
  /* SOME KINDS NEED THE ROOM. A share of the column suits a figure, a table, a note — things that read at a glance.
     Code, a page and a structured graph do not: they are the thing you are reading, and at a third of a column they
     are a letterbox with a scrollbar (owner, 2026-09-24: "canvas items are often not tall enough for their content
     - things like code and prose exploded"). They take a larger share, and a floor deep enough to be worth opening
     at all; past that the item's own scroll, the grip and Open in place are still there. */
  .it[data-type="code"] .it-bd,.it[data-type="html"] .it-bd,.it[data-type="explode"] .it-bd,.it[data-type="markdown"] .it-bd{
    max-height:clamp(220px,calc(.62 * var(--vc-vh,460px)),900px)}
  .it[data-size="s"][data-type="code"] .it-bd,.it[data-size="s"][data-type="explode"] .it-bd{max-height:clamp(150px,calc(.3 * var(--vc-vh,460px)),320px)}
  .it[data-size="xl"][data-type="code"] .it-bd,.it[data-size="xl"][data-type="explode"] .it-bd,.it[data-size="xl"][data-type="markdown"] .it-bd{max-height:none}
  /* and a structured graph's slot is drawn at the height it asked for: it is a diagram, not a strip */
  .it[data-type="explode"] .vc-live{min-height:260px}
  @media (prefers-reduced-motion:reduce){.it-bd{transition:none}}
  /* while the transcript is scrolling, a size change is a correction, not a move (the rule .stage already keeps) */
  .stage[data-scrolling] .it-bd{transition:none}
  /* ── FUSED: one element holding two or more (fuseOf). The panes are laid out INSIDE the body, so the card, its
        header, its rail and its ceiling are the group's — which is the whole point: it reads as one thing.
        beside — the lead and its diagram side by side, and the pair falls back to a stack when the column is
                 too narrow to give either of them a readable half.
        over   — the figure, then the rows it is drawn from.
        strip  — indicators flowed along the row, each as wide as it needs, no column of its own. */
  .it-bd.fu{display:flex;gap:9px;align-items:stretch}
  .it-bd.fu>*{min-width:0}
  .it-bd.fu-beside{flex-direction:row}
  .it-bd.fu-over{flex-direction:column}
  .it-bd.fu-strip{flex-direction:row;flex-wrap:wrap;align-items:flex-start;gap:7px}
  .it-bd.fu-beside>*{flex:1 1 0}
  .it-bd.fu-strip>*{flex:0 1 auto}
  .it[data-fuse] .it-bd{max-height:clamp(180px,calc(.52 * var(--vc-vh,460px)),720px)}
  /* a pane: its own label and nothing else — no border, no card. The hairline is the join, not a frame. */
  .fu-p{display:flex;flex-direction:column;min-height:0;min-width:0}
  .it-bd.fu-beside>.fu-p{border-left:1px solid rgba(255,255,255,.055);padding-left:9px}
  .it-bd.fu-over>.fu-p{border-top:1px solid rgba(255,255,255,.055);padding-top:6px}
  .fu-t{display:flex;align-items:center;gap:5px;font-size:9.5px;letter-spacing:.05em;color:var(--dim,#6b7480);
    margin-bottom:3px;flex:0 0 auto}
  .fu-t b{font-weight:600;color:var(--fg2,#c3cad4);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .fu-t .ic{flex:0 0 auto}
  .fu-t button{margin-left:auto;opacity:0;transition:opacity .15s;background:none;border:0;padding:0 2px;
    color:var(--dim,#6b7480);font:inherit;font-size:9px;cursor:pointer}
  .fu-p:hover .fu-t button,.fu-t button:focus{opacity:1}
  .fu-t button:hover{color:var(--acc,#5a9e8f)}
  .fu-b{flex:1 1 auto;min-height:0;overflow:auto}
  /* a pane's drawing takes the pane's height (owner, 2026-10-04: an exploded code item "you can barely see it"): the
     body was a block, so the explode diagram beside its code fell to its 140 px minimum whatever the code's height */
  .it-bd.fu-beside > .fu-p > .fu-b,.it-bd.fu-over > .fu-p > .fu-b{display:flex;flex-direction:column}
  /* an OPENED or resized fused item keeps its panes side by side: the opened body is a column (for one drawing to fill
     it), which stacked the explode under its code at 60 px */
  .it:is(.openin,.sized,.maxed) > .it-bd.fu-beside{flex-direction:row}
  .it:is(.openin,.sized,.maxed) > .it-bd.fu-beside > *{flex:1 1 0;min-width:0}   /* halves, not the code taking it all */
  /* ...and an OPENED fused item still has a ceiling (a dragged size or the maximised view sets its own): without one the
     diagram, which fits itself to its box, and the box, which takes the diagram's height, grew each other - a 20-line
     file's item went 2,142 px tall */
  .it.openin:not(.sized):not(.maxed)[data-fuse] > .it-bd{max-height:clamp(260px,calc(.8 * var(--vc-vh,460px)),900px)!important}
  /* the strip's panes are as wide as their content, not a share of a column */
  .it-bd.fu-strip>.fu-p{max-width:100%}
  .it-bd.fu-strip .fu-b{overflow:visible}
  /* how many are fused in, on the header line that already says what the thing is */
  .fu-w{font-size:9px;color:var(--acc,#5a9e8f);background:color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent);
    border-radius:6px;padding:0 5px;margin-left:4px;flex:0 0 auto}
  /* narrow: a reading pane beside another one is two unreadable columns */
  @container (max-width: 420px){.it-bd.fu-beside{flex-direction:column}
    .it-bd.fu-beside>.fu-p{border-left:0;padding-left:0;border-top:1px solid rgba(255,255,255,.055);padding-top:6px}}
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
  .edit textarea.code{font-family:ui-monospace,Consolas,monospace;white-space:pre;overflow-x:auto;tab-size:4}
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
  /* the live layer draws the thing itself — no ground of its own. A terminal and a rendered page bring their own
     (they are surfaces); a widget, a diagram and a graph are marks on the canvas, and a black plate behind them is
     the tile this canvas is getting rid of. */
  #live .lv{position:absolute;box-sizing:border-box;border-radius:0;overflow:hidden;background:none}
  #live .lv[data-kind="term"],#live .lv[data-kind="preview"],#live .lv[data-kind="panel"]{background:#000;border-radius:4px}
  #live .lv > *{display:block;width:100%;height:100%}
  #live iframe.vc-pframe{border:0;background:var(--s1,var(--bg1,#15181d))}
  /* the chat's syntax colours, inside this shadow root (a rule of the page cannot reach it) */
  .vc-code .hl-kw{color:var(--acc,#5a9e8f);font-weight:600}.vc-code .hl-str{color:var(--acc2,#8fb87a)}.vc-code .hl-com{color:var(--dim,#6b7480);font-style:italic}
  .vc-code .hl-num{color:var(--acc3,#c9955a)}.vc-code .hl-fn,.vc-code .hl-type{color:var(--acc4,#7aa2d6)}.vc-code .hl-tag{color:var(--acc,#5a9e8f)}.vc-code .hl-attr{color:var(--acc2,#8fb87a)}
  .vc-code .hl-add{background:rgba(95,207,154,.14);color:var(--acc2,#8fb87a)}.vc-code .hl-del{background:rgba(232,112,107,.14);color:var(--err,#c96b6b)}.vc-code .hl-hunk{color:var(--acc,#5a9e8f)}
  .vc-lint{font:10px/1.5 var(--mono,ui-monospace,monospace);padding:3px 8px;border-top:1px solid var(--border,#2a2f37);color:var(--ok,#8fb87a)}
  .vc-lint.warn{color:#d8a03a}.vc-lint.err{color:var(--err,#c96b6b)}.vc-lint b{font-weight:400;color:var(--dim,#6b7480)}.vc-lint i{font-style:normal;color:var(--dim,#6b7480)}
  .vc-lint-d{font:10px/1.5 var(--mono,ui-monospace,monospace);padding:1px 8px}.vc-lint-d.err{color:var(--err,#c96b6b)}.vc-lint-d.warn{color:#d8a03a}
  /* results, and the elements they are drawn as */
  .vc-res-h{display:flex;align-items:center;gap:6px;font-size:10.5px;padding:2px 0 6px;min-width:0}
  .vc-res-h code{font:10.5px var(--mono,ui-monospace,monospace);color:var(--fg,#dce1e8)}
  .vc-res-h .mono{font:9.5px var(--mono,ui-monospace,monospace);color:var(--dim,#6b7480);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .vc-res-h .sp{flex:1}.vc-res-h .dot{width:7px;height:7px;border-radius:50%;background:var(--dim,#6b7480);flex:0 0 auto}.vc-res-h .dot.on{background:var(--ok,#8fb87a)}.vc-res-h .dot.bad{background:var(--err,#c96b6b)}
  .vc-res-err{font:11px/1.5 var(--mono,ui-monospace,monospace);color:var(--err,#c96b6b);white-space:pre-wrap;word-break:break-word}
  .vc-json,.vc-kv{font:11px/1.55 var(--mono,ui-monospace,monospace)}.vc-json-t{font:600 11px system-ui,sans-serif;margin:0 0 4px}
  .vc-json details{margin-left:2px}.vc-json summary{cursor:pointer;color:var(--dim,#6b7480);list-style:none}.vc-json summary::before{content:'\u25b8 ';font-size:9px}.vc-json details[open] > summary::before{content:'\u25be '}
  .vc-json .j-row{display:flex;gap:6px;padding-left:12px;min-width:0}.vc-json .j-k{color:var(--acc4,#7aa2d6);flex:0 0 auto}.vc-json .j-k::after{content:':'}
  .vc-json .j-str{color:var(--acc2,#8fb87a);word-break:break-word}.vc-json .j-num{color:var(--acc3,#c9955a)}.vc-json .j-null,.vc-json .j-more{color:var(--dim,#6b7480)}
  .vc-kv .kv-r{display:grid;grid-template-columns:minmax(80px,38%) 1fr;gap:8px;padding:2px 0;border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 50%,transparent)}
  /* fields read in words: the names in the UI face, the values in the mono face; wide, as tiles (the value large) */
  .vc-kv{container:vckv / inline-size}
  .vc-kv .kv-k{font:500 11.5px/1.5 system-ui,sans-serif}.vc-kv .kv-v{font-size:12px}.vc-kv .kv-v.num{font-variant-numeric:tabular-nums}
  @container vckv (min-width: 460px) {
    .vc-kv-g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
    .vc-kv .kv-r{display:flex;flex-direction:column-reverse;gap:2px;padding:8px 10px;border:1px solid var(--border,#2a2f37);border-radius:8px;min-width:0}
    .vc-kv .kv-r.long{grid-column:1 / -1}
    .vc-kv .kv-v{font-size:15px;line-height:1.3;color:var(--fg,#dce1e8);overflow:hidden;text-overflow:ellipsis}
    .vc-kv .kv-r.long .kv-v{font-size:12px}
    .vc-kv .kv-k{font-size:10.5px;color:var(--dim,#8b93a1)}
  }
  .vc-kv .kv-k{color:var(--dim,#6b7480);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vc-kv .kv-v{word-break:break-word}.vc-kv .kv-v.yes{color:var(--ok,#8fb87a)}.vc-kv .kv-v.no{color:var(--err,#c96b6b)}
  .vc-chat{display:flex;flex-direction:column;gap:6px}.vc-msg{border-radius:8px;padding:5px 8px;background:var(--s2,var(--bg2,#1a1c20))}.vc-msg.u{background:color-mix(in srgb,var(--acc,#5a9e8f) 12%,transparent)}
  .vc-who{display:block;font:600 9px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--dim,#6b7480);margin-bottom:2px}
  .vc-many + .vc-many{margin-top:6px}
  /* MAXIMISED: the item takes the canvas and stays there, fixed, until it is restored */
  .mx{font-size:10px;color:var(--dim,#6b7480);flex:0 0 auto;cursor:pointer;padding:0 2px}
  .it:is(:hover,.hov) .it-hd .mx{color:var(--fg,#dce1e8)}.mx.on{color:var(--acc,#5a9e8f)!important}
  .mx.solo{position:absolute;right:20px;top:3px;z-index:3;opacity:0;transition:opacity .15s}
  .it:is(:hover,.hov) > .mx.solo,.it:focus-within > .mx.solo,.it.maxed > .mx.solo{opacity:.8}
  .it.maxed{z-index:8!important;display:flex;flex-direction:column;background:var(--s1,var(--bg1,#15181d))!important;box-shadow:0 12px 40px rgba(0,0,0,.45),0 0 0 1px var(--acc,#5a9e8f)!important;transition:none!important;border-radius:8px}
  .it.maxed > .it-bd{max-height:none!important;flex:1 1 auto;display:flex;flex-direction:column;min-height:0;overflow:auto}
  .it.maxed > .it-bd > *{flex:1 1 auto;min-height:0}
  .it.maxed .vc-codewrap,.it.maxed .vc-diag,.it.maxed .vc-wid,.it.maxed .vc-xp,.it.maxed .vc-term,.it.maxed .vc-panel,.it.maxed .vc-res{display:flex;flex-direction:column;min-height:0;flex:1 1 auto}
  .it.maxed .vc-live{flex:1 1 auto;height:auto!important;min-height:180px}
  .it.maxed .vc-pre{max-height:none!important;flex:1 1 auto;overflow:auto}
  :host([data-maxed]) #body{overflow:hidden!important}
  :host(:not([stage])) .it.maxed{position:sticky;top:0;height:calc(var(--vc-vh,560px) - 16px)}
  /* a preview being redrawn loads BEHIND the one on screen and takes its place when it has painted - never a blank frame */
  #live .lv > iframe.vc-pnext{position:absolute;left:0;top:0;opacity:0;pointer-events:none}
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
  /* the slot the overlay is sized to: a hole, not a plate (the ground it used to paint showed through as a tile
     behind every live item, and behind the ones whose content does not fill it, as a large blank area) */
  .vc-live{flex:1 1 auto;min-height:40px;height:110px;border-radius:0;background:none;display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--t3,var(--dim,#6b7480))}
  .it[data-size="s"] .vc-live{height:40px}.it[data-size="l"] .vc-live{height:230px}.it[data-size="xl"] .vc-live{height:440px}
  /* (the old rule collapsed the slot — see "WHAT YOU DRAGGED IS WHAT THE CONTENT FILLS" above, which replaces it) */
  /* A RENDERED PAGE NEEDS ROOM. The preview slot is a .vc-live, and a code item lands at size "s" (_cvLandSize
     gives 's' to everything that is not a diagram, table or image) - so a whole HTML document, which previews
     itself on sight, was drawn into a 620x40 strip: measured on the canvas, slotHeight 40, frameHeight 40. One
     line of a page, which is not a preview of anything. Diagrams were given their own height years ago
     (.vc-diag below); previews never were. Same treatment: a real height per size, and the dragged size still
     wins through .it.sized. Specificity carries .vc-live.vc-preview over the size rules above. */
  .vc-live.vc-preview{height:260px}
  /* a delivered report is a document to read, not a preview to glance at */
  .vc-live.vc-report{height:420px}.it[data-size="xl"] .vc-live.vc-report{height:560px}
  .it[data-size="s"] .vc-live.vc-preview{height:200px}
  .it[data-size="l"] .vc-live.vc-preview{height:380px}
  .it[data-size="xl"] .vc-live.vc-preview{height:560px}
  .it.sized .vc-live.vc-preview{height:auto}
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
  /* ...the same in the diagram card (the kit's bar over the live drawing) */
  .vc-diagwrap .vc-live{height:var(--dh,150px);max-height:400px}.it[data-size="s"] .vc-diagwrap .vc-live{max-height:120px}.it[data-size="l"] .vc-diagwrap .vc-live{max-height:640px}.it[data-size="xl"] .vc-diagwrap .vc-live{max-height:none}
  .it.sized .vc-diagwrap > .code-card,.it.openin .vc-diagwrap > .code-card,.it.maxed .vc-diagwrap > .code-card{display:flex;flex-direction:column;flex:1 1 auto;min-height:0}
  .it[data-type="diagram"] .it-bd{max-height:none}
  .vc-dgsrc{margin-top:6px;max-height:160px}
  /* a page, deeper: its reading's who and how long, what it names, where it points */
  .vc-src-m{font-size:10px;color:var(--t3,var(--dim,#6b7480));margin:2px 0 4px}
  /* a records item: the names its pages share, and each row's own */
  .vc-rbx-efacets .vc-rbx-fl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--t3,var(--dim,#6b7480));margin-right:4px;align-self:center}
  /* a research run at a glance: its sites as one bar, its pages' dates as a line */
  .vc-rbx-glance{display:flex;flex-direction:column;gap:6px;margin:6px 0 4px}
  /* a table inside a document reads as a table: a ruled header, rows apart, numbers lined up */
  .vc-md table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
  .vc-md th,.vc-md td{padding:5px 9px;text-align:left;vertical-align:top;border-bottom:1px solid color-mix(in srgb,var(--border,#2a2f37) 80%,transparent)}
  .vc-md th{font-weight:600;font-size:11px;color:var(--dim,#8b93a1);border-bottom-color:var(--border2,#3a3f48)}
  .vc-md tbody tr:nth-child(even) td{background:color-mix(in srgb,var(--fg,#dce1e8) 3%,transparent)}
  /* MINIMAL CARDS (owner, 2026-10-11: "make canvas elements more minimalist - hide the excess data displayed at the top
     of the cards like counts and stats - keep controls, menus and filters. but they should only become visible if opened
     or on mouseover in hover mode" - canvas and desk alike). Unless the canvas shows its full chrome (tier "full"):
     the counts and stats at the top of an item are not drawn (a list's own title line - the card's header names it - its
     sites bar and date line, a run's counts and job, a lookup's chips); the controls (the filter, sort and view bar, a
     run's tabs, the pager, the card's own icon row) appear over the item when it is pointed at, used or opened; the
     facets (sites, names) when the filter is in use or the item is opened. */
  :host(:not([data-tier="full"])) .it .vc-rbx > :is(.vc-rbx-top,.vc-rbx-glance){display:none}
  :host(:not([data-tier="full"])) .it :is(.vc-rr-n,.vc-rr-ft,.vc-act-n){display:none}
  :host(:not([data-tier="full"])) .it:not(:hover):not(.hov):not(:focus-within):not(.maxed) .vc-ev-chips{display:none}
  :host(:not([data-tier="full"])) .it .vc-rbx{position:relative}
  :host(:not([data-tier="full"])) .it .vc-rbx > .vc-rbx-tools{position:absolute;left:0;right:0;top:0;z-index:5;margin:0;padding:4px 6px;border-radius:8px;
    background:var(--bg2,#1d2128);box-shadow:0 6px 18px rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .15s}
  :host(:not([data-tier="full"])) .it:is(:hover,.hov,:focus-within,.maxed) .vc-rbx > .vc-rbx-tools{opacity:1;pointer-events:auto}
  /* over the list, it takes no room: a small card (which drops its tool row in the flow, see deskcard) has it too */
  @container deskcard (max-height: 380px) { :host(:not([data-tier="full"])) .it .vc-rbx > .vc-rbx-tools{display:flex!important} }
  :host(:not([data-tier="full"])) .it:not(:focus-within):not(.maxed) .vc-rbx > .vc-rbx-facets{display:none}
  :host(:not([data-tier="full"])) .it:is(:focus-within,.maxed) .vc-rbx > .vc-rbx-tools + .vc-rbx-facets{margin-top:38px}  :host(:not([data-tier="full"])) .it :is(.vc-rbx-foot,.vc-rr-tabs){opacity:0;transition:opacity .15s}
  :host(:not([data-tier="full"])) .it:is(:hover,.hov,:focus-within,.maxed) :is(.vc-rbx-foot,.vc-rr-tabs){opacity:1}
  :host([align="desk"]:not([desk-narrow]):not([data-tier="full"])) .stage .it:not(.desk-chip) > .hda{opacity:0}
  /* THE TITLE HAS THE HEADER WHILE THE CONTROLS ARE HIDDEN (WSU-69, the owner's desk 2026-10-11: titles cut to "C…", "N…"):
     the header kept 168-250 px for controls that are invisible until hover in these tiers. At rest the title takes the
     header; on hover the controls come in over its end on their own ground, faded at the left - nothing reflows. */
  :host([align="desk"]:not([desk-narrow]):not([data-tier="full"])) .stage .it:not(.desk-chip):not(.ownhead) > .it-hd{padding-right:12px!important}
  :host([align="desk"]:not([desk-narrow]):not([data-tier="full"])) .stage .it:not(.desk-chip) > .hda{padding-left:22px;background:linear-gradient(to right, transparent, var(--bg1,#16181d) 20px);border-radius:0 6px 0 0}
  :host([align="desk"]:not([desk-narrow]):not([data-tier="full"])) .stage .it:is(:hover,.hov,:focus-within,.desk-locked,.maxed) > .hda{opacity:1}
  /* ...and a drawing's own chrome (a graph's caption of counts, its view menu, explode's caption with its enlarge) shows
     when the drawing is pointed at; a combined card's tabs when the card is */
  :host(:not([data-tier="full"])) #live .lv :is(.vge-note,.vg-layer){opacity:0;transition:opacity .15s}
  :host(:not([data-tier="full"])) #live .lv:hover :is(.vge-note,.vg-layer){opacity:1}
  :host(:not([data-tier="full"])) .stage .it > .cmp-tabs{opacity:0;transition:opacity .15s}
  :host(:not([data-tier="full"])) .stage .it:is(:hover,.hov,:focus-within,.maxed) > .cmp-tabs{opacity:1}
  /* READING (see _readerFollow): the card fills the room with the page that was opened, alone, at full height */
  .it.reading .vc-rbx > :not(.vc-rbx-list):not(.vc-rbx-cards):not(.vc-tablewrap){display:none!important}
  .it.reading .vc-rbx :is(.vc-rbx-row,tr[data-rec-act]):not(.on){display:none!important}
  .it.reading .vc-rbx :is(.vc-rbx-list,.vc-rbx-cards){display:block;flex:1 1 auto;min-height:0;overflow:auto}
  .it.reading .vc-rbx .vc-rbx-row.on{border:0;padding:4px 2px}
  /* the page is the card's: no box of its own, the card's whole width and height (owner, 2026-10-11: "the text of reader
     mode has a seperate background and doesnt take up the available space") */
  .it.reading .vc-rbx-open :is(.vc-rbx-body,.vc-reader,.vc-rd-body){background:transparent!important;border-color:transparent!important;box-shadow:none!important;max-width:none!important;max-height:none!important;overflow:visible}
  .it.reading .vc-rbx-open .vc-rbx-body{font-size:14.5px;line-height:1.7;padding:4px 6px 18px}
  .rb-back{display:none!important}.it.reading .rb-back{display:inline-flex!important}
  /* A CARD FILLING THE ROOM IS ON TOP (owner, 2026-10-11: "the occasional see-through expanded card ... something triggers
     the transparency"; "minimised elements that are chips can cover its controls"). Measured: the maximised card's
     ground was solid, but it sat at z 8 - the .it.maxed rule written for the held canvas's flow - while
     the desk stacks its cards 10..110 by their distance to the chat: every card and pill drew over it, and through it
     what was under it showed. On the desk it is above every card and pill (150), below only a card being carried (200). */
  :host([align="desk"]:not([desk-narrow])) .stage .it.maxed{z-index:150!important}
  .stage .it:is(.maxed,.reading){background:var(--bg1,#15181d)!important;border-color:var(--border,#2a2f37)!important}
  /* the names and sites are a menu (the names · sites button and its widgets), not rows of pills, unless the full chrome */
  :host(:not([data-tier="full"])) .it .vc-rbx > .vc-rbx-facets{display:none!important}
  .vc-rbx-nbtn{white-space:nowrap}.vc-rbx-nbtn.set{color:var(--acc,#5a9e8f);border-color:color-mix(in srgb,var(--acc,#5a9e8f) 50%,transparent)}
  .vc-rbx-names{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:2px 0 8px;padding:8px 10px;border:1px solid var(--border,#2a2f37);border-radius:9px;background:var(--bg2,#1d2128);position:relative;z-index:6}
  .vc-rbx-names h5{margin:0 0 4px;font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--dim,#8b93a1)}
  .vc-rbx-names vera-widget{display:block;cursor:pointer}
  .it.reading .vc-rr-tabs,.it.reading .vc-rr-q{display:none}
  .it.reading .vc-rr-p:not([data-p="pages"]){display:none!important}
  .it.reading .vc-rr-p[data-p="pages"]{display:block!important;grid-column:1 / -1;border-left:0;padding-left:0;height:100%;max-height:none}
  /* a command that ran: the prompt line, then its output */
  .vc-ran{display:flex;flex-direction:column;gap:6px;min-height:0}
  .vc-ran-cmd{display:flex;align-items:baseline;gap:8px;min-width:0;font-family:var(--mono,monospace);font-size:11.5px}
  .vc-ran-cmd .ps{color:var(--acc,#5a9e8f);flex:none}
  .vc-ran-cmd code{flex:1 1 auto;min-width:0;color:var(--fg,#dce1e8);white-space:pre-wrap;word-break:break-all}
  .vc-ran-cmd .host{flex:none;font-size:10px;color:var(--dim,#8b93a1);padding:0 6px;border:1px solid var(--border,#2a2f37);border-radius:8px}
  .vc-ran .vc-out{margin:0;font-size:11.5px;line-height:1.5;overflow:auto;min-height:0}
  /* the desk's card header names the item; a bare relevance number beside the name ("1.00") says nothing there */
  :host([align="desk"]:not([desk-narrow])) .stage .it > .it-hd > .sc{display:none}
  /* a lookup's evidence (records of kind evidence): its question and how it was looked up; [n] badges; checked and gaps */
  .vc-ev{display:flex;flex-direction:column;gap:6px;margin:4px 0 6px}
  .vc-ev-run{display:flex;align-items:center;gap:8px;min-width:0}
  .vc-ev-run b{font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .vc-ev-run .t{margin-left:auto;font-size:10.5px;color:var(--dim,#8b93a1);white-space:nowrap}
  .vc-ev.running .vc-rr-dot{background:var(--acc,#5a9e8f);animation:vcActPulse 1.2s ease-in-out infinite}
  .vc-ev-q{font-size:12.5px;line-height:1.45;color:color-mix(in srgb,var(--fg,#dce1e8) 85%,transparent)}
  .vc-ev-chips{display:flex;flex-wrap:wrap;gap:4px}
  .vc-ev-chip{font-size:10.5px;border:1px solid var(--border,#2a2f37);border-radius:10px;padding:1px 8px;color:var(--fg,#dce1e8);white-space:nowrap}
  .vc-ev-chip i{font-style:normal;color:var(--dim,#8b93a1);margin-right:5px}.vc-ev-chip i:empty{display:none}
  .vc-ev-chip.ok{color:var(--ok,#5fb98a);border-color:color-mix(in srgb,var(--ok,#5fb98a) 40%,transparent)}
  .vc-ev-chip.bad{color:var(--warn,#e0b25a);border-color:color-mix(in srgb,var(--warn,#e0b25a) 40%,transparent)}
  .vc-rbx-fav.vc-rbx-num{font-size:10.5px;color:var(--acc,#5a9e8f);background:color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent)}
  .vc-rbx-fav.vc-rbx-num.intel{color:var(--dv4,#c58bd6);background:color-mix(in srgb,var(--dv4,#c58bd6) 16%,transparent)}
  .vc-ev-foot{margin:8px 0 2px;font-size:11.5px;color:var(--dim,#8b93a1)}
  .vc-ev-foot > summary{cursor:pointer;font-size:10.5px;letter-spacing:.04em;text-transform:uppercase}
  .vc-ev-foot > summary b{color:var(--warn,#e0b25a);font-weight:600}
  .vc-ev-foot ul{margin:4px 0 0;padding-left:18px;line-height:1.5}
  .vc-ev-gaps{color:var(--warn,#e0b25a)}
  .vc-rbx-map{display:flex;height:18px;border-radius:5px;overflow:hidden;gap:1px}
  .vc-rbx-map button{min-width:6px;border:0;padding:0 5px;cursor:pointer;background:hsl(var(--h) 45% 42% / .55);color:var(--t1,var(--fg,#e6e9ee));font-size:9.5px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;text-align:left}
  .vc-rbx-map button span{pointer-events:none}.vc-rbx-map button.on,.vc-rbx-map button:hover{background:hsl(var(--h) 55% 50% / .85)}
  .vc-rbx-map i{background:var(--s2,var(--bg2,#1c2026))}
  .vc-rbx-tl{display:flex;align-items:center;gap:8px;font-size:9.5px;color:var(--t3,var(--dim,#6b7480));font-variant-numeric:tabular-nums}
  .vc-rbx-tlb{position:relative;flex:1;height:12px;border-bottom:1px solid var(--bd,var(--border,#2a2f37))}
  .vc-rbx-tlb button{position:absolute;bottom:-4px;width:8px;height:8px;margin-left:-4px;border-radius:50%;border:0;padding:0;cursor:pointer;background:hsl(var(--h) 55% 55%)}
  .vc-rbx-tlb button:hover{transform:scale(1.5)}
  .vc-rbx-ents{display:flex;flex-wrap:wrap;gap:4px;margin:2px 0}.vc-rbx-ents span{font-size:9.5px;padding:0 6px;border-radius:99px;background:var(--s2,var(--bg2,#1c2026));color:var(--t2,var(--fg2,#c3cad4))}
  /* a page's pictures inside its reading are figures, not the page: a logo drawn at its natural size filled the item */
  .vc-src-body img,.vc-rbx-body img{max-width:100%;max-height:260px;width:auto;height:auto;object-fit:contain;display:block;margin:6px auto}
  .vc-src-ents{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0 2px}
  .vc-src-out{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:4px 0;font-size:10px}.vc-src-out span{color:var(--t3,var(--dim,#6b7480))}
  .vc-src-out a{color:var(--ac,var(--acc,#5a9e8f));text-decoration:none}.vc-src-out a:hover{text-decoration:underline}
  /* what a turn names: chips in their groups, a pressed one opens under its group */
  .vc-ent{display:flex;flex-direction:column;gap:8px}
  .vc-ent-gl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--t3,var(--dim,#6b7480));margin-bottom:3px}
  .vc-ent-cs{display:flex;flex-wrap:wrap;gap:4px}
  .vc-chip{display:inline-flex;align-items:center;gap:5px;border:0;border-radius:99px;padding:3px 9px;font:inherit;font-size:11px;cursor:pointer;background:var(--s2,var(--bg2,#1c2026));color:var(--t1,var(--fg,#dce1e8));box-shadow:inset 0 0 0 1px var(--bd,var(--border,#2a2f37))}
  .vc-chip i{font-style:normal;font-size:9px;color:var(--t3,var(--dim,#6b7480))}
  .vc-chip:hover,.vc-chip.on{box-shadow:inset 0 0 0 1px var(--ac,var(--acc,#5a9e8f));color:var(--ac,var(--acc,#5a9e8f))}
  .vc-ent-d{margin-top:6px;padding:6px 8px;border-radius:var(--r-sm,6px);background:var(--s2,var(--bg2,#1c2026))}
  .vc-ent-h{display:flex;align-items:center;gap:8px;margin-bottom:4px}.vc-ent-h .mono{font-family:var(--f-mono,ui-monospace,monospace);font-size:9px;color:var(--t3,var(--dim,#6b7480))}.vc-ent-h .sp{flex:1}
  .vc-ent-d p{margin:3px 0;font-size:11px;line-height:1.5;color:var(--t2,var(--fg2,#c3cad4))}
  vera-mermaid.vc-mm{display:block;min-height:120px;margin:.3em 0}`;

  /* ── THE PLACER (Notes/38 §3.5; the canvas's final form §2), pure: items → a column and a top for each. An item sits
     level with the turn using it now — or, for an item added by hand, the turn it was added beside (its level, never a
     relation); items whose turn is not in view pack after; auto items never overlap — a column's next item starts at
     max(its level, the column's bottom + gap); the column chosen is the one that lets it sit highest.

     THREE REGIMES, chosen by whether the caller gives the column its own VIEWPORT:

       stage  (no viewport — the projection this started as, kept for every caller that does not measure): the level
              is the turn's top in the TRANSCRIPT's scroll frame. The column is then as tall as the transcript and
              somebody else drives its scroll.
       held   (a viewport, and the set fits in it): the level is the turn's top ON SCREEN — its top less the
              transcript's scroll — CLAMPED into the column's own viewport, so an item can never be carried off the
              bottom by the turn it belongs to. This is what "stays in view" means, and the column keeps its own
              scroll rather than being driven from the transcript's.
       packed (a viewport, and the set does not fit even folded): the level ORDERS the items and nothing more; they
              stack from the top and the column scrolls itself. An item taller than the viewport is not clipped —
              the caller caps it and its own body scrolls.

     FOLDING: when the set cannot fit, the least strongly held items fold to their header (`hFold`) until it does —
     lowest weight first, and of two equals the lower one — so what you are reading stays whole. A pinned item never
     folds. The placer decides; the caller applies it and hands back honest open heights (`h`) next time, which is
     what keeps this from oscillating. ── */
  /* ── WHAT AN ITEM WANTS, IN COLUMN UNITS, before there is a column count to want it of ──
     under 1 = a share of one column (it flows beside its neighbours) · 1 = a column · 2 = a reading width · 4 = the
     stage. Both the placer's caller (which clamps this to the columns that exist) and autoCols (which CHOOSES how
     many exist) read the widths here, so the count and the widths can never disagree. A kind you read rather than
     glance at wants a reading width; a kind you glance at wants a share. ── */
  const WIDE = { code: 1, html: 1, explode: 1, markdown: 1, panel: 1, session: 1, table: 1 };
  /* colW, when the placer knows it: NOT GREEDY (owner, 2026-10-04: "lots of the widgets or elements in the canvas are very
     greedy with the width - the hover previews are often big enough"). A reading width was two columns - in the chat's
     column, the whole stage - for every markdown, table, panel and large item, the records and widgets enrichment
     places among them. Where one column is wide enough to read (340 px) they take one; code, a page, an explode and a
     terminal keep two, a line of code being worth its width. And a folded chip is the smallest share that still reads
     its title (~170 px), not a fixed third. Opened, dragged or xl still asks for the stage. */
  const CODEISH = { code: 1, html: 1, explode: 1, session: 1 };
  function unitsOf(d, colW) {
    d = d || {};
    if (d.folded) return colW ? ([1 / 3, 0.5, 1].find((s) => s * colW - 10 >= 170) || 1) : 1 / 3;   // a chip, whatever its kind
    if (d.open) return 4;                    // opened in place or dragged: it asked for the stage
    // a FUSED group is one element holding two or more: a strip of indicators is a column wide, anything with a
    // pane beside or under it is a reading width, whatever the lead on its own would have asked for
    if (d.fuse) return d.fuse === 'strip' ? 1 : 2;
    const sz = d.size || 'm', ty = d.type || '';
    if (sz === 'xl') return 4;
    if (WIDE[ty] || sz === 'l') return colW >= 340 && !CODEISH[ty] ? 1 : 2;
    return sz === 'xs' ? (1 / 3) : sz === 's' ? 0.5 : 1;
  }
  /* ── HOW MANY COLUMNS: the width says how many FIT, the content says how many are WANTED, and the answer is the
     smaller (owner, 2026-09-24: "i want the canvas to choose its column count from the width, and the content").

       fit   — a column narrower than `min` stops being readable (a line of code, a paragraph), so the stage holds
               floor((W+gap)/(min+gap)) of them, four at the most.
       want  — the content's own area in column units (`unitsOf`, an xl counted as the reading width it reads as).
               About `per` units per column: one diagram wants one column, a dozen chips want two, a wall of code
               and tables wants everything the width allows. Never more columns than there are items — three
               columns with two things in them is the empty grid this is here to stop being.

     Pure, exported, and the only place the count is decided: the element measures, this chooses. ── */
  function autoCols(width, items, o) {
    o = o || {};
    const gap = o.gap == null ? 10 : o.gap, min = o.min || 300, max = Math.max(1, o.max || 4), per = o.per || 3;
    const fit = Math.max(1, Math.min(max, Math.floor(((width || 0) + gap) / (min + gap))));
    const list = (items || []).filter(Boolean);
    if (!list.length) return 1;
    const area = list.reduce((a, d) => a + Math.min(2, unitsOf(d)), 0);
    const want = Math.max(1, Math.ceil(area / per));
    return Math.max(1, Math.min(fit, list.length, want));
  }

  /* ── FUSING (owner, 2026-09-24: "i wanted to fuse elements and use them together not just display a grid") ──
     Items that are about the same thing are drawn as ONE element with panes in it, not as neighbours in a grid.
     Three bindings, every one of them read off the document rather than guessed at:

       beside — an explode item is a DIAGRAM OF another item (content.binds). The source and its structure graph
                belong in one element, side by side; the item's own graph switch then opens a pane in place instead
                of putting a second card on the stage.
       over   — one figure and one table from the SAME message: the chart over the rows it is a chart of.
       strip  — three or more glanceable items (a share of a column each) from the same message: one strip of
                indicators under one header, instead of three cards each repeating where they came from.

     The LEAD is the item the group is drawn as; a MEMBER loses its own card and keeps its key, so every live slot,
     every run drawn to it and every capability that addresses it go on working. Nothing is written: this is a way
     of drawing the canvas, never a change to it. Pure — blocks in, groups out. ── */
  const FIGURE = { widget: 1, diagram: 1, image: 1, chart: 1 };
  function fuseOf(blocks, o) {
    o = o || {}; const except = o.except || {};
    const list = (blocks || []).filter((b) => b && b.key != null);
    const out = { of: {}, groups: {} };
    if (o.off || list.length < 2) return out;
    const K = (b) => String(b.key);
    const free = (b) => !out.of[K(b)] && !except[K(b)];
    const join = (lead, member, layout, why) => {
      const lk = K(lead), mk = K(member);
      const g = out.groups[lk] || (out.groups[lk] = { lead: lk, members: [], layout, why });
      g.members.push(mk); g.layout = layout; g.why = why; out.of[mk] = lk; out.of[lk] = lk;
    };
    // WHICH MESSAGE an item came from, preferring the reply block over the turn: two replies in one turn are two
    // groups, not one pile
    const msgOf = (b) => { const a = b.anchor && typeof b.anchor === 'object' ? b.anchor : null;
      return a ? String(a.from || a.mid || a.turn || '') : ''; };
    const by = {}; list.forEach((b) => { const m = msgOf(b); if (m) (by[m] = by[m] || []).push(b); });

    // 1. a diagram OF another item, bound by key — the one binding the document states outright
    list.forEach((b) => {
      if (b.type !== 'explode' || !free(b)) return;
      const bind = b.content && b.content.binds ? String(b.content.binds) : ''; if (!bind) return;
      const src = list.find((x) => K(x) === bind && free(x)); if (!src) return;
      join(src, b, 'beside', 'its structure, beside it');
    });
    // 2. one figure and one table out of the same message: the same rows, drawn and listed
    Object.keys(by).forEach((m) => {
      const tables = by[m].filter((b) => b.type === 'table' && free(b));
      const figs = by[m].filter((b) => FIGURE[b.type] && free(b));
      if (tables.length === 1 && figs.length === 1) join(figs[0], tables[0], 'over', 'the rows it is drawn from');
    });
    // 3. three or more glanceable things from one message: a strip, not a stack of cards
    Object.keys(by).forEach((m) => {
      const small = by[m].filter((b) => free(b) && unitsOf({ type: b.type, size: b.size || 'm' }) < 1);
      if (small.length < 3) return;
      small.slice(1).forEach((x) => join(small[0], x, 'strip', small.length + ' at a glance'));
    });
    return out;
  }

  function place(items, turns, o) {
    o = o || {}; const cols = Math.max(1, Math.min(4, o.columns || 1)), gap = o.gap == null ? 10 : o.gap, cw = o.colWidth || 300, pad = o.pad || 0;
    const view = Math.max(0, o.view || 0);   // stage only: the top of the window in view — an item with nothing to stand beside sits where you are looking
    const V = Math.max(0, o.viewport || 0);  // the column's own height; 0 keeps the old projection
    const scroll = o.scrollTop || 0;         // the transcript's scroll, so a turn's top reads as a place ON SCREEN
    const WT = o.weights || {};
    const T = turns || {}; const list = (items || []).filter(Boolean);
    const levelOf = (it) => it.mid || it.beside || ''; const known = (it) => { const m = levelOf(it); return !!(m && T[m] && typeof T[m].top === 'number'); };
    const topOf = (it) => T[levelOf(it)].top - (V ? scroll : 0);
    const rankOf = (it) => known(it) ? topOf(it) : Infinity;
    const order = list.map((it, i) => ({ it, i })).sort((a, b) => { const ka = known(a.it), kb = known(b.it); if (ka && kb) return (topOf(a.it) - topOf(b.it)) || (a.i - b.i); if (ka) return -1; if (kb) return 1; return a.i - b.i; });
    const fullH = (it) => Math.max(1, it.h || 1);
    const foldH = (it) => Math.max(1, Math.min(it.hFold || fullH(it), fullH(it)));
    const folded = new Set();
    const need = () => list.reduce((s, it) => s + (folded.has(String(it.key)) ? foldH(it) : fullH(it)), 0) + gap * Math.max(0, list.length - 1);
    const capacity = cols * Math.max(0, V - pad);
    if (V && o.fold !== false) {
      const wOf = (it) => { const w = WT[String(it.key)]; const n = (w && typeof w === 'object') ? w.score : w; return typeof n === 'number' ? n : 0.5; };
      const cand = list.filter((it) => !it.pinned).sort((a, b) => (wOf(a) - wOf(b)) || (rankOf(b) - rankOf(a)));
      /* FOLDING ONLY EVER RESCUES THE HELD REGIME. If the set cannot fit even with every candidate folded, folding
         buys nothing — the column is going to scroll either way — and folding them anyway would leave the reader
         scrolling a list of header lines. So: ask first whether it can be saved, and only then fold. */
      cand.forEach((it) => folded.add(String(it.key)));
      if (need() > capacity) folded.clear();
      else { folded.clear(); for (let i = 0; i < cand.length && need() > capacity; i++) folded.add(String(cand[i].key)); }
    }
    const fits = !V || need() <= capacity;
    const mode = !V ? 'stage' : (fits ? 'held' : 'packed');
    const heightOf = (it) => folded.has(String(it.key)) ? foldH(it) : fullH(it);
    /* ⛔ A SMALL ITEM DOES NOT TAKE A WHOLE ROW. Every item used to be as wide as the column, so a counter, a
       sticker or a short note sat in a full-width slot with a field of nothing beside it — the "large blank areas"
       the owner reported, and half of why this reads as a rigid grid. An item declares how much width it WANTS
       (`want`, a fraction: a sticker a third, a small item a half, everything else the whole) and the placer flows
       them: an item that fits beside the one before it sits beside it, and the row's height is the tallest in it.
       Nothing else changes — the level, the fold, the regimes are all as they were. */
    // a fraction of one column, or a NUMBER OF COLUMNS to span (capped at what the stage has)
    const wantOf = (it) => { const w = +it.want; if (!(w > 0)) return 1; return w <= 1 ? w : Math.min(cols, Math.round(w)); };
    /* A WIDTH IN PIXELS, when the item has one (dragged, or content that has its own width). It is NOT rounded to a
       share or to a whole number of columns — that rounding is what made a resized item snap back to the full stage —
       so an item like this covers the columns it happens to cover and keeps the width it was given. */
    const stageW = cols * cw + (cols - 1) * gap;
    const pxOf = (it) => { const p = +it.wpx; return p > 0 ? Math.min(p, stageW) : 0; };
    const bottoms = new Array(cols).fill(pad), used = new Array(cols).fill(false); const out = []; let maxB = pad;
    const row = { y: 0, x: 0, h: 0, col: -1, on: false, chip: false };   // the flow row in progress
    order.forEach(({ it }) => {
      /* a pixel width DEFINES the want, rather than sitting beside one: a caller that sends both (the element sends
         the fraction it worked out for its own sizing pass) cannot then disagree with itself about how many columns
         the item covers. */
      const h = heightOf(it), wpx = pxOf(it);
      const want = wpx ? Math.min(cols, (wpx + gap) / (cw + gap)) : wantOf(it);
      const ideal = (!known(it) || mode === 'packed') ? null
        : mode === 'stage' ? Math.max(pad, topOf(it))
        : Math.max(pad, Math.min(topOf(it), Math.max(pad, V - h)));   // held: level with its turn, never off the bottom
      /* DOES IT GO BESIDE THE ONE BEFORE IT? The row in progress is tried FIRST — otherwise the column's bottom has
         already moved past that row and every item is pushed underneath, which is the full-width column this is
         replacing. It goes beside only when the row has the width for it and its own level is not below the row:
         an item is never dragged off its level to make a row look tidy. */
      /* AN ITEM MAY SPAN COLUMNS. A `want` above 1 is a number of COLUMNS: a page, a diagram or a structured graph
         is not readable in a third of a three-column stage, and the columns are there to let small things sit
         together, not to cut big ones into strips (owner, 2026-09-24: "canvas items should be able to span
         columns"). A span takes the first run of columns wide enough for it, level with its turn like anything
         else, and ends the row in progress — nothing sits beside a thing that wide. */
      if (want > 1 && cols > 1) {
        // the columns it COVERS (a dragged width covers the partial one it reaches into), never what it is cut to
        const span = Math.max(1, Math.min(cols, wpx ? Math.ceil(want - 0.02) : Math.round(want)));
        let bcol = 0, by = Infinity;
        for (let c = 0; c + span <= cols; c++) {
          let floor = pad; for (let k = c; k < c + span; k++) floor = Math.max(floor, used[k] ? bottoms[k] + gap : bottoms[k]);
          const y = ideal == null ? Math.max(floor, mode === 'stage' ? view + pad : pad) : Math.max(ideal, floor);
          if (y < by) { by = y; bcol = c; }
        }
        out.push({ key: it.key, mid: it.mid || '', col: bcol, span, x: bcol * (cw + gap), y: by, h,
                   w: wpx || (span * cw + (span - 1) * gap), want,
                   level: ideal != null && by === ideal, folded: folded.has(String(it.key)), beside: false });
        for (let k = bcol; k < bcol + span; k++) { bottoms[k] = by + h; used[k] = true; }
        maxB = Math.max(maxB, by + h); row.on = false; return;
      }
      /* HOW FAR AN ITEM WILL COME TO JOIN A ROW. Level-with-the-turn is a preference, and for a CHIP — an item
         folded to its header line, or a sticker — it is worth almost nothing: every canvas item belongs to a
         different turn, so a rule of "same level only" meant chips NEVER shared a row and the canvas was a stack of
         one-line bars (owner, 2026-09-24: "not letting the items sit next to each other - they still occupy an
         entire column"). A chip will come up to a third of the column to join a run of chips; anything larger keeps
         its level, because a big item in the wrong place is a real loss and a chip in the wrong row is not. */
      const chip = h <= 46 && want <= 0.5;
      const reach = chip && row.chip ? Math.max(90, (V || 600) / 3) : 0.5;
      const canBeside = row.on && want < 1 && row.x + want <= 1.0001 && (ideal == null || ideal <= row.y + reach);
      let best, bestY, beside = false;
      if (canBeside) { best = row.col; bestY = row.y; beside = true; }
      else {
        best = 0; bestY = Infinity;
        for (let c = 0; c < cols; c++) { const floor = used[c] ? bottoms[c] + gap : bottoms[c];
          const y = ideal == null ? Math.max(floor, mode === 'stage' ? view + pad : pad) : Math.max(ideal, floor);
          if (y < bestY) { bestY = y; best = c; } }
      }
      const x0 = beside ? row.x : 0;
      out.push({ key: it.key, mid: it.mid || '', col: best, x: best * (cw + gap) + Math.round(x0 * cw), y: bestY, h,
                 w: wpx || (want < 1 ? Math.round(want * cw) - gap : cw), want,
                 level: ideal != null && bestY === ideal, folded: folded.has(String(it.key)), beside });
      if (beside) { row.x += want; row.h = Math.max(row.h, h); row.chip = row.chip && chip; }
      else { row.y = bestY; row.x = want; row.h = h; row.col = best; row.on = want < 1; row.chip = chip; }
      bottoms[best] = Math.max(bottoms[best], row.y === bestY ? bestY + row.h : bestY + h);
      used[best] = true; maxB = Math.max(maxB, bottoms[best]); });
    return { placements: out, height: maxB + pad + 8, columns: cols, mode, fits, viewport: V, folded: [...folded] };
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
  /* A WHOLE PAGE draws itself without being asked (Notes/42 defect 90): a doctype, an <html> or a <body> is a
     document the reply finished writing, not a fragment it was discussing. A fragment, a stylesheet or a script
     waits to be asked - running those on sight would execute things a reply was only talking about. */
  const WHOLE_PAGE = (lang, code) => ['html', 'svg'].indexOf(String(lang || '').toLowerCase()) >= 0
    && /<\s*(!doctype\s+html|html[\s>]|body[\s>]|svg[\s>])/i.test(String(code || ''));
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
  const KIND_GLYPH = { note: '✎', markdown: 'MD', code: '{}', explode: '✵', session: '>_', table: 'TB', widget: 'WG', loop: '⟳', diagram: '◇', image: '▣', schedule: '⏰', html: '<>', suggest: '✦', notebook: 'NB', panel: '▥', result: '\u21b3', json: '{\u2026}', kv: 'KV', chat: '\u275d' };
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
  function suggestionsOf(doc, blocks, focusMid, o) {
    const offer = String((o && o.explodeOffer) || 'both').toLowerCase();   // both · code · never (the canvas's setting)
    const out = []; const have = new Set((blocks || []).filter(b => b && b.key).map(b => String(b.key)));
    const slug = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'item';
    const push = (s, mid) => { if (!s) return; const o = typeof s === 'string' ? { n: s } : s; const n = String(o.n || o.title || o.text || o.label || ''); if (!n) return;
      const kind = String(o.kind || o.type || 'note'); const key = String(o.key || (kind + ':' + slug(n)));
      out.push({ n, kind, key, content: o.content && typeof o.content === 'object' ? o.content : { title: n, text: n }, mid: String(o.mid || o.turn || mid || ''), taken: have.has(key) }); };
    const docS = doc && Array.isArray(doc.suggestions) ? doc.suggestions : [];
    docS.forEach(s => push(s, focusMid));
    (blocks || []).forEach((b) => { if (!b || !b.key || (b.state || 'now') === 'hidden') return; const c = b.content || {}; const arr = Array.isArray(c.suggestions) ? c.suggestions : Array.isArray(c.can_also) ? c.can_also : [];
      const a = b.anchor && typeof b.anchor === 'object' ? String(b.anchor.turn || b.anchor.mid || '') : ''; arr.forEach(s => push(s, a || focusMid)); });
    /* CODE ON THE CANVAS OFFERS ITS OWN DIAGRAM -- offered, never forced ("the system proposes; the reader
       decides"). A code item of more than a screenful, and a passage long enough to have structure, add
       themselves to "Vera can also"; taking one builds the explode item BOUND to it, so the diagram and the
       source light each other by span. Anything already exploded does not ask again, and at most two ask at
       once so the rail stays a rail. How much it offers is the reader's own setting — both, code alone, or never
       (owner, 2026-09-23); "never" still leaves every other way in (the message action, the fence button, /explode).

       ⛔ AND IT IS NO LONGER A CARD OF ITS OWN. "Explode this code" arrived as a ghost item in the NOW band — a
       card, in the reader's way, about another card (owner, 2026-09-24: "instead of a separate Vera can also card
       id like a smoother mechanism to change to the exploded mode built into the existing element"). The offer
       belongs to the thing it is about: the item that CAN be exploded carries the switch, and `explodeOffer` still
       decides whether it is drawn at all (see `canExplode` where the card is built). What is left here is the
       document's own suggestions, which are about the canvas rather than about one item. */
    const seen = new Set(); return out.filter(s => !seen.has(s.key) && seen.add(s.key)).slice(0, 8);
  }
  /* what an item can be turned into, where the item itself can offer it: code with enough of it to have a shape, a
     passage long enough to have structure. The reader's setting narrows it; "never" leaves every other way in. */
  const PROSE_LANGS = new Set(['text', 'txt', 'plain', 'plaintext', 'markdown', 'md', 'prose', 'none']);
  function proseLike(t, lang) {
    lang = String(lang || '').toLowerCase().trim();
    if (PROSE_LANGS.has(lang)) return true;
    if (lang) return false;
    const s = String(t || ''); const lines = Math.max(1, s.split('\n').filter((l) => l.trim()).length);
    const sym = (s.match(/[{}();=<>\[\]$#|&]/g) || []).length / Math.max(1, s.length);
    const words = s.split(/\s+/).filter(Boolean).length / lines;
    return sym < 0.015 && words >= 6 && /[.!?]["')\]]?(\s|$)/.test(s);
  }
  function canExplode(b, offer) {
    offer = String(offer || 'both').toLowerCase(); if (offer === 'never' || !b || !b.key) return '';
    const c = b.content || {};
    /* PROSE IN A FENCE IS PROSE (owner, 2026-10-09: "text output to the desk from the chat ... explode opens a code explode
       area not prose and the text is prose"): the chat lands every fenced block as a code item, so a passage fenced as
       text / markdown - or with no language that reads as sentences - is offered the prose explode, not the code one */
    if (b.type === 'code' && typeof c.code === 'string') {
      if (proseLike(c.code, c.lang || c.language)) return (offer === 'both' && c.code.length >= 400) ? 'prose' : '';
      if (c.code.split('\n').length >= 12) return 'code';
    }
    if (offer !== 'both') return '';
    const prose = (b.type === 'markdown' && typeof c.md === 'string') ? c.md
                : (b.type === 'note' && typeof c.text === 'string') ? c.text : '';
    return (prose && prose.length >= 800) ? 'prose' : '';
  }
  /* the NOW bar's words: what this turn is waiting on, else what is live */
  /* WHAT THE BAND SAYS, WHICH IS ONLY EVER SOMETHING TO ACT ON. It used to count: "3 now · 2 in focus", over a
     column in which those three items are visible and countable by eye — a readout of the obvious, and the owner
     asked for it to go (2026-09-23). What is left is what the items themselves cannot tell you: that this turn is
     waiting on an answer from you, that the answer went, and what else Vera could put here. When there is none of
     that, the bar says nothing at all and the caller does not draw it. */
  function nowText(now, decision) {
    /* and it counts nothing at all now — not even the suggestions. "NOW · 1 suggested" was a line above a card
       that says the same thing, doing nothing you could act on (owner, 2026-09-24). What is left is the only
       state the items cannot show for themselves: this turn is waiting on an answer from you, or it got one. */
    if (decision && !decision.answer) return (hhmm(decision.since) ? hhmm(decision.since) + ' · ' : '') + 'waiting on you · 1 input';
    if (decision) return (hhmm(decision.answered) ? hhmm(decision.answered) + ' · ' : '') + 'answered';
    return '';
  }
  /* A DRAGGED HEIGHT, AS THE SIZE RECORD IT BECOMES. The thresholds follow the same bounds the faces are drawn to
     (§3.2), so dragging an item to "about a third of the column" records `m` in a tall column and in a short one
     alike; without a viewport they are the numbers this always used. */
  function sizeOfHeight(h, vh) {
    h = Number(h) || 0; vh = Number(vh) || 0;
    if (!vh) return h <= 96 ? 's' : h <= 210 ? 'm' : h <= 380 ? 'l' : 'xl';
    const at = (f, lo, hi) => Math.max(lo, Math.min(f * vh, hi));
    return h <= at(.16, 56, 170) + 24 ? 's' : h <= at(.34, 120, 430) + 30 ? 'm' : h <= at(.58, 210, 780) + 40 ? 'l' : 'xl';
  }
  /* the turns in order of their measured tops; an item is aged when its turn is more than `back` turns behind the focus */
  function turnOrder(turns) { const T = turns || {}; return Object.keys(T).filter(k => T[k] && typeof T[k].top === 'number').sort((a, b) => T[a].top - T[b].top); }
  function isAged(mid, focusMid, order, back) {
    back = back == null ? 2 : back; if (!mid || !focusMid || mid === focusMid) return false;
    const i = (order || []).indexOf(mid), f = (order || []).indexOf(focusMid); if (i < 0 || f < 0) return false; return i < f - back;
  }
  /* compact = a header line: Hover and Zen fold everything not opened; an aged item folds in every tier; the NOW
     items (the decision, the suggestions), a hovered one, the turn in view's own and anything the relevance pass holds
     IN FOCUS never fold — the band says "in focus", so the item is open (a placed widget folded away one turn later) */
  /* WHAT FOLDS TO A HEADER LINE. A tier is a preference about items the conversation has MOVED ON from, not an
     instruction to fold the whole canvas: in Zen every item folded, so a canvas of eight things was eight identical
     bars — a list of titles, which is exactly the "rigid grid" the owner keeps reporting. An item folds when it has
     aged, or when a focus set exists and it is not in it. With no focus and no ageing (a canvas open on its own,
     every item as current as the next) nothing folds, whatever the tier. */
  function foldOf(o) {
    o = o || {}; if (o.now || o.open || o.hovered || o.fresh || o.inFocus) return false;
    if (o.aged) return true;
    return !!(o.tier && o.tier !== 'full' && o.hasFocus && !o.inFocus);
  }
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
    const c = (b && b.content) || {}; const own = c.title || c.name || c.goal || c.filename || c.caption || c.widget || c.panel || c.cap;
    if (own) return String(own);
    if (b && b._bid) { const first = String(c[textFieldOf(b.type)] || '').split('\n').map(s => s.trim()).find(Boolean) || ''; const line = first.replace(/^#+\s*|^[-*]\s+\[.\]\s*|^[-*]\s+|\*\*/g, '').slice(0, 60); return line || ({ diagram: 'Diagram', explode: 'Explode', table: 'Table', image: 'Image', code: 'Code', markdown: 'Text', note: 'Note', html: 'HTML' }[b.type] || String(b.type || 'block')); }
    /* no title of its own: named by what it holds, never by its key ("m7:code:0", "m9:table:0" read as nothing) - code by
       its language and first definition, a table by its first columns and its rows, a terminal by its command */
    if (b && b.type === 'code' && c.code) { const m = String(c.code).match(/^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|fn|func)\s+([A-Za-z_$][\w$]*)/m);
      return (c.lang || 'code') + ' · ' + (m ? m[1] : String(c.code).replace(/\n$/, '').split('\n').length + ' lines'); }
    if (b && b.type === 'table' && Array.isArray(c.columns)) { const cols = c.columns.map(String).filter((x) => x.trim()).slice(0, 3);
      const n = Array.isArray(c.rows) ? c.rows.length : 0; if (cols.length || n) return (cols.length ? cols.join(' · ') + ' — ' : 'Table — ') + n + ' row' + (n === 1 ? '' : 's'); }
    if (b && b.type === 'session' && c.command) return '$ ' + String(c.command).replace(/\s+/g, ' ').slice(0, 60);
    if (b && b.type === 'html' && c.html) { const h = String(c.html).match(/<(title|h1|h2|h3)[^>]*>([\s\S]*?)<\/\1>/i);
      const tx = h ? h[2].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim() : ''; if (tx) return tx.slice(0, 70); }
    return String(b && b.key ? String(b.key).split(':').slice(1).join(':') : '') || String((b && b.type) || 'block');
  }
  const EDITABLE = ['note', 'markdown', 'code', 'html'];
  /* the desk card's controls, as icons (vera-design: an icon, never a letter), each in the theme's own colour */
  const HDI = ((s) => { const o = {}; Object.keys(s).forEach((k) => { o[k] = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + s[k] + '</svg>'; }); return o; })({
    lock: '<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
    unlock: '<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 4.8-.9"/>',
    explode: '<circle cx="4" cy="4" r="1.6"/><circle cx="12" cy="5" r="1.6"/><circle cx="7" cy="12" r="1.6"/><path d="M5.5 4.2l5 .6M4.6 5.5l1.8 5M11 6.4l-3.1 4.4"/>',
    source: '<path d="M6 4L2.5 8 6 12M10 4l3.5 4L10 12"/>',
    edit: '<path d="M3 13l.8-3.2L10.4 3.2l2.4 2.4-6.6 6.6z"/>',
    ctx: '<circle cx="8" cy="8" r="5"/><circle cx="8" cy="8" r="1.6" fill="currentColor"/>',
    max: '<path d="M3 6V3h3M13 6V3h-3M3 10v3h3M13 10v3h-3"/>',
    restore: '<path d="M6 3v3H3M10 3v3h3M6 13v-3H3M10 13v-3h3"/>',
    drop: '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    pill: '<rect x="2.5" y="9.5" width="11" height="4" rx="2"/><path d="M8 2.5v4.5M5.8 5l2.2 2.2L10.2 5"/>',
    more: '<circle cx="3.5" cy="8" r="1" fill="currentColor"/><circle cx="8" cy="8" r="1" fill="currentColor"/><circle cx="12.5" cy="8" r="1" fill="currentColor"/>',
    list: '<path d="M3 4.5h10M3 8h10M3 11.5h10"/>',
    cards: '<rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1"/><rect x="9" y="9" width="4.5" height="4.5" rx="1"/>',
    table: '<rect x="2.5" y="3" width="11" height="10" rx="1"/><path d="M2.5 6.5h11M2.5 9.8h11M6.5 3v10"/>',
    graph: '<circle cx="4" cy="11.5" r="1.6"/><circle cx="8" cy="4" r="1.6"/><circle cx="12.5" cy="10" r="1.6"/><path d="M4.8 10.1l2.4-4.7M9.2 5.2l2.6 3.4M5.6 11.3l5.3-1"/>',
    chart: '<path d="M3 13V8M6.5 13V4M10 13V9.5M13.5 13V6"/>',
    chips: '<rect x="2" y="3.5" width="6" height="3.5" rx="1.75"/><rect x="9" y="3.5" width="5" height="3.5" rx="1.75"/><rect x="2" y="9" width="8" height="3.5" rx="1.75"/>',
    fields: '<path d="M3 4.5h3M3 8h3M3 11.5h3M8 4.5h5M8 8h5M8 11.5h5"/>',
  });
  /* VIEWS - THE SAME THING, SEEN ANOTHER WAY (owner, 2026-10-10: "the elements used to be able to swap from graph to card to
     list can you extend this out to all compatible canvas/desk elements - i cant see the option to do it on most elements").
     A kind that can be seen more than one way says how: records (graph · list · cards · table), a table (table · cards ·
     list · chart when a column is numbers), the names a reply mentions (chips · list). The choice is the reader's, per
     item, kept in this browser; a records item's lives with its other browsing state. */
  const VIEW_LABEL = { list: 'List', cards: 'Cards', table: 'Table', graph: 'Graph', chart: 'Chart', chips: 'Chips', fields: 'Fields' };
  const viewStore = (el) => { if (el._views) return el._views; try { el._views = JSON.parse(localStorage.getItem('vera:canvas:views:' + el.canvasId) || '{}') || {}; } catch (e) { el._views = {}; } return el._views; };
  const viewOf = (el, key, dflt) => (el && key && viewStore(el)[key]) || dflt;
  const viewSet = (el, key, v) => { const m = viewStore(el); m[key] = v; try { localStorage.setItem('vera:canvas:views:' + el.canvasId, JSON.stringify(m)); } catch (e) { /* this page only */ } };
  /* a table of three or more columns reads as a list on a desk card (a 250 px card cut a model table into words per line);
     a narrow one, or one on a held column, as a table - the reader's choice always wins */
  const tableDefault = (c, el) => (el && el.deskAlign && el.deskAlign() && (c.columns || []).length >= 3) ? 'list' : 'table';
  const numCol = (c) => { const cols = c.columns || [], rows = (c.rows || []).filter(Array.isArray); for (let j = 1; j < cols.length; j++) { const vals = rows.map((r) => parseFloat(String(r[j]).replace(/[,%$£€]/g, ''))); if (rows.length >= 2 && vals.filter((v) => isFinite(v)).length >= Math.ceil(rows.length * 0.8)) return j; } return -1; };
  const recCanGraph = (c, el, key) => { if (c.kind === 'memory' || c.view === 'graph') return true; const ents = (el && el._recEnts && el._recEnts[String(key)]) || {}; const n = {}; Object.keys(ents).forEach((id) => (ents[id] || []).forEach((e) => { n[e[0]] = (n[e[0]] || 0) + 1; })); return Object.keys(n).some((k) => n[k] >= 2); };
  /* the views an item offers, and the one it shows: [] when it has only one way to be seen */
  const viewsOf = (b, el) => { const c = (b && b.content) || {}, key = String((b && b.key) || '');
    if (b.type === 'records') { const v = (recCanGraph(c, el, key) ? ['graph'] : []).concat(['list', 'cards', 'table']); return { views: v, cur: recState(el, key).view || 'list' }; }
    if (b.type === 'table' && Array.isArray(c.rows) && c.rows.length) { const v = ['table', 'cards', 'list'].concat(numCol(c) > 0 ? ['chart'] : [], tableCats(c).length ? ['graph'] : []); return { views: v, cur: viewOf(el, key, tableDefault(c, el)) }; }
    if (b.type === 'entities' && Array.isArray(c.groups) && c.groups.length) return { views: ['chips', 'list', 'graph'], cur: viewOf(el, key, 'chips') };
    if (b.type === 'kv') { const f = c.fields || c.data || {}; const n = Array.isArray(f) ? f.length : Object.keys(f).length; if (n >= 3) return { views: ['fields', 'graph'], cur: viewOf(el, key, 'fields') }; }
    return { views: [], cur: '' }; };
  /* the kinds whose drawer already draws a head — name, state and the controls that belong to that kind. The card
     does not draw a second one over them; see "ONE HEADER PER ITEM" where the card is built. */
  /* the person's behind / front / among pick per panel item, kept in this browser for every canvas and frame */
  const DESK_MIN_W = 520;   /* layout px of stage: below it a Desk canvas lays out as Held (deskAlign) */
  const LAYER_KEY = 'vera:canvas:layers';
  function layersLoad() { try { const m = JSON.parse(localStorage.getItem(LAYER_KEY) || '{}'); return m && typeof m === 'object' ? m : {}; } catch (e) { return {}; } }
  function layersSave(m) { try { localStorage.setItem(LAYER_KEY, JSON.stringify(m)); } catch (e) { /* kept for this page */ } }
  const OWN_HEAD = new Set(['code', 'html', 'explode', 'session', 'diagram', 'notebook', 'panel']);

  class VeraCanvas extends (typeof HTMLElement !== 'undefined' ? HTMLElement : class {}) {
    /* align · preview · explode-offer are the canvas's SETTINGS (the owner's four decisions, 2026-09-23), handed down
       from the page's Settings rather than decided here:
         align="held|strict"        held (the default) holds an item in the column's own viewport; strict keeps the old
                                    projection — always exactly level with its turn, the column scrolled in step,
                                    which is what somebody watching one long turn wants.
         preview="on|off"           whether an HTML item, and a code block that is a whole page, DRAW by default or
                                    show their source. Either way the drawing is a sandboxed frame.
         explode-offer="both|code|never"  whether the canvas offers to explode a code item, a prose item, or neither. */
    static get observedAttributes() { return ['canvas-id', 'rows', 'compact', 'columns', 'rail', 'session-id', 'bare', 'blocks', 'align', 'preview', 'explode-offer', 'fuse', 'chat-side', 'chat-rect', 'desk-arrange', 'panel-room']; }

    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._rev = null;
      this._timer = null;
      this._open = new Set();     // items opened in place
      this._px = {};              // a dragged height per item, within its size record
      this._pw = {};              // and a dragged WIDTH: pixels, not a share of a column (owner, 2026-09-25)
      this._editKey = null;       // the item whose text is being edited
      this._layer = {};           // a desk panel item sent behind the others or brought in front: key -> 'behind'|'front'
      this._rzT = 0;
    }

    connectedCallback() {
      this.shadowRoot.innerHTML = `<style>${CSS}</style>
        <div class="shell"><aside class="rail" id="rail" data-w="canvas.rail" hidden></aside>
        <div class="wrap">
          <div class="back" part="back"><slot name="back"></slot></div>          <div class="head">
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
      if (this._stageRO) { try { this._stageRO.disconnect(); } catch (e) {} this._stageRO = null; }
    }

    attributeChangedCallback(name) {
      if (name === 'canvas-id' && this.shadowRoot.childElementCount) {
        this._rev = null;
        this.refresh();
      }
      if (name === 'columns' && this._doc) this.render(this._doc);
      if (name === 'rail' && this.shadowRoot.childElementCount) { if (this.hasAttribute('rail')) this._railMount(); else { const r = this.shadowRoot.getElementById('rail'); if (r) r.hidden = true; if (this._railTimer) { clearInterval(this._railTimer); this._railTimer = null; } } }
      if (name === 'session-id' && this._railTimer) this._railRefresh();
      // a setting changed: preview and the explode offer are drawn, alignment is placed
      if ((name === 'preview' || name === 'explode-offer' || name === 'fuse') && this._doc) this.render(this._doc);
      if (name === 'align' && this.hasAttribute('stage')) { this._view = null; this._html = null; if (this._doc) this.render(this._doc); else this._placeNow(); }
      if ((name === 'chat-side' || name === 'desk-arrange') && this.deskAlign() && this.hasAttribute('stage')) this._placeNow();
      if (name === 'panel-room') { try { this._panelRoom(); } catch (e) { /* optional */ } }
      // a floating chat moved: place again on the next frame (a drag sends many)
      if (name === 'chat-rect' && this.deskAlign() && this.hasAttribute('stage') && !this._chatRaf) {
        this._chatRaf = requestAnimationFrame(() => { this._chatRaf = 0; this._placeNow(); });
      }
    }
    /* held (the default) or strict: the one place the two regimes are named, so nothing else has to ask twice */
    strictAlign() { return String(this.getAttribute('align') || 'held').toLowerCase() === 'strict'; }
    /* desk: the approved desk's rules (/ui/desk-layout.js, window.VeraDesk) with the chat docked on the column's left */
    /* the desk needs room: on a stage narrower than DESK_MIN_W (narrow and compact screens) "sized by distance from the
       chat" has nowhere to go - every card came out full size and piled up (the verification matrix: 171-190 overlapping
       pairs) - so the canvas lays out as Held until it has room again; desk-narrow says so */
    deskAlign() { return String(this.getAttribute('align') || '').toLowerCase() === 'desk' && !this.hasAttribute('desk-narrow'); }
    previewOn() { return String(this.getAttribute('preview') || 'on').toLowerCase() !== 'off'; }
    explodeOffer() { const v = String(this.getAttribute('explode-offer') || 'both').toLowerCase(); return ['both', 'code', 'never'].indexOf(v) >= 0 ? v : 'both'; }
    /* ── the stage (the column's split projection): the host hands the transcript's turn tops ({mid:{top,height}} in
       the transcript's scroll frame) and keeps the column's scroll in step; the element places and reports. ── */
    setTurns(turns, o) {
      this._turns = turns && typeof turns === 'object' ? turns : {}; this._turnsH = o && o.height > 0 ? o.height : 0;
      // the turn in focus (data-focus-mid, set by the host) decides which items have aged; a change repaints
      const fm = this.dataset.focusMid || '';
      if (fm !== this._focusMid) { this._focusMid = fm; if (this._doc && this._doc.mode === 'session') { this.render(this._doc); return; } }
      if (this.hasAttribute('stage')) this._placeNow();
    }
    /* WHERE THE TRANSCRIPT IS, WHICH IS NOT WHERE THE COLUMN IS. The host says how far the transcript has scrolled
       and where its top sits; the column places its items level with the turns on screen and keeps its own scroll.
       It used to be the other way round — the column's scrollTop was written from the transcript's on every frame,
       which is why an item moved with its turn instead of staying in view, and why an item taller than the room
       below its turn could not be scrolled to (the canvas's final form §0.2.2). */
    setView(msgsScrollTop, msgsTopClient) {
      const st = this.shadowRoot.getElementById('stage'); if (!st) return;
      if (this._rz) return;                                  // a resize in hand keeps the column still
      /* the transcript is moving: place instantly until it has been still for a beat (defect 89) */
      st.setAttribute('data-scrolling', '');
      if (this._scrollIdle) clearTimeout(this._scrollIdle);
      this._scrollIdle = setTimeout(() => { this._scrollIdle = null; try { st.removeAttribute('data-scrolling'); } catch (_) {} }, 140);
      const before = this._scrollTop;
      this._scrollTop = Math.max(0, Math.round(msgsScrollTop || 0));
      this._viewTop = msgsTopClient || 0;
      if (this._view == null) this._view = 0;                // the stage regime's floor for a turn-less item
      /* STRICT ALIGNMENT (align="strict"): the column IS the transcript's scroll frame — an item is always exactly
         level with its turn, so the column has to be scrolled in step, and an item can be carried off the bottom
         with the turn it belongs to. That is the whole point of asking for it, and it is the mode this element was
         before held layout; it is kept because watching one long turn is a real way to work (owner, 2026-09-23). */
      if (this.strictAlign()) {
        const body = this.shadowRoot.getElementById('body');
        if (body) { const br = body.getBoundingClientRect();
          body.scrollTop = Math.max(0, Math.round(this._scrollTop + st.offsetTop - ((msgsTopClient || 0) - br.top)));
          this._view = Math.max(0, body.scrollTop - st.offsetTop); }
      }
      if (before !== this._scrollTop || this._placed == null) this._placeNow();
    }
    /* the name the host used while the column was a projection of the transcript; kept so an older page still
       works — it no longer moves the column, it only tells it where the transcript is. */
    syncScroll(msgsScrollTop, msgsTopClient) { return this.setView(msgsScrollTop, msgsTopClient); }
    /* WHAT IS PARKED, AND WHICH MESSAGES IT BELONGS TO. A parked item is shelved, not deleted, and the host
       needs to know which turns it came from to decide whether scrolling back has made it relevant again -
       otherwise an item goes away for good the moment the conversation moves on, and the only way to see it is
       to make it a second time. Read off the document rather than the DOM, because a parked item is a chip. */
    parkedAnchors() {
      const out = []; const bl = (this._doc && this._doc.blocks) || [];
      bl.forEach((b) => {
        if (!b || !b.key || b.state !== 'parked') return;
        const seen = []; (b.anchors || []).concat(b.anchor ? [b.anchor] : []).forEach((x) => {
          if (!x || typeof x !== 'object') return;
          [x.turn, x.mid, x.from, x.beside].forEach((v) => { const t = String(v || ''); if (t && seen.indexOf(t) < 0) seen.push(t); });
        });
        if (seen.length) out.push({ key: String(b.key), anchors: seen });
      });
      return out;
    }
    itemRects() {
      const out = []; const F = this._focus;
      this.shadowRoot.querySelectorAll('.it[data-key]').forEach((el) => { const r = el.getBoundingClientRect(); out.push({ key: el.dataset.key, mid: el.dataset.mid || '', from: el.dataset.from || '', anchors: String(el.dataset.anchors || '').split(' ').filter(Boolean), col: +(el.dataset.col || 0), state: el.classList.contains('pinned') ? 'pinned' : 'now', inFocus: !F || F.has(el.dataset.key) || el.classList.contains('pinned'), out: el.classList.contains('out'), compact: el.classList.contains('compact'), open: el.classList.contains('openin'), ghost: el.classList.contains('ghost'), rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height } }); });
      return out;
    }
    /* THE DESK (align="desk"; owner approved the desk 2026-10-06, shared planning context-and-ui PLAN-WS-U-desk U-D2).
       The chat sits on the column's left edge, so the whole column is the desk's free side: an item is full size
       beside the chat, a card further out, a chip near the far edge (VeraDesk.sizeAt), laid out in bands (VeraDesk
       .arrange 'orbit' with a docked chat) - this turn's items first, then pinned ones, then the newest. A width you
       dragged (data-pw) is your size and holds until ~93% out; a pinned item with a size of yours holds it anywhere.
       Closer to the chat stacks higher. Nothing here runs unless align is "desk". */
    /* A PANEL ITEM AS A DESK LAYER (owner, 2026-10-07: "open panels as canvas items and have them behind / at the front
       in full screen - dynamically sized, not actually full"). Behind: the panel fills the largest part of the desk the
       chat leaves free, under every other item; front: the same, over them; '' puts it back among them. The card never
       leaves the stage, so its frame is never reloaded. */
    /* the desk as it stands, for the exploded view: every item's key, title, face, layer, kind and turn (read off the
       cards the desk placed - nothing is changed) */
    deskSnapshot() {
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage'); if (!st) return [];
      return [...st.querySelectorAll('.it[data-key]')].map((c) => {
        const t = c.querySelector(':scope > .it-hd > .t') || c.querySelector('.vc-th > b');
        return { key: c.dataset.key, label: (t && t.textContent || c.dataset.key).trim(), face: c.dataset.deskFace || '', layer: this._layer[c.dataset.key] || '',
                 type: c.dataset.type || '', mid: c.dataset.mid || '' };
      });
    }
    /* the desk's ends of the chat's runs: on the desk a run stops at its edge, and the items it runs to carry a mark */
    runTargets(keys) {
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage'); if (!st) return;
      const set = new Set((keys || []).map(String));
      st.querySelectorAll('.it[data-key]').forEach((c) => { const on = set.has(c.dataset.key); if (c.classList.contains('run-target') !== on) c.classList.toggle('run-target', on); });
    }
    /* point one item out on the desk (the exploded view's double click): it rides on top and pulses for a moment */
    flash(key) {
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage'); if (!st) return false;
      let c = [...st.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === String(key)); if (!c) return false;
      /* a tab behind the front of a combined card (see _deskGroups) comes to the front to be shown */
      if (c.classList.contains('cmp-behind')) { const m = this._cmpGet(); m[String(key)] = Date.now(); this._cmpSet(m); (this._cmpPick = this._cmpPick || {})[String(key)] = Date.now(); this._placeNow();
        c = [...st.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === String(key)) || c; }
      c.classList.remove('desk-flash'); void c.offsetWidth; c.classList.add('desk-flash');
      const z = c.style.zIndex; c.style.zIndex = '180';
      clearTimeout(c._flT); c._flT = setTimeout(() => { c.classList.remove('desk-flash'); c.style.zIndex = z; }, 1600);
      return true;
    }
    /* A PLACEHOLDER IN THE CHAT OPENS ITS ITEM (owner, 2026-10-11, WSU-60: "a placeholder in the chat opens and focuses
       its item"): the item comes to the front and pulses. On the desk a tab behind its combined card comes forward
       (flash) and a pill opens as it does under the pointer, until the pointer goes elsewhere; in the held column a
       folded item is opened and scrolled into view. False while the item is not drawn yet - the caller tries again. */
    focusItem(key, again) {
      const find = () => { const st = this.shadowRoot && this.shadowRoot.getElementById('stage');   // a render replaces the stage: looked up each time
        return st ? [...st.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === String(key)) : null; };
      const c = find(); if (!c) return false;
      if (this.deskAlign()) {
        if (!this.flash(key)) return false;
        const it = find(); if (it && it.classList.contains('desk-chip')) { if (again) this._hovKey = null; this._hovSet(String(key)); }
        /* renders landing just after the click (the first seconds after a session loads, measured: two in 900 ms) redraw
           the stage, and the pulse and the opened pill go with them - the item is pointed out again, a few times at most */
        if (!again) { clearInterval(this._fiT); let ticks = 0, redo = 0;
          this._fiT = setInterval(() => { if (++ticks > 4) return clearInterval(this._fiT);
            const now = find(); if (now && !now.classList.contains('desk-flash') && redo++ < 3) this.focusItem(key, true); }, 350); }
        return true;
      }
      if (c.classList.contains('compact') || c.classList.contains('overfold')) { this._open.add(String(key)); if (this._doc) this.render(this._doc); }
      this._revealSoon(String(key));
      this.flash(key);
      return true;
    }
    /* A PANEL BEHIND THE ITEMS MAKES ROOM (owner, 2026-10-06: "could it automatically move the widgets out of the way
       (optionally)"): each panel item sent behind is told where the other cards cover its frame, as shares of the frame's
       view (the frame has its own scale); its dashboard moves its widgets out from under them. Any other panel item is
       told nothing covers it, which hands its widgets back. Off with panel-room="off". */
    _panelRoom() {
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage'); if (!st || !this._live) return;
      const on = this.getAttribute('panel-room') !== 'off' && this.deskAlign();
      const cards = [...st.querySelectorAll('.it[data-key]')];
      Object.keys(this._live).forEach((key) => {
        const lv = this._live[key], fr = lv && lv.querySelector && lv.querySelector('iframe'); if (!fr || !fr.contentWindow) return;
        /* a frame that (re)loads has forgotten: tell it again once its dashboard is up */
        if (!fr._roomBound) { fr._roomBound = true; fr.addEventListener('load', () => { lv._roomSig = null; setTimeout(() => { try { this._panelRoom(); } catch (e) { /* optional */ } }, 900); }); }
        const card = cards.find((c) => c.dataset.key === key);
        let shares = [];
        if (on && card && this._layer[key] === 'behind' && lv.style.display !== 'none') {
          const F = fr.getBoundingClientRect();
          if (F.width > 20 && F.height > 20) shares = cards.filter((c) => c !== card && c.getClientRects().length).map((c) => c.getBoundingClientRect())
            .filter((r) => r.left < F.right && r.right > F.left && r.top < F.bottom && r.bottom > F.top)
            .map((r) => ({ l: +Math.max(0, (r.left - F.left) / F.width).toFixed(4), t: +Math.max(0, (r.top - F.top) / F.height).toFixed(4),
                           r: +Math.min(1, (r.right - F.left) / F.width).toFixed(4), b: +Math.min(1, (r.bottom - F.top) / F.height).toFixed(4) }));
        }
        const sig = JSON.stringify(shares) + (on ? '' : ':off');
        if (lv._roomSig === sig) return; lv._roomSig = sig;
        try { fr.contentWindow.postMessage({ type: 'vera:desk:obstacles', rects: shares, makeRoom: on }, '*'); } catch (e) { /* a frame that cannot be told keeps its layout */ }
      });
    }
    /* the desk's kept order (keys, nearest the chat first), per canvas, in this browser */
    _deskOrderGet() {
      if (this._deskOrder !== undefined) return this._deskOrder;
      try { const v = JSON.parse(localStorage.getItem('vera:canvas:deskorder:' + this.canvasId) || 'null'); this._deskOrder = Array.isArray(v) ? v.map(String) : null; } catch (e) { this._deskOrder = null; }
      return this._deskOrder;
    }
    _deskOrderSet(keys) {
      this._deskOrder = keys ? keys.map(String) : null;
      try { if (keys) localStorage.setItem('vera:canvas:deskorder:' + this.canvasId, JSON.stringify(this._deskOrder)); else localStorage.removeItem('vera:canvas:deskorder:' + this.canvasId); } catch (e) { /* kept for this page */ }
    }
    /* A TURN BRINGS ITS ITEMS IN: the keys given (the turn's focus set, best first) go to the front of the desk's order -
       beside the chat - and the rest keep theirs. A card the person placed by hand stays where they put it. Returns
       how many of the keys are on the desk. */
    bringIn(keys) {
      const want = (keys || []).map(String); if (!want.length) return 0;
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage');
      const have = new Set(st ? [...st.querySelectorAll('.it[data-key]')].map((c) => c.dataset.key) : []);
      const front = want.filter((k) => have.has(k)); if (!front.length) return 0;
      const cur = this._deskOrderGet() || [...have];
      this._deskOrderSet(front.concat(cur.filter((k) => front.indexOf(k) < 0)));
      try { this._placeNow(); } catch (e) { /* placed on the next paint */ }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:brought', { bubbles: true, detail: { keys: front } })); } catch (e) { /* no listener */ }
      return front.length;
    }
    /* WHERE THE PERSON PUT THE CARDS on the desk: {key: {fx, fy}}, shares of the desk, per canvas, kept in this browser */
    _deskPosGet() {
      if (this._deskPos) return this._deskPos;
      try { this._deskPos = JSON.parse(localStorage.getItem('vera:canvas:deskpos:' + this.canvasId) || '{}') || {}; } catch (e) { this._deskPos = {}; }
      return this._deskPos;
    }
    _deskPosSet(key, at) {
      const m = this._deskPosGet(); if (at) m[key] = at; else delete m[key];
      try { localStorage.setItem('vera:canvas:deskpos:' + this.canvasId, JSON.stringify(m)); } catch (e) { /* kept for this page only */ }
    }
    /* A LOCKED CARD DOES NOT MOVE (owner, 2026-10-09: "users need the ability to pin items so they cant move"): its place
       (a share of the desk), its size and its face are kept as they were when it was locked. The arrangement, a turn
       bringing its items in, the topic changing and the separation of overlaps all go round it; it cannot be dragged
       or resized until it is freed. Freed, it stays where it is as a card placed by hand. Kept per canvas. */
    _lockGet() {
      if (this._locks) return this._locks;
      try { this._locks = JSON.parse(localStorage.getItem('vera:canvas:desklock:' + this.canvasId) || '{}') || {}; } catch (e) { this._locks = {}; }
      return this._locks;
    }
    _lockSet(key, at) {
      const m = this._lockGet(); if (at) m[key] = at; else delete m[key];
      try { localStorage.setItem('vera:canvas:desklock:' + this.canvasId, JSON.stringify(m)); } catch (e) { /* kept for this page only */ }
    }
    /* the ⋯ menu of a desk card: what is not worth a place in its one row of controls. On the stage (a card clips what is
       inside it), under the button, closed by its own use, a second ⋯ or a press anywhere else. */
    _hdMenu(key, btn) {
      const st = this.shadowRoot.getElementById('stage'); if (!st || !key) return;
      const had = st.querySelector(':scope > .hd-pop'); if (had) { had.remove(); if (had.dataset.key === key && had.dataset.kind !== 'view') return; }
      const c = [...st.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === key); if (!c) return;
      const b = this._blockOf(key) || {}; const k = esc(key); const L = (this._layer || {})[key] || '';
      const row = (act, label, title, on) => `<button class="ib${on ? ' on' : ''}" data-act="${act}" data-layer-key="${k}" title="${esc(title || label)}">${esc(label)}</button>`;
      /* a narrow card's row keeps lock, fill, ⋯ and drop: its other icons are here instead (never in both) */
      const narrow = c.offsetWidth <= 320; const hd = (a) => c.querySelector(':scope > .hda > .hb[data-act="' + a + '"]');
      let h = (narrow && hd('explode') ? row('explode', hd('explode').classList.contains('on') ? 'Back to the source' : 'Explode', hd('explode').title) : '')
        + (narrow && hd('edit') ? row('edit', 'Edit', hd('edit').title) : '')
        + (narrow && hd('ctx') ? row('ctx', hd('ctx').classList.contains('on') ? '✓ In context' : 'Put in context', hd('ctx').title, hd('ctx').classList.contains('on')) : '')
        + row('park', 'Park', 'Park it below the flow') + row('size', 'Size: ' + (c.dataset.size || 'm'), 'The next size')
        + (this._deskPosGet()[key] ? row('desk-auto', 'Back to the arrangement', 'Placed by hand - give it back to the arrangement') : '')
        + row('fold', c.classList.contains('ufold') ? 'Unfold' : 'Fold', 'Fold it to its header line')
        + (c.classList.contains('cmp-front') ? row('cmpsep', 'Take it out of the group', 'Show it as a card of its own; the group keeps its other tabs')
          : this._sepGet()[key] ? row('cmpjoin', 'Combine with similar items', 'Put it back with the items of its kind on its topic, as a tab') : '');
      /* the card's own head's buttons (a code item's copy · preview · edit · run, a terminal's, a diagram's): that head is not
         drawn on the desk, so its buttons are listed here and a row presses the original - one container, nothing lost */
      /* the head's own buttons only - not the entries of a dropdown inside it (Send to's targets), and not one the header row
         already has (explode) */
      const inMenu = (x) => { for (let e = x.parentElement; e && !(e.classList && e.classList.contains('it-bd')); e = e.parentElement) { const cl = String(e.className || ''); if (/(^|[\s_-])(menu|pop|popup|dropdown|drop|list)([\s_-]|$)/i.test(cl) || e.getAttribute('role') === 'menu' || e.tagName === 'DETAILS') return true; } return false; };
      const rowHas = new Set([...c.querySelectorAll(':scope > .hda > .hb')].map((x) => x.dataset.act));
      const own = [...c.querySelectorAll(':scope > .it-bd :is(.code-bar,.vc-codehead,.vc-th) :is(button,[role=button],a.btn)')].filter((x) => !x.closest('.hda') && !inMenu(x) && (x.textContent || x.title || '').trim()
        && !(rowHas.has('explode') && /^explode$/i.test((x.textContent || '').trim())));
      if (own.length && b.type !== 'panel') { c._ownBtns = own; h += '<hr>' + own.slice(0, 10).map((x, i) => `<button class="ib" data-act="ownbtn" data-own="${i}" data-layer-key="${k}" title="${esc(x.title || '')}">${esc(((x.textContent || '').trim() || x.title || 'action').slice(0, 40))}</button>`).join(''); }
      if (b.type === 'panel') h += '<hr>' + row('layer-behind', 'Behind the items', 'Fill the free desk, under the other items', L === 'behind') + row('layer-front', 'In front', 'Fill the free desk, over the other items', L === 'front')
        + row('pquery', 'Query', 'panel.query - what the panel holds') + row('pdispatch', 'Dispatch…', 'panel.dispatch - drive it') + row('prefresh', 'Refresh', 'Reload the panel') + row('pstand', 'Open standalone ↗', 'Open it in a tab of its own');
      const pop = document.createElement('div'); pop.className = 'hd-pop'; pop.dataset.key = key; pop.dataset.w = 'canvas.item.menu'; pop.innerHTML = h;
      const S = st.getBoundingClientRect(), r = (btn || c).getBoundingClientRect(), Z = st.offsetWidth ? (S.width / st.offsetWidth) || 1 : 1;
      st.appendChild(pop);
      const pw = pop.offsetWidth || 180; let left = Math.round((r.right - S.left) / Z - pw), top = Math.round((r.bottom - S.top) / Z + 4);
      left = Math.max(4, Math.min(st.clientWidth - pw - 4, left)); pop.style.left = left + 'px'; pop.style.top = top + 'px';
      const doc = this.ownerDocument || document;
      const off = (ev) => { const p = ev.composedPath ? ev.composedPath() : []; if (p.includes(pop) || (btn && p.includes(btn))) return; pop.remove(); doc.removeEventListener('mousedown', off, true); };
      setTimeout(() => doc.addEventListener('mousedown', off, true), 0);
    }
    /* the item's views, under its View button: one row each, the one shown lit; a choice redraws the item that way */
    _viewMenu(key, btn) {
      const st = this.shadowRoot.getElementById('stage'); if (!st || !key) return;
      const had = st.querySelector(':scope > .hd-pop'); if (had) { had.remove(); if (had.dataset.key === key && had.dataset.kind === 'view') return; }
      const b = this._blockOf(key); if (!b) return; const vw = viewsOf(b, this); if (vw.views.length < 2) return;
      const pop = document.createElement('div'); pop.className = 'hd-pop'; pop.dataset.key = key; pop.dataset.kind = 'view'; pop.dataset.w = 'canvas.item.views';
      pop.innerHTML = vw.views.map((v) => `<button class="ib vrow${v === vw.cur ? ' on' : ''}" data-act="setview" data-view="${v}" data-layer-key="${esc(key)}"><span class="vic">${HDI[v] || ''}</span>${esc(VIEW_LABEL[v] || v)}</button>`).join('');
      const S = st.getBoundingClientRect(), r = (btn || st).getBoundingClientRect(), Z = st.offsetWidth ? (S.width / st.offsetWidth) || 1 : 1;
      st.appendChild(pop);
      const pw = pop.offsetWidth || 140; let left = Math.round((r.right - S.left) / Z - pw); left = Math.max(4, Math.min(st.clientWidth - pw - 4, left));
      pop.style.left = left + 'px'; pop.style.top = Math.round((r.bottom - S.top) / Z + 4) + 'px';
      const doc = this.ownerDocument || document;
      const off = (ev) => { const p = ev.composedPath ? ev.composedPath() : []; if (p.includes(pop) || (btn && p.includes(btn))) return; pop.remove(); doc.removeEventListener('mousedown', off, true); };
      setTimeout(() => doc.addEventListener('mousedown', off, true), 0);
    }
    toggleLock(key) {
      key = String(key || ''); const g = this._deskGeo; if (!key || !g) return false;
      const c = [...this.shadowRoot.querySelectorAll('.stage .it[data-key]')].find((x) => x.dataset.key === key); if (!c) return false;
      const w = c.offsetWidth, h = c.offsetHeight, cx = (parseFloat(c.style.left) || 0) + w / 2, cy = (parseFloat(c.style.top) || 0) + h / 2;
      if (this._lockGet()[key]) { this._lockSet(key, null); this._deskPosSet(key, { fx: +(cx / g.W).toFixed(4), fy: +(cy / g.H).toFixed(4) }); }
      else this._lockSet(key, { fx: +(cx / g.W).toFixed(4), fy: +(cy / g.H).toFixed(4), w, h, face: c.dataset.deskFace || 'card' });
      c.classList.toggle('desk-locked', !!this._lockGet()[key]);
      this._html = null; if (this._doc) this.render(this._doc); else this._placeNow();
      return !!this._lockGet()[key];
    }
    setLayer(key, mode) {
      key = String(key || ''); if (!key) return;
      if (mode === 'behind' || mode === 'front') this._layer[key] = mode; else delete this._layer[key];
      const picks = layersLoad(); picks[key] = (mode === 'behind' || mode === 'front') ? mode : 'among'; layersSave(picks);
      const card = this.shadowRoot && [...this.shadowRoot.querySelectorAll('.stage .it')].find((c) => c.dataset.key === key);
      if (card) card.querySelectorAll('[data-act^="layer-"]').forEach((bt) => bt.classList.toggle('on', bt.dataset.act === 'layer-' + this._layer[key]));
      try { this._placeNow(); } catch (e) { /* placed on the next paint */ }
    }
    /* SIMILAR ITEMS ON ONE TOPIC, ONE CARD (owner, 2026-10-10: "id like similar elements on similar topics to be able to
       combine into composite desk items and for things on the desk to take up as much space as possible"). A turn that
       searched the web and the fabric and ran a research put three reading lists side by side, each with a third of the
       room. Items of one family - reading lists (web, news, fabric, a research run, a page), tables and fields, widgets,
       documents, diagrams, names, timelines, images - that came from the same turn or are about the same subject (the
       part of the title after its " · ") now share one card as tabs, the way windows are tabbed on a desktop. Only the
       tab in front is placed, so the desk's room goes to fewer cards; each tab is still the whole item, with its own
       controls, graph and menu. A locked item, and one the person took out (its ⋯ menu), stays on its own. The tab
       last chosen is in front; a newly arrived member comes to the front. Up to eight to a card. */
    _deskGroups(cards) {
      const FAM = { records: 'reading', research: 'reading', source: 'reading', table: 'data', kv: 'data', widget: 'widgets', markdown: 'docs',
        diagram: 'diagrams', entities: 'names', timeline: 'dates', image: 'images' };
      const res = { behind: new Set(), membersOf: new Map(), groups: [] };
      cards.forEach((c) => { c.classList.remove('cmp-behind', 'cmp-front'); delete c.dataset.cmpN; const tb = c.querySelector(':scope > .cmp-tabs'); if (tb) tb.remove(); });
      if (this.getAttribute('desk-combine') === 'off' || this.hasAttribute('desk-narrow')) return res;
      const sep = this._sepGet(), locks = this._lockGet();
      const titleOf = (c) => ((c.querySelector(':scope > .it-hd > .t') || {}).textContent || '').replace(/\s+/g, ' ').trim();
      const subjOf = (t) => { const i = t.indexOf(' · '); return (i >= 0 ? t.slice(i + 3) : '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim(); };
      const prefOf = (t) => { const i = t.indexOf(' · '); return i >= 0 ? t.slice(0, i) : t; };
      const elig = cards.filter((c) => { const k = c.dataset.key || ''; return k && FAM[c.dataset.type || ''] && !sep[k] && !locks[k] && !(this._max && this._max === k) && !(this._rz && this._rz.el === c); });
      /* joined by family when they share a turn or a subject */
      const par = new Map(elig.map((c) => [c, c]));
      const find = (c) => { while (par.get(c) !== c) { par.set(c, par.get(par.get(c))); c = par.get(c); } return c; };
      const by = {};
      elig.forEach((c) => { const f = FAM[c.dataset.type]; const s = subjOf(titleOf(c));
        [c.dataset.mid ? f + '|m|' + c.dataset.mid : '', s.length >= 4 ? f + '|s|' + s : ''].filter(Boolean)
          .forEach((b) => { if (by[b]) par.set(find(c), find(by[b])); else by[b] = c; }); });
      const sets = new Map(); elig.forEach((c) => { const r = find(c); if (!sets.has(r)) sets.set(r, []); sets.get(r).push(c); });
      const act = this._cmpGet(); let stamped = false; const now = Date.now();
      sets.forEach((list) => {
        for (let i = 0; i < list.length; i += 8) {
          const g = list.slice(i, i + 8); if (g.length < 2) continue;
          /* a member never seen in a group before is the newest: it comes to the front */
          g.forEach((c) => { if (act[c.dataset.key] == null) { act[c.dataset.key] = now; stamped = true; } });
          /* THE TAB THE CONVERSATION IS ABOUT IS IN FRONT (WSU-71, owner 2026-10-11: "the actual bitcoin price chart which
             should have stayed open did not, and closed. The other unrelated chart stayed open"): two charts on one subject
             are one card, and the card stays in when any tab is in the topic - but the front was only the last one
             touched, so the card stayed showing the unrelated chart with the bitcoin one behind it. The front is now the
             member the turn's topic ranks best; a tab the person picked after the topic was set still wins. */
          const T = this._topic; let front = g.reduce((a, c) => ((act[c.dataset.key] || 0) >= (act[a.dataset.key] || 0) ? c : a), g[0]);
          if (T && T.keys && T.keys.size) {
            const tRank = (c) => { const i = T.order.indexOf(c.dataset.key || ''); return i >= 0 ? i : (T.mid && (c.dataset.mid === T.mid || c.dataset.from === T.mid) ? T.order.length : Infinity); };
            const inT = g.filter((c) => tRank(c) !== Infinity).sort((a, b) => tRank(a) - tRank(b));
            const pickedSince = ((this._cmpPick || {})[front.dataset.key] || 0) > (this._topicAt || 0);   /* the person clicked its tab after the topic moved */
            if (inT.length && !pickedSince) front = inT[0];
          }
          res.membersOf.set(front, g); res.groups.push(g.map((c) => c.dataset.key));
          g.forEach((c) => { if (c !== front) { res.behind.add(c); c.classList.add('cmp-behind'); } });
          front.classList.add('cmp-front'); front.dataset.cmpN = String(g.length);
          /* the tabs name what differs: on one subject, the part before it (Web, From the fabric, Research); else the title */
          const subs = new Set(g.map((c) => subjOf(titleOf(c))));
          const lab = (c) => { const t = titleOf(c); return subs.size === 1 && [...subs][0] ? prefOf(t) : t; };
          const tb = document.createElement('div'); tb.className = 'cmp-tabs'; tb.dataset.w = 'canvas.item.group';
          tb.innerHTML = g.map((c) => `<button class="cmp-tab${c === front ? ' on' : ''}" data-act="cmptab" data-layer-key="${esc(c.dataset.key)}" title="${esc(titleOf(c))}"><i class="cmp-ty">${esc(String(c.dataset.type || '').slice(0, 2).toUpperCase())}</i>${esc(lab(c).slice(0, 42))}</button>`).join('');
          const hd = front.querySelector(':scope > .it-hd'); if (hd) hd.after(tb); else front.prepend(tb);
        }
      });
      if (stamped) this._cmpSet(act);
      return res;
    }
    /* A CARD IS NO TALLER THAN WHAT IT HOLDS (owner, 2026-10-10: "the current ui for most desk elements is not styled well";
       measured 2026-10-11: a note used 48 px of a 426 px card, a page 147 of 332, fields 205 of 335, names 156 of 426 -
       the desk sized every card by where it sat and then grew it into the free room, whatever was in it). After a
       placement the cards that hold a fixed amount - not a drawing, a graph, a widget, a reading list or a run, which
       take the room they are given and scroll - are measured: how far down their content reaches. A card with room to
       spare is given that height (never under 96 px) and does not grow past it; one whose content outgrows it is let go
       again. A change re-places the desk, at most four times in a row. */
    _deskFit() {
      const sr = this.shadowRoot; if (!sr || this.getAttribute('desk-fit') === 'off') return;
      /* a diagram is measured too: its drawing has its own shape, and its slot is as tall as the drawing (_diagramGrew) */
      /* a notebook cell is measured too: its source and its output were each stretched to half a tall card (2026-10-11) */
      const FILLS = new Set(['records', 'research', 'widget', 'explode', 'panel', 'html', 'image']);   /* a live shell is skipped by its .vc-live */
      const nat = this._natH = this._natH || {}; let changed = false;
      sr.querySelectorAll('.stage .it[data-key][data-desk-face]').forEach((c) => {
        const key = c.dataset.key, face = c.dataset.deskFace || '';
        if (!/^(card|max)$/.test(face) || c.classList.contains('pill-open') || c.classList.contains('cmp-behind') || c.classList.contains('desk-locked') || c.classList.contains('desk-placed') || (this._rz && this._rz.el === c)) return;
        /* an image fills its card up to half again its own size: a small picture blown up to the desk is not "as much room as
           design allows" (a 200 px logo drawn 650 px tall) */
        if (c.dataset.type === 'image') { const im = c.querySelector('.vc-fig img'), bd0 = c.querySelector(':scope > .it-bd');
          if (im && im.naturalWidth > 0 && im.naturalHeight > 0 && bd0) { const s = Math.min(1.5, (bd0.clientWidth - 8) / im.naturalWidth);
            const cap = c.querySelector('.vc-fig figcaption'); const want = Math.max(96, Math.round(c.offsetHeight - bd0.clientHeight + im.naturalHeight * s + (cap ? cap.offsetHeight + 6 : 0) + 12));
            if (want < c.offsetHeight - 8 && (nat[key] == null || Math.abs(nat[key] - want) > 16)) { nat[key] = want; changed = true; } }
          return; }
        /* A WIDGET IS NEVER SHORTER THAN ITS FACE (WSU-70, owner 2026-10-11: "some elements are not tall enough like metric
           value change"): a widget fills its card, so it was never measured - and a card the desk made short clipped the
           figure's lower lines (its change, its trace). The widget's own body says how much more it needs; the card gets
           that as its least height (kept in _minH), and only grows, never shrinks below it, while that widget is there. */
        /* only the short-content forms (DESK_FORM_MAXW): a table, a log or a long list scrolls in its card by design */
        const wfm = c.dataset.type === 'widget' ? (() => { const cc = this._contentOf(key) || {}; return String(cc.form || cc.widget || (cc.record && cc.record.form) || (cc.draw && cc.draw.form) || '').replace(/@iso$/, ''); })() : '';
        if (wfm && DESK_FORM_MAXW[wfm] && wfm !== 'files' && wfm !== 'list') { const lv = this._live && this._live[key], wi = lv && lv.firstChild;
          const rt = wi && wi.shadowRoot && wi.shadowRoot.querySelector('.vw-body, .vw-root');
          if (rt && rt.clientHeight > 0) { const more = rt.scrollHeight - rt.clientHeight; const mh = this._minH || (this._minH = {});
            if (more > 6) { const want = c.offsetHeight + more + 8; if (!mh[key] || want > mh[key] + 4) { mh[key] = want; changed = true; } } } }
        if (FILLS.has(c.dataset.type || '') || (c.dataset.type !== 'diagram' && c.querySelector('.vc-live'))) { if (nat[key] != null) { delete nat[key]; changed = true; } return; }
        /* AS WIDE AS WHAT IT HOLDS (WSU-69, the owner's session 2026-10-11: a checklist 775 px wide with 285 px of text, a note
           420 with 51, the "Metric · Value · Change" table 775 with 157): a text-like card is measured for how far right
           its content reaches and is no wider than that and its header's controls need (never under 240 px). When its
           content fills the width it was given (it grew, or wraps), the measure is let go so it can widen again. */
        if (/^(note|markdown|table|kv|entities)$/.test(c.dataset.type || '')) { const bdw = c.querySelector(':scope > .it-bd');
          if (bdw && bdw.clientWidth > 0) { const B0 = bdw.getBoundingClientRect(); let right = 0;
            bdw.querySelectorAll('*').forEach((e) => { if (e.childElementCount && !/^(TD|TH|LI|BUTTON|INPUT)$/.test(e.tagName)) return; const rg = document.createRange(); rg.selectNodeContents(e); [...rg.getClientRects()].forEach((q) => { if (q.width) right = Math.max(right, q.right - B0.left); }); });
            const Zw = c.offsetWidth ? (c.getBoundingClientRect().width / c.offsetWidth) || 1 : 1; const used = Math.round(right / Zw);
            const nw = this._natW || (this._natW = {});
            if (used >= bdw.clientWidth - 6) { if (nw[key] != null) { delete nw[key]; changed = true; } }
            /* ...and never narrower than its header needs: the whole title beside the controls (a 240 px card cut every
               title to its first letter, "M…", "C…" - measured on the owner's desk) */
            else { const hd = c.querySelector(':scope > .it-hd'), tt = hd && hd.querySelector(':scope > .t');
              const ctl = hd ? [...hd.children].filter((x) => x !== tt && getComputedStyle(x).display !== 'none').reduce((s, x) => s + x.offsetWidth, 0) : 0;
              /* the header's padding is where the controls sit (absolutely placed over it): counted, or the floor left the
                 title no room in the tier that shows them */
              const hcs = hd ? getComputedStyle(hd) : null; const pad = hcs ? (parseFloat(hcs.paddingLeft) || 0) + (parseFloat(hcs.paddingRight) || 0) : 0;
              /* the title's TEXT width (a range over it): the title box stretches to fill the header, so its scrollWidth is
                 the room it was given, not what it needs */
              let tw = 0; if (tt) { const rg = document.createRange(); rg.selectNodeContents(tt); tw = Math.ceil([...rg.getClientRects()].reduce((s, q) => s + q.width, 0)); if (tt.scrollWidth > tt.clientWidth) tw = Math.max(tw, tt.scrollWidth); }
              const head = tt ? Math.min(600, tw + ctl + pad + 16) : 0;
              const want = Math.max(240, head, used + (c.offsetWidth - bdw.clientWidth) + 36); if (want < c.offsetWidth - 24 && (nw[key] == null || Math.abs(nw[key] - want) > 16)) { nw[key] = want; changed = true; } } } }
        const bd = c.querySelector(':scope > .it-bd'); if (!bd || !bd.clientHeight) return;
        const B = bd.getBoundingClientRect(); let low = B.top;
        /* how far the content reaches: its leaves (text runs, images, controls), not the boxes that stretch to fill */
        bd.querySelectorAll('*').forEach((e) => { if (e.childElementCount && !/^(IMG|SVG|CANVAS|BUTTON|INPUT|SELECT|TEXTAREA|TR|LI)$/i.test(e.tagName)) return;
          const q = e.getBoundingClientRect(); if (q.height && q.width) low = Math.max(low, q.bottom); });
        const Z = c.offsetHeight ? (c.getBoundingClientRect().height / c.offsetHeight) || 1 : 1;
        const over = bd.scrollHeight > bd.clientHeight + 4;
        const want = Math.max(96, Math.round(c.offsetHeight - bd.clientHeight + (low - B.top) / Z + 14));
        /* fitted a little short (the body's own padding, a last line's descent): the overflow is added, not the fit dropped -
           dropping it let the card grow back and be measured again, round and round */
        if (over && nat[key] != null) { const more = bd.scrollHeight - bd.clientHeight;
          if (more > 160) delete nat[key]; else nat[key] = c.offsetHeight + more + 6; changed = true; return; }
        /* WSU-70: a card a little short for what it holds (a five-row table, a short list) was left clipped - only a card
           already fitted got its overflow added. A short overflow is now its least height too; a long one still scrolls. */
        if (over && nat[key] == null) { const more = bd.scrollHeight - bd.clientHeight; if (more > 4 && more <= 240) { const mh = this._minH || (this._minH = {}); const need = c.offsetHeight + more + 6; if (!mh[key] || need > mh[key] + 4) { mh[key] = need; nat[key] = need; changed = true; } } return; }
        if (over || want >= c.offsetHeight - 8) return;
        if (nat[key] == null || Math.abs(nat[key] - want) > 16) { nat[key] = want; changed = true; }
      });
      if (changed && (this._fitRuns = (this._fitRuns || 0) + 1) <= 4) { this._inFit = true; try { this._placeNow(); } finally { this._inFit = false; } }
    }
    /* A CARD MINIMISED TO A PILL (owner, 2026-10-11: "id like a way to minimise them to a pill from the top bar controls"):
       kept per canvas; the desk places it on its border as a pill whatever the topic; a click on the pill opens it again */
    _pillGet() { if (this._pillA) return this._pillA; try { this._pillA = JSON.parse(localStorage.getItem('vera:canvas:deskpill:' + this.canvasId) || '{}') || {}; } catch (e) { this._pillA = {}; } return this._pillA; }
    _pillSet(key, on) { const m = this._pillGet(); if (on) m[key] = 1; else delete m[key]; try { localStorage.setItem('vera:canvas:deskpill:' + this.canvasId, JSON.stringify(m)); } catch (e) { /* kept for this page only */ } }
    _cmpGet() { if (this._cmpA) return this._cmpA; try { this._cmpA = JSON.parse(localStorage.getItem('vera:canvas:deskfront:' + this.canvasId) || '{}') || {}; } catch (e) { this._cmpA = {}; } return this._cmpA; }
    _cmpSet(m) { this._cmpA = m; const ks = Object.keys(m); if (ks.length > 400) ks.sort((a, b) => m[a] - m[b]).slice(0, ks.length - 400).forEach((k) => delete m[k]);
      try { localStorage.setItem('vera:canvas:deskfront:' + this.canvasId, JSON.stringify(m)); } catch (e) { /* kept for this page only */ } }
    _sepGet() { if (this._sepA) return this._sepA; try { this._sepA = JSON.parse(localStorage.getItem('vera:canvas:desksep:' + this.canvasId) || '{}') || {}; } catch (e) { this._sepA = {}; } return this._sepA; }
    _sepSet(key, on) { const m = this._sepGet(); if (on) m[key] = 1; else delete m[key]; try { localStorage.setItem('vera:canvas:desksep:' + this.canvasId, JSON.stringify(m)); } catch (e) { /* kept for this page only */ } }
    _placeDesk(st, cards) {
      const D = root.VeraDesk, body = this.shadowRoot.getElementById('body');
      this._liveFollow(650);   /* every card it places may glide (.45 s): what is drawn over them follows each frame */
      this._placeSeq = (this._placeSeq || 0) + 1;
      const W = st.clientWidth || 300;
      const bar = this.shadowRoot.querySelector('.addbar'), bh = this.shadowRoot.querySelector('.band.now > .band-h');
      const pad = (bar ? bar.offsetHeight : 0) + (bh ? bh.offsetHeight : 0);
      const H = Math.max(360, (body ? body.clientHeight : 600) - pad - 8);
      const f = Math.max(0.8, Math.min(1.6, W / 1000));          // a wide column gets proportionally larger faces
      const desk = { W: W, H: H, m: 8, top: 4, edgeX: 24, edgeTop: 24, edgeBottom: 22, scale: f };      // the chat beside the column: on its left edge (the default), or on its right when the page puts the chat there
      const side = String(this.getAttribute('chat-side') || 'left').toLowerCase();
      const cr = String(this.getAttribute('chat-rect') || '').split(',').map(Number);
      // floating: the chat is over the stage, where the page says (chat-rect, stage pixels); else beside the column
      const R = side === 'float' && cr.length === 4 && cr.every((n) => isFinite(n)) && cr[2] > 0
        ? { l: cr[0], t: cr[1], r: cr[0] + cr[2], b: cr[1] + cr[3] }
        : side === 'right' ? { l: W, t: 0, r: W + 440, b: H } : { l: -440, t: 0, r: 0, b: H };
      const fm = this._focusMid || '';
      /* a maximised item on the desk is a front layer: the free part of the desk, never over the chat (owner, 2026-10-06:
         "fullscreen (not including the chat area)") */
      const layOf = (c) => (this._max && this._max === c.dataset.key) ? 'front' : this._layer[c.dataset.key || ''];
      const layered = cards.filter((c) => layOf(c));
      cards = cards.filter((c) => !layOf(c));
      /* SIMILAR ITEMS ON ONE TOPIC ARE ONE CARD (see _deskGroups): only the tab in front is placed; the others take its
         rectangle, unseen, after the placement below */
      const grp = this._deskGroups(cards);
      cards = cards.filter((c) => !grp.behind.has(c));
      this._deskGeo = { R, desk, W, H };   /* a drag sizes the card it moves with the same rules, live */
      const up = this._deskPosGet();
      const ranked = cards.map((c, i) => ({ c, i })).sort((a, b) => {
        const fa = fm && a.c.dataset.mid === fm ? 1 : 0, fb = fm && b.c.dataset.mid === fm ? 1 : 0;
        if (fa !== fb) return fb - fa;
        const pa = a.c.classList.contains('pinned') ? 1 : 0, pb = b.c.classList.contains('pinned') ? 1 : 0;
        if (pa !== pb) return pb - pa;
        return b.i - a.i;
      });
      // one: where each card goes and its size there (out of the chat's path); two: separate any that overlap; three: write
      /* a card the person dragged stays where it was put (a share of the desk, so it holds when the desk resizes) and
         goes first, so the others move round it */
      const userAt = (key) => { const u = up[key]; return u && isFinite(u.fx) && isFinite(u.fy) ? [u.fx * W, u.fy * H] : null; };
      /* THE DESK'S ORDER IS KEPT (owner, 2026-10-06: the desk organised automatically; plan U-D6: "nothing moves without
         the person's say"). The order above - the turn in focus, pinned, newest - is taken once and kept per canvas; after
         that a placement follows the kept order, and a scroll, a focus change or a refresh moves nothing. A new item
         joins at the end (the border). Only bringIn() - a turn bringing its items in, when that is on - or the person
         changes it. */
      const kept = this._deskOrderGet();
      if (kept) {
        const at = {}; kept.forEach((k, i) => { at[k] = i; });
        const nat = new Map(ranked.map((x, i) => [x, i]));
        ranked.sort((a, b) => { const ka = a.c.dataset.key || '', kb = b.c.dataset.key || '';
          return (at[ka] != null ? at[ka] : 1e6 + nat.get(a)) - (at[kb] != null ? at[kb] : 1e6 + nat.get(b)); });
      }
      const nowOrder = ranked.map((x) => x.c.dataset.key || '').filter(Boolean);
      if (!kept || nowOrder.length !== kept.length || nowOrder.some((k, i) => kept[i] !== k)) this._deskOrderSet(nowOrder);
      /* the primary topic leads (see setTopic): its items, best first, then the rest in the kept order - each of those
         marked for the border. Pinned (in context) items count as in the topic. Only on a desk that brings items in. */
      const T = this.getAttribute('bring-in') !== 'off' ? this._topic : null; const outside = new Set();
      if (T) {
        const tAt = {}; T.order.forEach((k, i) => { tAt[k] = i; });
        /* ...and anything made FOR the turn in focus (its own anchors), the moment it lands: the topic set is asked once a
           turn, so an item a call of that turn adds afterwards (its activity, its result) was out of it and went to the border */
        const ownTurn = (c) => !!T.mid && (c.dataset.mid === T.mid || c.dataset.from === T.mid || (c.dataset.anchors || '').split(' ').indexOf(T.mid) >= 0);
        /* a composite card is in the topic when any of its tabs is, and ranks as its best one */
        const mem = (x) => grp.membersOf.get(x.c) || [x.c];
        const inT = (x) => mem(x).some((c) => tAt[c.dataset.key || ''] != null || c.classList.contains('pinned') || ownTurn(c));
        const rk = (x) => Math.min.apply(null, mem(x).map((c) => (tAt[c.dataset.key] != null ? tAt[c.dataset.key] : -1)));
        const lead = ranked.filter(inT).sort((a, b) => rk(a) - rk(b));
        const rest = ranked.filter((x) => !inT(x)); rest.forEach((x) => outside.add(x.c.dataset.key || ''));
        ranked.length = 0; lead.concat(rest).forEach((x) => ranked.push(x));
      }
      /* THE CARDS TAKE THE ROOM THERE IS (owner, 2026-10-09: "a really good automatic layout and sizing system"; 2026-10-10:
         "the auto sizing of elements is also not done"): the largest face was 300 x 196 grown only with the desk's width,
         so two cards beside the chat were as small as ten. The faces now share about half the free desk among the cards
         beside the chat (the topic's, or the first nine), up to 2.4 times; never smaller than the width alone made them.
         Chips keep their size; the bands take their depth from the face, so the arrangement follows. */
      /* minimised to a pill (_pillGet): on the border whatever the topic - a combined card goes with its front tab */
      { const pills = this._pillGet(); const pilled = ranked.filter((x) => (grp.membersOf.get(x.c) || [x.c]).some((c) => pills[c.dataset.key || ''] || (up[c.dataset.key || ''] && up[c.dataset.key || ''].chip)));
        if (pilled.length) { pilled.forEach((x) => outside.add(x.c.dataset.key || ''));
          const rest = ranked.filter((x) => pilled.indexOf(x) < 0); ranked.length = 0; rest.concat(pilled).forEach((x) => ranked.push(x)); } }
      const nNear = Math.max(1, Math.min(9, ranked.filter((x) => !outside.has(x.c.dataset.key || '')).length));
      desk.scale = Math.max(f, Math.min(2.4, Math.sqrt((W * H * 0.5) / (nNear * 300 * 196)))); desk.scaleY = 0;
      /* beside a docked chat the bands are columns: the cards are as wide as the columns the border's chips leave, and as
         tall as the first band's share of the desk's height (it holds up to three) - a tall desk had 3 cards of 196 px in
         1150 px of band. Never taller than 1.3 times their width. */
      if (R.r <= 0 || R.l >= W) {
        const bands = nNear > 3 ? 2 : 1, per = Math.min(3, nNear);
        const sx = Math.max(f, Math.min(2.6, ((W - 170 - 14 * (bands + 1)) / bands) / 300));
        const sy = Math.max(f, Math.min(2.6, ((H - 16) / per - 14) / 196));
        desk.scale = sx; desk.scaleY = Math.min(sy, (sx * 300 * 1.3) / 196);
      }
      /* the arrangement deals its places in the kept order (it was dealt before the order was applied, so a turn's items
         were "brought in" without moving) */
      /* the arrangement the person chose (owner, 2026-10-06: "automatically organised but let the user configure it"):
         orbit (bands out from the chat), by kind, by time (turns ago), or a stack; each item says its kind and its age */
      const mode = ['orbit', 'kind', 'timeline', 'stack'].indexOf(this.getAttribute('desk-arrange')) >= 0 ? this.getAttribute('desk-arrange') : 'orbit';
      const mids = [...new Set(cards.map((c) => c.dataset.mid || '').filter(Boolean))].sort((a, b) => (parseInt(b.replace(/\D/g, ''), 10) || 0) - (parseInt(a.replace(/\D/g, ''), 10) || 0));
      const pos = D.arrange(mode, R, ranked.map((x) => ({ id: x.c.dataset.key || String(x.i), kind: x.c.dataset.type || '',
        age: x.c.dataset.mid ? mids.indexOf(x.c.dataset.mid) : null, border: outside.has(x.c.dataset.key || '') })), desk);
      const locks = this._lockGet();
      const lockOf = (key) => { const l = locks[key]; return l && isFinite(l.fx) && isFinite(l.fy) && l.w > 0 && l.h > 0 ? l : null; };
      const sized = (k) => !!(up[k] && up[k].w > 0 && up[k].h > 0);
      const firm = (x) => (this._rz && this._rz.el === x.c ? 8 : 0) + (lockOf(x.c.dataset.key || '') ? 4 : 0) + (sized(x.c.dataset.key || '') ? 2 : 0) + (userAt(x.c.dataset.key || '') ? 1 : 0);
      ranked.sort((a, b) => firm(b) - firm(a));   /* the card being sized, then locked, then placed by hand: the others go round them */
      const plan = ranked.map((x) => {
        const c = x.c, key = c.dataset.key || String(x.i);
        /* the card under the hand's resize: its top-left where it was, its size the hand's; the others go round it */
        if (this._rz && this._rz.el === c) { const r = this._rz, rw = Math.min(W - 16, r.w), rh = Math.min(H - 8, r.h);
          return { c, id: key, so: {}, fixed: true, sizing: true, x: r.l0 + rw / 2, y: r.t0 + rh / 2, w: rw, h: rh }; }
        const lk = lockOf(key); c.classList.toggle('desk-locked', !!lk);
        /* locked, or sized by the person (its rectangle kept with its place): fixed at exactly that, the others go round */
        const us = !lk && up[key] && up[key].w > 0 && up[key].h > 0 && isFinite(up[key].fx) && isFinite(up[key].fy) ? up[key] : null;
        const fx = lk || us;
        if (fx) { const lw = Math.min(W - 16, fx.w), lh = Math.min(H - 8, fx.h); if (us) c.classList.add('desk-placed');
          return { c, id: key, so: {}, fixed: true, lock: fx, x: Math.max(lw / 2, Math.min(W - lw / 2, fx.fx * W)), y: Math.max(lh / 2, Math.min(H - lh / 2, fx.fy * H)), w: lw, h: lh }; }
        const p = userAt(key) || pos[key] || [W - 90, H - 30];
        c.classList.toggle('desk-placed', !!userAt(key));
        /* the person's size: the drag in progress, else what the canvas remembers for the item (this._pw / this._px) -
           not only the card's data-pw, which a compact card's markup leaves out, so a resized desk card lost its size
           on the redraw that followed the resize */
        const live = this._rz && this._rz.el === c ? this._rz : null;
        const pw = (live ? live.w : 0) || this._pw[key] || parseInt(c.dataset.pw || '0', 10) || 0;
        const ph = (live ? live.h : 0) || this._px[key] || 0;
        /* dropped in pill territory (deskPos.chip): the person's earlier width does not hold it big there */
        const user = pw > 0 && !(up[key] && up[key].chip) ? { w: pw, h: ph || Math.round(pw * 0.66) } : null;
        const so = { size: user, pinned: !!user && c.classList.contains('pinned'), icon: p[2] === 'icon' };
        const po = D.pushOut(R, p[0], p[1], (xx, yy) => D.sizeAt(R, xx, yy, so, desk), desk);
        /* a card is no taller than what it holds (see _deskFit): its measured height caps its face and its growth */
        const nat = (this._natH || {})[key];
        /* ...and no wider than its kind uses (owner, 2026-10-11: "some desk items like the 'mentioned' take up too much width
           for their content"): names, a note, fields, a page, what ran - their content wraps short; a wide card of them was
           a wide empty band */
        /* ...and a widget by its form (WSU-69, owner 2026-10-11: "some of the elements are still too wide, like the notes
           checklist"): lists, checklists, fields and single figures wrap short too; charts, tables and maps keep the room */
        const wf = c.dataset.type === 'widget' ? (() => { const cc = this._contentOf(key) || {}; return String(cc.form || cc.widget || (cc.record && cc.record.form) || (cc.draw && cc.draw.form) || '').replace(/@iso$/, ''); })() : '';
        const nw0 = ({ entities: 460, note: 420, kv: 560, activity: 480, source: 560, loop: 560, session: 640 })[c.dataset.type || '']
          || DESK_FORM_MAXW[wf];
        /* ...and a text-like card no wider than its content measured (_deskFit, WSU-69) */
        const nwm = (this._natW || {})[key]; const nw = nwm ? Math.min(nw0 || Infinity, nwm) : nw0;
        /* ...and never shorter than its own face needs (WSU-70): a widget clipped in its card is measured (_deskFit) */
        const mh = (this._minH || {})[key];
        return { c, id: key, so, x: po.x, y: po.y, w: Math.min(W - 16, po.size.w, nw || Infinity), h: Math.max(Math.min(H - 8, mh || 0), Math.min(H - 8, po.size.h, nat || Infinity)), face0: po.size.face, maxH: nat || undefined, maxW: nw || undefined, minH: mh || undefined, stay: !!userAt(key) };
      });
      D.separate(plan, R, (xx, yy, it) => { const s = D.sizeAt(R, xx, yy, it.so, desk); const chip = s.face === 'chip' || s.face === 'icon';
        return { w: Math.min(W - 16, s.w, chip ? Infinity : (it.maxW || Infinity)), h: Math.max(chip ? 0 : Math.min(H - 8, it.minH || 0), Math.min(H - 8, s.h, chip ? Infinity : (it.maxH || Infinity))) }; }, { W: W, H: H });
      /* ...up the desk into the room above them (D.settle), the person's places, locks and sizes staying where they are... */
      if (typeof D.settle === 'function' && this.getAttribute('desk-settle') !== 'off') {
        D.settle(plan, R, { W: W, H: H }, { gap: 12 });
        /* a card that rose keeps the size it was pulled apart at: re-sized where it now sits, it could meet its neighbours */
        plan.forEach((it) => { if (it.settled && !it.fixed) { it.keepSize = true; if (!it.face0) it.face0 = D.faceOf(it.w, it.h); } });
      }
      /* ...then every card takes the free desk round it (D.grow): a chip stays a chip, and what the person sized, locked
         or is sizing stays as it is. Off with the canvas's desk-fill="off". */
      if (typeof D.grow === 'function' && this.getAttribute('desk-fill') !== 'off') {
        plan.forEach((it) => { if (it.fixed || it.keepSize) { it.chip = it.h <= 40; return; }
          const s0 = D.sizeAt(R, it.x, it.y, it.so, desk); it.face0 = s0.face; it.chip = s0.face === 'chip' || s0.face === 'icon'; });
        D.grow(plan, R, { W: W, H: H }, { maxW: W - 16, maxH: H - 8 });
      }
      const out = [];
      plan.forEach((it) => {
        const c = it.c, key = it.id, s = it.lock ? { w: it.w, h: it.h, face: it.lock.face || 'card', nd: 0.5 }
          /* grown into the free desk: that size, its face from it (a card grown past a full card's size is a full card) */
          : it.grown ? Object.assign({}, D.sizeAt(R, it.x, it.y, it.so, desk), { w: it.w, h: it.h, face: D.faceOf(it.w, it.h) })
          : it.sizing ? { w: it.w, h: it.h, face: c.dataset.deskFace || 'max', nd: 0 }
          /* moved where only its present size fits: it keeps that size and its face there, or it would grow over its neighbours */
          : it.keepSize ? Object.assign({}, D.sizeAt(R, it.x, it.y, it.so, desk), { w: it.w, h: Math.min(it.h, it.h > 40 && it.maxH ? it.maxH : Infinity), face: it.face0 || 'chip' })
          : it.maxH ? (() => { const s0 = D.sizeAt(R, it.x, it.y, it.so, desk); if (s0.face === 'chip' || s0.face === 'icon') return s0; const hh = Math.min(s0.h, it.maxH); return Object.assign({}, s0, { h: hh, face: D.faceOf(s0.w, hh) === 'chip' ? 'card' : D.faceOf(s0.w, hh) }); })()
          : D.sizeAt(R, it.x, it.y, it.so, desk);
        const isChip = s.face === 'chip' || s.face === 'icon';
        const w = Math.min(W - 16, s.w, isChip ? Infinity : (it.maxW || Infinity)), h = Math.max(isChip ? 0 : Math.min(H - 8, it.minH || 0), Math.min(H - 8, s.h));
        const left = Math.max(0, Math.min(W - w, Math.round(it.x - w / 2))), top = Math.max(0, Math.min(H - h, Math.round(it.y - h / 2)));
        c.classList.remove('overfold', 'capped'); c.style.maxHeight = '';
        c.classList.toggle('desk-chip', s.face === 'chip' || s.face === 'icon');
        c.classList.toggle('desk-icon', s.face === 'icon');
        c.dataset.deskFace = s.face;
        if (s.face === 'icon') { const t = c.querySelector('.it-hd > .t'); c.title = t ? t.textContent.trim() : ''; } else if (c.title) c.removeAttribute('title');
        // the desk owns the size here: a compact card's own height rule must not grow a chip under its neighbours
        c.style.left = left + 'px'; c.style.top = top + 'px'; c.style.setProperty('width', w + 'px', 'important'); c.style.setProperty('height', h + 'px', 'important'); c.style.setProperty('min-height', '0px', 'important');
        c.style.zIndex = String(it.sizing ? D.stackOrder(0, { dragging: true }) : D.stackOrder(s.nd));
        out.push({ key: key, x: left, y: top, w: w, h: h, face: s.face });
        /* the group's other tabs: the same rectangle and face, unseen until their tab is chosen */
        (grp.membersOf.get(c) || []).forEach((m) => { if (m === c) return;
          m.style.left = left + 'px'; m.style.top = top + 'px'; m.style.setProperty('width', w + 'px', 'important'); m.style.setProperty('height', h + 'px', 'important'); m.style.setProperty('min-height', '0px', 'important');
          m.dataset.deskFace = s.face; m.classList.toggle('desk-chip', s.face === 'chip' || s.face === 'icon'); m.classList.toggle('desk-icon', s.face === 'icon'); m.style.zIndex = c.style.zIndex;
          out.push({ key: m.dataset.key, x: left, y: top, w: w, h: h, face: s.face, behind: true }); });
      });
      /* the layered panels: the largest part of the desk the chat leaves free, under (behind) or over (front) the rest */
      const g = D.fullRegion(R, { W: W, H: H, m: 8, top: 4 });
      const behindNow = layered.some((c) => layOf(c) === 'behind');
      if (this.hasAttribute('layered') !== behindNow) this.toggleAttribute('layered', behindNow);
      layered.forEach((c) => {
        const mode = layOf(c), left = Math.max(0, Math.round(g.l)), top = Math.max(0, Math.round(g.t));
        const w = Math.max(120, Math.round(Math.min(W, g.r) - left)), h = Math.max(120, Math.round(Math.min(H, g.b) - top));
        c.classList.remove('overfold', 'capped', 'desk-chip', 'desk-icon', 'compact'); c.style.maxHeight = ''; c.removeAttribute('title');
        c.dataset.deskFace = 'layer-' + mode;
        c.style.left = left + 'px'; c.style.top = top + 'px'; c.style.setProperty('width', w + 'px', 'important'); c.style.setProperty('height', h + 'px', 'important'); c.style.setProperty('min-height', '0px', 'important');
        c.style.zIndex = mode === 'front' ? String(D.stackOrder(0, { full: true })) : '1';
        out.push({ key: c.dataset.key, x: left, y: top, w: w, h: h, face: 'layer-' + mode });
      });
      /* each layered panel's PILL, over every item at the panel's corner: a panel sent behind has its own head under the
         items, so its controls are here as well - always in reach (owner, 2026-10-07: "i dont see the behind or front") */
      st.querySelectorAll(':scope > .lay-pill').forEach((p) => p.remove());
      layered.filter((c) => this._layer[c.dataset.key]).forEach((c) => {
        const mode = this._layer[c.dataset.key], k = esc(c.dataset.key), other = mode === 'front' ? 'behind' : 'front';
        const t = ((c.querySelector('.vc-th b') || c.querySelector('.it-hd .t') || {}).textContent || c.dataset.key).trim();
        const pill = document.createElement('div'); pill.className = 'lay-pill'; pill.dataset.w = 'canvas.panel.layer';
        pill.innerHTML = `<span class="ic">▤</span><b title="${esc(t)}">${esc(t)}</b><span class="m">${mode === 'front' ? 'in front' : 'behind'}</span>`
          + `<button class="ib" data-act="layer-${other}" data-layer-key="${k}" title="${other === 'front' ? 'Bring it over the other items' : 'Send it behind the other items'}">${other}</button>`
          + `<button class="ib" data-act="layer-${mode}" data-layer-key="${k}" title="Put it back among the other items">among</button>`;
        pill.style.left = (Math.round(g.l) + 10) + 'px'; pill.style.top = (Math.round(g.t) + 6) + 'px';
        st.appendChild(pill);
      });
      st.style.height = H + 'px';
      /* a new card is in its place now: it fades in there (it was unseen, not sliding in from a default spot) */
      const news = [...st.querySelectorAll('.it.desk-new')];
      if (news.length) requestAnimationFrame(() => requestAnimationFrame(() => news.forEach((c) => c.classList.remove('desk-new'))));
      this._placed = { mode: 'desk', placements: out, height: H, folded: [] };
      try { this._liveLayout(); } catch (e) { /* the live layout readers are optional */ }
      // the cards slide to their places (.45s): the live layer follows once they have arrived
      clearTimeout(this._deskLiveT);
      this._deskLiveT = setTimeout(() => { try { this._liveLayout(); } catch (e) { /* optional */ } try { this._panelRoom(); } catch (e) { /* optional */ }
        try { this._deskFit(); } catch (e) { /* optional */ } }, 520);
      /* a placement that is not a refit's own lets the refit run again (see _deskFit) */
      if (!this._inFit) this._fitRuns = 0;
      try { this.dispatchEvent(new CustomEvent('vera:canvas:placed', { bubbles: true, detail: { n: out.length, mode: 'desk', height: H,
        faces: out.reduce((m, o) => { m[o.face] = (m[o.face] || 0) + 1; return m; }, {}) } })); } catch (e) { /* no listener */ }
    }
    _placeNow() {
      const st = this.shadowRoot.getElementById('stage'); if (!st) return;
      const W = st.clientWidth || 300, gap = 10;
      /* THE STAGE'S WIDTH IS AN INPUT NOW, so a change to it has to re-place: dragging the chat|canvas handle, opening
         the graph column, a window resize. Width only — the height this very pass writes would otherwise loop. */
      if (!this._stageRO) { try { this._stageRO = new ResizeObserver(() => {
          const s = this.shadowRoot.getElementById('stage'); const ww = s ? s.clientWidth : 0;
          if (ww && ww !== this._stageW) this._placeNow();
        }); this._stageRO.observe(st); } catch (e) { /* no observer: the host still drives placement */ } }
      this._stageW = W;
      /* a card being dragged on the desk is the person's: nothing places the desk under it (a focus change, a refresh, the
         chat moving all re-placed it mid-drag and pulled the card back); the drop places it once */
      if (this._dg && this._dg.moved && this.deskAlign()) return;
      { const wantDesk = String(this.getAttribute('align') || '').toLowerCase() === 'desk', narrow = wantDesk && W < DESK_MIN_W;
        if (this.hasAttribute('desk-narrow') !== narrow) { this.toggleAttribute('desk-narrow', narrow); this._html = null; if (this._doc) { this.render(this._doc); return; } } }
      const maxCard = this._max ? st.querySelector('.it.maxed') : null;   // placed on its own, below
      const cards = [...st.querySelectorAll('.it')].filter((c) => c !== maxCard);
      if (this.deskAlign()) {
        // the rules live in one library; it is fetched the first time desk is chosen, then the stage is placed again
        /* the maximised card too: the desk places it over the free part of the desk (it was set aside for a placement
           the desk never reached, and drew 0 px wide) */
        if (root.VeraDesk) { this._placeDesk(st, maxCard ? cards.concat(maxCard) : cards);
          try { this._liveLayout(); } catch (e) { /* optional */ }   /* in the same frame: a graph starts its glide WITH its card */
          return; }
        if (!root.__veraDeskLoading) {
          root.__veraDeskLoading = true;
          const s = document.createElement('script'); s.src = '/ui/desk-layout.js';
          s.onload = () => this._placeNow(); s.onerror = () => { root.__veraDeskLoading = false; };
          document.head.appendChild(s);
        }
      } else cards.forEach((c) => { if (c.dataset.deskFace) { c.classList.remove('desk-chip'); delete c.dataset.deskFace; c.style.zIndex = ''; c.style.removeProperty('height'); c.style.removeProperty('width'); c.style.removeProperty('min-height'); } });
      /* AN ITEM THAT GROWS AFTER IT WAS PLACED IS PLACED AGAIN (owner, 2026-10-03: "the last element added does not fully
         display untill the next message is sent"). The stage measured each card once, when it was placed; a card that
         grew afterwards - its markdown and widgets finishing, a run's output, an explode panel, an editor opened -
         kept the slot of its first height, ran over the item below it and past the end of the stage until something
         else (the next message) placed the stage again. Each card's height is watched now; a real change re-places. */
      if (!this._cardRO && typeof ResizeObserver !== 'undefined') {
        this._cardH = new WeakMap();
        this._cardRO = new ResizeObserver((entries) => {
          let moved = false;
          entries.forEach((e) => { if (e.target.classList.contains('peek')) return;   // a preview opening is not a card growing
            const h = Math.round(e.contentRect.height); if (this._cardH.get(e.target) !== h) { if (this._cardH.has(e.target)) moved = true; this._cardH.set(e.target, h); } });
          /* not on the desk: the desk sets every card's size itself (and animates it), so a size change there is the desk's
             own - re-placing on it re-placed the desk every frame of every move, and fought a card being dragged (owner,
             2026-10-08: "its flickering a bit if elements are moved on the desk and if new elements join") */
          if (moved && !this._cardRaf && !this.deskAlign()) this._cardRaf = requestAnimationFrame(() => { this._cardRaf = 0; this._placeNow(); });
        });
      }
      if (this._cardRO) cards.forEach((c) => { if (!c._roOn) { c._roOn = true; this._cardRO.observe(c); } });
      /* what each card wants, read off the card and independent of the count — so the count can be chosen from it */
      const descOf = (c) => ({ type: c.dataset.type || '', size: c.dataset.size || 'm', fuse: c.dataset.fuse || '',
        folded: c.classList.contains('compact') || c.classList.contains('overfold'),
        open: c.classList.contains('openin') || c.classList.contains('sized') });
      /* THE COUNT IS THE STAGE'S OWN, not a number somebody typed. `columns` absent or "auto" (the default) lets the
         width and the content decide it; 1-4 pins it, which is what the banner's COLS buttons are for. */
      const attr = String(this.getAttribute('columns') || 'auto').trim().toLowerCase();
      const pinned = attr && attr !== 'auto' ? Math.max(1, Math.min(4, parseInt(attr, 10) || 0)) : 0;
      const cols = pinned || autoCols(W, cards.map(descOf), { gap });
      const w = Math.floor((W - gap * (cols - 1)) / cols);
      if ((this.dataset.cols || '') !== String(cols)) this.dataset.cols = String(cols);   // what the column actually chose, readable by the page
      /* WIDTH BEFORE HEIGHT. An item that flows beside its neighbour is measured at the width it will actually have,
         or every height here is the height of a different item than the one drawn. */
      /* AN ITEM FOLDED TO ITS HEADER LINE IS A CHIP, AND CHIPS SIT TOGETHER. Folded items each took a full row, so a
         canvas of folded items was a stack of identical bars — the shape the owner keeps calling a rigid grid. */
      /* over 1 = a number of COLUMNS to span, clamped to the columns there are: a kind you read takes the stage when
         there is more than one column to take; an l or xl item of any kind takes two. */
      // the same classifier the count was chosen by, told the column width it now has (a reading width, a chip's share)
      const wantOf = (c) => { const u = unitsOf(descOf(c), w); return u > 1 ? Math.max(1, Math.min(cols, Math.round(u))) : u; };
      /* ⛔ A WIDTH YOU DRAGGED IS A NUMBER OF PIXELS, and it outranks every share and every span. The drag in progress
         counts as much as one already dropped, or the placer would fight the mouse: it set the card back to the share
         its kind asks for on the very next frame, which is why dragging left did nothing at all. Bounded by the
         stage: a card cannot be dragged wider than the columns there are. */
      const stageW = cols * w + (cols - 1) * gap;
      const pwOf = (c) => {
        const live = this._rz && this._rz.el === c ? this._rz.w : 0;
        const saved = live || parseInt(c.dataset.pw || '0', 10) || 0;
        return saved > 0 ? Math.max(120, Math.min(saved, stageW)) : 0;
      };
      const pws = cards.map(pwOf);
      const wants = cards.map((c, i) => pws[i] ? Math.min(cols, (pws[i] + gap) / (w + gap)) : wantOf(c));
      cards.forEach((c, i) => { const ww = wants[i];
        if (pws[i]) { c.style.width = pws[i] + 'px'; return; }
        const span = ww > 1 ? Math.min(cols, Math.round(ww)) : 1;
        c.style.width = (ww < 1 ? Math.round(ww * w) - gap : span * w + (span - 1) * gap) + 'px'; });
      /* ⛔ AND AN ITEM WHOSE CONTENT HAS A WIDTH OF ITS OWN IS NEVER WIDER THAN THAT. A sticker-sized widget drew a
         90px chip in the third of a column it was given and left the rest of it blank (owner, 2026-09-24: "canvas
         items that are smaller than a column have large blank areas i.e. widgets"). The face reports its real width
         (vera-widget.naturalWidth via _widgetFit; the faces meant to fill report nothing), and the card is cut down
         to it — so what comes next can sit beside it instead of after the blank.
         Read every card, then write every card: the natural width is measured against the share just applied, and
         a read/write per card in one loop is a layout flush per card. An item you opened or dragged is left alone,
         as everywhere else — you asked for that room. */
      const natural = cards.map((c, i) => {
        if (pws[i] || wants[i] >= 1 || c.classList.contains('openin') || c.classList.contains('sized') || c.classList.contains('compact')) return 0;
        const s = c.querySelector('.vc-live[data-natw]'); if (!s) return 0;
        const nat = parseInt(s.dataset.natw, 10); if (!(nat > 0)) return 0;
        const chrome = Math.max(0, c.clientWidth - s.clientWidth);   // the card's own padding, measured rather than assumed
        const px = nat + chrome; const share = c.clientWidth;
        return px > 40 && px < share - 8 ? px : 0;                   // only when it really is narrower than its share
      });
      cards.forEach((c, i) => { if (!natural[i]) return;
        c.style.width = natural[i] + 'px';
        wants[i] = Math.max(0.08, (natural[i] + gap) / (w + gap));   // and the placer flows the rest of the row against THAT
      });
      const bar = this.shadowRoot.querySelector('.addbar'), bh = this.shadowRoot.querySelector('.band.now > .band-h');
      const pad = (bar ? bar.offsetHeight : 0) + (bh ? bh.offsetHeight : 0);   // the sticky heads overlay the stage's top: nothing is placed under them
      const body = this.shadowRoot.getElementById('body');
      /* the column's OWN height. Two different uses, and only one of them is about the regime:
           VH — what a size's ceiling is a share of (§3.2). The column is this tall whichever way items are aligned.
           V  — what the HELD regime bounds a position against. Strict hands the placer none, which is exactly its
                old projection (place()'s `stage` regime): one flag, two behaviours, no second placer. */
      const strict = this.strictAlign();
      const VH = body ? body.clientHeight : 0;
      const V = strict ? 0 : VH;
      if (VH && this._vh !== VH) { this._vh = VH; this.style.setProperty('--vc-vh', VH + 'px'); }
      /* an item taller than the column is capped and scrolls its own body rather than running off the end. Written
         in three passes — clear every cap, read every height, then set the caps — because this runs on every scroll
         frame now, and a write/read per card in one loop is a layout flush per card.
         Strict alignment caps nothing (V is 0 there): an item is level with its turn and as tall as it is, and the
         column scrolls the whole transcript — which is the regime the reader asked for. */
      /* ⛔ AND AN ITEM YOU EXPANDED IS NEVER CAPPED. The cap is for items nobody asked to be big — it stops one
         running off the end of the column. An item you OPENED IN PLACE or DRAGGED TALLER is the opposite: you asked
         for the room, and "the column makes room" is what its own button promises. Capping those made both gestures
         do nothing at all — the card grew and was clamped back inside the same frame, so the visible area never
         changed (owner, 2026-09-24: "re-sizing the canvas items doesnt function", "open in place doesnt do
         anything"). The column scrolls for them instead, which is what the packed regime is for. */
      const capH = Math.max(120, V - pad - 10);
      const asked = (c) => c.classList.contains('openin') || c.classList.contains('sized') || c.classList.contains('peek');
      if (V) { cards.forEach((c) => { if (c.style.maxHeight) { c.style.maxHeight = ''; c.classList.remove('capped'); } });
        const hs = cards.map((c) => c.offsetHeight);
        cards.forEach((c, i) => { if (hs[i] > capH && !asked(c)) { c.style.maxHeight = capH + 'px'; c.classList.add('capped'); } }); }
      else cards.forEach((c) => { if (c.classList.contains('capped')) { c.style.maxHeight = ''; c.classList.remove('capped'); } });
      this._fullH = this._fullH || {};
      const items = cards.map((c, ci) => { const k = c.dataset.key || ''; const hd = c.querySelector(':scope > .it-hd:not(.own-fold)') || c.querySelector(':scope > .it-hd');
        // a folded item being previewed is still its folded line to the column: its preview lies over the items below
        const isFolded = c.classList.contains('overfold'); const measured = c.classList.contains('peek') && c._peekH ? c._peekH : c.offsetHeight;
        if (!isFolded) this._fullH[k] = measured;                              // what it is when OPEN: the placer judges on that, so folding cannot oscillate
        return { key: k, h: isFolded ? (this._fullH[k] || measured) : measured, hFold: hd && hd.offsetHeight ? hd.offsetHeight + 2 : 28,
                 want: wants[ci], wpx: pws[ci] || 0, mid: c.dataset.mid || '', beside: c.dataset.beside || '',
                 /* AN ITEM YOU OPENED IS NEVER FOLDED BY THE COLUMN, any more than a pinned one is: you asked for it
                    open, and a room that shuts what you just opened is worse than a room that scrolls. */
                 pinned: c.classList.contains('pinned') || c.classList.contains('openin') }; });
      const weights = {}; const S = this._scores || {};
      Object.keys(S).forEach((k) => { const v = S[k]; weights[k] = v && typeof v.score === 'number' ? v.score : 0.5; });
      const P = place(items, this._turns || {}, { columns: cols, gap, colWidth: w, pad, viewport: V,
        scrollTop: this._scrollTop || 0, weights, view: this._view != null ? this._view : (body ? body.scrollTop : 0) });
      const foldSet = new Set(P.folded || []); let refold = false;
      cards.forEach((c) => { const want = foldSet.has(c.dataset.key || '');
        if (c.classList.contains('overfold') !== want) { c.classList.toggle('overfold', want); refold = true; } });
      P.placements.forEach((p) => { const c = cards.find((x) => x.dataset.key === p.key); if (!c) return;
        c.style.left = p.x + 'px'; c.style.top = p.y + 'px'; c.dataset.col = String(p.col);
        if (p.w > 0) c.style.width = p.w + 'px';                 // the width it flowed at
        c.classList.toggle('level', !!p.level); c.classList.toggle('beside', !!p.beside);
        if (c.classList.contains('peek')) this._peekGeom(c); });
      /* THE STAGE IS THE ITEMS' OWN HEIGHT, not the transcript's. It used to be made as tall as the whole transcript
         so the column could be driven from the transcript's scrollTop — the projection this replaces. */
      st.style.height = (P.mode === 'stage' ? Math.max(P.height, (this._turnsH || 0) + 40) : P.height) + 'px';
      /* the maximised item: the whole width of the stage and the height of the column's view, where the column is
         scrolled to - and the column does not scroll while it is up (:host([data-maxed]) #body), so it stays there */
      if (maxCard && body) { const vis = Math.max(pad, st.offsetTop - body.scrollTop);   // where the visible area starts under the sticky heads, from the column's top
        const mtop = Math.max(0, body.scrollTop + vis - st.offsetTop) + 4; const mh = Math.max(180, (VH || 420) - vis - 12);
        maxCard.style.left = '0px'; maxCard.style.top = mtop + 'px'; maxCard.style.width = stageW + 'px'; maxCard.style.height = mh + 'px'; maxCard.style.maxHeight = ''; maxCard.classList.remove('capped', 'overfold');
        if ((parseInt(st.style.height, 10) || 0) < mtop + mh + 8) st.style.height = (mtop + mh + 8) + 'px'; }
      // nothing overflows: the column has no business being scrolled somewhere (it may have been, before this).
      // Never in strict, where the scroll is the transcript's and setView has just written it.
      if (body && !strict && P.mode !== 'packed' && body.scrollTop && st.offsetHeight <= body.clientHeight) body.scrollTop = 0;
      this._placed = P;
      this._liveLayout();
      // the fold set changed: the heights it was judged on are stale by exactly those items — place once more
      if (refold && !this._refolding) { this._refolding = 1; requestAnimationFrame(() => { this._refolding = 0; this._placeNow(); }); }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:placed', { bubbles: true, detail: { n: P.placements.length, level: P.placements.filter((p) => p.level).length, columns: cols, auto: !pinned, height: P.height, mode: P.mode, folded: (P.folded || []).length } })); } catch (e) { /* observers are optional */ }
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
        /* a canvas that has drawn keeps its last good render: the chat clears _rev to force a reload whenever items land,
           and a fetch that failed then replaced the body - the live layer with it - so every graph on the desk was
           destroyed while its card stayed (owner, 2026-10-10: "the graph seems to get wiped out by new element loading
           into the desk but it container remains") */
        if (this._rev === null && !this._doc) body.innerHTML = '<div class="err">Canvas unreachable.</div>';
        return;                                   // keep the last good render
      }
      if (!doc || doc.error) {
        if (!this._doc) body.innerHTML = `<div class="err">${esc((doc && doc.error) || 'Not found')}</div>`;   /* ...the same for an error answer */
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
      /* a tool card's text landed as a document (the chat's old fallback, turn:<mid>:cap:<n>, a code fence of the card's
         header and result - "blank except for the text m1:cap:0 research.run ...") is not drawn: the chat no longer makes
         them, and those already kept are skipped */
      if (Array.isArray(doc.blocks)) doc.blocks = doc.blocks.filter((b) => !(b && b.type === 'markdown' && /^turn:[^:]+:cap:\d+$/.test(String(b.key || ''))));
      const blocks = doc.blocks || [];
      // what the host is streaming into an item outranks whatever an older revision says it holds (liveUpdate)
      if (this._liveOv) blocks.forEach((b) => { const ov = b && b.key != null && this._liveOv[String(b.key)]; if (ov) b.content = Object.assign({}, b.content || {}, ov); });
      try { this._srcPrefetch(blocks); } catch (e) {}
      /* each panel item's layer: the person's pick, else the layer it was landed with ("UI panel: on the desk" lands behind) */
      try { const picks = layersLoad(); blocks.forEach((b) => { if (!b || b.type !== 'panel' || b.key == null) return; const k = String(b.key), pk = picks[k], cl = b.content && b.content.layer;
        if (pk === 'behind' || pk === 'front') this._layer[k] = pk; else if (pk === 'among') delete this._layer[k]; else if (cl === 'behind' || cl === 'front') this._layer[k] = cl; }); } catch (e) {}
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
    /* THE CONVERSATION'S PRIMARY TOPIC (owner, 2026-10-10: "the chat focus (primary topic) still isnt steering/controlling
       the items on the desk - how close they are to chat or if they are moved all the way out of focus and minimised"):
       the items the turn in focus is about - the relevance pass's focus set for that turn, best first. On a desk that
       brings items in, they come in beside the chat (the most useful nearest, as many as the bands hold); every other
       item goes out to the far border as a chip. A question on two topics has both in its set, so both stay in. Locked
       and hand-placed cards stay where they are. Called by the chat whenever the turn in focus changes. */
    setTopic(keys, scores, mid) {
      const sc = scores && typeof scores === 'object' ? scores : {};
      const val = (k) => { const v = sc[k]; return v && typeof v.score === 'number' ? v.score : (typeof v === 'number' ? v : 0); };
      /* ONLY WHAT THE TURN IS REALLY ABOUT: the relevance pass's focus set holds nearly everything (the last turns' items
         all score 0.5 for being recent - 16-17 of 19 items in either test turn), so the topic is the items scoring 0.75
         or more (the turn's own and those its words or names reach); none so strong - those at 0.6 or more */
      let ks = Array.isArray(keys) ? keys.map(String) : null;
      if (ks && Object.keys(sc).length) { const strong = ks.filter((k) => val(k) >= 0.75); ks = strong.length ? strong : ks.filter((k) => val(k) >= 0.6); }
      /* a turn about nothing on the desk ("hello", "thanks") does not change the conversation's topic: the last one holds */
      if (ks && !ks.length && this._topic) return false;
      const sig = (mid || '') + '|' + (ks ? ks.join(',') : '-');
      if (sig === this._topicSig) return false; this._topicSig = sig;
      this._topic = ks ? { keys: new Set(ks), order: ks.slice().sort((a, b) => val(b) - val(a)), mid: mid || '' } : null;
      this._topicAt = Date.now();   /* a combined card's tab picked after this wins over the topic's choice (WSU-71) */
      if (this.deskAlign()) { try { this._placeNow(); } catch (e) { /* placed on the next pass */ } }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:topic', { bubbles: true, detail: { mid: mid || '', keys: ks || [] } })); } catch (e) { /* no listener */ }
      return true;
    }
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
      // THE ITEM YOU MAXIMISED stays maximised, per canvas, until you restore it - or until it is no longer on show
      if (this._maxFor !== this.canvasId) { this._maxFor = this.canvasId; try { this._max = localStorage.getItem('vera:canvas:max:' + this.canvasId) || ''; } catch (e) { this._max = ''; } }
      if (this._max && !keyed.some((b) => String(b.key) === this._max && ['now', 'pinned'].indexOf(b.state || 'now') >= 0)) this._max = '';
      if (this._max) this.dataset.maxed = '1'; else delete this.dataset.maxed;
      const plain = view.filter(b => !(b && b.key));
      const by = st => keyed.filter(b => (b.state || 'now') === st);
      const newest = arr => plainDoc ? byOrder(arr) : arr.slice().sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
      const pinned = by('pinned'), now = newest(by('now')), parked = by('parked'), hidden = by('hidden');
      const head = this.shadowRoot.getElementById('count');
      if (head) head.textContent = plainDoc ? keyed.length + (keyed.length === 1 ? ' block' : ' blocks') : now.length + ' now · ' + pinned.length + ' pinned · ' + parked.length + ' parked';
      const modeEl = this.shadowRoot.getElementById('mode'); if (modeEl) { modeEl.hidden = !plainDoc; modeEl.textContent = String(doc.mode || 'static') + (doc.topic ? ' · ' + doc.topic : ''); modeEl.className = 'pill mode-' + esc(doc.mode || 'static'); modeEl.title = doc.mode === 'dynamic' ? 'dynamic — it tracks its topic; agents fill it in' : 'static — a working area'; }
      const tier = this.tier(), F = this._focus;
      // the tier onto the host, so the CSS can answer it: a rule on <html> cannot cross a shadow root (defect 85)
      if (this.dataset.tier !== tier) this.dataset.tier = tier;
      // which items already have their diagram open, so each can draw its switch the right way round
      const xplodedKeys = new Set(((doc && doc.blocks) || []).filter((x) => x && x.type === 'explode' && x.content && x.content.binds).map((x) => String(x.content.binds)));
      const focusMid = this.dataset.focusMid || ''; this._focusMid = focusMid;
      const order = turnOrder(this._turns || {});
      const stage = this.hasAttribute('stage');
      /* FUSED GROUPS (fuseOf, above). Worked out per band over the items as the document has them; a MEMBER is then
         drawn as a pane of its LEAD instead of a card of its own, so two things about the same thing are one element.
         `fuse="off"` (the reader's own setting) turns it off; `split` on a pane takes that one item back out of its
         group for this session, which is a way of looking at the canvas and not a change to it. */
      const fuseOff = String(this.getAttribute('fuse') || 'on').toLowerCase() === 'off';
      const except = {}; (this._split || new Set()).forEach((k) => { except[String(k)] = 1; });
      const FU = fuseOf(now, { off: fuseOff, except }), FUP = fuseOf(pinned, { off: fuseOff, except });
      const byKey = {}; keyed.forEach((b) => { byKey[String(b.key)] = b; });
      const groupOf = (b) => FU.groups[String(b.key)] || FUP.groups[String(b.key)] || null;
      const isMember = (b) => { const k = String(b.key); const l = FU.of[k] || FUP.of[k]; return !!l && l !== k; };
      // the NOW band's two parts: the decision this turn waits on (the first live item that carries one) and what
      // Vera can also do (the suggestions the document and the live items carry)
      const decisions = now.concat(pinned).map(b => ({ b, d: decisionOf(b) })).filter(x => x.d);
      const decision = (decisions.find(x => !x.d.answer) || decisions[0] || {}).d || null;
      const suggs = suggestionsOf(doc, keyed, focusMid, { explodeOffer: this.explodeOffer() }); this._suggs = suggs;
      const nowTxt = plainDoc ? (now.length ? now.length + (now.length === 1 ? ' block' : ' blocks') : 'nothing yet') + ' · ' + (doc.mode || 'static') + (decision && !decision.answer ? ' · waiting on you' : '') : nowText(now, decision);
      const titleOf = b => blockTitle(b);
      const askHtml = d => `<div class="askb" data-w="canvas.decision">
            <span class="why">surfaced because <b>${esc(d.why)}</b></span>
            <span class="q">${esc(d.question)}</span>
            <div class="opts">${d.options.map(o => `<button data-act="answer" data-ans="${esc(o.v)}" class="${d.answer === o.v ? 'on' : ''}" title="Answer — it goes to the run">${esc(o.n)}</button>`).join('')}${d.answer ? '' : '<span class="or">or type below — it goes to the run, not into a new turn</span>'}</div>
            <span class="st">${d.answer ? 'answered' + (hhmm(d.answered) ? ' ' + hhmm(d.answered) : '') + ' · "' + esc(d.answer) + '" sent to the run' : 'waiting' + (hhmm(d.since) ? ' since ' + hhmm(d.since) : '') + ' · the composer answers this too'}</span></div>`;
      const editHtml = b => { const c = b.content || {}; const ask = c.ask && typeof c.ask === 'object' && !c[textFieldOf(b.type)]; const f = ask ? 'ask.question' : textFieldOf(b.type); const v = ask ? (c.ask.question || '') : (c[f] || '');   // a decision's editor edits its question
        // Done / Cancel above the text, where editing starts (owner, 2026-10-04: scrolling to the bottom to stop editing)
        return `<div class="edit" data-w="canvas.update"><div class="edit-a"><button class="ib" data-act="save">Done</button><button class="ib" data-act="cancel">Cancel</button><span class="vc-dim">${esc(b.type)} · ${f}</span></div>
          <textarea class="ta${b.type === 'code' || b.type === 'html' ? ' code' : ''}" data-field="${f}" spellcheck="false">${esc(v)}</textarea></div>`; };
      /* A PANE: a fused member, drawn inside its lead's body. Its own drawer, its own key (so its live slot, its
         runs and every capability that addresses it are unchanged) — and one control, to take it back out. */
      const paneHtml = m => {
        const fn = BLOCK[m.type] || BLOCK.note; let ih;
        try { ih = fn(m.content || {}, m.size, String(m.key), this); }
        catch (e) { ih = `<div class="err">Could not render a ${esc(m.type)} block.</div>`; }
        return `<div class="fu-p" data-key="${esc(m.key)}" data-type="${esc(m.type)}"><span class="fu-t"><i class="ic vc-badge" data-kind="${esc(m.type)}">${esc(glyphOf(m.type))}</i><b>${esc(blockTitle(m))}</b><button data-act="split" title="Take it out of this element — its own card again">split</button></span><div class="fu-b">${ih}</div></div>`;
      };
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
        /* EVERY MESSAGE THIS ITEM BELONGS TO, not only the last one. The document has kept them all along -
           anchors only ever grow - and the item carried just the latest, so the host could ask "is the message
           that made this on screen?" about one message and no other. A reply that calls a tool is two messages;
           the item is anchored to one of them, and scrolling to the other dropped it. */
        const anchorMids = (() => { const seen = []; (b.anchors || []).concat(a ? [a] : []).forEach((x) => {
          if (!x || typeof x !== 'object') return;
          [x.turn, x.mid, x.from, x.beside].forEach((v) => { const t = String(v || ''); if (t && seen.indexOf(t) < 0) seen.push(t); });
        }); return seen; })();
        // WHERE IT CAME FROM, when that is not the turn: a reply's item was made by the reply, and its run should
        // leave the reply's block rather than the question above it (Notes/42 defect 87). The turn stays the turn -
        // the exploded scene and the harvest key a station by it.
        const from = a && a.from ? String(a.from) : '';
        // an item added by hand: yours, level with the turn it was added beside, related to no turn (no run, never aged)
        const beside = a && !mid ? String(a.beside || '') : ''; const yours = !!(a && a.origin === 'you' && !mid);
        const open = this._open.has(key) || editing || this._max === key;   // a maximised item is open
        const maxed = this._max === key;
        const aged = isAged(mid, focusMid, order) && !(F && F.has(key)) && b.state !== 'pinned';
        // a hovered folded item is drawn folded: its preview is laid over the column after the render (_peekOpen), never
        // opened in the layout - that moved the column under the pointer
        const hovered = false;
        const fresh = !!mid && mid === focusMid;                                    // the turn in view produced it: open, in every tier
        // a header line, until opened. `hasFocus`: with no focus set there is nothing to be out of, so nothing folds
        const wouldFold = foldOf({ tier, aged, open, now: isNow, fresh, hasFocus: !!F, inFocus: !!F && F.has(key) });
        /* FOLDED BY YOU (owner, 2026-09-28: the enrich items 'often dont have good results and need to be collapsible'): ▾ on
           the header folds any item to its header line and it stays folded - remembered per canvas - until ▸ or a click
           on the header opens it. What the canvas brings forth unasked (from='enrich') starts folded: its header says
           what it is and how much it holds, and it opens when it is wanted. Opening it in place, maximising or editing
           it overrides the fold. */
        /* PLACED FOR THE QUESTION (turn.prepare, from='prepare'; owner, 2026-10-04: "im not seeing any of the markets info
           ... perhaps it did but i cant see it"): what was picked for the question and told to the model shows open - the
           reply talks about it. Any of its anchors counts: the after-reply pass may bring the same item back as 'enrich'. */
        const prepared = (b.anchors || []).concat(a ? [a] : []).some((x) => x && typeof x === 'object' && x.from === 'prepare');
        const ufold = !b._bid && !open && this._isUFolded(key, from, prepared);
        const compact = (wouldFold && !hovered) || ufold;
        const px = this._px[key];
        /* the WIDTH you dragged it to, as a data attribute rather than a style: the placer applies it (it is the one
           thing that knows the columns it has to fit into), and it must survive the markup being rewritten */
        const pw = this._pw[key];
        /* ONE EDIT PER ITEM (owner, 2026-10-04: two Edit buttons on a code item). A code item's card bar has the kit's
           editor (numbered, linted, full screen; saved through the `edited` hook) and a reading item's row has its own
           Edit - the header's would be the second. It stays where nothing else edits: a note, a page, any item drawn
           without the kit. */
        const kitEdits = !!(root.VeraOut && key && (b.type === 'code' || b.type === 'markdown'));
        const editable = EDITABLE.includes(b.type) && !kitEdits;
        const bid = b._bid ? String(b.id) : '';   // a keyless block: addressed by its id (canvas.update · canvas.remove · canvas.move)
        /* ONE HEADER PER ITEM. Several kinds draw a head of their own — a code item its filename and its preview
           switch, a terminal its host and its dot, an explode item what it is bound to — and the card drew ANOTHER
           one above it with the same name. Two rows saying what a thing is, one of them with the controls (owner,
           2026-09-24: "canvas items have 2 header elements ... combine them"). The card's own header is dropped for
           those kinds: the drawer's head IS the header, and it is the one with the controls on it. */
        const ownHead = OWN_HEAD.has(b.type);
        /* the switch into the exploded view, ON THE ITEM it is about — no ghost card in the band offering it. It is
           the same bound explode item as before (keyed explode:<key>, bound by span both ways); the difference is
           that the offer lives where the thing does, and reads as a view of it rather than another card. */
        const xplodable = !bid && !!canExplode(b, this.explodeOffer());
        const xploded = xplodable && xplodedKeys.has(String(b.key));
        // what is fused INTO this one: drawn as panes of its body, so the two read as one thing
        const g = groupOf(b);
        const panes = g ? g.members.map((k) => byKey[k]).filter(Boolean) : [];
        const cls = 'it ' + esc(b.state || 'now') + fcls + (panes.length ? ' fused' : '') + (fresh ? ' fresh' : '') + (wouldFold ? ' foldable' : '') + (compact ? ' compact' : '') + (wouldFold && hovered ? ' hovopen' : '') + (open ? ' openin' : '') + (aged ? ' aged' : '') + (dec ? ' now' : '') + (isNow ? ' waiting' : '') + (px ? ' sized' : '') + (ownHead ? ' ownhead' : '') + (maxed ? ' maxed' : '') + (maxed && this._autoMax === key ? ' reading' : '') + (ufold ? ' ufold' : '') + (this._hovKey === key ? ' hov' : '') + (this.deskAlign() && this._lockGet()[key] ? ' desk-locked' : '');
        return `<div class="${cls}" data-key="${esc(b.key)}" data-size="${size}" data-type="${esc(b.type)}"${panes.length ? ' data-fuse="' + esc(g.layout) + '" data-fused="' + esc(panes.map(p => p.key).join(' ')) + '"' : ''}${mid ? ' data-mid="' + esc(mid) + '"' : ''}${from ? ' data-from="' + esc(from) + '"' : ''}${anchorMids.length ? ' data-anchors="' + esc(anchorMids.join(' ')) + '"' : ''}${beside ? ' data-beside="' + esc(beside) + '"' : ''}${scoreTxt ? ' data-score="' + esc(scoreTxt) + '"' : ''}${px && !compact ? ' style="height:' + Math.round(px) + 'px"' : ''}${pw && !compact ? ' data-pw="' + Math.round(pw) + '"' : ''}>
          ${ownHead && !compact && !bid ? `<span class="fd solo" data-act="fold" title="Fold it to its header line - it stays folded until you open it">\u25be</span>` : ''}${ownHead && !compact ? `<span class="dp solo" data-act="desk-auto" title="Placed by hand on the desk - back to the arrangement">⤺</span><span class="mx solo${maxed ? ' on' : ''}" data-act="max" title="${maxed ? 'Restore it to its place in the column' : 'Maximise it in the canvas - it stays there, fixed, until you restore it'}">${maxed ? '\u2750' : '\u26f6'}</span><span class="xp solo" data-act="open" title="${open ? 'Fold it back' : 'Open in place — the column makes room'}">${open ? '⤡' : '⤢'}</span>` : ''}${
            /* the header line is drawn for EVERY item. An item whose drawer has its own head shows it only when the COLUMN
               folds it (its body, and that head inside it, hidden): it used to be nothing but three hover-only icons,
               0 px tall (owner, 2026-10-04: "if collapsed some canvas elements are only appearing on hover") */ ''}${
              `<div class="it-hd${ownHead && !compact ? ' own-fold' : ''}">${bid ? '' : `<span class="fd" data-act="fold" title="${ufold ? 'Open it' : 'Fold it to its header line - it stays folded until you open it'}">${ufold ? '\u25b8' : '\u25be'}</span>`}<span class="ic vc-badge" data-kind="${esc(b.type)}" title="${esc(b.type)}">${esc(glyphOf(b.type))}</span><span class="t" title="${esc(title)}">${esc(title)}</span>${b.type === 'panel' && this.deskAlign() ? '<span class="ly" data-act="layer-behind" title="Send it behind the other items, filling the free desk">⇣</span><span class="ly" data-act="layer-front" title="Bring it over the other items, filling the free desk">⇡</span>' : ''}${Array.isArray(c.items) ? `<span class="fdn" title="what it holds">${c.items.length}</span>` : ''}${scoreTxt ? '<span class="sc" title="' + esc('relevance ' + scoreTxt + (why ? ' — ' + why : '')) + '">' + esc(scoreTxt) + '</span>' : ''}
            ${mid ? '<span class="src" title="the turn using it">' + esc(mid) + '</span>' : yours ? '<span class="src" title="added by you — it relates to no turn">you</span>' : ''}<span class="k">${esc(bid ? b.type : b.key)}</span>
            ${panes.length ? '<span class="fu-w" title="' + esc(panes.length + ' fused: ' + g.why) + '">+' + panes.length + '</span>' : ''}<span class="dp" data-act="desk-auto" title="Placed by hand on the desk - back to the arrangement">⤺</span><span class="mx${maxed ? ' on' : ''}" data-act="max" title="${maxed ? 'Restore it to its place in the column' : 'Maximise it in the canvas - it stays there, fixed, until you restore it'}">${maxed ? '\u2750' : '\u26f6'}</span><span class="xp" data-act="open" title="${open ? 'Fold it back' : 'Open in place — the column makes room'}">${open ? '⤡' : '⤢'}</span></div>`}
          ${this.deskAlign() ? (() => {
            /* THE DESK CARD'S ONE ROW OF CONTROLS (owner, 2026-10-09: "the controls should mostly be at the top and some like
               panels have multiple layer of controls and duplicated ones"): a panel card carried its header, the panel's own
               bar with the title again and six buttons, and a footer rail of five more. On the desk: the few that matter,
               as icons at the top right; everything else in the ⋯ menu. The footer and the panel's bar are not drawn. */
            const locked = !!this._lockGet()[key]; const ctxOn = inF && (F || b.state === 'pinned');
            const hb = (act, ic, title, on) => `<span class="hb${on ? ' on' : ''}" data-act="${act}" role="button" tabindex="0" title="${esc(title)}">${HDI[ic]}</span>`;
            const vw = viewsOf(b, this);
            return `<div class="hda" data-w="canvas.item.controls">`
              + (vw.views.length > 1 ? hb('hdview', vw.cur in HDI ? vw.cur : 'list', 'View: ' + (VIEW_LABEL[vw.cur] || vw.cur) + ' - see it as ' + vw.views.filter((v) => v !== vw.cur).map((v) => VIEW_LABEL[v].toLowerCase()).join(', ')) : '')
              + hb('lock', locked ? 'lock' : 'unlock', locked ? 'Locked where it is - nothing moves or resizes it; click to free it' : 'Lock it where it is: the desk will not move or resize it', locked)
              + (xplodable ? hb('explode', xploded ? 'source' : 'explode', xploded ? 'Back to the source alone' : 'See it as a structured diagram', xploded) : '')
              + (editable ? hb('edit', 'edit', editing ? 'Editing' : 'Edit its text', editing) : '')
              + (bid ? '' : hb('ctx', 'ctx', ctxOn ? 'In context - click to release it' : 'Put it in context (pins it, so it never leaves focus)', ctxOn))
              + (maxed ? '' : hb('pill', 'pill', 'Minimise it to a pill on the border - a click on the pill opens it again'))
              + hb('max', maxed ? 'restore' : 'max', maxed ? 'Restore it' : 'Fill the free desk with it', maxed)
              + hb('hdmore', 'more', 'More: park, size, back to the arrangement' + (b.type === 'panel' ? ', behind / front, query, refresh, standalone' : ''))
              + hb('remove', 'drop', 'Take it off the desk')
              + `</div>`; })() : ''}
          <div class="it-bd${panes.length ? ' fu fu-' + esc(g.layout) : ''}">${inner}${panes.map(paneHtml).join('')}</div>
          <div class="it-ft" data-w="canvas.item.rail"><span class="it-a">
              ${xplodable ? `<button data-act="explode" class="${xploded ? 'on' : ''}" title="${xploded ? 'Back to the source alone — the diagram goes' : 'See it as a structured diagram, bound to this item by span'}">${xploded ? 'source' : 'graph'}</button>` : ''}
              ${editable ? `<button data-act="edit" class="${editing ? 'on' : ''}" title="Edit its text — saved through canvas.update">${editing ? 'Editing' : 'Edit'}</button>` : ''}
              ${(() => { const vw = viewsOf(b, this); if (vw.views.length < 2 || this.deskAlign()) return ''; const nx = vw.views[(vw.views.indexOf(vw.cur) + 1) % vw.views.length];
                return `<button data-act="setview" data-view="${nx}" title="Seen as ${esc((VIEW_LABEL[vw.cur] || vw.cur).toLowerCase())} - click for ${esc(VIEW_LABEL[nx].toLowerCase())}">${esc(VIEW_LABEL[vw.cur] || vw.cur)} ↻</button>`; })()}
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
      /* ONE BANNER (the canvas's final form §4.2). The add bar IS the column's header row: what this canvas is and
         which revision it is at, then the kinds you can add, then whatever the host hangs beside them. It used to be
         two rows — the host drew a title row and the element drew the add bar under it — which spent 32px of a narrow
         column saying "Session canvas" on a line of its own.
         The host's own controls come through SLOTS rather than being redrawn here: they stay light-DOM nodes of the
         host, so the ids it looks them up by, the handlers on them and the driven ribbon it shows all keep working.
         `banner-start` carries what this canvas is; `banner-end` its controls. */
      /* PARKED ITEMS ARE REACHED FROM THE BANNER, not from the foot of the stage. They were a band after the items,
         sticky to the bottom of a body as tall as the transcript — so the only way to see what you had parked was to
         scroll to the very end of the column, and a thing you cannot find is a thing you lost (owner, 2026-09-24: "i
         cant find the parked items once parked"). Park is meant to be a shelf you can take something back off.
         Same popover the hidden items already use, beside it, in the one row that is always in view. */
      const shelf = (n, label, title, items) => items.length
        ? `<span class="hidwrap"><button class="hidbtn" data-act="${n}" title="${title}">${label} · ${items.length} ▾</button><div class="hidpop" hidden>${items.map(chip).join('')}</div></span>` : '';
      html += `<div class="addbar" data-w="canvas.add"><slot name="banner-start"></slot><span class="lbl">Add</span>${ADD_KINDS.map(k => `<button class="add" data-act="add" data-kind="${k.n}" title="Add a ${k.n} to the session canvas (a ${k.kind} item)"><b>${esc(k.ik)}</b>${k.n}</button>`).join('')}` +
        shelf('parkpop', 'parked', 'Parked items — shelved, not gone; a click brings one back into view', stage ? parked : []) +
        shelf('hid', 'hidden', 'Hidden items — a click brings one back', hidden) +
        /* RECENTLY REMOVED (WSU-66, owner 2026-10-11: "removed canvas element should be restorable"): what canvas.remove and
           canvas.clear took is kept on the document (doc.removed, the last 30); a click puts one back as it was */
        (() => { const rem = ((doc && doc.removed) || []).filter((r) => r && r.key && !keyed.some((b) => String(b.key) === String(r.key))).slice().reverse().slice(0, 12);
          return rem.length ? `<span class="hidwrap"><button class="hidbtn" data-act="rmvpop" title="Recently removed — a click puts one back">removed · ${rem.length} ▾</button><div class="hidpop" hidden>${rem.map((r) => { const c = (r.block && r.block.content) || {}; const t = c.title || c.name || String(r.key).split(':').slice(1).join(':') || (r.block && r.block.type) || r.key;
            return `<button class="chip rmv" data-act="restore" data-rkey="${esc(r.key)}" title="Put it back on the canvas"><i></i>↺ ${esc(String(t).slice(0, 48))}</button>`; }).join('')}</div></span>` : ''; })() +
        /* START AGAIN. A working area you cannot empty fills up until you stop trusting it. Pinned items are what
           you said to keep, so they are kept; a second click within the beat clears those too. */
        (keyed.length ? `<button class="hidbtn" data-act="clear" title="Clear the canvas — pinned items stay; click again to clear those too">clear</button>` : '') +
        `<slot name="banner-end"></slot></div>`;
      // a FUSED MEMBER is drawn inside its lead, never again as a card of its own
      if (pinned.length) html += `<div class="band pinned"><div class="band-h">pinned · ${pinned.length}</div>${pinned.filter(b => !isMember(b)).map(card).join('')}</div>`;
      // the NOW band: what this turn is waiting on first (the decision, then what Vera can also do), then the live items, newest
      // first; on the stage they are placed level with their turns (absolute, after a measure); in the flow they stack
      const nowOrder = now.filter(b => !isMember(b)).slice().sort((x, y) => { const dx = decisionOf(x), dy = decisionOf(y); const wx = dx && !dx.answer ? 0 : dx ? 1 : 2, wy = dy && !dy.answer ? 0 : dy ? 1 : 2; return wx - wy; });
      const nowCards = (nowOrder.length ? nowOrder.slice(0, 1).map(card).join('') : '') + ghost + nowOrder.slice(1).map(card).join('');
      /* the band's header is drawn only when it HAS something to say (see nowText): an empty "NOW" over a column of
         visible items is a label on a label. A named canvas keeps its BLOCKS line, which is that document's state. */
      html += `<div class="band now">${(plainDoc || nowTxt) ? `<div class="band-h"><span class="nowbar ${decision && !decision.answer ? 'wait' : 'ok'}" data-w="canvas.now" title="${plainDoc ? 'The canvas, in its order' : 'What this turn is waiting on'}"><i></i><b>${plainDoc ? 'BLOCKS' : 'NOW'}</b> ${esc(nowTxt)}</span></div>` : ''}` +
        (nowCards ? (stage ? '<div class="stage" id="stage">' + nowCards + '</div>' : nowCards) : plainDoc ? '<div class="empty">Nothing on this canvas — add a block above, or let an agent fill it.</div>' : '<div class="empty">Nothing in the NOW band — nothing is waiting on you; items land here as the conversation uses them.</div>') + '</div>';
      // on the stage the shelf is in the banner (above); in the flow projection the band still reads bottom-to-top
      if (parked.length && !stage) html += `<div class="band parked"><div class="band-h">parked · ${parked.length}</div><div class="chips">${parked.map(chip).join('')}</div></div>`;
      if (plain.length) html += plain.map(b => {
        const fn = BLOCK[b.type] || BLOCK.note; let inner;
        try { inner = fn(b.content || {}); } catch (e) { inner = `<div class="err">Could not render a ${esc(b.type)} block.</div>`; }
        return `<div class="blk" data-type="${esc(b.type)}">${inner}</div>`;
      }).join('');
      /* ⛔ THE SAME MARKUP IS NOT REDRAWN. A streamed diagram or widget arrives as a write every beat, and every one
         of those bumped the revision and brought us here — where the whole column's innerHTML was replaced with a
         string identical to the one already in it. Every placeholder was destroyed and rebuilt, so the live overlay
         had to be re-laid over new boxes several times a second: the item blinked on every token (owner, 2026-09-25:
         "they flicker in the chat and canvas as it streams in and its nowhere near as smooth as it was in chat").
         What CHANGED in those writes is the live content, which does not live in this markup at all — it lives in
         the overlay, and _mountLive below hands it to the element that is already mounted. So: write the markup only
         when the markup is different, and let the live layer take the update. */
      const keepTop = body.scrollTop;
      const layers = this._layers(body);
      const mark = this.canvasId + '\u0000' + html;    // another canvas's identical markup is not this canvas's
      // a code card being edited in place (the kit's editor) keeps the column as it is until the edit ends
      if (this._html !== mark && layers.items.querySelector('.code-card.editing')) this._editHeld = true;
      else if (this._html !== mark || !layers.items.childElementCount) {
        /* AN ITEM BEING WRITTEN FOLLOWS ITS LAST LINE (owner, 2026-10-03: "it also doesnt auto scroll to the most recent
           line if the user is already scrolled to the bottom or not been scrolled up"). Redrawing the markup put every
           scrolling part of every item back at its top. What each one was doing is read first - at its end (or never
           scrolled), or held somewhere by the reader - and given back: the first follows the new end, the second keeps
           its place. */
        /* WSU-64 (owner 2026-10-11: "as the text streams in the most recent content should stay in focus - at the moment
           scroll gets stuck to the top and you can't scroll down as input streams in"): the research element's panes
           (.vc-rr-p) scroll on their own and were not in this list, so every streamed revision put them back at the top
           under the reader's wheel. They are kept like the others, and any part marked data-keep-scroll is too. A part
           the reader has scrolled up in keeps its place while it grows, with a "latest" chip that goes back to the end. */
        const SCROLLERS = '.it-bd, pre, .vc-out, .vc-rr-p, [data-keep-scroll]';
        const sc = {};
        layers.items.querySelectorAll('.it[data-key]').forEach((it) => {
          const k = it.dataset.key; const parts = it.querySelectorAll(SCROLLERS);
          parts.forEach((el, j) => { if (el.scrollHeight > el.clientHeight + 2) sc[k + '\u0000' + j] = { top: el.scrollTop, end: el.scrollHeight - el.scrollTop - el.clientHeight < 24, h: el.scrollHeight }; else sc[k + '\u0000' + j] = { top: 0, end: true, h: el.scrollHeight }; });
        });
        this._html = mark;
        layers.items.innerHTML = html;
        /* ON THE DESK THE REDRAWN CARDS START WHERE THEY WERE: fresh markup has no place, so every card showed at a default
           spot and slid back - the flicker when an item joined. Each card that was on the desk takes its last place and
           size at once (no animation), so only a card the desk really moves animates, from where it was; a card that is
           new starts unseen and fades in where the desk puts it. */
        if (stage && this.deskAlign() && this._placed && this._placed.mode === 'desk') {
          const prev = {}; (this._placed.placements || []).forEach((p) => { prev[p.key] = p; });
          const fresh = [...layers.items.querySelectorAll('.it[data-key]')];
          fresh.forEach((it) => { const p = prev[it.dataset.key];
            if (!p) { it.classList.add('desk-new'); return; }
            it.classList.add('desk-hold');
            it.style.left = p.x + 'px'; it.style.top = p.y + 'px';
            it.style.setProperty('width', p.w + 'px', 'important'); it.style.setProperty('height', p.h + 'px', 'important'); it.style.setProperty('min-height', '0px', 'important');
            it.dataset.deskFace = p.face || ''; it.classList.toggle('desk-chip', p.face === 'chip' || p.face === 'icon'); it.classList.toggle('desk-icon', p.face === 'icon'); });
          void layers.items.offsetWidth;                       /* the held places are drawn before the desk moves anything */
          fresh.forEach((it) => it.classList.remove('desk-hold'));
        }
        if (!stage) body.scrollTop = keepTop;
        layers.items.querySelectorAll('.it[data-key]').forEach((it) => {
          const k = it.dataset.key; it.querySelectorAll(SCROLLERS).forEach((el, j) => {
            const s = sc[k + '\u0000' + j]; if (!s || el.scrollHeight <= el.clientHeight + 2) return;
            el.scrollTop = s.end ? el.scrollHeight : s.top;
            /* held above a growing end: the chip says there is more below and takes the reader there (it then follows) */
            if (!s.end && el.scrollHeight > s.h + 4 && el.matches('.vc-rr-p, [data-keep-scroll], pre, .vc-out')) {
              const b = document.createElement('button'); b.className = 'vc-latest'; b.type = 'button'; b.textContent = '↓ latest'; b.title = 'Go to the newest text and follow it';
              b.addEventListener('click', (e) => { e.stopPropagation(); el.scrollTop = el.scrollHeight; b.remove(); });
              el.appendChild(b); } });
        });
      }
      this._bind(body);
      if (stage) this._placeNow();
      /* ⛔ THE COLUMN STAYS WHERE THE READER LEFT IT. Replacing the items' markup empties the stage for a moment, the
         browser clamps the column's scroll to the top, and on the stage nothing ever put it back - so every redraw
         threw the reader to the top: opening an item in place, a new message, the turn in view changing (which is
         what the context tracking does on every scroll), a focus change (owner, 2026-09-27: "the double arrow make
         the canvas scroll to the top", "new messages in the chat also make the canvas scroll to the top"). Strict
         alignment is the exception: there the transcript owns the column's scroll and setView has just written it. */
      if (stage && !this.strictAlign() && body.scrollTop !== keepTop) body.scrollTop = Math.min(keepTop, Math.max(0, body.scrollHeight - body.clientHeight));
      this._mountLive(body);
      /* the shared output kit in this shadow root (its click handler, its style) and on what was just drawn (lint,
         whole pages, diagrams) - loaded once when a page that shows the canvas did not load it, then drawn again */
      if (root.VeraOut) { try { root.VeraOut.wire(this.shadowRoot, this._voHooks()); root.VeraOut.hydrate(layers.items); } catch (e) {} }
      else if (!this._voLoading) { this._voLoading = true; ensureLib('/ui/elements/vera_output.js', '').then((ok) => { if (ok && root.VeraOut && this._doc) { this._html = null; this.render(this._doc); } }); }
      if (this._editKey) { const ta = body.querySelector('.it[data-key="' + this._editKey.replace(/"/g, '\\"') + '"] textarea'); if (ta && !this._editFocused) { this._editFocused = true; try { ta.focus(); } catch (e) {} } if (ta) this._editWire(ta); }
      // a preview open before the redraw is open after it (the markup was rebuilt folded)
      if (this._peekKey) { const pk = this._peekKey; this._peekKey = null; this._peekOpen(pk); }
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
      /* AN OPEN ADD MENU CLOSES ON A PRESS ANYWHERE ELSE, OR ON ESCAPE (owner, 2026-10-10: "the menus dont close if clicked
         away from"). A press on the add bar itself is the bar's own (its buttons open, switch or close a menu). */
      { const od = this.ownerDocument || document;
        const closeAdd = () => { const p = this.shadowRoot && this.shadowRoot.querySelector('.addbar .addpop'); if (p) { p.remove(); return true; } return false; };
        od.addEventListener('mousedown', (ev) => { const p = this.shadowRoot && this.shadowRoot.querySelector('.addbar .addpop'); if (!p) return;
          const path = ev.composedPath ? ev.composedPath() : []; if (path.some((n) => n && n.classList && n.classList.contains('addbar'))) return; closeAdd(); }, true);
        od.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && closeAdd()) ev.stopPropagation(); }, true);
        /* a press in another frame (the harness, the chat's menu) never reaches this document: the window losing focus is it */
        try { (od.defaultView || window).addEventListener('blur', () => { closeAdd(); }); } catch (e) { /* no window */ } }
      body.addEventListener('click', (ev) => {
        const t = ev.target; if (!t || !t.closest) return;
        if (t.closest('textarea')) return;
        /* a card minimised to a pill (its ⋯ / pill control) opens again where the arrangement puts it, on a click */
        const pc = t.closest('.stage .it.desk-chip[data-key]');
        if (pc && body.contains(pc) && this._pillGet()[pc.dataset.key] && !t.closest('[data-act]:not(.it-hd):not(.t)')) { ev.stopPropagation(); this._pillSet(pc.dataset.key, false); this._html = null; if (this._doc) this.render(this._doc); return; }
        const act = t.closest('[data-act]');
        if (act && body.contains(act)) { ev.stopPropagation(); this._act(act, ev); return; }
        // a source's own two buttons: read the page, see the page
        const sa = t.closest('[data-src-act]');
        if (sa && body.contains(sa)) { ev.stopPropagation(); this._srcAct(sa.dataset.srcKey, sa.dataset.srcAct); return; }
        // a records item: page, sort, view, facet, open a record, read it, land it
        /* a bar of the names · sites menu's widgets (inside the widget's own root): that name or site filters the list */
        const nmenu = t.closest('.vc-rbx-names');
        if (nmenu && body.contains(nmenu)) { ev.stopPropagation(); const path = ev.composedPath ? ev.composedPath() : [];
          const bar = path.find((e) => e && e.classList && e.classList.contains('vb-rw')); const nw = path.find((e) => e && e.classList && e.classList.contains('vc-rbx-nw'));
          const nm = bar && bar.querySelector('.n'); const name = nm ? (nm.getAttribute('title') || nm.textContent || '').trim() : '';
          if (name && nw) this._recAct(nmenu.dataset.recKey, nw.dataset.pick === 'dom' ? 'pickdom' : 'pickent', name); return; }
        /* A RESULT'S TITLE OPENS IT IN READER MODE (owner, 2026-10-11: "if you click the actual main title link text it takes
           you to full reader mode of the page instead of to the page"): the page fills the room (see _readerFollow). A
           ctrl / cmd / middle click still opens the page itself, and so does "open ↗". */
        const tl = t.closest('a.vc-rbx-t');
        if (tl && body.contains(tl) && !(ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button === 1)) { const row = tl.closest('[data-rec-act][data-rec-key]');
          if (row && row.dataset.recArg) { ev.preventDefault(); ev.stopPropagation(); this._recAct(row.dataset.recKey, 'read', row.dataset.recArg); return; } }
        if (t.closest('[data-rec-stop]') || t.closest('.vc-rbx-q') || (t.closest('a') && t.closest('.vc-rbx'))) return;
        const ra = t.closest('[data-rec-act]');
        // inside an OPENED record (its text, its facts, its fields) or an opened neighbour's text, a click is reading, not
        // closing: only the buttons there act - the row's own open/close is its head
        if (ra && body.contains(ra) && ((ra.matches('.vc-rbx-row,tr') && t.closest('.vc-rbx-open') && ra.contains(t.closest('.vc-rbx-open')))
            || (ra.matches('.vc-rbx-nbr') && t.closest('.vc-rbx-nbt')))) { ev.stopPropagation(); return; }
        if (ra && body.contains(ra)) { ev.stopPropagation(); this._recAct(ra.dataset.recKey, ra.dataset.recAct, ra.dataset.recArg); return; }
        // a timeline's axis: a dot is the way to its event (the list scrolls to it and lights it a moment)
        const tg = t.closest('[data-tl-go]');
        if (tg && body.contains(tg)) { ev.stopPropagation(); const box = tg.closest('.vc-tl'); const row = box && box.querySelector('.vc-tl-e[data-i="' + tg.dataset.tlGo + '"]');
          if (row) { try { row.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { row.scrollIntoView(); } row.classList.add('flash'); setTimeout(() => row.classList.remove('flash'), 1400); } return; }
        // the calendar's month buttons and its days
        const wa = t.closest('[data-wid-act]');
        if (wa && body.contains(wa)) { ev.stopPropagation(); this._widAct(wa.dataset.widKey, wa.dataset.widAct); return; }
        const cm = t.closest('[data-cal-mv]');
        if (cm && body.contains(cm)) { ev.stopPropagation(); this._calAct(cm.dataset.calKey, cm.dataset.calMv); return; }
        const cd = t.closest('[data-cal-day]');
        if (cd && body.contains(cd)) { ev.stopPropagation(); this._calAct(cd.dataset.calKey, cd.dataset.calDay); return; }
        // a line of a code item: the card that covers it lights in every explode item bound to this one
        const ln = t.closest('.vc-codewrap[data-code] .vc-line');
        if (ln && body.contains(ln)) { const w = ln.closest('.vc-codewrap');
          w.querySelectorAll('.vc-line.tap').forEach((x) => x.classList.remove('tap')); ln.classList.add('tap');
          this._sourceToExplode(w); return; }
        const ch = t.closest('.chip[data-key]'); if (ch) { ev.stopPropagation(); this.call('canvas.add', { key: ch.dataset.key }); return; }
        const hd = t.closest('.it-hd'); const it = hd && hd.closest('.it[data-key]');
        if (it && !it.classList.contains('ghost')) { ev.stopPropagation(); if (it.classList.contains('ufold')) this._setFold(it.dataset.key, false); else this._toggleOpen(it.dataset.key); }
      });
      // hover: the host hears which item is under the pointer (the runs light up); in the Hover tier a folded item
      // opens in the layout while the pointer is on it — the column makes room, like a click in Zen
      // a SELECTION of lines says more than a click: the card covering the whole range lights when the drag ends
      body.addEventListener('mouseup', (ev) => { const w = ev.target.closest && ev.target.closest('.vc-codewrap[data-code]');
        if (w) setTimeout(() => this._sourceToExplode(w), 0); });
      /* WHAT IS UNDER THE POINTER, WITHOUT FLICKER (owner, 2026-09-29: 'the canvas elements flicker if you put the mouse near
         its edge or the edge of inner frames/divs'). A widget, a frame, a graph is drawn in the LIVE layer - a sibling of
         the items, over their slots - so crossing onto one left the item: :hover dropped (its ground and controls faded),
         a hover event went out, and in the Hover tier the item folded, the column re-placed and it opened again. The
         live layer now belongs to its item (.lv carries the key), hover is a class the render keeps (.hov) rather than
         only :hover, and a change of item is settled for a beat so an edge or a gap between two items is not a flip. */
      const hovAt = (ev) => { const t = ev.target; if (!t || !t.closest) return null;
        const it = t.closest('.it[data-key]'); if (it) return it.dataset.key; const lv = t.closest('.lv[data-key]'); return lv ? lv.dataset.key : null; };
      const hovTo = (k) => { clearTimeout(this._hovT); if (k === this._hovWant) return; this._hovWant = k;
        this._hovT = setTimeout(() => this._hovSet(this._hovWant), k ? 60 : 160); };
      body.addEventListener('mouseover', (ev) => hovTo(hovAt(ev)));
      body.addEventListener('mouseleave', () => hovTo(null));
      // the corner grip: a drag sizes the item; the drop saves it as the item's size (s · m · l · xl)
      body.addEventListener('mousedown', (ev) => { const g = ev.target.closest && ev.target.closest('.rz'); if (!g) return; const it = g.closest('.it[data-key]'); if (!it) return;
        ev.preventDefault(); ev.stopPropagation();
        // its height and width as they are NOW - read before 'sized' (which lets a drawing take the room) changes them
        const h0 = it.offsetHeight, w0 = it.offsetWidth; it.classList.add('sized', 'resizing');
        /* BOTH AXES. The grip's cursor has always said nwse-resize, but only clientY was ever read: the height
           followed the mouse and the width was whatever the placer handed out — and since a dragged item asked for
           the stage, that was every column of it. So an item you resized went full width and no amount of dragging
           left brought it back (owner, 2026-09-25: "if i resize an element in the canvas it takes the full width and
           cant be shrunk in width"). The width is now a pixel fact of the item, like its height. */
        /* the pointer moves in screen pixels and the card is sized in layout pixels: under the UI scale (zoom 1.4) a drag
           grew it 1.4 times as far as the hand moved */
        const Z = it.offsetWidth ? (it.getBoundingClientRect().width / it.offsetWidth) || 1 : 1;
        this._rz = { key: it.dataset.key, el: it, y0: ev.clientY, h0, h: h0, x0: ev.clientX, w0, w: w0, Z,
          l0: parseFloat(it.style.left) || 0, t0: parseFloat(it.style.top) || 0 };
        /* on the desk the card you are sizing rides over the rest (owner, 2026-10-10: grown by its corner, "other panels
           can move partially behind it"): it and its graph are on top, with their ground, until it is let go */
        if (this.deskAlign() && root.VeraDesk) { it.style.zIndex = String(root.VeraDesk.stackOrder(0, { dragging: true })); try { this._liveLayout(); } catch (e) { /* optional */ } } });
      /* MOVING A CARD ON THE DESK (owner-approved desk, 2026-10-06: "as you drag them in they become big enough to glance
         at"). Its header is the handle; past 4 px it follows the pointer, sized live by its distance from the chat and
         stacked on top; let go, it stays there (a share of the desk, per canvas). A double click on the header gives it
         back to the arrangement. A control in the header (data-act, a field) is never a handle. */
      const deskHandle = (ev) => { if (!this.deskAlign() || ev.button !== 0) return null; const t = ev.target; if (!t || !t.closest) return null;
        if (t.closest('[data-act],.rz,input,textarea,select,button,a,.lay-pill')) return null;
        const hd = t.closest('.it-hd,.vc-th'); const it = hd && hd.closest('.stage .it[data-key]');
        /* a card filling the room (maximised, or reading) is a handle too: carried, it goes back to the desk (see below) */
        if (it && it.dataset.deskFace === 'layer-front' && this._max === it.dataset.key) return it;
        if (!it || it.dataset.deskFace === 'layer-behind' || it.dataset.deskFace === 'layer-front' || it.classList.contains('desk-locked')) return null; return it; };
      body.addEventListener('mousedown', (ev) => { const it = deskHandle(ev); if (!it) return;
        const st = this.shadowRoot.getElementById('stage'); if (!st) return;
        ev.preventDefault();   /* a card carried by its header does not select the text it passes over */
        /* FROM FULL BACK TO THE DESK BY CARRYING IT (owner, 2026-10-11: "id like to be able to move items from fully expanded
           back to the desk by moving them"): a press on a filling card's header waits for the hand to move; then the card
           is restored and picked up under the hand, and goes where it is let go */
        if (it.dataset.deskFace === 'layer-front') { this._dgMax = { key: it.dataset.key, x0: ev.clientX, y0: ev.clientY }; return; }
        const Z = st.offsetWidth ? (st.getBoundingClientRect().width / st.offsetWidth) || 1 : 1;
        this._dg = { el: it, key: it.dataset.key, x0: ev.clientX, y0: ev.clientY, Z,
          cx0: (parseFloat(it.style.left) || 0) + it.offsetWidth / 2, cy0: (parseFloat(it.style.top) || 0) + it.offsetHeight / 2, moved: false }; });
      body.addEventListener('dblclick', (ev) => { const it = deskHandle(ev); if (!it) return;
        if (!this._deskPosGet()[it.dataset.key]) return; ev.preventDefault();
        this._deskPosSet(it.dataset.key, null); this._placeNow(); });
      /* a click that ends a drag is not a click on what was under it */
      body.addEventListener('click', (ev) => { if (Date.now() - (this._dgEnd || 0) < 250) { ev.stopPropagation(); ev.preventDefault(); } }, true);
      const doc = this.ownerDocument || document;
      doc.addEventListener('mousemove', (ev) => {
        const m = this._dgMax;
        if (m) { if (Math.abs(ev.clientX - m.x0) + Math.abs(ev.clientY - m.y0) < 8) return; this._dgMax = null;
          if (this._autoMax === m.key) this._autoMax = null; if (this._max === m.key) this._toggleMax(m.key);
          const st = this.shadowRoot.getElementById('stage'); const el = st && [...st.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === m.key); if (!el) return;
          const S = st.getBoundingClientRect(), Z = st.offsetWidth ? (S.width / st.offsetWidth) || 1 : 1;
          /* the card's header under the hand: its centre is half its height below the pointer */
          const px = (ev.clientX - S.left) / Z, py = (ev.clientY - S.top) / Z;
          this._dg = { el, key: m.key, x0: ev.clientX, y0: ev.clientY, Z, cx0: px, cy0: py + Math.max(20, el.offsetHeight / 2 - 16), moved: false };
          return; }
        const d = this._dg; if (!d) return;
        const dx = (ev.clientX - d.x0) / d.Z, dy = (ev.clientY - d.y0) / d.Z;
        if (!d.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
        if (!d.moved) { d.moved = true; d.el.classList.add('desk-dragging'); d.el.style.zIndex = String(root.VeraDesk ? root.VeraDesk.stackOrder(0, { dragging: true }) : 200);
          /* a pill opened by hover is let go as it starts moving - the drag sizes it from here on (WSU-68) */
          clearTimeout(this._pillT); if (d.el._cpw) { d.el._cpw = null; d.el.classList.remove('chip-peek', 'pill-open'); } }
        ev.preventDefault();
        const g = this._deskGeo, D = root.VeraDesk; if (!g || !D) return;
        const cx = Math.max(10, Math.min(g.W - 10, d.cx0 + dx)), cy = Math.max(10, Math.min(g.H - 10, d.cy0 + dy));
        const pw = this._pw[d.key] || parseInt(d.el.dataset.pw || '0', 10) || 0, ph = this._px[d.key] || 0;
        const so = { size: pw > 0 ? { w: pw, h: ph || Math.round(pw * 0.66) } : null, pinned: pw > 0 && d.el.classList.contains('pinned') };
        /* a card sized by the person keeps its size as it is carried; any other is sized by where it is */
        const kept = this._deskPosGet()[d.key]; let own = kept && kept.w > 0 && kept.h > 0 ? kept : null;
        /* ...but carried out to where a card is a pill, it IS a pill, whatever size it was given (owner, 2026-10-11: "trying to
           get the 'from the fabric' desk element to turn into a pill by dragging it away from chat"; code with its explode
           open "wont turn back into a chip if dragged to the far edge"): a kept size, or the person's width, held it big. Let
           go there, it is a pill and its kept size goes. */
        const bare = D.sizeAt(g.R, cx, cy, {}, g.desk);
        if (bare.face === 'chip' || bare.face === 'icon') own = null;
        const s = own ? { w: own.w, h: own.h, face: own.face || 'max', t: 1 } : ((bare.face === 'chip' || bare.face === 'icon') ? bare : D.sizeAt(g.R, cx, cy, so, g.desk)), w = Math.min(g.W - 16, s.w), h = Math.min(g.H - 8, s.h);
        d.cx = cx; d.cy = cy; d.own = own;
        /* the face changes with a margin: a card held near a boundary (chip | card | full) flipped its contents back and
           forth as the hand trembled - the flicker while moving (owner, 2026-10-08). The size still follows smoothly. */
        const TH = { chip: 0.25, card: 0.6 }, M = 0.05, cur = d.el.dataset.deskFace || s.face;
        let face = s.face;
        if (cur === 'chip' && face !== 'chip' && s.t < TH.chip + M) face = 'chip';
        else if (cur === 'card' && face === 'chip' && s.t > TH.chip - M) face = 'card';
        else if (cur === 'card' && face === 'max' && s.t < TH.card + M) face = 'card';
        else if (cur === 'max' && face === 'card' && s.t > TH.card - M) face = 'max';
        if (face !== cur || !d.el.dataset.deskFace) { d.el.classList.toggle('desk-chip', face === 'chip'); d.el.classList.remove('desk-icon'); d.el.dataset.deskFace = face; }
        d.el.style.left = Math.round(cx - w / 2) + 'px'; d.el.style.top = Math.round(cy - h / 2) + 'px';
        d.el.style.setProperty('width', w + 'px', 'important'); d.el.style.setProperty('height', h + 'px', 'important');
        try { this._liveLayout(); } catch (e) { /* optional */ } this._liveFollow(500); });
      doc.addEventListener('mouseup', () => { this._dgMax = null; const d = this._dg; if (!d) return; this._dg = null;
        if (!d.moved) return;
        this._dgEnd = Date.now(); d.el.classList.remove('desk-dragging');
        /* let go where a card is a pill: it is kept a pill there (the person's earlier size does not hold it big, see the plan) */
        /* WHERE IT IS LET GO (owner, 2026-10-11: "i drag the pill to the desk and it goes back to the furthest area from chat.
           pills put near the furthest point should kinda stick to it so they align on whatever side is furthest from chat"):
           let go on the desk, it stays there (and a card minimised to a pill is a pill no more); let go where a card is a
           pill, it joins the border's column of pills - no exact spot is kept, so it lines up with them */
        const asChip = /^(chip|icon)$/.test(d.el.dataset.deskFace || '');
        const g = this._deskGeo;
        if (g && d.cx != null) {
          if (asChip) this._deskPosSet(d.key, { chip: true });
          else { this._deskPosSet(d.key, Object.assign({ fx: +(d.cx / g.W).toFixed(4), fy: +(d.cy / g.H).toFixed(4) }, d.own ? { w: d.own.w, h: d.own.h, face: d.own.face } : {}));
            if (this._pillGet()[d.key]) this._pillSet(d.key, false); }
        }
        this._placeNow(); });
      doc.addEventListener('mousemove', (ev) => { const r = this._rz; if (!r) return;
        const h = Math.max(40, r.h0 + (ev.clientY - r.y0) / (r.Z || 1)); r.h = h; r.el.style.height = h + 'px';
        const w = Math.max(120, r.w0 + (ev.clientX - r.x0) / (r.Z || 1)); r.w = w; r.el.style.width = w + 'px';
        r.el.classList.add('openin');
        /* on the desk only the card being resized moves while the grip is held (its widget follows it); the desk is
           placed once, on the drop - re-placing every card on every move made the resize jitter and lag */
        if (this.deskAlign()) { try { this._liveLayout(); } catch (e) { /* optional */ } this._liveFollow(400);
          /* ...and when the hand rests a moment the others move out of its way (the card being sized stays put, its
             corner under the hand) - not on every move, which made the resize jitter */
          clearTimeout(this._rzPlaceT); this._rzPlaceT = setTimeout(() => { if (this._rz === r) { try { this._placeNow(); } catch (e) { /* optional */ } } }, 220); }
        else if (!r.raf) r.raf = requestAnimationFrame(() => { r.raf = 0; if (this.hasAttribute('stage')) this._placeNow(); }); });
      doc.addEventListener('mouseup', () => { const r = this._rz; if (!r) return; this._rz = null; r.el.classList.remove('resizing'); this._rzT = Date.now();
        const key = r.key; this._px[key] = r.h; this._pw[key] = r.w; this._open.add(key);
        /* on the desk a card you sized stays where it is: grown, it was separated outward into the chip zone and came back
           a chip. Its place is pinned as a drag pins it; the others move round it. */
        const g = this._deskGeo;
        if (g && this.deskAlign()) {
          const cx = (parseFloat(r.el.style.left) || 0) + r.w / 2, cy = (parseFloat(r.el.style.top) || 0) + r.h / 2;
          /* ...AT THE SIZE YOU GAVE IT (owner, 2026-10-10: "i adjust the size of a graph element and another element can
             fall inside it from the right edge"): the placer re-sized a sized card by its distance from the chat - grown
             towards the border it came back smaller, or a chip, and the rest were placed for a size it did not have. Its
             rectangle is the person's now: kept exactly, and everything else goes round it. */
          const face = (r.el.dataset.deskFace && r.el.dataset.deskFace !== 'chip' && r.el.dataset.deskFace !== 'icon') ? r.el.dataset.deskFace : 'max';
          this._deskPosSet(key, { fx: +(Math.max(0, Math.min(g.W, cx)) / g.W).toFixed(4), fy: +(Math.max(0, Math.min(g.H, cy)) / g.H).toFixed(4), w: Math.round(r.w), h: Math.round(r.h), face });
        }
        /* ...and onto the card NOW: the placer reads the card's data-pw, which only a redraw rewrites - a drag that kept
           the size class placed the card at the width of the drag before and it snapped back (owner, 2026-10-04: "i
           also cannot resize an element on the canvas to span more columns than it has") */
        r.el.dataset.pw = String(Math.round(r.w));
        const size = sizeOfHeight(r.h, this._vh || 0);   // against the column you dragged it in, not a fixed number
        try { this.dispatchEvent(new CustomEvent('vera:canvas:resized', { bubbles: true, detail: { key, height: r.h, size } })); } catch (e) {}
        if (size !== r.el.dataset.size) this._setSize(key, size); else if (this.hasAttribute('stage')) this._placeNow(); });
      // a picker's search box: the rows that do not carry the words are hidden, a group with none left with them
      body.addEventListener('input', (ev) => { const q = ev.target; if (!q || !q.dataset || q.dataset.recQ == null) return;
        const key = q.dataset.recQ, st = recState(this, key); st.q = String(q.value || ''); st.page = 0; clearTimeout(this._recQT);
        this._recQT = setTimeout(() => { const pos = q.selectionStart; if (this._doc) this.render(this._doc);
          const nq = body.querySelector('[data-rec-q="' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]'); if (nq) { nq.focus(); try { nq.setSelectionRange(pos, pos); } catch (_) {} } }, 220); });
      body.addEventListener('input', (ev) => { const q = ev.target; if (!q || !q.classList || !q.classList.contains('pk-q')) return; const s = String(q.value || '').toLowerCase().trim(); const list = q.closest('.pk') && q.closest('.pk').querySelector('.pk-list'); if (!list) return;
        let grp = null, any = false; [...list.children].forEach((n) => { if (n.classList.contains('grp')) { if (grp) grp.hidden = !any; grp = n; any = false; return; } const on = !s || (n.dataset.q || '').includes(s); n.hidden = !on; any = any || on; }); if (grp) grp.hidden = !any; });
      body.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && this._editKey) { this._editKey = null; this._editFocused = false; if (this._doc) this.render(this._doc); } });
    }
    /* a folded item opens in the layout under the pointer — every folded item in the Hover tier, an aged one in any
       tier ("hover to read, click to open") — and folds back when the pointer leaves; one opened by a click stays */
    // the hovered item, settled: its class, the host's event, the Hover tier's opening
    _hovSet(k) {
      k = k || null; if (k === this._hovKey) return;
      const body = this.shadowRoot && this.shadowRoot.getElementById('body'); if (!body) return;
      const find = (x) => x ? body.querySelector('#items .it[data-key="' + String(x).replace(/"/g, '\\"') + '"]') : null;
      const prev = find(this._hovKey); if (prev) prev.classList.remove('hov');
      this._chipPeek(prev, false);
      this._hovKey = k; const it = find(k); if (it) it.classList.add('hov');
      this._chipPeek(it, true);
      this._hoverOpen(it);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:hover', { bubbles: true, detail: { key: k } })); } catch (e) {}
    }
    /* A PILL SAYS WHAT IT IS UNDER THE POINTER (owner, 2026-10-10: "if they are minimised to a pill its often too small to
       read what they are, could expand to display the entire title on mouse over"): a chip or an icon on the desk widens
       in place to its whole title, over its neighbours and with a ground, toward the desk if it would run off its edge;
       it goes back when the pointer leaves. Nothing else moves; if the desk was placed meanwhile it is placed again. */
    _chipPeek(it, on) {
      if (!it || !this.deskAlign()) return;
      if (!on) { clearTimeout(this._pillT); const s = it._cpw; if (!s) return; it._cpw = null; it.classList.remove('chip-peek', 'pill-open'); if (s.icon) it.classList.add('desk-icon');
        if (s.open) { it.classList.add('desk-chip'); it.dataset.deskFace = s.face; it.style.setProperty('height', s.h, 'important'); it.style.top = s.t; }
        if (s.seq !== this._placeSeq) { try { this._placeNow(); } catch (e) { /* placed on the next pass */ } }
        else { it.style.setProperty('width', s.w, 'important'); it.style.left = s.l; it.style.zIndex = s.z; }
        if (s.open) { try { this._liveLayout(); } catch (e) { /* optional */ } }
        requestAnimationFrame(() => { it.style.transition = ''; }); return; }
      if (it._cpw || !it.classList.contains('desk-chip')) return;
      /* NOT WHILE IT IS BEING CARRIED (WSU-68, owner 2026-10-11: "some of the elements can't be fully dragged to the far
         edge ... they get so far and turn into a pill but can't be dragged all the way - seems a bit random"): a card that
         became a pill under the hand is hovered, and a pause of 260 ms opened it again as a 460 x 340 card kept inside the
         desk - pulled back from the edge it was being carried to. Random because it took a pause. */
      if (it.classList.contains('desk-dragging') || (this._dg && this._dg.moved)) return;
      /* A PILL OPENS UNDER THE POINTER (owner, 2026-10-11: "when a desk item is minimised to a pill and the user mouses over
         it it should expand - not for the code and mentioned desk items"): the title widening below only ran when a title
         was cut off, and "Mentioned" or "python · scan" fit. After a short pause the pill opens as a card - its content,
         about 460 x 340, over its neighbours and kept on the desk - and goes back to a pill when the pointer leaves. */
      const key = it.dataset.key; clearTimeout(this._pillT);
      this._pillT = setTimeout(() => { if (this._hovKey !== key || (it._cpw && it._cpw.open) || !it.isConnected || it.classList.contains('desk-dragging') || (this._dg && this._dg.moved)) return;
        /* the title widening (below) ran first: undone, the card opens in its place */
        if (it._cpw) { const w0 = it._cpw; it._cpw = null; it.classList.remove('chip-peek'); it.style.setProperty('width', w0.w, 'important'); it.style.left = w0.l; it.style.zIndex = w0.z; if (w0.icon) it.classList.add('desk-icon'); }
        if (!it.classList.contains('desk-chip')) return;
        const st = it.parentElement; const W = (st && st.clientWidth) || 1000, H = (st && st.clientHeight) || 800;
        const s = { w: it.style.getPropertyValue('width'), h: it.style.getPropertyValue('height'), l: it.style.left, t: it.style.top, z: it.style.zIndex,
          icon: it.classList.contains('desk-icon'), face: it.dataset.deskFace, seq: this._placeSeq, open: true };
        it._cpw = s; it.style.transition = 'none';
        it.classList.remove('desk-chip', 'desk-icon'); it.classList.add('chip-peek', 'pill-open'); it.dataset.deskFace = 'card';
        const w = Math.min(W - 8, 460), h = Math.min(H - 8, 340);
        let left = parseFloat(s.l) || 0, top = parseFloat(s.t) || 0;
        if (left + w > W - 4) left = Math.max(4, W - 4 - w); if (top + h > H - 4) top = Math.max(4, H - 4 - h);
        it.style.setProperty('width', w + 'px', 'important'); it.style.setProperty('height', h + 'px', 'important');
        it.style.left = left + 'px'; it.style.top = top + 'px'; it.style.zIndex = '210';
        try { this._liveLayout(); } catch (e) { /* optional */ } }, 260);
      const icon = it.classList.contains('desk-icon');
      const s = { w: it.style.getPropertyValue('width'), l: it.style.left, z: it.style.zIndex, icon, seq: this._placeSeq };
      it.style.transition = 'none'; if (icon) it.classList.remove('desk-icon');
      const t = it.querySelector(':scope > .it-hd > .t'); if (!t) { if (icon) it.classList.add('desk-icon'); it.style.transition = ''; return; }
      const short = t.scrollWidth - t.clientWidth;
      if (short <= 1 && !icon) { it.style.transition = ''; return; }   // its whole title already shows
      it._cpw = s; it.classList.add('chip-peek');
      const W = (it.parentElement && it.parentElement.clientWidth) || 1000;
      const w = Math.min(W - 8, it.offsetWidth + Math.max(0, t.scrollWidth - t.clientWidth) + 8);
      let left = parseFloat(it.style.left) || 0; if (left + w > W - 4) left = Math.max(4, W - 4 - w);
      it.style.setProperty('width', w + 'px', 'important'); it.style.left = left + 'px'; it.style.zIndex = '210';
    }
    /* A FOLDED ITEM, PREVIEWED IN PLACE (owner, 2026-10-04: "hovering over lots of collapsed canvas items makes the
       previews flicker in and out as it doesnt stay in-place under the mouse. sometime hovering them doesnt open the
       preview"). It opened inside the layout: the column re-placed around the opened item, the item or its neighbours
       slid, the pointer was over something else, it folded, the column re-placed - the flicker. And only items the tier
       folded opened; one folded by you or by the enrich default (the same chip to look at) did not. Now any folded item
       previews after a short pause, OVER the items below it: to the column it is still its folded line (the placer reads
       its folded height, its height watcher ignores it), it opens down and to the right from where the pointer is, at
       least a reading width wide, and folds back the moment the pointer is elsewhere. A click still opens it for good. */
    _hoverOpen(it) {
      clearTimeout(this._peekT);
      const key = it && it.dataset ? it.dataset.key : null;
      if (this._peekKey && this._peekKey !== key) this._peekClose();
      /* not on the desk: a desk card shows its body by its distance from the chat, a chip by being brought in. The peek
         re-flowed every card the pointer crossed (compact off, peek and hovopen on, back again) and with it the graph
         drawn over the card - resized 34-53 times in one pass of the mouse, the graphs' flicker under the hand */
      if (this.deskAlign()) return;
      if (!key || this._peekKey === key || this._rz || it.classList.contains('ghost') || !(it.classList.contains('compact') || it.classList.contains('overfold'))) return;
      this._peekT = setTimeout(() => { if (this._hovKey === key) this._peekOpen(key); }, 220);
    }
    // an item scrolled into the column's view once the canvas has drawn and placed it (a few tries: the add is a round trip)
    _revealSoon(key, tries) {
      const n = (tries | 0) + 1; if (n > 12) return;
      setTimeout(() => {
        const it = this._peekFind(key); const body = this.shadowRoot && this.shadowRoot.getElementById('body');
        if (!it || !body || it.classList.contains('overfold') || it.offsetHeight < 60) return this._revealSoon(key, n);
        const b = body.getBoundingClientRect(), r = it.getBoundingClientRect();
        if (r.bottom > b.bottom - 8) body.scrollTop += Math.min(r.bottom - b.bottom + 16, r.top - b.top - 60);
        else if (r.top < b.top + 60) body.scrollTop -= (b.top + 60 - r.top);
      }, n === 1 ? 600 : 400);
    }
    _peekFind(key) { const body = this.shadowRoot && this.shadowRoot.getElementById('body');
      return key && body ? body.querySelector('#items .it[data-key="' + String(key).replace(/"/g, '\\"') + '"]') : null; }
    _peekOpen(key) {
      const it = this._peekFind(key); if (!it || !(it.classList.contains('compact') || it.classList.contains('overfold'))) return;
      it._peekH = it.offsetHeight; it._peekSave = { w: it.style.width, l: it.style.left };
      this._peekKey = key;
      // a fold the column made stays the column's (it re-folds on its own pass); the preview shows the body over it
      it._peekWas = it.classList.contains('compact') ? 'compact' : 'overfold';
      it.classList.remove('compact'); it.classList.add('peek', 'hovopen');
      this._peekGeom(it);
      try { this._liveLayout(); } catch (e) {}
    }
    // a preview is at least a reading width wide (the column's share can be a sliver), kept inside the stage
    _peekGeom(it) {
      const st = this.shadowRoot && this.shadowRoot.getElementById('stage'); const W = st ? st.clientWidth : 0; if (!W) return;
      const left = parseFloat(it.style.left) || 0, cur = it.offsetWidth, want = Math.min(W, Math.max(cur, 440));
      it._peekSave = { w: it.style.width, l: it.style.left };
      if (want > cur) { it.style.width = want + 'px'; if (left + want > W) it.style.left = Math.max(0, W - want) + 'px'; }
    }
    _peekClose() {
      const key = this._peekKey; this._peekKey = null; clearTimeout(this._peekT);
      const it = this._peekFind(key); if (!it || !it.classList.contains('peek')) return;
      it.classList.remove('peek', 'hovopen'); if (it._peekWas !== 'overfold') it.classList.add('compact');
      const s = it._peekSave; if (s) { it.style.width = s.w; it.style.left = s.l; } it._peekSave = null;
      try { this._liveLayout(); } catch (e) {}
    }
    /* the folds you made, per canvas: {key: true|false}; an item you never folded or opened takes its default (an enrich
       item folded, the rest open) */
    _folds() {
      const id = String(this.canvasId || '');
      if (this._foldsFor !== id) { this._foldsFor = id; this._foldMap = {}; try { this._foldMap = JSON.parse(localStorage.getItem('vera:canvas:fold:' + id) || '{}') || {}; } catch (e) { this._foldMap = {}; } }
      return this._foldMap;
    }
    _isUFolded(key, from, prepared) {
      const f = this._folds(); key = String(key);
      if (Object.prototype.hasOwnProperty.call(f, key)) return !!f[key];
      if (prepared) return false;
      return from === 'enrich' || key.indexOf('enrich:') === 0;
    }
    _setFold(key, on) {
      key = String(key); const f = this._folds(); f[key] = !!on;
      const ks = Object.keys(f); if (ks.length > 400) ks.slice(0, ks.length - 400).forEach((k) => { delete f[k]; });   // bounded
      try { localStorage.setItem('vera:canvas:fold:' + this._foldsFor, JSON.stringify(f)); } catch (e) {}
      // folding closes it; unfolding is asking to see it, so it opens IN PLACE - at its size's cap a list opened to its
      // graph was cut off under its own header line (measured on the mirror)
      if (on) this._open.delete(key); else this._open.add(key);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:fold', { bubbles: true, detail: { key, folded: !!on } })); } catch (e) {}
      if (this._doc) this.render(this._doc);
    }
    _toggleOpen(key) {
      if (Date.now() - (this._rzT || 0) < 350) return;   // the click that ended a resize
      if (this._open.has(key)) this._open.delete(key); else this._open.add(key);
      try { this.dispatchEvent(new CustomEvent('vera:canvas:open', { bubbles: true, detail: { key, open: this._open.has(key) } })); } catch (e) {}
      if (this._doc) this.render(this._doc);
    }
    _toggleMax(key) {
      const body = this.shadowRoot.getElementById('body');
      if (this._max !== key && body) this._maxTop = body.scrollTop;   // where the reader was, for when it is restored
      const back = this._max === key; this._max = back ? '' : key;
      try { const k = 'vera:canvas:max:' + this.canvasId; if (this._max) localStorage.setItem(k, this._max); else localStorage.removeItem(k); } catch (e) {}
      if (this._max) this.dataset.maxed = '1'; else delete this.dataset.maxed;
      try { this.dispatchEvent(new CustomEvent('vera:canvas:max', { bubbles: true, detail: { key, max: this._max === key } })); } catch (e) {}
      if (this._doc) this.render(this._doc);
      if (back && body && this._maxTop != null) { body.scrollTop = this._maxTop; this._maxTop = null; }
    }
    _blockOf(key) { const bl = (this._doc && this._doc.blocks) || []; key = String(key); return bl.find(b => b && b.key != null && String(b.key) === key) || (key.startsWith('blk:') ? bl.find(b => b && !b.key && String(b.id) === key.slice(4)) : null) || null; }
    /* how a write names the block: the resolver's key, or — a keyless block of an agent's canvas — its block_id */
    _bidOf(key) { const b = this._blockOf(key); return b && !b.key ? String(b.id) : ''; }
    /* THE ITEM BEING WRITTEN, AS IT IS WRITTEN (owner, 2026-10-03: "the streaming to the canvas is not smooth like the
       chat"). The host's writes go to the server a few times a second and come back as a revision; between them the
       item stood still and then jumped. The host hands the growing content here on every paint instead: the item's
       content in this canvas's document is replaced and the column redrawn on the next frame - the write that
       persists it still follows, and the revision it brings back changes nothing that is already drawn. */
    liveUpdate(key, content) {
      const b = this._blockOf(key); if (!b || !content || typeof content !== 'object') return false;
      // held over every document until the host says the stream is done: a revision that left the server before the
      // last write would otherwise draw the item a step back
      (this._liveOv = this._liveOv || {})[String(key)] = content;
      (this._liveAt = this._liveAt || {})[String(key)] = Date.now();
      b.content = Object.assign({}, b.content || {}, content);
      this._liveRedraw(); this._liveExpire();
      return true;
    }
    /* THE STREAM IS OVER, SO IS THE STREAMING CARD (owner, 2026-10-03: "sometimes the code is stuck in the 'streaming'
       state and the controls on the top of the code in the canvas dont appear"). Two ways it stuck: the end let go of the
       item but drew nothing, so a card whose last revision had already come back stayed the streaming card (no bar)
       until something else redrew the column; and a stream that never ended in the chat's landing (stopped, failed,
       the page's own error) never let go at all. Now the end redraws, and an item nothing has grown for a few seconds
       lets go by itself. */
    liveDone(key) {
      if (!this._liveOv) return;
      if (key == null) { this._liveOv = null; this._liveAt = null; } else { delete this._liveOv[String(key)]; if (this._liveAt) delete this._liveAt[String(key)]; }
      this._liveRedraw();
    }
    _liveRedraw() { if (!this._liveRaf) this._liveRaf = requestAnimationFrame(() => { this._liveRaf = 0; if (this._doc) this.render(this._doc); }); }
    _liveExpire() {
      if (this._liveT) return;
      this._liveT = setTimeout(() => {
        this._liveT = 0; if (!this._liveOv) return;
        const now = Date.now(); let gone = false;
        Object.keys(this._liveOv).forEach((k) => { if (now - ((this._liveAt && this._liveAt[k]) || 0) > 8000) { delete this._liveOv[k]; if (this._liveAt) delete this._liveAt[k]; gone = true; } });
        if (gone) this._liveRedraw();
        if (Object.keys(this._liveOv).length) this._liveExpire();
      }, 2000);
    }
    /* what the canvas does its own way for the shared code card (vera_output.js hooks, this shadow root only): Preview in
       the live layer and Explode as the bound explode item - through this column's one dispatcher, as its own buttons
       always went - and an applied edit kept with the item (canvas.update), so it is there after any redraw */
    _voHooks() {
      if (this.__voh) return this.__voh;
      const viaAct = (act, b) => { if (!b) return false; const was = b.dataset.act; b.dataset.act = act; try { this._act(b); } finally { if (was == null) delete b.dataset.act; else b.dataset.act = was; } return true; };
      const keyOf = (c) => { const it = c && c.closest && c.closest('.it[data-key]'); return it ? it.dataset.key : ''; };
      this.__voh = {
        preview: (blk, id, b) => viaAct('cprev', b),
        explode: (blk, id, b) => viaAct('explode', b),
        dpop: (blk, id, b) => viaAct('dgopen', b),      // a diagram's Pop out: the canvas's own "open in the chat"
        // a diagram's SVG / PNG: the drawing is in the column's live layer, not in the card
        dimg: (blk, id, b, c, fmt) => { const L = this._live && this._live[keyOf(c)]; const vm = L && L.querySelector && L.querySelector('vera-mermaid');
          if (vm && typeof vm._download === 'function') { vm._download(fmt); return true; } return false; },
        edited: (blk, id, d, c) => { const key = keyOf(c); const bl = key && this._blockOf(key); if (!bl) return;
          const was = bl.content || {}; const next = { code: d.code };
          if (bl.type === 'diagram' || was.mermaid != null) { next.mermaid = d.code; if (was.source != null) next.source = d.code; }   // a diagram draws from `mermaid` first
          return this.call('canvas.update', Object.assign(this._ref(key), { content: Object.assign({}, was, next) })); } };
      // an edit in progress is never redrawn away: the column waits for it, then draws what it missed
      this.shadowRoot.addEventListener('vera:code:editend', () => { if (this._editHeld) { this._editHeld = false; this._html = null; if (this._doc) this.render(this._doc); } });
      return this.__voh;
    }
    _ref(key) { const bid = this._bidOf(key); return bid ? { block_id: bid } : { key }; }
    /* every action on the column — the old per-item controls (pin · park · size · remove) and the board's new ones
       (add · open · edit · answer · take · in context) — one dispatcher */
    _act(btn, ev) {
      const act = btn.dataset.act; let it = btn.closest('.it[data-key]');
      /* WHOSE BUTTON IT IS. A fused member is drawn inside its lead's card, so a control its own drawer put there
         (a code item's preview switch, say) would otherwise act on the LEAD's key. The nearest pane wins. */
      const pane = btn.closest('.fu-p[data-key]');
      const key = btn.dataset.layerKey || (pane ? pane.dataset.key : it ? it.dataset.key : '');   /* a layered panel's pill is outside its card */
      /* the desk's ⋯ menu sits on the stage, outside its card: it acts on its card, and goes once one of its entries is used */
      const pop = btn.closest('.hd-pop'); if (pop) pop.remove();
      if (!it && key) it = [...this.shadowRoot.querySelectorAll('.stage .it[data-key]')].find((x) => x.dataset.key === key) || null;
      if (act === 'lock') return this.toggleLock(key);
      if (act === 'pill') { this._pillSet(key, true); this._html = null; if (this._doc) this.render(this._doc); else this._placeNow(); return; }
      /* a composite card's tab: that member to the front, in the same place; taking one out, or back in (see _deskGroups) */
      if (act === 'cmptab' || act === 'cmpsep' || act === 'cmpjoin') {
        if (act === 'cmptab') { const m = this._cmpGet(); m[key] = Date.now(); this._cmpSet(m); (this._cmpPick = this._cmpPick || {})[key] = Date.now(); }
        else this._sepSet(key, act === 'cmpsep');
        this._html = null; if (this._doc) this.render(this._doc); else this._placeNow(); return; }
      if (act === 'hdmore') return this._hdMenu(key, btn);
      if (act === 'hdview') return this._viewMenu(key, btn);
      if (act === 'ownbtn') { const o = it && it._ownBtns && it._ownBtns[+btn.dataset.own]; if (o) { try { o.click(); } catch (e) { /* its own handler */ } } return; }   /* a card's own head's button, from its ⋯ menu */
      /* a view chosen (see VIEWS): a records item keeps it with its browsing state, any other kind in the view store */
      if (act === 'setview') { const v = btn.dataset.view; const b = this._blockOf(key); if (!v || !b) return;
        if (b.type === 'records') { const st = recState(this, key); st.view = v; st.page = 0; } else viewSet(this, key, v);
        if (this._doc) this.render(this._doc); return; }
      const focusMid = this.dataset.focusMid || '';
      /* a fused pane's own control: take THIS item out of the group it was drawn into and give it its card back.
         A reader's choice about how the canvas is drawn, so it is kept here and written nowhere. */
      if (act === 'split') {
        if (!pane) return;
        (this._split || (this._split = new Set())).add(String(pane.dataset.key));
        if (this._doc) this.render(this._doc);
        return;
      }
      // an explode item's "clear": drop the pointer at both ends — every line unlit here, every card unlit there
      if (act === 'xpsync') { const w = this._codeItemFor(key); if (w) w.querySelectorAll('.vc-line.lit,.vc-line.tap').forEach((x) => x.classList.remove('lit', 'tap'));
        const L = this._live || {}; Object.keys(L).forEach((k) => { if (k !== key || L[k].dataset.kind !== 'explode') return; const sg = L[k].firstChild && L[k].firstChild.querySelector && L[k].firstChild.querySelector('vera-structgraph'); if (sg && sg.lightSpan) sg.lightSpan(null); }); return; }
      /* the item's own switch into its diagram, and back. Forward: the bound explode item, keyed off this one, so
         the two light each other by span. Back: the diagram goes and the source stays — a view you can leave. */
      if (act === 'explode') {
        const b = this._blockOf(key); if (!b) return;
        const bound = ((this._doc && this._doc.blocks) || []).find((x) => x && x.type === 'explode' && x.content && String(x.content.binds || '') === key);
        if (bound) { if (this.deskAlign() && this._max === key) this._toggleMax(key); return this.call('canvas.remove', bound.key ? { key: String(bound.key) } : { block_id: String(bound.id) }); }
        const c = b.content || {}; const what = canExplode(b, this.explodeOffer());
        const content = what === 'code'
          ? { binds: key, title: String(c.filename || c.title || 'code') + ' — exploded' }
          : { binds: key, text: String(c.md || c.text || c.code || '').slice(0, 20000), title: String(c.title || c.filename || 'passage') + ' — exploded' };
        /* in the source's band and at the source's turn (owner, 2026-10-04: the explode "opens very far away from the
           other elements"): fusing is per band, so a diagram of a PINNED item - added to 'now', with no turn - was a card
           of its own placed where nothing anchors it, far from its code; fusing switched off did the same */
        /* ...and the item you exploded is one you OPENED (owner, 2026-10-04: "the explode opens but there isnt enough space
           to scroll down and see it"): the diagram made it taller, the column had no room, and its held layout folded
           it - the weakest first - to its header line, diagram and all. An item you opened is never folded by the column;
           once it is placed it is brought into view. */
        this._open.add(key); this._revealSoon(key);
        /* on the desk the card grows to show its exploded view: it fills the free part of the desk, over the rest, until the
           explode is closed (owner, 2026-10-09: "the element doesnt expand to display the explode viewer") */
        if (this.deskAlign() && this._max !== key) this._toggleMax(key);
        return this.call('canvas.add', Object.assign({ kind: 'explode', key: 'explode:' + key, content, at: b.state === 'pinned' ? 'pinned' : 'now', size: 'l' },
          b.anchor && typeof b.anchor === 'object' ? { anchor: b.anchor } : {}));
      }
      if (act === 'pin' || act === 'ctx') return this.call(it && it.classList.contains('pinned') ? 'canvas.add' : 'canvas.pin', { key });
      if (act === 'park') return this.call('canvas.park', { key });
      if (act === 'remove') {
        /* removed, with Undo for a few seconds (WSU-66); the removed shelf keeps it after that */
        const b0 = this._blockOf(key), c0 = (b0 && b0.content) || {}, name = c0.title || c0.name || String(key).split(':').slice(1).join(':') || 'item';
        return Promise.resolve(this.call('canvas.remove', this._ref(key))).then((r) => { if (b0 && b0.key && !(r && r.ok === false)) this._undoToast('Removed ' + name, () => this.call('canvas.restore', { key: String(b0.key) })); return r; });
      }
      if (act === 'size') { const i = ITEM_SIZES.indexOf(it.dataset.size); delete this._px[key]; delete this._pw[key]; return this._setSize(key, ITEM_SIZES[(i + 1) % ITEM_SIZES.length]); }
      if (act === 'up' || act === 'down') {   // a plain canvas keeps the document's order: canvas.move by block id
        const b = this._blockOf(key); if (!b) return; const bl = ((this._doc && this._doc.blocks) || []).filter(Boolean).slice().sort((x, y) => ((x.layout && x.layout.order) || 0) - ((y.layout && y.layout.order) || 0));
        const i = bl.indexOf(b), j = act === 'up' ? i - 1 : i + 1; if (i < 0 || j < 0 || j >= bl.length) return;
        return this.call('canvas.move', { block_id: String(b.id), order: j });
      }
      if (act === 'fold') return this._setFold(key, !(it && it.classList.contains('ufold')));
      if (act === 'open') return this._toggleOpen(key);
      if (act === 'max') return this._toggleMax(key);
      if (act === 'desk-auto') { this._deskPosSet(key, null); return this._placeNow(); }
      if (act === 'layer-behind' || act === 'layer-front') return this.setLayer(key, this._layer[key] === act.slice(6) ? '' : act.slice(6));
      if (act === 'rerun') { const b = this._blockOf(key); const c = (b && b.content) || {}; if (!c.cap) return; btn.textContent = 'running\u2026'; return this.call('canvas.run', { cap: c.cap, args: c.args || {}, key }); }
      // the banner's shelves (parked · hidden): the button opens its own popover. 'park' on an ITEM parks that item;
      // these are the shelves those items went to, so they carry their own act rather than sharing one.
      if (act === 'hid' || act === 'parkpop' || act === 'rmvpop') { const pop = btn.parentElement && btn.parentElement.querySelector('.hidpop'); if (pop) pop.hidden = !pop.hidden; return; }
      if (act === 'restore') { const rk = btn.dataset.rkey; if (rk) return this.call('canvas.restore', { key: rk }); return; }
      /* clear: the first press keeps what you pinned and says so; a second press within a few seconds takes those
         too. No dialog — the shelf is not where a confirmation belongs, and every item it removes can be put back
         from the "removed" shelf (canvas.restore; the document keeps the last 30). */
      if (act === 'clear') {
        const pinnedKeys = ((this._doc && this._doc.blocks) || []).filter((b) => b && b.key && b.state === 'pinned').map((b) => String(b.key));
        const again = this._clearAt && (Date.now() - this._clearAt) < 4000;
        this._clearAt = again ? 0 : Date.now();
        btn.textContent = again ? 'clearing…' : (pinnedKeys.length ? 'clear all?' : 'clearing…');
        return this.call('canvas.clear', { keep: again ? '' : pinnedKeys.join(',') });
      }
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
      if (act === 'cprev') { this._prevOn = this._prevOn || {};
        // flip what is on SCREEN: the first press on a page that opened drawn turns it off, not on
        this._prevOn[key] = !(this._prevSeen && this._prevSeen[key]);
        this._open.add(key); if (this._doc) this.render(this._doc); return; }
      // a named thing: open its sentences under its group (a second press closes), or look it up beside the item
      if (act === 'srcent') { const t = String(btn.dataset.entT || ''); if (!t) return; btn.disabled = true;
        this.callResult('turn.entity.look', { session_id: this._sid(), text: t, type: String(btn.dataset.entY || '') }).then(() => { btn.disabled = false; this.refresh && this.refresh(); });
        return; }
      if (act === 'entopen') { this._entOpen = this._entOpen || {}; const id = String(btn.dataset.ent || ''); this._entOpen[key] = this._entOpen[key] === id ? '' : id; this._open.add(key); if (this._doc) this.render(this._doc); return; }
      if (act === 'entlook') { const c = this._contentOf(key) || {}; const [gi, ei] = String(btn.dataset.ent || '').split(':').map(Number);
        const e = ((((c.groups || [])[gi]) || {}).entities || [])[ei]; if (!e) return; btn.textContent = 'looking…'; btn.disabled = true;
        this.callResult('turn.entity.look', { session_id: this._sid(), text: e.text, type: e.type, mid: c.mid || '' })
          .then((r) => { btn.textContent = r && r.ok ? ((r.items || []).length ? 'beside this →' : 'nothing found') : 'failed'; btn.disabled = false; this.refresh && this.refresh(); });
        return; }
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
        /* a live element that is no longer in the live layer (the layer was rebuilt) is dead - a graph destroys itself when
           it is taken out of the page - so it is made again rather than kept as a name with nothing drawn */
        if (el && !live.contains(el)) { delete L[key]; el = null; }
        if (!el) {
          // the wrapper is the column's (placed, sized, hidden); the element inside is the estate's own and keeps its styles
          let inner;
          if (kind === 'term') { inner = document.createElement('vera-terminal'); inner.setAttribute('ws', h.dataset.ws || ''); ensureLib('/ui/vera-terminal.js', 'vera-terminal'); }
          else if (kind === 'mermaid') { inner = document.createElement('vera-mermaid'); inner.setAttribute('bare', ''); inner.setAttribute('fill', ''); inner.setAttribute('title', h.dataset.title || 'diagram'); h.textContent = '';
            inner.addEventListener('vm:rendered', () => this._diagramGrew(key, inner)); this._mermaidInto(inner, key); }
          // `bare`: the widget draws its figure and nothing else. Its own frame inside a canvas item was a box in a
          // box — and it lives in a second shadow root, so no rule of ours could reach it (owner, 2026-09-24).
          else if (kind === 'widget') { inner = document.createElement('vera-widget'); inner.setAttribute('size', h.dataset.size || 'm'); inner.setAttribute('bare', ''); inner.setAttribute('item-drawer', ''); const rc = this._contentOf(key); if (rc) { inner.record = rc.record || rc; try { inner._recJson = JSON.stringify(rc.record || rc); } catch (e) {} } h.textContent = '';
            // a chip-faced widget is as wide as its face: the card takes that width and the rest of the row is free
            inner.addEventListener('widget:rendered', () => this._widgetFit(key, inner)); }
          else if (kind === 'preview') { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key);
          inner.setAttribute('sandbox', 'allow-scripts');   // no network, no cookies, no same-origin: it only draws
          // a code item previews its code; an html item previews the page it IS (its content field is `html`)
          h.textContent = ''; const cc = this._contentOf(key) || {}; const pd = this._pdoc(key, h.dataset.lang || cc.lang, cc.code || cc.html || ''); inner._doc = pd; inner.srcdoc = pd; }
          else if (kind === 'explode') { inner = document.createElement('vera-graph-embed'); h.textContent = ''; inner.setAttribute('resize', 'off');   // the item is sized by the canvas
            ensureLib('/ui/vera-graph-embed.js', 'vera-graph-embed');
            this._explodeAttrs(inner, key);
            // the diagram answers the reader: a card click scrolls the bound code item to that span and lights it
            inner.addEventListener('vera-graph-node', (ev) => this._explodeToSource(key, (ev.detail || {}).node)); }
          // a records item's GRAPH view (the fabric's answer): the same chrome-less Vera graph, fed the item's records
          else if (kind === 'rgraph') { inner = document.createElement('vera-graph-embed'); h.textContent = '';
            inner.setAttribute('renderer', 'data'); inner.setAttribute('expand', 'off'); inner.setAttribute('height', String(Math.max(60, (h.clientHeight || 300) - 16)));
            /* calm: settled before it is shown and still after, a resize only re-frames it (the desk's graphs flickered
               while nobody touched them: each one re-ran its layout for 20 s after every load and every resize) */
            inner.setAttribute('calm', ''); inner.setAttribute('min-h', '60');
            ensureLib('/ui/vera-graph-embed.js', 'vera-graph-embed');
            this._rgFeed(inner, key);
            inner.addEventListener('vera-graph-node', (ev) => this._rgNode(key, (ev.detail || {}).node)); }
          // a delivered report: Vera's own page, but written by a model - scripts may run (its diagrams), it may open
          // a tab, it may not be same-origin with the chat
          else if (kind === 'report') { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key);
            inner.setAttribute('sandbox', 'allow-scripts allow-popups allow-popups-to-escape-sandbox'); h.textContent = ''; inner.src = h.dataset.src || 'about:blank'; }
          else { inner = document.createElement('iframe'); inner.className = 'vc-pframe'; inner.setAttribute('title', key); inner.src = h.dataset.src || 'about:blank'; }
          el = document.createElement('div'); el.className = 'lv'; el.dataset.kind = kind; el.dataset.key = key; el.appendChild(inner); L[key] = el; live.appendChild(el);
          try { this.dispatchEvent(new CustomEvent('vera:canvas:live', { bubbles: true, detail: { key, kind, ws: h.dataset.ws || '', src: h.dataset.src || '' } })); } catch (e) {}
        } else if (kind === 'widget') { const inner = el.firstChild, rc = this._contentOf(key); const sz = h.dataset.size || 'm'; if (h.textContent) h.textContent = ''; if (inner && inner.getAttribute('size') !== sz) inner.setAttribute('size', sz); try { const j = JSON.stringify((rc && (rc.record || rc)) || null); if (inner && j && inner._recJson !== j) { inner._recJson = j; inner.record = rc.record || rc; } } catch (e) {}
          this._widgetFit(key, inner);   // a re-rendered slot is a fresh face: its own width again
        } else if (kind === 'mermaid') { if (h.textContent) h.textContent = ''; this._mermaidInto(el.firstChild, key); this._diagramGrew(key, el.firstChild);   // a re-rendered slot is new markup: the drawn diagram's height again
        } else if (kind === 'preview') { if (h.textContent) h.textContent = ''; const cc = this._contentOf(key) || {};
          /* an html item's page is its `html`, not `code` - reading only `code` redrew every html item as an empty page on
             its first update; and the redraw is double-buffered (_previewSwap) so there is never a blank frame */
          this._previewSwap(el, this._pdoc(key, h.dataset.lang || cc.lang, cc.code || cc.html || ''));
        } else if (kind === 'report') { if (h.textContent) h.textContent = ''; const fr = el.firstChild, want = h.dataset.src || '';
          if (fr && want && fr.getAttribute('src') !== want) fr.setAttribute('src', want);   // a re-render never reloads the same report
        } else if (kind === 'explode') { if (h.textContent) h.textContent = ''; this._explodeAttrs(el.firstChild, key);
        } else if (kind === 'rgraph') { if (h.textContent) h.textContent = ''; this._rgFeed(el.firstChild, key);
        } else if (kind === 'term' && h.dataset.ws) { const t = el.firstChild; if (t && t.getAttribute('ws') !== h.dataset.ws) { t.setAttribute('ws', h.dataset.ws); try { t.destroy && t.destroy(); t.connect(h.dataset.ws); } catch (e) {} } }
      });
      /* a PREVIEW whose slot has gone for a moment - its item folded to a header line as the turn in view moved, a tier
         change - is kept, hidden, for a while rather than destroyed: coming back it would be a NEW frame, and a new frame
         is a blank one until its page paints (the flicker the owner saw as the chat streamed on). Anything else goes. */
      Object.keys(L).forEach((k) => { const lv = L[k];
        if (body.querySelector('#items .vc-live[data-key="' + k.replace(/"/g, '\\"') + '"]')) { lv._goneAt = 0; return; }
        if (lv.dataset.kind === 'preview' && !(lv._goneAt && Date.now() - lv._goneAt > 15000)) { lv._goneAt = lv._goneAt || Date.now(); lv.style.display = 'none'; return; }
        try { lv.remove(); } catch (e) {} delete L[k]; });
      // the slots move after a render (placement, fonts, a frame's load): every slot is observed, and a slow tick
      // catches what no observer reports, only while live elements exist
      try { if (this._liveRO) body.querySelectorAll('#items .vc-live[data-live], #items .it').forEach((n) => this._liveRO.observe(n)); } catch (e) {}
      const any = Object.keys(L).length > 0;
      if (any && !this._liveTick) this._liveTick = setInterval(() => this._liveLayout(), 500);
      if (!any && this._liveTick) { clearInterval(this._liveTick); this._liveTick = null; }
      this._liveLayout();
    }
    /* ── the span binding between an explode item and the code item it is a diagram of ────────────────────
       The contract's cards each carry the span they came from (EXPLODE.md §3), so the two directions are the
       same fact read each way: a card's span → the lines to light here; a selection's lines → the card that
       covers them. Nothing is persisted; this is a reader's pointer, not a document change. */
    /* the graph of a records item: rebuilt only when its records change (a render is not a new answer), handed to the
       embed once it is defined */
    /* THE EDITOR IS AS TALL AS ITS TEXT (owner, 2026-10-04: "pressing edit on code in the canvas only lets you edit the
       first few lines"): the textarea opened at its 72 px minimum - four lines of a sixty-line file, the rest behind a
       scroll inside a scroll. It takes its text's height (to 70% of the window), grows as it is typed into, and in code
       Tab indents rather than leaving the field. */
    _editWire(ta) {
      const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 4, Math.round((root.innerHeight || 900) * 0.7)) + 'px'; };
      fit(); if (ta._vcEdit) return; ta._vcEdit = true;
      ta.addEventListener('input', fit);
      if (ta.classList.contains('code')) ta.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Tab' || ev.ctrlKey || ev.altKey || ev.metaKey) return; ev.preventDefault();
        const s = ta.selectionStart, e = ta.selectionEnd, v = ta.value;
        if (s === e && !ev.shiftKey) { ta.value = v.slice(0, s) + '    ' + v.slice(e); ta.selectionStart = ta.selectionEnd = s + 4; return; }
        // a selection (or shift): indent / outdent each line it touches
        const ls = v.lastIndexOf('\n', s - 1) + 1; const block = v.slice(ls, e);
        const out = ev.shiftKey ? block.replace(/^( {1,4}|\t)/gm, '') : block.replace(/^/gm, '    ');
        ta.value = v.slice(0, ls) + out + v.slice(e); ta.selectionStart = ls; ta.selectionEnd = ls + out.length; fit();
      });
    }
    _rgFeed(embed, key) {
      if (!embed) return;
      /* any other kind's graph view (see graphOf): built from the item's content, redrawn only when that changes */
      const ob = this._blockOf(key);
      if (ob && ob.type !== 'records') {
        let sig2 = ''; try { sig2 = ob.type + '|' + JSON.stringify(ob.content || {}).length + '|' + JSON.stringify(ob.content || {}).slice(0, 200); } catch (e) { sig2 = String(Date.now()); }
        if (embed._rgSig === sig2) return; embed._rgSig = sig2;
        const g2 = graphOf(ob); const go2 = () => { try { embed.setGraph(g2); } catch (e) {} this._adoptGraphCss(); };
        if (typeof embed.setGraph === 'function') go2(); else if (root.customElements) root.customElements.whenDefined('vera-graph-embed').then(go2).catch(() => {});
        return;
      }
      const c = this._contentOf(key) || {}; const bodies = recState(this, key).body || {};
      const ents = (this._recEnts && this._recEnts[String(key)]) || null; const g = recGraph(c, bodies, ents);
      const sig = (c.query || '') + '|' + (Array.isArray(c.items) ? c.items.map((r) => r && r.id).join(',') : '') + '|' + Object.keys(bodies).map((k) => k + ':' + ((bodies[k] && bodies[k].nb) || []).length).join(',')
        + '|' + (ents ? Object.keys(ents).map((k) => k + ':' + ents[k].length).join(',') : '');
      if (embed._rgSig === sig) return; embed._rgSig = sig;
      const go = () => { try { embed.setGraph(g); } catch (e) {} this._adoptGraphCss(); };
      if (typeof embed.setGraph === 'function') go();
      else if (root.customElements) root.customElements.whenDefined('vera-graph-embed').then(go).catch(() => {});
    }
    /* THE GRAPH'S STYLES, INSIDE THE COLUMN. vera_graph.js puts its stylesheet in the document's <head>, and the canvas
       draws in a shadow root the head does not reach - so the physics graph came up unstyled: its canvas squashed to a
       strip and its own drawers (terminal, table, the ask box) laid out as bare text (measured on the mirror,
       2026-09-29). The explode items never showed it: their renderer is the struct one. The sheet is copied in once,
       when the library has written it (it loads on demand, so a few tries). */
    _adoptGraphCss(tries) {
      const sr = this.shadowRoot; if (!sr || sr.querySelector('style[data-vg-adopted]')) return;
      const doc = this.ownerDocument || document;
      const ss = Array.from(doc.querySelectorAll('head style')).filter((s) => /\.vg-[a-z]/.test(s.textContent || ''));
      if (!ss.length) { const n = (tries | 0) + 1; if (n < 12) setTimeout(() => this._adoptGraphCss(n), 250 * n); return; }
      ss.forEach((s) => { const c = s.cloneNode(true); c.setAttribute('data-vg-adopted', ''); sr.appendChild(c); });
    }
    // a record's node, clicked: the list, on the page that holds it, with that record open
    _rgNode(key, node) {
      const id = node && String(node.id || '');
      // a name opens the list at the pages that name it
      if (id.indexOf('e:') === 0) { const st = recState(this, key); st.view = 'list'; st.ent = id.slice(2); st.page = 0; if (this._doc) this.render(this._doc); return; }
      if (!id || id.indexOf('r:') !== 0) return;
      const rid = id.slice(2); const b = this._blockOf(key); const c = (b && b.content) || {};
      const items = (Array.isArray(c.items) ? c.items : []).filter((r) => r && typeof r === 'object');
      const st = recState(this, key); st.view = 'list'; st.q = ''; st.dom = ''; st.ent = ''; st.sort = 'rank'; st.open = st.open || {}; st.open[rid] = true;
      const per = ({ s: 4, m: 6, l: 10, xl: 20 }[b && b.size] || 8); const i = items.findIndex((r) => String(r.id) === rid);
      st.page = i >= 0 ? Math.floor(i / per) : 0;
      if (this._doc) this.render(this._doc);
    }
    _explodeAttrs(embed, key) {
      const c = this._contentOf(key) || {};
      const set = (k, v) => { const s = v == null ? '' : String(v); if (s ? embed.getAttribute(k) !== s : embed.hasAttribute(k)) { if (s) embed.setAttribute(k, s); else embed.removeAttribute(k); } };
      set('renderer', 'struct'); set('expand', 'off');
      set('height', Math.max(140, parseInt(c.height, 10) || 300));
      const bound = c.binds ? (this._contentOf(String(c.binds)) || null) : null;
      // BOUND TO NOTHING is the difference between "wait" and "there is nothing coming": an explode item whose
      // code item is not on this canvas used to sit at "exploding…" for ever (owner: "doesnt draw anything")
      const slot = this.shadowRoot && this.shadowRoot.querySelector('.vc-live[data-live="explode"][data-key="' + String(key).replace(/"/g, '\\"') + '"]');
      if (c.binds && !bound) {
        if (slot) slot.innerHTML = '<span class="vc-dim">nothing to explode \u2014 this is bound to <b>' + esc(String(c.binds))
          + '</b>, which is not an item on this canvas</span>';
        ['code', 'path', 'text', 'record', 'lang', 'depth'].forEach((a) => embed.removeAttribute(a));
        return;
      }
      // BOUND: the bound item's own code is what is exploded — a snippet that exists only on this canvas (an
      // LLM's reply) explodes exactly as a repo file does, and the spans come back in ITS coordinates
      if (bound && (bound.code || bound.path)) { if (bound.code) { set('code', bound.code); set('lang', bound.lang || ''); set('path', bound.path || bound.filename || ''); } else { set('path', bound.path); } }
      else if (c.record) { set('record', c.record); set('ranges', c.ranges ? JSON.stringify(c.ranges) : ''); set('mode', c.mode || ''); }
      else if (c.path || c.paths) { set('path', c.path || [].concat(c.paths)[0]); set('depth', c.depth == null ? '' : c.depth); }
      else if (c.code) { set('code', c.code); set('lang', c.lang || ''); }
      else if (c.text) { set('text', c.text); set('mode', c.mode || ''); }
      if (c.layers) set('layers', [].concat(c.layers).join(','));
      // `set` treats '' as "remove", so `set('assess','')` was a no-op that could never turn the rail on
      if (c.assess) { if (!embed.hasAttribute('assess')) embed.setAttribute('assess', '1'); }
      else if (embed.hasAttribute('assess')) embed.removeAttribute('assess');
      embed._boundKey = c.binds ? String(c.binds) : '';
    }
    _codeItemFor(key) {
      const body = this.shadowRoot.getElementById('body'); if (!body) return null;
      const c = this._contentOf(key) || {};
      if (c.binds) return body.querySelector('.it[data-key="' + String(c.binds).replace(/"/g, '\\"') + '"] .vc-codewrap[data-code]');
      return null;
    }
    _explodeToSource(key, node) {
      const wrap = this._codeItemFor(key); const span = node && node.span; if (!wrap || !span) return;
      const lines = wrap.querySelectorAll('.vc-line');
      let l0 = span.line, l1 = span.line_end || span.line;
      if (!l0 && span.start != null) {                      // a prose card: characters → the lines holding them
        const text = ((this._contentOf(String((this._contentOf(key) || {}).binds)) || {}).code) || '';
        l0 = text.slice(0, span.start).split('\n').length; l1 = text.slice(0, Math.max(span.start, span.end)).split('\n').length;
      }
      if (!l0) return;
      let first = null;
      lines.forEach((ln) => { const n = +ln.dataset.line; const on = n >= l0 && n <= l1; ln.classList.toggle('lit', on); if (on && !first) first = ln; });
      if (first) { const it = wrap.closest('.it'); if (it) this._open.add(it.dataset.key);
        try { first.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { try { first.scrollIntoView(); } catch (_) {} } }
      try { this.dispatchEvent(new CustomEvent('vera:canvas:explode:select', { bubbles: true, detail: { key, node, line: l0, line_end: l1 } })); } catch (e) {}
    }
    /* the other way: what the reader selected (or clicked) in a code item lights the card that covers it */
    _sourceToExplode(wrap) {
      const body = this.shadowRoot.getElementById('body'); if (!body || !wrap) return;
      const it = wrap.closest('.it[data-key]'); if (!it) return;
      const codeKey = it.dataset.key;
      const sel = (this.shadowRoot.getSelection ? this.shadowRoot.getSelection() : (typeof window !== 'undefined' ? window.getSelection() : null));
      let l0 = 0, l1 = 0;
      const lineOf = (n) => { while (n && n !== wrap) { if (n.dataset && n.dataset.line) return +n.dataset.line; n = n.parentNode; } return 0; };
      if (sel && sel.rangeCount && !sel.isCollapsed) { l0 = lineOf(sel.anchorNode); l1 = lineOf(sel.focusNode); if (l0 > l1) { const t = l0; l0 = l1; l1 = t; } }
      if (!l0) { const hit = wrap.querySelector('.vc-line.tap'); if (hit) l0 = l1 = +hit.dataset.line; }
      if (!l0) return;
      const L = this._live || {};
      Object.keys(L).forEach((k) => { const em = L[k].firstChild; if (!em || L[k].dataset.kind !== 'explode' || em._boundKey !== codeKey) return;
        const sg = em.querySelector && em.querySelector('vera-structgraph');
        if (sg && sg.lightSpan) sg.lightSpan({ line: l0, line_end: l1 }); });
      try { this.dispatchEvent(new CustomEvent('vera:canvas:source:select', { bubbles: true, detail: { key: codeKey, line: l0, line_end: l1 } })); } catch (e) {}
    }
    /* ⛔ A PREVIEW IS NEVER BLANKED TO BE REDRAWN. Setting srcdoc on the frame on screen unloads it, and the frame is
       empty - white, then the page's own dark ground - until the new document paints: an html item flickered
       black/white on every streamed beat, and kept flickering while the chat streamed on after it (owner,
       2026-09-27). The next document loads in a second frame BEHIND the one on screen and replaces it once it has
       painted; while one is loading only the newest document is kept, and a swap is at most every half second. */
    /* a page the chat is still writing follows its newest line: each swap is a fresh page, which opens at its top - so
       as the page grew past the frame its new content was never in view (owner, 2026-10-03: "the canvas is better but
       still bad"). While the item is streaming its document carries the scroll to its end; finished, it opens as itself. */
    _pdoc(key, lang, src) { const d = previewDoc(lang, src); return (this._liveOv && this._liveOv[String(key)]) ? d + '<script>try{scrollTo(0,document.documentElement.scrollHeight)}catch(e){}<\/script>' : d; }
    _previewSwap(wrap, doc) {
      if (!wrap) return;
      const shown = () => [...wrap.querySelectorAll(':scope > iframe.vc-pframe')].filter((f) => !f._pending).pop() || null;
      const cur0 = shown();
      if (wrap._want === doc || (wrap._want === undefined && cur0 && cur0._doc === doc)) return;
      wrap._want = doc;
      if (wrap._busy) return;
      const go = () => {
        const d = wrap._want; const cur = shown();
        if (cur && cur._doc === d) { wrap._busy = false; return; }
        wrap._busy = true; const t0 = Date.now();
        const nf = document.createElement('iframe'); nf.className = 'vc-pframe vc-pnext'; nf.setAttribute('sandbox', 'allow-scripts');
        nf.setAttribute('title', (cur && cur.getAttribute('title')) || ''); nf._pending = true; nf._doc = d;
        nf.addEventListener('load', () => { requestAnimationFrame(() => requestAnimationFrame(() => {
          nf._pending = false; nf.classList.remove('vc-pnext');
          wrap.querySelectorAll(':scope > iframe.vc-pframe').forEach((f) => { if (f !== nf) f.remove(); });
          setTimeout(() => { wrap._busy = false; if (wrap._want !== d) go(); }, Math.max(0, 500 - (Date.now() - t0)));
        })); }, { once: true });
        nf.srcdoc = d; wrap.appendChild(nf);
      };
      go();
    }
    /* THE LIVE LAYER FOLLOWS A MOVING CARD EVERY FRAME (owner, 2026-10-09: "the graph element still flicker - especially if
       moved"). A graph, a widget or a frame is drawn over its card, not in it, and was placed once per mouse move, on a
       resize, or by the half-second tick: a card gliding to its place (left/top/size transitions, .35-.45 s) or growing
       while dragged left its graph behind - measured in the harness, 30 of 162 drag frames off by up to 11 px and a glide
       back 202 px behind. While anything moves, the layer is placed on every frame until it has settled. */
    _liveFollow(ms) {
      this._followTo = Math.max(this._followTo || 0, performance.now() + (ms || 500));
      if (this._followRaf) return;
      const step = () => { this._followRaf = 0; try { this._liveLayout(); } catch (e) { /* optional */ }
        if (performance.now() < this._followTo) this._followRaf = requestAnimationFrame(step); };
      this._followRaf = requestAnimationFrame(step);
    }
    _liveLayout() {
      const L = this._live; if (!L) return; const body = this.shadowRoot.getElementById('body'); if (!body) return;
      if (!this._followBound) { this._followBound = true;
        /* a card that starts to glide or grow (its placement, a face change) is followed for as long as it moves */
        body.addEventListener('transitionrun', (ev) => { const t = ev.target; if (!t || !t.classList || !t.classList.contains('it')) return;
          if (/^(left|top|width|height)$/.test(ev.propertyName)) this._liveFollow(520); }); }
      const B = body.getBoundingClientRect();
      /* SCREEN PIXELS ARE NOT LAYOUT PIXELS under a UI scale (zoom 1.4 on the panel): the rects below are measured on
         screen and the live layer is placed in layout pixels, so every offset was multiplied by the scale and a widget
         drew far from its card (2026-10-07: a slot at 569,1044 drew its widget at 773,1489). Divide by the scale. */
      const k0 = body.offsetWidth ? (B.width / body.offsetWidth) : 1, Z = k0 > 0.2 && k0 < 5 ? k0 : 1;
      Object.keys(L).forEach((k) => {
        const el = L[k]; const h = body.querySelector('#items .vc-live[data-key="' + k.replace(/"/g, '\\"') + '"]');
        /* a maximised item covers the column - but what is drawn INSIDE it stays: a code item's exploded diagram is a pane of
           the code's card (fused beside it) under its own key, and was hidden with everything else */
        if (this._max && k !== this._max && !(h && h.closest('.it') && h.closest('.it').dataset.key === this._max)) { el.style.display = 'none'; el._at = false; return; }
        if (!h || !h.getClientRects().length) { el.style.display = 'none'; el._at = false; return; }   // back, it appears in place
        const r = h.getBoundingClientRect(); const w = Math.max(0, Math.round(r.width / Z)), ht = Math.max(0, Math.round(r.height / Z));
        const card = h.closest('.it');
        let lx = Math.round((r.left - B.left) / Z + body.scrollLeft), ly = Math.round((r.top - B.top) / Z + body.scrollTop), tr = 'none';
        /* ON THE DESK IT GLIDES WITH ITS CARD, it does not chase it: placed each frame at where the card WAS, it trailed a
           card on the fast start of its glide by up to 150 px (measured). It is placed at the card's destination (the
           card's own left/top, plus where its slot sits in the card) with the card's own transition, so both move as one. */
        if (card && this.deskAlign() && card.style.left && !card.classList.contains('desk-dragging') && !card.classList.contains('resizing')) {
          const stg = card.parentElement; const S = stg.getBoundingClientRect(); const c0 = card.getBoundingClientRect();
          lx = Math.round((S.left - B.left) / Z + body.scrollLeft + stg.clientLeft + parseFloat(card.style.left) + (r.left - c0.left) / Z);
          ly = Math.round((S.top - B.top) / Z + body.scrollTop + stg.clientTop + (parseFloat(card.style.top) || 0) + (r.top - c0.top) / Z);
          if (el._at) { const cs = getComputedStyle(card); const sp = (s) => String(s || '').split(/,(?![^(]*\))/).map((x) => x.trim());
            const P = sp(cs.transitionProperty), Du = sp(cs.transitionDuration), Tf = sp(cs.transitionTimingFunction), De = sp(cs.transitionDelay);
            const of = (p) => { let i = P.indexOf(p); if (i < 0) i = P.indexOf('all'); if (i < 0) return ''; const d = Du[i % Du.length]; return parseFloat(d) > 0 ? p + ' ' + d + ' ' + Tf[i % Tf.length] + ' ' + De[i % De.length] : ''; };
            tr = [of('left'), of('top')].filter(Boolean).join(', ') || 'none'; }
        }
        /* A GRAPH ARRIVING ON THE DESK IS SHOWN ONCE IT IS STILL (owner, 2026-10-10: "the graphs still flicker when they
           first enter the desk"): its card grows in over a third of a second, and the graph was redrawn at each size on
           the way - measured, 4-5 sizes and 2-4 blank frames in its first 0.76 s. Its drawing stays unseen until its box
           has not changed for a moment (its last refit done), then fades in. */
        if (!el._at && el.dataset.kind === 'rgraph' && this.deskAlign() && el.firstChild) { el._fading = true; el.firstChild.style.transition = 'opacity .18s'; el.firstChild.style.opacity = '0'; }
        el._at = true;
        if (el._fading && el._fadeSz !== w + 'x' + ht) { el._fadeSz = w + 'x' + ht; clearTimeout(el._fadeT);   /* restarted only by a change of size */
          el._fadeT = setTimeout(() => { el._fading = false; el._fadeSz = ''; if (el.firstChild) el.firstChild.style.opacity = ''; }, 480); }
        if (el.style.transition !== tr) el.style.transition = tr;
        el.style.display = ''; el.style.left = lx + 'px'; el.style.top = ly + 'px';
        /* THE LIVE LAYER IS NOT INSIDE THE CARD, so the card's own clipping never reached it: a widget whose slot ran
           past a smaller card (a desk face, a capped item) drew over its neighbours (2026-10-07, a checks widget on the
           desk). Each live element is cut to its card's box and stacks with its card. */
        if (card) {
          const c = card.getBoundingClientRect();
          const ct = Math.max(0, Math.round((c.top - r.top) / Z)), cl = Math.max(0, Math.round((c.left - r.left) / Z));
          const cr = Math.max(0, Math.round((r.right - c.right) / Z)), cb = Math.max(0, Math.round((r.bottom - c.bottom) / Z));
          let clip = (ct || cl || cr || cb) ? 'inset(' + ct + 'px ' + cr + 'px ' + cb + 'px ' + cl + 'px)' : '';
          /* on the desk the card's corner grip stays reachable: the live element is drawn over the card, so its corner
             is cut away (a widget covered the grip and no widget card could be resized) */
          const grip = this.deskAlign() && card.querySelector(':scope > .rz');
          if (grip && grip.getClientRects().length) {
            const L0 = cl, T0 = ct, R0 = w - cr, B0 = ht - cb;
            const x1 = Math.min(R0, Math.round((c.right - r.left) / Z)) - 20, y1 = Math.min(B0, Math.round((c.bottom - r.top) / Z)) - 20;
            if (x1 > L0 && y1 > T0 && x1 < R0 + 20 && y1 < B0 + 20)
              clip = 'polygon(' + [[L0, T0], [R0, T0], [R0, y1], [x1, y1], [x1, B0], [L0, B0]].map((p) => p[0] + 'px ' + p[1] + 'px').join(',') + ')';
          }
          if (el.style.clipPath !== clip) el.style.clipPath = clip;
          const z = card.style.zIndex || '';
          if (el.style.zIndex !== z) el.style.zIndex = z;
        }
        if (el.style.width !== w + 'px' || el.style.height !== ht + 'px') { el.style.width = w + 'px'; el.style.height = ht + 'px'; if (el.dataset.kind === 'term') { const t = el.firstChild; try { t && t._doFit && t._doFit(); } catch (e) {} } if (el.dataset.kind === 'mermaid') { const m = el.firstChild; try { m && m.fit && m.fit(); } catch (e) {} }
          /* a records item's graph takes its slot's height (owner, 2026-10-04: "elements on the canvas still dont resize on
             the y axis ... im trying to expand the graph elements"): the embed's box was fixed at the height it was made
             with, so the slot grew under a graph that stayed put. Its caption line sits under the box. */
          /* ...once the box has SETTLED: a card growing as it is dragged or glides changes its slot every frame, and
             redrawing the graph at each size (36 times in one drag, measured) made its nodes jump; the box clips it meanwhile */
          if (el.dataset.kind === 'rgraph') { const g = el.firstChild; clearTimeout(el._fitT);
            el._fitT = setTimeout(() => { const hh = parseInt(el.style.height, 10) || 0;
              try { if (g && typeof g._setHeight === 'function' && hh && Math.abs((g._h || 0) - (hh - 16)) > 3) g._setHeight(Math.max(this.deskAlign() ? 60 : 160, hh - 16)); } catch (e) {}
              // ...and fits its nodes to the new box (they were laid out for the old one)
              setTimeout(() => { try { const gg = g && g._graph; if (gg && typeof gg.fit === 'function') gg.fit(); } catch (e) {} }, 120); }, 260); } }
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
    /* ── the widget's face has a width of its own: the CARD takes it ──────────────────────────────────────────
       A sticker-sized widget was given a share of a column (a third, a half) and drew a 90px chip in it, leaving
       the rest of that share blank — the "large blank areas" the owner reported. The element answers how wide its
       face really is (vera-widget.naturalWidth, 0 for the faces that are meant to fill), the slot carries it, and
       the placer gives the card exactly that much and lets whatever is next sit beside it.
       Nothing is written and no size record changes: this is the card fitting its content. ────────────────────── */
    _widgetFit(key, inner) {
      const body = this.shadowRoot.getElementById('body');
      const h = body && body.querySelector('#items .vc-live[data-key="' + String(key).replace(/"/g, '\\"') + '"]');
      if (!h || !inner || typeof inner.naturalWidth !== 'function') return;
      let nw = 0; try { nw = inner.naturalWidth() || 0; } catch (e) { nw = 0; }
      const was = h.dataset.natw || '';
      const now = nw > 0 ? String(Math.round(nw)) : '';
      if (was === now) return;
      if (now) h.dataset.natw = now; else delete h.dataset.natw;
      if (this.hasAttribute('stage')) requestAnimationFrame(() => { if (this.hasAttribute('stage')) this._placeNow(); });
    }
    /* the diagram's source into the estate's mermaid element (loaded once from the page); a changed source redraws it;
       the host's own window.mermaid draws when the element cannot be had, the source shows when nothing can */
    _mermaidInto(inner, key) {
      const c = this._contentOf(key) || {}; const code = String(c.mermaid || c.code || c.source || '').trim();
      const live = !!(this._liveOv && this._liveOv[String(key)]);
      /* the same source again is nothing to do - unless it was last drawn as a stream and the stream is over (drawn whole now) */
      if (!inner || (inner._mmCode === code && (live || inner._mmWhole))) return; inner._mmCode = code; inner._mmWhole = !live;
      ensureLib('/ui/elements/vera_mermaid.js', 'vera-mermaid').then((ok) => {
        if (!inner.isConnected) return;
        if (typeof inner.render !== 'function' && typeof customElements !== 'undefined' && customElements.get('vera-mermaid')) { try { customElements.upgrade(inner); } catch (e) {} }
        /* ⛔ STREAM IT, the way the chat does. A diagram arrives on the canvas a line at a time now (the harvest
           runs on the stream), and `render` can only draw a COMPLETE diagram — a half-written one throws, so the
           item sat blank until the last token and the whole thing appeared at once: "it still goes to the canvas
           after its been fully rendered in chat" (owner, 2026-09-24), even though the writes were arriving all
           along. `stream` draws the complete lines and holds the partial one, which is exactly what the chat's own
           live fence does. A finished diagram is just the case where no line is partial. */
        /* ...but a diagram nothing is writing any more is drawn whole: `stream` never says a source is broken (a line it
           cannot draw is held as "partial"), so an invalid one stayed a blank card forever (2026-10-11: a reply's
           "bar / x-axis / series" pseudo-chart, two empty cards). `render` draws it, or shows the parse error with the source. */
        const streaming = !!(this._liveOv && this._liveOv[String(key)]);
        if (!streaming && typeof inner.render === 'function') { try { inner.render(code); } catch (e) {} return; }
        if (typeof inner.stream === 'function') { try { inner.stream(code); } catch (e) { try { inner.render(code); } catch (_) {} } return; }
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
    /* A SOURCE, OPENED. The page's text and its picture are fetched HERE and not when the item landed: a deep run
       reads dozens of pages, and forty screenshots taken to be thumbnails nobody opens is forty browser sessions
       for nothing. Fetched once and written back into the item through canvas.update, so it is the canvas that
       remembers - the second look costs nothing, and it survives a reload the way the rest of the item does. */
    /* A RECORDS item, browsed: page · sort · view · a domain facet · a record opened, read, landed, read on. The
       browser's state is the element's (recState), so paging does not write the canvas; what a record's reading
       fetched is kept beside it for the session's look. Landing a url writes a source item through canvas.add
       (keyed source:<url>, so it is never there twice). */
    /* A PAGE OPENED TAKES THE ROOM (owner, 2026-10-11: "if i open something inside a canvas element that should take up the
       entire desk ... the reader mode portion should expand to take up the desk and then shrink to where it was before").
       A record opened in a list - a search result, a research source - fills the free desk (the canvas column off the
       desk), showing that page alone (.reading); closing the last open one puts the card back where it was. A card the
       person maximised themselves is left as they had it. */
    /* ...only READER MODE fills the room (owner, 2026-10-11: "it takes up the full screen just to show the blurb text ... id
       rather it opened a bit on the first click"): a plain open shows the whole blurb in place (grow false - it may only
       give the room back); reader mode, or the title, is what fills it. */
    _readerFollow(key, grow) {
      const st = recState(this, key); const any = Object.keys(st.open || {}).some((k) => st.open[k]);
      if (any && grow !== false) { const b = this._blockOf(key); if (b && b.type === 'research') { st.tab = 'pages'; st.side = 'pages'; } }   /* a run's page is read in its Pages pane */
      if (any && grow !== false && this._max !== key) { this._autoMax = key; this._toggleMax(key); return true; }
      if (!any && this._autoMax === key) { this._autoMax = null; if (this._max === key) this._toggleMax(key); return true; }
      return false;
    }
    async _recAct(key, act, arg) {
      const b = this._blockOf(key); const c = (b && b.content) || null; if (!c) return;
      const st = recState(this, key); const items = Array.isArray(c.items) ? c.items : [];
      const it = items.find((r) => r && String(r.id) === String(arg));
      const redraw = () => { if (this._doc) this.render(this._doc); };
      st.body = st.body || {}; st.open = st.open || {};
      if (act === 'page') { st.page = arg === 'prev' ? Math.max(0, (st.page | 0) - 1) : arg === 'next' ? (st.page | 0) + 1 : (+arg || 0); return redraw(); }
      if (act === 'sort' || act === 'view') { st[act] = arg; st.page = 0; return redraw(); }
      if (act === 'dom') { st.dom = st.dom === arg ? '' : arg; st.page = 0; return redraw(); }
      /* the names · sites menu: a filter in force is cleared by its button; otherwise the button opens or closes the menu */
      if (act === 'names') { if (st.ent || st.dom) { st.ent = ''; st.dom = ''; st.page = 0; } else st.namesOpen = !st.namesOpen; return redraw(); }
      if (act === 'pickent' || act === 'pickdom') { st[act === 'pickent' ? 'ent' : 'dom'] = arg; st.page = 0; st.namesOpen = false; return redraw(); }
      /* a research run's pane: shown alone on a narrow card; beside the report on a wide one (the report never leaves) */
      if (act === 'rrtab') { st.tab = arg; if (arg !== 'report') st.side = arg; return redraw(); }
      if (act === 'rrlay') { try { localStorage.setItem('vera:canvas:rrlay', arg === 'below' ? 'below' : 'right'); } catch (e) { /* this page only */ } return redraw(); }
      if (act === 'ent') { st.ent = st.ent === arg ? '' : arg; st.page = 0; return redraw(); }
      if (act === 'goto') return this._rgNode(key, { id: 'r:' + arg });   // a timeline dot: its page, opened, wherever it sits
      if (act === 'open') { if (it) { st.open[it.id] = !st.open[it.id]; this._open && this._open.add && this._open.add(key);
          if (!st.open[it.id] && this._readerFollow(key, false)) return;
          // a fabric record opened is READ at once: its facts, its source, its text - the depth is the point of opening it
          const cur = st.body[it.id] || {};
          if (st.open[it.id] && it.ref && it.ref.record_id && !cur.text && !cur.busy && !cur.err) { redraw(); return this._recAct(key, 'rec', arg); } }
        return redraw(); }
      if (act === 'read' && it && it.url) {
        st.open[it.id] = true; st.body[it.id] = { busy: 'reading the page (reader mode)' }; if (!this._readerFollow(key)) redraw();
        const rd = await this._readPage(String(it.url));
        st.body[it.id] = rd.reader ? { reader: rd.reader } : rd.text ? { text: rd.text } : { err: rd.err || 'the page gave nothing back' };
        /* a page read here names things too: its names join the list's name filters and its graph, as a cached reading's do */
        if (rd.reader && Array.isArray(rd.reader.entities)) { this._recEnts = this._recEnts || {}; const em = this._recEnts[String(key)] = this._recEnts[String(key)] || {};
          em[String(it.id)] = rd.reader.entities.filter((e) => e && e.text && PAGE_ENT.test(String(e.type || e.label || ''))).slice(0, 12).map((e) => [String(e.text), String(e.type || e.label)]); }
        return redraw();
      }
      /* a page's picture, held with the list's browsing state (not written to the canvas), and its explode as its own item -
         what a source item's "see the page" and "explode" do (see ONE PAGE VIEW) */
      if (act === 'shot' && it && it.url) {
        const cur = st.body[it.id] || {}; st.open[it.id] = true;
        if (cur.shot) { st.body[it.id] = Object.assign({}, cur, { shot: '' }); return redraw(); }
        st.body[it.id] = Object.assign({}, cur, { busy: 'taking the picture', err: '' }); redraw();
        let r = null; try { r = await this.callResult('browser.screenshot', { url: String(it.url), full_page: false }); } catch (e) { r = null; }
        const src = shotSrc(r);
        st.body[it.id] = Object.assign({}, st.body[it.id], { busy: '', shot: src, err: src ? '' : ((r && r.error) || 'no picture came back') }); return redraw();
      }
      if (act === 'xpl' && it) {
        const cur = st.body[it.id] || {}; const md = String((cur.reader && cur.reader.markdown) || cur.text || it.text || '');
        if (!md.trim()) { st.body[it.id] = Object.assign({}, cur, { err: 'read the page first' }); return redraw(); }
        let h = 0; for (const ch of String(it.url || it.id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
        const xk = 'explode:page:' + h.toString(36); this._revealSoon(xk);
        return this.call('canvas.add', Object.assign({ kind: 'explode', key: xk, at: b && b.state === 'pinned' ? 'pinned' : 'now', size: 'l',
          content: { text: md.slice(0, 20000), title: String(it.title || it.url || 'page') + ' — exploded', binds: '' } },
          b && b.anchor && typeof b.anchor === 'object' ? { anchor: b.anchor } : {}));
      }
      if ((act === 'rec' || act === 'more') && it && it.ref && it.ref.record_id) {
        const cur = st.body[it.id] || {}; const offset = act === 'more' ? (cur.next | 0) : 0;
        st.open[it.id] = true; st.body[it.id] = Object.assign({}, act === 'more' ? cur : {}, { busy: 'reading the record' }); redraw();
        // a fabric record through the fabric's own read (memory.read does not know fabric records - 'record not found');
        // the memory store's read for the rest
        let r = null; try { r = await this.callResult('fabric.record.get', { record_id: String(it.ref.record_id), offset, max_chars: 8000 }); } catch (e) { r = null; }
        let viaFabric = !!(r && r.ok);
        if (!viaFabric) { try { r = await this.callResult('memory.read', { record_id: String(it.ref.record_id), offset, max_chars: 6000 }); } catch (e) { r = null; } }
        const text = String((r && r.text) || '');
        if (!text) { st.body[it.id] = Object.assign({}, cur, { busy: '', err: (r && r.error) || 'the record gave nothing back' }); return redraw(); }
        const keep = { nb: cur.nb, nbOpen: cur.nbOpen, nbText: cur.nbText };
        st.body[it.id] = Object.assign(keep, { text: (act === 'more' ? (cur.text || '') + '\n\n' : '') + text,
          next: (r && r.next_offset != null && r.next_offset < (r.total_chars || 0)) ? r.next_offset : null,
          rec: viaFabric ? { source: r.source || {}, tags: r.tags || [], created_at: r.created_at || '', data: r.data || {}, total_chars: r.total_chars || 0 } : cur.rec });
        return redraw();
      }
      // the records NEAREST this one, across every dataset - listed under it, and drawn into the graph view
      if (act === 'nb' && it && it.ref && it.ref.record_id) {
        const cur = st.body[it.id] || {}; st.open[it.id] = true;
        st.body[it.id] = Object.assign({}, cur, { busy: 'finding its neighbours', err: '' }); redraw();
        let r = null; try { r = await this.callResult('fabric.loom.record_match', { record_id: String(it.ref.record_id), max_matches: 8 }); } catch (e) { r = null; }
        const ms = (r && Array.isArray(r.matches)) ? r.matches.filter((m) => m && m.id) : null;
        st.body[it.id] = Object.assign({}, st.body[it.id], { busy: '', nb: ms || [], err: ms ? '' : ((r && r.error) || 'no neighbours came back') });
        return redraw();
      }
      // a neighbour, opened in place: its text read through the fabric
      if (act === 'nbopen') {
        const parts = String(arg || '').split('|'); const owner = items.find((x) => x && String(x.id) === parts[0]); const mid = parts[1];
        if (!owner || !mid) return; const cur = st.body[owner.id] || {}; const nbOpen = Object.assign({}, cur.nbOpen); nbOpen[mid] = !nbOpen[mid];
        st.body[owner.id] = Object.assign({}, cur, { nbOpen }); redraw();
        if (!nbOpen[mid] || (cur.nbText || {})[mid]) return;
        let r = null; try { r = await this.callResult('fabric.record.get', { record_id: mid, max_chars: 4000 }); } catch (e) { r = null; }
        const nbText = Object.assign({}, (st.body[owner.id] || {}).nbText); nbText[mid] = String((r && r.text) || (r && r.error) || 'nothing came back');
        st.body[owner.id] = Object.assign({}, st.body[owner.id], { nbText }); return redraw();
      }
      if (act === 'land' && it && it.url) {
        return this.call('canvas.add', { kind: 'source', key: 'source:' + it.url, size: 's',
          content: { url: String(it.url), title: String(it.title || ''), domain: String(it.domain || ''), snippet: String(it.snippet || '').slice(0, 1200), query: String(c.query || '') } });
      }
      if (act === 'loadmore' && c.next && c.next.cap) {
        this._readout(key, '… more from ' + c.next.cap);
        const r = await this.callResult(c.next.cap, c.next.args || {});
        const rows = (r && (r.results || r.headlines || r.sources || r.items || r.records || r.rows)) || [];
        const have = new Set(items.map((x) => String(x.url || x.id)));
        const add = (Array.isArray(rows) ? rows : []).filter((x) => x && typeof x === 'object').map((x, i) => ({ id: String(x.id || x.url || ('more' + items.length + i)), title: String(x.title || x.name || x.url || ''), url: x.url ? String(x.url) : undefined, snippet: String(x.snippet || x.summary || x.text || '').slice(0, 1200), when: x.published || x.date || x.ts || undefined })).filter((x) => !have.has(String(x.url || x.id)));
        if (!add.length) return this._readout(key, 'nothing more');
        return this.call('canvas.update', { key, content: Object.assign({}, c, { items: items.concat(add), total: items.length + add.length, next: (r && r.next) || null }) });
      }
    }
    /* a page, READ: reader mode first (browser.reader - the article as markdown, or a composite of a fragmented page),
       the page's bare text when the reader found nothing to call a body (or is not on this instance). Kept small
       enough to write into the item: a reader answer is its fields and at most 40k of markdown. */
    async _readPage(url) {
      /* reader.page first (canvas-enrichment slice 2 - owner, 2026-10-03: "the reader mode should not be processed as the
         user clicks it - takes a while to load"): a page the research, a fetch or a click read in the last week opens
         from the reader cache at once; one never read is read the cheap way (the fetched page, in memory) before the
         slow one (a real render). browser.reader stays the fallback on an instance without reader.page. */
      let r = null; try { r = await this.callResult('reader.page', { url, max_chars: 40000 }); } catch (e) { r = null; }
      if (!(r && r.ok) && !(r && /no article/i.test(String(r.error || '')))) { try { r = await this.callResult('browser.reader', { url, max_chars: 40000 }); } catch (e) { r = null; } }
      const mdTxt = String((r && r.markdown) || '');
      if (mdTxt.trim().length > 200) {
        const keep = ['title', 'byline', 'site', 'published', 'image', 'minutes', 'words', 'composite', 'parts', 'url', 'entities', 'via', 'cached'];
        const reader = { markdown: mdTxt.slice(0, 40000) }; keep.forEach((k) => { if (r[k] != null && r[k] !== '') reader[k] = r[k]; });
        return { reader };
      }
      let c = null; try { c = await this.callResult('browser.content', { url, max_chars: 40000 }); } catch (e) { c = null; }
      const text = String((c && (c.text || c.content)) || '');
      return text ? { text } : { err: (c && c.error) || (r && r.error) || '' };
    }
    /* every source drawn asks the reader cache (never the web) whether its page has been read; a page the research or a
       fetch already read arrives with its reading, so "read" opens at once and the item shows who, how long and what */
    _srcPrefetch(blocks) {
      this._srcProbed = this._srcProbed || new Set();
      const todo = (blocks || []).filter((b) => b && b.type === 'source' && b.key != null && b.content && b.content.url && !b.content.reader
        && !b.content.text && !b.content.failed && !this._srcProbed.has(String(b.key))).slice(0, 12);
      todo.forEach((b) => this._srcProbed.add(String(b.key)));
      const keep = ['title', 'byline', 'site', 'published', 'image', 'minutes', 'words', 'composite', 'parts', 'url', 'entities', 'via'];
      todo.forEach(async (b) => {
        let r = null; try { r = await this.callResult('reader.page', { url: String(b.content.url), max_chars: 40000, mode: 'cache' }); } catch (e) { return; }
        if (!(r && r.ok && String(r.markdown || '').trim().length > 200)) return;
        const reader = { markdown: String(r.markdown).slice(0, 40000) }; keep.forEach((k) => { if (r[k] != null && r[k] !== '') reader[k] = r[k]; });
        this.call('canvas.update', { key: String(b.key), content: Object.assign({}, b.content, { reader }) });
      });
      /* a records item's pages: their entities (its "names" facets) and their reading (a row opens at once), held in
         the element only - the canvas is not written, so twenty rows cost no twenty updates */
      this._recEnts = this._recEnts || {};
      const SAL = /^(PERSON|ORG|GPE|LOC|FAC|PRODUCT|EVENT|NORP|WORK_OF_ART|LAW|TICKER)$/;
      let left = 40;
      (blocks || []).forEach((b) => {
        if (!b || b.type !== 'records' || b.key == null || !b.content || !Array.isArray(b.content.items)) return;
        const k = String(b.key); const em = this._recEnts[k] = this._recEnts[k] || {};
        b.content.items.filter((r) => r && r.url && r.id != null && !this._srcProbed.has(k + '|' + r.id)).slice(0, 20).forEach((r) => {
          if (left-- <= 0) return; this._srcProbed.add(k + '|' + r.id);
          this.callResult('reader.page', { url: String(r.url), max_chars: 40000, mode: 'cache' }).then((x) => {
            if (!(x && x.ok)) return;
            em[String(r.id)] = (Array.isArray(x.entities) ? x.entities : []).filter((e) => e && e.text && SAL.test(String(e.type || e.label || '')))
              .slice(0, 12).map((e) => [String(e.text), String(e.type || e.label)]);
            const st = recState(this, k); st.body = st.body || {};
            const had = st.body[r.id] || {};
            if (String(x.markdown || '').trim().length > 200 && !had.reader && !had.text) {
              const reader = { markdown: String(x.markdown).slice(0, 40000) }; keep.forEach((kk) => { if (x[kk] != null && x[kk] !== '') reader[kk] = x[kk]; });
              st.body[r.id] = Object.assign({}, had, { reader });
            }
            clearTimeout(this._recEntT); this._recEntT = setTimeout(() => { if (this._doc) this.render(this._doc); }, 400);
          }).catch(() => {});
        });
      });
    }
    async _srcAct(key, act) {
      const b = this._blockOf(key); const c = (b && b.content) || null; if (!c || !c.url) return;
      this._srcOpen = this._srcOpen || {};
      if (act === 'read') {
        // already fetched: this is only the fold
        // already fetched, so this press is only the fold - a local repaint, the way the code preview folds
        if (c.text || c.reader) { this._srcOpen[key] = !this._srcOpen[key]; this._open.add(key); if (this._doc) this.render(this._doc); return; }
        this._readout(key, '\u2026 reading the page (reader mode)');
        const rd = await this._readPage(String(c.url));
        if (!rd.reader && !rd.text) return this._readout(key, rd.err || 'nothing came back');
        this._srcOpen[key] = true;
        return this.call('canvas.update', { key, content: Object.assign({}, c, rd.reader ? { reader: rd.reader } : { text: rd.text.slice(0, 40000) }) });
      }
      if (act === 'explode') {
        const md = String((c.reader && c.reader.markdown) || c.text || ''); if (!md.trim()) return this._readout(key, 'read the page first');
        const sb = this._blockOf(key) || {};   // the page's band and turn, as for a code item's diagram
        this._revealSoon('explode:' + key);    // its own card (a page's explode binds nothing): brought into view once placed
        return this.call('canvas.add', Object.assign({ kind: 'explode', key: 'explode:' + key, at: sb.state === 'pinned' ? 'pinned' : 'now', size: 'l',
          content: { text: md.slice(0, 20000), title: String(c.title || 'page') + ' \u2014 exploded', binds: '' } },
          sb.anchor && typeof sb.anchor === 'object' ? { anchor: sb.anchor } : {}));
      }
      if (act === 'shot') {
        if (c.shot) return this.call('canvas.update', { key, content: Object.assign({}, c, { shot: '' }) });
        this._readout(key, '\u2026 taking the picture');
        const r = await this.callResult('browser.screenshot', { url: String(c.url), full_page: false });
        /* browser.screenshot answers {ok, image_b64, url, title, load_ms} - image_b64, which is the one name
           this was not reading. The picture was fetched every time and thrown away every time: the press did
           nothing, silently, which is the worst way for it to fail. Measured against the live cap. */
        /* the cap answers base64 bytes, and not always PNG despite the argument's name - measured, example.com came back
           starting "/9j/", which is JPEG; shotSrc reads the magic (shared with a records list's pages) */
        const src = shotSrc(r);
        if (!src) return this._readout(key, (r && r.error) || 'no picture came back');
        return this.call('canvas.update', { key, content: Object.assign({}, c, { shot: src }) });
      }
    }
    /* THE MONTH AND THE DAY GO BACK TO THE DIARY. Not to a copy held in the item: a calendar whose contents were
       fixed when it landed would be wrong by the next event written, and it is a calendar - being current is the
       whole of what it is for. The month's events are re-read from cal.events.list and written into the item, so
       the answer survives a reload and the next look costs nothing. */
    async _calAct(key, act) {
      const b = this._blockOf(key); const c = (b && b.content) || null; if (!c) return;
      const pad = (n) => (n < 10 ? '0' : '') + n;
      const now = new Date();
      const cur = /^\d{4}-\d{2}$/.test(String(c.month || '')) ? String(c.month)
        : now.getFullYear() + '-' + pad(now.getMonth() + 1);
      if (/^-?\d+$/.test(String(act))) {
        const step = +act;
        let month;
        if (step === 0) month = now.getFullYear() + '-' + pad(now.getMonth() + 1);
        else { const [Y, M] = cur.split('-').map(Number); const d = new Date(Y, M - 1 + step, 1);
          month = d.getFullYear() + '-' + pad(d.getMonth() + 1); }
        this._readout(key, '\u2026 ' + month);
        /* cal.events.list takes `end` EXCLUSIVE, at midnight: asking to the last day of the month drops that
           whole day. Measured - four events seeded into September, a 01..30 range answered with three, and the
           one on the 30th was the one missing. So the range runs to the FIRST OF THE NEXT MONTH, and what comes
           back is filtered to the month asked for, since that request also picks up the 1st of the next one. */
        const from = month + '-01';
        const [yy, mm] = month.split('-').map(Number);
        const nxt = new Date(yy, mm, 1);
        const to = nxt.getFullYear() + '-' + pad(nxt.getMonth() + 1) + '-01';
        const r = await this.callResult('cal.events.list', { start: from, end: to });
        const events = ((r && Array.isArray(r.events)) ? r.events : [])
          .filter((e) => String(e && e.start || '').slice(0, 7) === month);
        return this.call('canvas.update', { key, content: Object.assign({}, c, { month, selected: '', events }) });
      }
      // a day: local only, the month's events are already here
      const day = String(act || '');
      const next = Object.assign({}, c, { selected: c.selected === day ? '' : day });
      return this.call('canvas.update', { key, content: next });
    }
    /* a widget, read again. Its record says which capability it draws and with what arguments - the same pair the
       chat used when it placed it - so this is that cap called once more and the answer put back in the item. */
    async _widAct(key, act) {
      if (act !== 'refresh') return;
      const b = this._blockOf(key); const c = (b && b.content) || null; if (!c) return;
      const rec = (c.draw || c.form) ? c : (c.record || null); if (!rec) return;
      const cap = (rec.reads && rec.reads.cap) ? String(rec.reads.cap) : ''; if (!cap) return;
      const args = (rec.reads && rec.reads.args && typeof rec.reads.args === 'object') ? rec.reads.args : {};
      this._readout(key, '\u2026 ' + cap);
      const r = await this.callResult(cap, args);
      if (!r || r.ok === false) return this._readout(key, (r && r.error) || 'nothing came back');
      const next = Object.assign({}, c);
      if (next.draw || next.form) next.data = r;
      else { next.record = Object.assign({}, next.record); next.record.data = r; }
      return this.call('canvas.update', { key, content: next });
    }
    _sid() { return String((this._doc && this._doc.session) || this.getAttribute('session-id') || ''); }
    _contentOf(key) { const b = this._blockOf(key); if (!b) return null; if (b.type === 'result') { const v = resultView(b.content || {}); return Object.assign({}, (v && v.content) || {}); } return Object.assign({}, b.content || {}); }   // a result's live slot is the element it is drawn as
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
    /* THE CANVAS'S CONTROLS FROM OUTSIDE IT (WSU-72, owner 2026-10-11: "id like the session canvas controls along the top to
       be moved to the chat lhm to give the session canvas full height and a more minimalist look"). With chrome="none" the
       add bar is not drawn; the host draws the same controls elsewhere (the chat's LHM) and drives them through these:
       the kinds to add (an anchor element places the kind's picker), the shelves, bringing an item back, restoring a
       removed one, and clearing. Each does exactly what the add bar's own button did. */
    kinds() { return ADD_KINDS.map((k) => ({ n: k.n, ik: k.ik, kind: k.kind })); }
    addKind(n, anchor) {
      const k = ADD_KINDS.find((x) => x.n === n); if (!k) return false; const btn = anchor || this;
      if (btn.dataset && !btn.dataset.kind) btn.dataset.kind = n;
      if (k.hosts) return this._hostPick(btn);
      if (k.pick) return this._panelPick(btn);
      if (k.menu) return this._noteMenu(btn, k);
      if (k.sheet) return this._widgetSurface() ? this._widgetPick(btn, k) : this._sheetless(btn, k);
      return this._listed(btn, k);
    }
    shelves() {
      const doc = this._doc || {}; const blocks = (doc.blocks || []).filter((b) => b && b.key);
      const name = (b) => { const c = b.content || {}; return c.title || c.name || c.filename || String(b.key).split(':').slice(1).join(':') || b.type; };
      const of = (st) => blocks.filter((b) => (b.state || 'now') === st).map((b) => ({ key: String(b.key), title: name(b), type: b.type }));
      const on = new Set(blocks.map((b) => String(b.key)));
      const removed = (doc.removed || []).filter((r) => r && r.key && !on.has(String(r.key))).slice().reverse().slice(0, 12)
        .map((r) => ({ key: String(r.key), title: name(Object.assign({ key: r.key }, r.block || {})), type: (r.block && r.block.type) || '', at: r.at || '' }));
      return { parked: of('parked'), hidden: of('hidden'), removed, items: blocks.filter((b) => ['now', 'pinned'].indexOf(b.state || 'now') >= 0).length, revision: doc.revision || 0 };
    }
    bringBack(key) { return this.call('canvas.add', { key: String(key) }); }
    restore(key) { return this.call('canvas.restore', { key: String(key) }); }
    clearAll(keepPinned) {
      const pinnedKeys = ((this._doc && this._doc.blocks) || []).filter((b) => b && b.key && b.state === 'pinned').map((b) => String(b.key));
      return this.call('canvas.clear', { keep: keepPinned === false ? '' : pinnedKeys.join(',') });
    }
    /* a short notice with one action (Undo), at the foot of the canvas; it leaves after 8 s or when acted on (WSU-66) */
    _undoToast(text, undo) {
      const sr = this.shadowRoot; if (!sr) return; const old = sr.querySelector('.vc-toast'); if (old) old.remove();
      const t = document.createElement('div'); t.className = 'vc-toast'; t.setAttribute('role', 'status');
      t.innerHTML = '<span></span><button type="button">Undo</button>'; t.firstChild.textContent = String(text || '').slice(0, 80);
      const done = () => { clearTimeout(t._t); t.remove(); };
      t.querySelector('button').addEventListener('click', (e) => { e.stopPropagation(); done(); try { undo && undo(); } catch (er) { /* the shelf still has it */ } });
      t._t = setTimeout(done, 8000); sr.appendChild(t);
    }
    /* a panel item drawn without its page asks once; when the address comes back the column is drawn again (WSU-67) */
    _panelResolve(id) {
      const P = this._pResolving || (this._pResolving = new Set()); if (P.has(id)) return; P.add(id);
      panelSrc(id).then(() => { P.delete(id); this._html = null; if (this._doc) this.render(this._doc); }).catch(() => { P.delete(id); });
    }
    _panelAdd(id, label) {
      id = String(id || '').trim(); if (!id) return; const key = 'panel:' + id; const focusMid = this.dataset.focusMid || '';
      const args = { kind: 'panel', key, content: Object.assign({ panel: id, title: label || id }, this.deskAlign() && this.getAttribute('panel-layer') === 'desk' ? { layer: 'behind' } : {}), at: 'now', size: 'l' }; args.anchor = { origin: 'you', beside: focusMid };   // picked by hand: beside the turn in view, related to no turn
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
    _switch(id) { id = String(id || ''); if (!id) return; this._open = new Set(); this._px = {}; this._pw = {}; this._editKey = null; this.setAttribute('canvas-id', id); this._railMark(); try { this.dispatchEvent(new CustomEvent('vera:canvas:switch', { bubbles: true, detail: { id } })); } catch (e) {} }
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

  const api = { place, autoCols, unitsOf, fuseOf, checkRoutes, decisionOf, suggestionsOf, canExplode, nowText, sizeOfHeight, turnOrder, isAged, foldOf, ADD_KINDS, NOTE_MENU, ADD_WHAT, fromClipboard, blockTitle, railRows, foldOf, ITEM_SIZES, KIND_GLYPH, BLOCK, splitHl, codeLintHtml, resultView, genericView, jsonTree, langRunCmd, unwrap, hostRowsOf, panelRowsOf, pickerHtml, mdx, readerHtml, recGraph, version: 6 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VeraCanvas = Object.assign(root.VeraCanvas || {}, api);
  if (typeof customElements !== 'undefined' && !customElements.get('vera-canvas')) {
    customElements.define('vera-canvas', VeraCanvas);
  }
})();

/* The Loop Lab's tables, upgraded in place (served at /ui/elements/looplab_tables.js).

   The Work, Ship, Agents and Mission tables are the full record - row expansions with review cards, sandbox
   controls, a goal's stats - so they stay the page's own markup; this gives every one of them, as it is drawn and
   every time it is redrawn, what a table in the widget system has:
     sort       click a header (again to reverse); numbers with units and commas, dates and text each sort as
                themselves; a row's expansion (a tr.*-detail under it) moves with it; the choice survives the table
                being redrawn by the next poll (per table, this tab)
     sticky     the header row stays in view while the table scrolls under it
     bars       a column that is all numbers draws each as a quiet bar in its cell, so a row that stands out is
                seen before it is read
     bar        over a table of six rows or more: how many rows it shows, and the visible rows as CSV
   Nothing here changes what a table holds or what its rows do. */
(function () {
  'use strict';
  if (window.__veraLoopLabTables) return; window.__veraLoopLabTables = true;
  const KEY = 'vera:looplab:sort:';
  const isDetail = (tr) => /(^|\s)[a-z-]*-detail(\s|$)/.test(tr.className || '');
  const headOf = (tbl) => { const r = tbl.tHead ? tbl.tHead.rows[0] : tbl.querySelector('tr'); return r && r.querySelector('th') ? r : null; };
  const idOf = (tbl, i) => tbl.id || (tbl.closest('[id]') ? tbl.closest('[id]').id : 'table') + ':' + i;
  const num = (s) => { const m = String(s).replace(/,/g, '').match(/^[^\d+-]*([-+]?\d+(?:\.\d+)?)\s*(ms|s|m|h|d|%|k|kb|mb|gb)?\b/i); if (!m) return null;
    let v = parseFloat(m[1]); const u = (m[2] || '').toLowerCase(); if (u === 'ms') v /= 1000; else if (u === 'm') v *= 60; else if (u === 'h') v *= 3600; else if (u === 'd') v *= 86400; else if (u === 'k' || u === 'kb') v *= 1e3; else if (u === 'mb') v *= 1e6; else if (u === 'gb') v *= 1e9; return v; };
  const val = (td) => { if (!td) return { k: 3, v: '' }; const raw = (td.getAttribute('data-sort') != null ? td.getAttribute('data-sort') : td.textContent).trim();
    if (!raw || raw === '—' || raw === '-') return { k: 2, v: '' };
    if (/^\d{4}-\d{2}-\d{2}/.test(raw) || /^\d{1,2}\/\d{1,2}[ /]/.test(raw)) { const t = Date.parse(raw); if (isFinite(t)) return { k: 0, v: t }; }
    const n = num(raw); if (n != null && /^[^a-z]*[\d.,]+\s*(ms|s|m|h|d|%|k|kb|mb|gb)?\b/i.test(raw)) return { k: 0, v: n };
    return { k: 1, v: raw.toLowerCase() }; };
  function groups(tbl, head) {
    const body = head.parentElement, rows = Array.from(body.rows).filter((r) => r !== head), out = []; let g = null;
    rows.forEach((r) => { if (isDetail(r) && g) g.push(r); else { g = [r]; out.push(g); } });
    return { body, out };
  }
  function sortBy(tbl, i, dir) {
    const head = headOf(tbl); if (!head) return; const { body, out } = groups(tbl, head);
    out.forEach((g, ix) => { g._ix = ix; g._v = val(g[0].cells[i]); });
    out.sort((a, b) => (a._v.k - b._v.k) || (a._v.v < b._v.v ? -dir : a._v.v > b._v.v ? dir : a._ix - b._ix));
    const frag = document.createDocumentFragment(); out.forEach((g) => g.forEach((r) => frag.appendChild(r))); body.appendChild(frag);
    Array.from(head.cells).forEach((th, j) => { th.classList.toggle('vtb-on', j === i); th.setAttribute('data-vtb-dir', j === i ? (dir > 0 ? 'asc' : 'desc') : ''); });
  }
  function bars(tbl) {
    const head = headOf(tbl); if (!head) return; const { out } = groups(tbl, head); if (out.length < 4) return;
    Array.from(head.cells).forEach((th, i) => {
      const tds = out.map((g) => g[0].cells[i]).filter(Boolean); if (tds.length < 4) return;
      const vs = tds.map((td) => (td.children.length > 1 ? null : (val(td).k === 0 && !/^\d{4}-/.test(td.textContent.trim()) ? val(td).v : null)));
      const nums = vs.filter((v) => v != null), filled = tds.filter((td) => val(td).k !== 2).length; if (nums.length < 4 || nums.length < filled * 0.8) return;   // a blank (—) is not a word
      const hi = Math.max(...nums.map(Math.abs)); if (!hi) return;
      // a column that is mostly one value (12 goals, 12 goals, ...) draws a stripe, not a picture: bars only where the
      // values actually spread - four distinct values at least, and none of them holding half the rows
      const cnt = {}; nums.forEach((v) => { cnt[v] = (cnt[v] || 0) + 1; });
      if (Object.keys(cnt).length < 4 || Math.max(...Object.values(cnt)) >= nums.length / 2) return;
      tds.forEach((td, k) => { const v = vs[k]; if (v == null) return; td.classList.add('vtb-bar'); td.style.setProperty('--vtb', Math.round(Math.abs(v) / hi * 100) + '%'); });
    });
  }
  function csv(tbl) {
    const head = headOf(tbl), { out } = groups(tbl, head); const q = (s) => '"' + String(s).replace(/\s+/g, ' ').trim().replace(/"/g, '""') + '"';
    const lines = [Array.from(head.cells).map((c) => q(c.textContent)).join(',')].concat(out.filter((g) => g[0].style.display !== 'none').map((g) => Array.from(g[0].cells).map((c) => q(c.textContent)).join(',')));
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' })); a.download = (tbl.id || 'loop-lab-table') + '.csv'; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  function enhance(tbl, n) {
    if (tbl.__vtb || tbl.closest('vera-widget,vera-dashboard,.co-sheet,.vtb-skip')) return; const head = headOf(tbl); if (!head) return;
    tbl.__vtb = true; tbl.classList.add('vtb');
    const sid = KEY + idOf(tbl, n);
    Array.from(head.cells).forEach((th, i) => {
      if (th.querySelector('button,input,select,a') || th.colSpan > 1) return;
      th.classList.add('vtb-s'); th.title = (th.title ? th.title + ' · ' : '') + 'click to sort';
      th.addEventListener('click', () => { const cur = th.getAttribute('data-vtb-dir'); const dir = cur === 'desc' ? 1 : -1; sortBy(tbl, i, dir); try { sessionStorage.setItem(sid, JSON.stringify([i, dir])); } catch (_) {} });
    });
    try { const s = JSON.parse(sessionStorage.getItem(sid) || 'null'); if (s && head.cells[s[0]]) sortBy(tbl, s[0], s[1]); } catch (_) {}
    bars(tbl);
    const { out } = groups(tbl, head);
    if (out.length >= 6 && !(tbl.previousElementSibling && tbl.previousElementSibling.classList.contains('vtb-bar-top'))) {
      const b = document.createElement('div'); b.className = 'vtb-bar-top';
      b.innerHTML = '<span>' + out.length + ' rows</span><button type="button" title="the rows shown, as CSV">⤓ CSV</button>';
      b.querySelector('button').onclick = () => csv(tbl); tbl.parentNode.insertBefore(b, tbl);
    }
  }
  const CSS = 'table.vtb tr:first-child th{position:sticky;top:0;z-index:2;background:var(--bg1,#1f1d1a);box-shadow:0 1px 0 var(--border,#3a3530)}'
    + 'th.vtb-s{cursor:pointer;user-select:none;white-space:nowrap}th.vtb-s:hover{color:var(--acc,#5a9e8f)}'
    + 'th.vtb-s::after{content:"↕";opacity:.25;margin-left:4px;font-size:.85em}th.vtb-on[data-vtb-dir="asc"]::after{content:"↑";opacity:.9;color:var(--acc,#5a9e8f)}th.vtb-on[data-vtb-dir="desc"]::after{content:"↓";opacity:.9;color:var(--acc,#5a9e8f)}'
    + 'td.vtb-bar{background-image:linear-gradient(90deg,color-mix(in srgb,var(--acc,#5a9e8f) 16%,transparent) var(--vtb),transparent var(--vtb));background-repeat:no-repeat}'
    + '.vtb-bar-top{display:flex;justify-content:flex-end;align-items:center;gap:8px;font:10px var(--mono,monospace);color:var(--dim2,#8a7e70);margin:2px 0 3px}'
    + '.vtb-bar-top button{font:inherit;padding:1px 8px;border-radius:5px;border:1px solid var(--border,#3a3530);background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}.vtb-bar-top button:hover{color:var(--text,#ddd);border-color:var(--acc,#5a9e8f)}';
  function boot() {
    if (!document.getElementById('vtb-css')) { const st = document.createElement('style'); st.id = 'vtb-css'; st.textContent = CSS; document.head.appendChild(st); }
    let t = 0;
    const scan = () => { t = 0; Array.from(document.querySelectorAll('.sec table')).forEach((tbl, i) => { try { enhance(tbl, i); } catch (_) {} }); };
    new MutationObserver((ms) => { if (t) return; if (!ms.some((m) => Array.from(m.addedNodes).some((n) => n.nodeType === 1 && (n.tagName === 'TABLE' || (n.querySelector && n.querySelector('table')))))) return; t = setTimeout(scan, 120); })
      .observe(document.body, { childList: true, subtree: true });
    scan();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})();

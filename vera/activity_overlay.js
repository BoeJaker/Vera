/* ── Vera Activity Overlay ─────────────────────────────────────────────────────
 * A self-mounting, floating activity feed that lives across the TOP of the app,
 * over whatever else is open. A compact pill shows the latest activity (a live
 * ticker); clicking it drops down the full, FILTERABLE timeline — narrator takes,
 * dreams, loops, v8 programs — driven by the same /activity/timeline feed the
 * Activity panel uses. Loads once (guarded), positions itself, and polls.
 * ────────────────────────────────────────────────────────────────────────────*/
(function () {
  if (window.__veraActivityOverlay) return;
  window.__veraActivityOverlay = true;

  const ICON = { narrator: '💬', dream_cycle: '☾', dream: '☾', loop_live: '🔄',
                 program: '⚙', project: '📁', goal: '🎯', chat: '🗨', cap: '▷',
                 artifact: '📦' };
  const LABEL = { narrator: 'Narrator', dream_cycle: 'Dream', dream: 'Dream',
                  loop_live: 'Loop', program: 'Program', project: 'Project',
                  goal: 'Goal', chat: 'Chat', cap: 'Cap', artifact: 'Artifact' };

  let events = [], filter = null, open = false, lastSig = '';

  const root = document.createElement('div');
  root.id = 'vera-activity-overlay';
  root.innerHTML = `
<style>
  #vera-activity-overlay{position:fixed;top:6px;left:50%;transform:translateX(-50%);
    z-index:2147483000;font:12px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    color:var(--text,#dce1e8);pointer-events:none}
  #vera-activity-overlay *{pointer-events:auto;box-sizing:border-box}
  .vao-pill{display:inline-flex;align-items:center;gap:8px;max-width:min(560px,80vw);
    padding:5px 12px;border-radius:16px;cursor:pointer;
    background:var(--bg1,#15181d);border:1px solid var(--border,#2a2f37);
    box-shadow:0 4px 18px rgba(0,0,0,.35)}
  .vao-pill:hover{border-color:var(--acc,#5a9e8f)}
  .vao-dot{width:7px;height:7px;border-radius:50%;background:var(--acc,#5a9e8f);flex:0 0 auto;animation:vaoP 2.2s infinite}
  @keyframes vaoP{0%,100%{opacity:1}50%{opacity:.35}}
  .vao-latest{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2,#9aa4b2)}
  .vao-count{flex:0 0 auto;font-size:10px;color:var(--dim,#6b7480);background:var(--bg2,#1c2026);
    border-radius:9px;padding:1px 7px}
  .vao-caret{flex:0 0 auto;color:var(--dim,#6b7480);font-size:10px}
  .vao-panel{margin-top:6px;width:min(560px,88vw);max-height:min(60vh,520px);display:flex;flex-direction:column;
    background:var(--bg1,#15181d);border:1px solid var(--border,#2a2f37);border-radius:12px;
    box-shadow:0 10px 34px rgba(0,0,0,.45);overflow:hidden}
  .vao-chips{display:flex;flex-wrap:wrap;gap:5px;padding:9px 11px;border-bottom:1px solid var(--border,#2a2f37);flex:0 0 auto}
  .vao-chip{font-size:11px;padding:2px 9px;border-radius:11px;cursor:pointer;
    background:var(--bg2,#1c2026);border:1px solid var(--border2,#39414c);color:var(--text2,#9aa4b2)}
  .vao-chip:hover{color:var(--text,#dce1e8)}
  .vao-chip.on{background:var(--acc,#5a9e8f);color:#06120f;border-color:var(--acc,#5a9e8f)}
  .vao-list{overflow-y:auto;padding:4px 0}
  .vao-ev{display:flex;gap:9px;padding:7px 12px;align-items:flex-start;border-bottom:1px solid rgba(255,255,255,.03)}
  .vao-ev:hover{background:var(--bg2,#1c2026)}
  .vao-ev-k{flex:0 0 auto;font-size:13px;line-height:1.3}
  .vao-ev-b{flex:1 1 auto;min-width:0}
  .vao-ev-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#dce1e8)}
  .vao-ev-s{color:var(--dim,#6b7480);font-size:11px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vao-ev-ts{flex:0 0 auto;color:var(--dim,#6b7480);font-size:10px;font-variant-numeric:tabular-nums}
  .vao-empty{color:var(--dim,#6b7480);text-align:center;padding:22px}
</style>
<div class="vao-pill" id="vao-pill">
  <span class="vao-dot"></span>
  <span class="vao-latest" id="vao-latest">Activity</span>
  <span class="vao-count" id="vao-count"></span>
  <span class="vao-caret" id="vao-caret">▾</span>
</div>
<div class="vao-panel" id="vao-panel" style="display:none">
  <div class="vao-chips" id="vao-chips"></div>
  <div class="vao-list" id="vao-list"></div>
</div>`;

  function mount() {
    if (!document.body) return void setTimeout(mount, 200);
    document.body.appendChild(root);
    root.querySelector('#vao-pill').addEventListener('click', () => {
      open = !open;
      root.querySelector('#vao-panel').style.display = open ? 'flex' : 'none';
      root.querySelector('#vao-caret').textContent = open ? '▴' : '▾';
      if (open) render();
    });
    poll();
    setInterval(poll, 4000);
  }

  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtTs = ts => { try { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } };
  const kinds = () => [...new Set(events.map(e => e.kind))];
  const filtered = () => filter ? events.filter(e => e.kind === filter) : events;

  async function poll() {
    try {
      const r = await fetch(location.origin + '/activity/timeline?scope=all&limit=60',
        { headers: { 'Accept': 'application/json' } });
      const j = await r.json();
      const c = (j && j.content !== undefined) ? j.content : j;
      events = (c && c.events) || [];
    } catch (e) { /* keep last */ }
    updatePill();
    if (open) render();
  }

  function updatePill() {
    const e = events[0];
    root.querySelector('#vao-latest').textContent =
      e ? ((ICON[e.kind] || '•') + ' ' + (e.title || '').slice(0, 52)) : 'Activity';
    root.querySelector('#vao-count').textContent = events.length ? String(events.length) : '';
  }

  function render() {
    const chips = root.querySelector('#vao-chips');
    chips.innerHTML = `<span class="vao-chip${!filter ? ' on' : ''}" data-k="">all</span>` +
      kinds().map(k => `<span class="vao-chip${filter === k ? ' on' : ''}" data-k="${esc(k)}">${ICON[k] || ''} ${esc(LABEL[k] || k)}</span>`).join('');
    chips.querySelectorAll('.vao-chip').forEach(ch => ch.addEventListener('click', () => {
      filter = ch.dataset.k || null; render();
    }));
    const list = root.querySelector('#vao-list');
    const evs = filtered().slice(0, 40);
    list.innerHTML = evs.length ? evs.map(e => `
      <div class="vao-ev">
        <span class="vao-ev-k">${ICON[e.kind] || '•'}</span>
        <div class="vao-ev-b">
          <div class="vao-ev-t">${esc(e.title || '')}</div>
          ${(e.summary && e.summary !== e.title) ? `<div class="vao-ev-s">${esc(String(e.summary).slice(0, 160))}</div>` : ''}
        </div>
        <span class="vao-ev-ts">${fmtTs(e.ts)}</span>
      </div>`).join('') : '<div class="vao-empty">No recent activity.</div>';
  }

  mount();
})();

/* ── Vera Activity Overlay ─────────────────────────────────────────────────────
 * A filterable activity feed for the top bar — narrator takes, dreams, loops, v8
 * programs — driven by the same /activity/timeline feed the Activity panel uses.
 *
 * Two deliberate layout rules:
 *  1. The PILL IS IN FLOW. It mounts INSIDE the app <header> as a real child, so
 *     it takes part in the header's layout and can never cover anything. Only
 *     the drop-down panel floats, and only while it is open.
 *  2. The feed is a HORIZONTAL TIMELINE — a scrolling lane with a time axis,
 *     oldest → newest left to right, opening scrolled to "now" on the right.
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
  // Per-kind accent so a lane of mixed activity is readable at a glance. Falls
  // back to the theme accent for any kind added server-side later.
  const HUE = { narrator: '#7aa2f7', dream_cycle: '#bb9af7', dream: '#bb9af7',
                loop_live: '#7dcfff', program: '#9ece6a', project: '#e0af68',
                goal: '#f7768e', chat: '#7aa2f7', cap: '#6b7480',
                artifact: '#e0af68' };

  let events = [], filter = null, open = false, expanded = null;
  let narrOn = null, narrBusy = false;       // null = status not known yet

  const style = document.createElement('style');
  style.textContent = `
  #vao-pill{display:inline-flex;align-items:center;gap:7px;max-width:min(420px,32vw);
    padding:3px 10px;border-radius:14px;cursor:pointer;flex:0 1 auto;min-width:0;
    font:12px/1.35 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);
    color:var(--text,#dce1e8)}
  #vao-pill:hover{border-color:var(--acc,#5a9e8f)}
  #vao-pill.on{border-color:var(--acc,#5a9e8f)}
  #vao-pill .vao-dot{width:6px;height:6px;border-radius:50%;background:var(--acc,#5a9e8f);
    flex:0 0 auto;animation:vaoP 2.2s infinite}
  @keyframes vaoP{0%,100%{opacity:1}50%{opacity:.3}}
  @media (prefers-reduced-motion:reduce){#vao-pill .vao-dot{animation:none}}
  #vao-pill .vao-latest{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    color:var(--text2,#9aa4b2);min-width:0}
  #vao-pill .vao-count{flex:0 0 auto;font-size:10px;color:var(--dim,#6b7480);
    background:var(--bg1,#15181d);border-radius:8px;padding:0 6px;
    font-variant-numeric:tabular-nums}
  #vao-pill .vao-caret{flex:0 0 auto;color:var(--dim,#6b7480);font-size:9px}

  /* Only this part floats — and only while open. */
  #vao-panel{position:fixed;z-index:2147483000;display:none;flex-direction:column;
    font:12px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    color:var(--text,#dce1e8);
    background:var(--bg1,#15181d);border:1px solid var(--border,#2a2f37);
    border-radius:12px;box-shadow:0 12px 38px rgba(0,0,0,.45);overflow:hidden}
  #vao-panel.open{display:flex}
  .vao-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    padding:8px 11px;border-bottom:1px solid var(--border,#2a2f37);flex:0 0 auto}
  .vao-chip{font-size:11px;padding:2px 9px;border-radius:11px;cursor:pointer;
    background:var(--bg2,#1c2026);border:1px solid var(--border2,#39414c);
    color:var(--text2,#9aa4b2);white-space:nowrap}
  .vao-chip:hover{color:var(--text,#dce1e8)}
  .vao-chip.on{background:var(--acc,#5a9e8f);color:#06120f;border-color:var(--acc,#5a9e8f)}
  .vao-chip:focus-visible,#vao-pill:focus-visible{outline:2px solid var(--acc,#5a9e8f);outline-offset:2px}
  .vao-spacer{flex:1 1 auto}
  .vao-hint{font-size:10px;color:var(--dim,#6b7480)}
  /* Narrator on/off — the overlay already shows the narrator's output, so its
     switch belongs here rather than buried in the dream panel. */
  .vao-narr{display:inline-flex;align-items:center;gap:6px;font-size:11px;
    padding:2px 9px;border-radius:11px;cursor:pointer;white-space:nowrap;
    background:var(--bg2,#1c2026);border:1px solid var(--border2,#39414c);
    color:var(--text2,#9aa4b2)}
  .vao-narr:hover{color:var(--text,#dce1e8);border-color:var(--acc,#5a9e8f)}
  .vao-narr[disabled]{opacity:.55;cursor:progress}
  .vao-narr .led{width:7px;height:7px;border-radius:50%;flex:0 0 auto;
    background:var(--dim,#6b7480)}
  .vao-narr.on{border-color:var(--acc,#5a9e8f);color:var(--text,#dce1e8)}
  .vao-narr.on .led{background:var(--acc,#5a9e8f);animation:vaoP 2.2s infinite}

  /* ── the horizontal timeline ── */
  .vao-track{overflow-x:auto;overflow-y:hidden;flex:1 1 auto;
    scrollbar-width:thin;scrollbar-color:var(--border2,#39414c) transparent}
  .vao-track::-webkit-scrollbar{height:8px}
  .vao-track::-webkit-scrollbar-thumb{background:var(--border2,#39414c);border-radius:4px}
  .vao-lane{position:relative;display:flex;gap:10px;min-width:100%;
    width:max-content;padding:8px 12px 12px;box-sizing:border-box}
  /* the axis the ticks sit on: 8 (pad) + 16 (time) + 7.5 (half tick row) */
  .vao-lane::before{content:"";position:absolute;left:0;right:0;top:31px;height:1px;
    background:var(--border,#2a2f37)}
  .vao-node{position:relative;flex:0 0 208px;display:flex;flex-direction:column;min-width:0}
  .vao-time{height:16px;font-size:10px;color:var(--dim,#6b7480);
    font-variant-numeric:tabular-nums;letter-spacing:.03em}
  .vao-tickrow{height:15px;display:flex;align-items:center}
  .vao-tick{width:9px;height:9px;border-radius:50%;background:var(--acc,#5a9e8f);
    box-shadow:0 0 0 2px var(--bg1,#15181d);position:relative;z-index:1}
  .vao-card{margin-top:8px;background:var(--bg2,#1c2026);border:1px solid var(--border,#2a2f37);
    border-left-width:2px;border-radius:8px;padding:6px 8px;cursor:pointer;min-width:0}
  .vao-card:hover{border-color:var(--border2,#39414c)}
  .vao-kind{display:flex;align-items:center;gap:5px;font-size:10px;
    text-transform:uppercase;letter-spacing:.06em;color:var(--dim,#6b7480);margin-bottom:3px}
  .vao-title{color:var(--text,#dce1e8);overflow:hidden;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
  .vao-sum{color:var(--text2,#9aa4b2);font-size:11px;margin-top:3px;overflow:hidden;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-word}
  .vao-node.exp{flex-basis:340px}
  .vao-node.exp .vao-title,.vao-node.exp .vao-sum{-webkit-line-clamp:unset;display:block}
  .vao-now{position:relative;flex:0 0 auto;display:flex;flex-direction:column;
    justify-content:flex-start;padding-top:16px}
  .vao-now-tick{height:15px;display:flex;align-items:center;color:var(--acc,#5a9e8f);font-size:10px}
  .vao-empty{padding:26px 14px;color:var(--dim,#6b7480);text-align:center;width:100%}`;

  const pill = document.createElement('div');
  pill.id = 'vao-pill';
  pill.tabIndex = 0;
  pill.setAttribute('role', 'button');
  pill.setAttribute('aria-expanded', 'false');
  pill.innerHTML = `<span class="vao-dot"></span>
    <span class="vao-latest">Activity</span>
    <span class="vao-count"></span><span class="vao-caret">▾</span>`;

  const panel = document.createElement('div');
  panel.id = 'vao-panel';
  panel.innerHTML = `<div class="vao-head"></div><div class="vao-track"></div>`;

  const $ = (sel, r) => (r || panel).querySelector(sel);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtTs = ts => { try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch (e) { return ''; } };
  const kinds = () => [...new Set(events.map(e => e.kind))];
  const filtered = () => (filter ? events.filter(e => e.kind === filter) : events);
  const hue = k => HUE[k] || 'var(--acc,#5a9e8f)';

  function mount() {
    if (!document.body) return void setTimeout(mount, 200);
    document.head.appendChild(style);
    document.body.appendChild(panel);

    // IN FLOW inside the header — never overlapping anything. Placed before the
    // right-hand controls so it uses the header's own flexible middle space.
    const header = document.querySelector('header');
    if (header) {
      const right = header.querySelector('.hdr-right');
      header.insertBefore(pill, right || null);
    } else {
      // No app header (a standalone page): still in flow, at the top of body.
      const bar = document.createElement('div');
      bar.style.cssText = 'display:flex;justify-content:center;padding:6px 8px';
      bar.appendChild(pill);
      document.body.insertBefore(bar, document.body.firstChild);
    }

    pill.addEventListener('click', toggle);
    pill.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && open) toggle(); });
    document.addEventListener('click', e => {
      if (open && !panel.contains(e.target) && !pill.contains(e.target)) toggle();
    });
    window.addEventListener('resize', () => { if (open) place(); });
    // A horizontal lane is unusable with a vertical wheel — translate it.
    $('.vao-track').addEventListener('wheel', e => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        e.currentTarget.scrollLeft += e.deltaY;
      }
    }, { passive: false });

    poll();
    setInterval(poll, 4000);
  }

  function place() {
    const r = pill.getBoundingClientRect();
    const gap = 6;
    panel.style.top = Math.round(r.bottom + gap) + 'px';
    panel.style.left = '12px';
    panel.style.right = '12px';
    panel.style.maxHeight = Math.max(180, window.innerHeight - r.bottom - gap - 16) + 'px';
  }

  function toggle() {
    open = !open;
    panel.classList.toggle('open', open);
    pill.classList.toggle('on', open);
    pill.setAttribute('aria-expanded', String(open));
    $('.vao-caret', pill).textContent = open ? '▴' : '▾';
    if (open) { place(); render(); scrollToNow(); }
  }

  function scrollToNow() {
    const t = $('.vao-track');
    if (t) t.scrollLeft = t.scrollWidth;      // newest is on the right
  }

  async function narratorStatus() {
    try {
      const r = await fetch(location.origin + '/system/narrator/status',
        { headers: { Accept: 'application/json' } });
      const j = await r.json();
      const c = (j && j.content !== undefined) ? j.content : j;
      narrOn = !!(c && c.enabled);
    } catch (e) { /* leave as-is; the switch shows the last known state */ }
  }

  async function toggleNarrator() {
    if (narrBusy) return;
    narrBusy = true;
    const want = !narrOn;
    if (open) render();                       // show the pending state at once
    try {
      const r = await fetch(location.origin + '/system/narrator/'
                            + (want ? 'start' : 'stop'),
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: '{}' });
      const j = await r.json();
      const c = (j && j.content !== undefined) ? j.content : j;
      // Trust the server's own answer over our optimistic guess. start/stop
      // report `narrator_enabled`; only status uses the bare `enabled`.
      narrOn = (c && c.narrator_enabled !== undefined) ? !!c.narrator_enabled
             : (c && c.enabled !== undefined) ? !!c.enabled
             : want;
    } catch (e) {
      await narratorStatus();                 // failed — resync rather than lie
    } finally {
      narrBusy = false;
      if (open) render();
    }
  }

  async function poll() {
    try {
      const r = await fetch(location.origin + '/activity/timeline?scope=all&limit=60',
        { headers: { 'Accept': 'application/json' } });
      const j = await r.json();
      const c = (j && j.content !== undefined) ? j.content : j;
      events = (c && c.events) || [];
    } catch (e) { /* keep the last good feed */ }
    if (narrOn === null || open) await narratorStatus();
    updatePill();
    if (open) {
      const t = $('.vao-track');
      const atEnd = t ? (t.scrollWidth - t.scrollLeft - t.clientWidth < 40) : true;
      render();
      if (atEnd) scrollToNow();               // only auto-follow if already at "now"
    }
  }

  function updatePill() {
    const e = events[0];
    $('.vao-latest', pill).textContent =
      e ? ((ICON[e.kind] || '•') + ' ' + String(e.title || '').slice(0, 60)) : 'Activity';
    $('.vao-count', pill).textContent = events.length ? String(events.length) : '';
  }

  function render() {
    const head = $('.vao-head');
    head.innerHTML =
      `<span class="vao-chip${!filter ? ' on' : ''}" data-k="" tabindex="0">all</span>` +
      kinds().map(k =>
        `<span class="vao-chip${filter === k ? ' on' : ''}" data-k="${esc(k)}" tabindex="0">` +
        `${ICON[k] || ''} ${esc(LABEL[k] || k)}</span>`).join('') +
      `<span class="vao-spacer"></span>` +
      `<span class="vao-narr${narrOn ? ' on' : ''}" id="vao-narr" tabindex="0" role="switch" ` +
      `aria-checked="${narrOn === true}" title="Turn Vera's system narrator on or off">` +
      `<span class="led"></span>Narrator ${narrOn === null ? '…' : (narrOn ? 'on' : 'off')}</span>` +
      `<span class="vao-hint">oldest → newest · scroll sideways</span>`;
    const nb = head.querySelector('#vao-narr');
    if (nb) {
      if (narrBusy) nb.setAttribute('disabled', '');
      const hit = e => { e.stopPropagation(); toggleNarrator(); };
      nb.addEventListener('click', hit);
      nb.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hit(e); }
      });
    }
    head.querySelectorAll('.vao-chip').forEach(ch => {
      const pick = () => { filter = ch.dataset.k || null; render(); scrollToNow(); };
      ch.addEventListener('click', pick);
      ch.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); }
      });
    });

    const track = $('.vao-track');
    // Feed arrives newest-first; a timeline reads oldest → newest.
    const evs = filtered().slice(0, 60).reverse();
    if (!evs.length) {
      track.innerHTML = '<div class="vao-empty">No recent activity.</div>';
      return;
    }
    track.innerHTML = `<div class="vao-lane">` + evs.map(e => {
      // Identity, not position — a refresh must not move the expanded card.
      const k = e.kind, key = (e.ts || '') + '|' + k + '|' + String(e.title || '').slice(0, 40);
      const sum = (e.summary && e.summary !== e.title) ? String(e.summary) : '';
      return `<div class="vao-node${expanded === key ? ' exp' : ''}" data-key="${esc(key)}">
        <div class="vao-time">${esc(fmtTs(e.ts))}</div>
        <div class="vao-tickrow"><span class="vao-tick" style="background:${hue(k)}"></span></div>
        <div class="vao-card" style="border-left-color:${hue(k)}">
          <div class="vao-kind"><span>${ICON[k] || '•'}</span>${esc(LABEL[k] || k)}</div>
          <div class="vao-title">${esc(e.title || '')}</div>
          ${sum ? `<div class="vao-sum">${esc(sum)}</div>` : ''}
        </div></div>`;
    }).join('') +
      `<div class="vao-now"><div class="vao-time">now</div>
        <div class="vao-now-tick">▸</div></div></div>`;

    track.querySelectorAll('.vao-node').forEach(n => {
      n.querySelector('.vao-card').addEventListener('click', ev => {
        ev.stopPropagation();
        expanded = (expanded === n.dataset.key) ? null : n.dataset.key;
        render();
      });
    });
  }

  mount();
})();

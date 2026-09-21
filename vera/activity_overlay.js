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
                 run: '◇', artifact: '📦' };
  const LABEL = { narrator: 'Narrator', dream_cycle: 'Dream', dream: 'Dream',
                  loop_live: 'Loop', program: 'Program', project: 'Project',
                  goal: 'Goal', chat: 'Chat', cap: 'Cap', run: 'Run', artifact: 'Artifact' };
  // Per-kind accent so a lane of mixed activity is readable at a glance. Falls
  // back to the theme accent for any kind added server-side later.
  const HUE = { narrator: 'var(--dv3,#38bdf8)', dream_cycle: 'var(--ac5,#bb9af7)', dream: 'var(--ac5,#bb9af7)',
                loop_live: 'var(--ac,#5a9e8f)', v8_loop: 'var(--ac,#5a9e8f)', program: 'var(--dv2,#5ec9a0)', project: 'var(--dv4,#c9955a)',
                goal: 'var(--dv5,#ec4899)', chat: 'var(--dv1,#a78bfa)', cap: 'var(--t2,#8a92a0)',
                run: 'var(--dv6,#c96b6b)', artifact: 'var(--dv7,#fb923c)' };

  let events = [], filter = null, open = false, expanded = null;
  let narrOn = null, narrBusy = false;       // null = status not known yet

  const style = document.createElement('style');
  style.textContent = `
  /* THE TICKER — in the header's flow (the Harness board's .vao) */
  #vao-pill{display:inline-flex;align-items:center;gap:8px;height:30px;max-width:min(360px,30vw);padding:0 11px;border-radius:var(--r-sm,6px);cursor:pointer;flex:0 1 auto;min-width:0;
    font:11px/1.35 var(--f-ui,var(--sans,system-ui,sans-serif));background:var(--s2,var(--bg2,#1c2026));color:var(--t2,var(--dim2,#9aa4b2));border:none;box-sizing:border-box}
  #vao-pill:hover,#vao-pill.on{color:var(--t1,var(--text,#dce1e8));background:var(--fill,color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent))}
  #vao-pill .vao-dot{width:6px;height:6px;border-radius:50%;background:var(--ac,var(--acc,#5a9e8f));flex:0 0 auto;box-shadow:0 0 0 3px color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 22%,transparent);animation:vaoP 2.2s infinite}
  @keyframes vaoP{0%,100%{opacity:1}50%{opacity:.4}}
  @media (prefers-reduced-motion:reduce){#vao-pill .vao-dot{animation:none}}
  #vao-pill .vao-latest{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  #vao-pill .vao-count{flex:0 0 auto;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7480));background:var(--s3,var(--bg3,#22262e));border-radius:99px;padding:1px 6px;font-variant-numeric:tabular-nums}
  #vao-pill .vao-caret{flex:0 0 auto;color:var(--t3,var(--dim,#6b7480));font-size:9px}
  #vao-pill:focus-visible{outline:2px solid var(--ac,var(--acc,#5a9e8f));outline-offset:2px}

  /* THE PANEL — in the column flow under the header (the board's .vpanel); floats only on a page with no header */
  #vao-panel{display:none;flex-direction:column;flex-shrink:0;margin:8px 16px 10px;border-radius:var(--ui-radius,10px);overflow:hidden;
    font:12px/1.45 var(--f-ui,var(--sans,system-ui,sans-serif));color:var(--t1,var(--text,#dce1e8));background:var(--s1,var(--bg1,#15181d));box-shadow:0 1px 0 0 var(--bd,var(--border,#2a2f37)) inset,0 0 0 1px var(--bd2,var(--border2,#39414c))}
  #vao-panel.open{display:flex}
  #vao-panel.float{position:fixed;z-index:2147483000;margin:0;box-shadow:0 12px 38px rgba(0,0,0,.45)}
  .vao-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:9px 12px;flex:0 0 auto}
  .vao-chip{display:inline-flex;align-items:center;gap:6px;height:23px;padding:0 10px;border-radius:99px;cursor:pointer;font-size:10px;color:var(--t3,var(--dim,#6b7480));background:var(--s2,var(--bg2,#1c2026));border:none;white-space:nowrap}
  .vao-chip i{width:7px;height:7px;border-radius:2px;background:var(--kc,var(--t3,#6b7480));display:inline-block}
  .vao-chip .vao-n{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7480))}
  .vao-chip:hover{color:var(--t1,var(--text,#dce1e8))}
  .vao-chip.on{color:var(--t1,var(--text,#dce1e8));box-shadow:inset 0 0 0 1.5px currentColor}
  .vao-chip:focus-visible,.vao-card:focus-visible{outline:2px solid var(--ac,var(--acc,#5a9e8f));outline-offset:2px}
  .vao-spacer{flex:1 1 auto}
  .vao-hint{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7480))}
  .vao-narr{display:inline-flex;align-items:center;gap:7px;height:23px;padding:0 10px;border-radius:99px;cursor:pointer;white-space:nowrap;font-size:10px;background:var(--s2,var(--bg2,#1c2026));border:none;color:var(--t2,var(--dim2,#9aa4b2))}
  .vao-narr:hover{color:var(--t1,var(--text,#dce1e8))}
  .vao-narr[disabled]{opacity:.55;cursor:progress}
  .vao-narr .led{width:7px;height:7px;border-radius:50%;flex:0 0 auto;background:var(--t3,var(--dim,#6b7480))}
  .vao-narr.on .led{background:var(--ac2,var(--acc2,#5ec9a0));animation:vaoP 2.2s infinite}

  /* THE LANE — oldest → newest, the cards on the board's surface, "now" at the end */
  .vao-track{overflow-x:auto;overflow-y:hidden;flex:0 0 auto;scrollbar-width:thin;scrollbar-color:var(--bd2,var(--border2,#39414c)) transparent}
  .vao-track::-webkit-scrollbar{height:8px}
  .vao-track::-webkit-scrollbar-thumb{background:var(--bd2,var(--border2,#39414c));border-radius:4px}
  .vao-lane{display:flex;gap:9px;padding:0 12px 12px;align-items:stretch;height:132px;box-sizing:border-box;min-width:100%;width:max-content}
  .vao-node{width:184px;flex:0 0 184px;display:flex;flex-direction:column;gap:6px;height:100%;min-height:0;min-width:0}
  .vao-time{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7480));height:auto;flex:0 0 auto}
  .vao-tickrow{display:none}
  .vao-card{flex:1;min-height:0;overflow:hidden;margin:0;background:var(--s2,var(--bg2,#1c2026));border:none;border-radius:var(--r-sm,6px);padding:9px 11px;display:flex;flex-direction:column;gap:3px;cursor:pointer;transition:box-shadow .15s ease;--kc:var(--ac,var(--acc,#5a9e8f))}
  .vao-card:hover{box-shadow:inset 3px 0 0 0 var(--kc),0 0 0 1px var(--bd2,var(--border2,#39414c))}
  .vao-card.on{box-shadow:inset 3px 0 0 0 var(--kc),0 0 0 1.5px var(--kc)}
  .vao-kind{display:flex;align-items:center;gap:6px;font-size:9px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;color:var(--kc);margin:0}
  .vao-kind i{width:7px;height:7px;border-radius:2px;background:var(--kc);display:inline-block}
  .vao-title{font-size:11.5px;color:var(--t1,var(--text,#dce1e8));line-height:1.35;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
  .vao-sum{font-size:10px;color:var(--t2,var(--dim2,#9aa4b2));line-height:1.45;margin:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
  .vao-meta{display:flex;gap:4px;flex-wrap:wrap;margin-top:auto;padding-top:5px;overflow:hidden;max-height:22px}
  .vao-tag{font:8.5px/1.5 var(--f-mono,var(--mono,ui-monospace,monospace));color:var(--t3,var(--dim,#6b7480));background:var(--s3,var(--bg3,#22262e));border:none;border-radius:99px;padding:0 6px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vao-detail{display:none}
  .vao-now{width:52px;flex:0 0 52px;display:flex;flex-direction:column;gap:6px;align-items:center;justify-content:center;color:var(--ac,var(--acc,#5a9e8f));font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;padding:0}
  .vao-now-tick{font-size:13px}
  .vao-empty{padding:26px 14px;color:var(--t3,var(--dim,#6b7480));text-align:center;width:100%}

  /* THE RUN FLOW — the whole run laid out under the lane: every step, in · out, how long, where it has got to */
  .vao-flow{flex:0 0 auto;margin:0 12px 12px;border-radius:var(--r-sm,6px);background:var(--s2,var(--bg2,#1c2026));box-shadow:0 0 0 1px var(--bd,var(--border,#2a2f37));overflow:hidden}
  .vao-fh{display:flex;align-items:center;gap:8px;height:34px;padding:0 12px;font-size:11px;color:var(--t1,var(--text,#dce1e8));box-shadow:inset 0 -1px 0 0 var(--bd,var(--border,#2a2f37))}
  .vao-fh i{width:7px;height:7px;border-radius:2px;flex-shrink:0;display:inline-block}
  .vao-fh b{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9.5px;color:var(--t3,var(--dim,#6b7480));font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .vao-fc{height:22px;padding:0 9px;border-radius:99px;border:none;font:inherit;font-size:9.5px;color:var(--t3,var(--dim,#6b7480));background:transparent;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;white-space:nowrap}
  .vao-fc:hover{color:var(--t1,var(--text,#dce1e8));box-shadow:0 0 0 1px var(--bd2,var(--border2,#39414c))}
  .vao-fc.on{color:var(--ac,var(--acc,#5a9e8f));background:var(--fill,color-mix(in srgb,var(--acc,#5a9e8f) 14%,transparent))}
  .vao-fsteps{display:flex;flex-direction:column;padding:6px 12px 10px;max-height:260px;overflow:auto}
  .vao-fs{display:flex;align-items:center;gap:10px;padding:5px 0;position:relative;box-shadow:inset 0 -1px 0 0 var(--bd,var(--border,#2a2f37))}
  .vao-fs:last-child{box-shadow:none}
  .vao-fdot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--t3,var(--dim,#6b7480));opacity:.5}
  .vao-fs.done .vao-fdot{background:var(--ac2,var(--acc2,#5ec9a0));opacity:1}
  .vao-fs.run .vao-fdot{background:var(--ac,var(--acc,#5a9e8f));opacity:1;box-shadow:0 0 0 3px color-mix(in srgb,var(--ac,var(--acc,#5a9e8f)) 24%,transparent)}
  .vao-fs.fail .vao-fdot{background:var(--dv6,#c96b6b);opacity:1}
  .vao-fs.ask .vao-fdot{background:var(--ac3,var(--acc3,#c9955a));opacity:1}
  .vao-fn{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10px;color:var(--t1,var(--text,#dce1e8));width:118px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vao-fd{font-size:10.5px;color:var(--t2,var(--dim2,#9aa4b2));flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vao-fio{display:flex;gap:4px;flex-shrink:0}
  .vao-fio span{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;padding:1px 7px;border-radius:99px;white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis}
  .vao-fio .in{color:var(--dv1,#a78bfa);background:color-mix(in srgb,var(--dv1,#a78bfa) 13%,transparent)}
  .vao-fio .out{color:var(--dv2,#5ec9a0);background:color-mix(in srgb,var(--dv2,#5ec9a0) 13%,transparent)}
  .vao-fms{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:9px;color:var(--t3,var(--dim,#6b7480));width:46px;text-align:right;flex-shrink:0}
  .vao-fnote{padding:8px 12px 10px;font-size:10px;color:var(--t3,var(--dim,#6b7480))}
  .vao-action{display:grid;grid-template-columns:32px 72px minmax(0,1fr);gap:5px;align-items:center;font-size:10px;padding:2px 0;color:var(--t2,var(--dim2,#9aa4b2))}
  .vao-action .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t1,var(--text,#dce1e8))}
  @media (max-width:640px){
    #vao-pill{max-width:46vw;padding:0 7px}.vao-hint{display:none}
    #vao-panel{margin:6px 4px 8px}
    .vao-node{flex-basis:min(82vw,280px);width:min(82vw,280px)}
  }`;

  const pill = document.createElement('div');
  pill.id = 'vao-pill';
  pill.tabIndex = 0;
  pill.setAttribute('role', 'button');
  pill.setAttribute('aria-expanded', 'false');
  pill.setAttribute('aria-controls', 'vao-panel');
  pill.setAttribute('aria-label', 'Open recent Vera activity');
  pill.innerHTML = `<span class="vao-dot"></span>
    <span class="vao-latest">Activity</span>
    <span class="vao-count"></span><span class="vao-caret">▾</span>`;

  const panel = document.createElement('div');
  panel.id = 'vao-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Recent Vera activity');
  panel.setAttribute('aria-hidden', 'true');
  panel.innerHTML = `<div class="vao-head"></div><div class="vao-track"></div><div class="vao-flowhost"></div>`;
  let flowKey = null, flowSteps = null, flowBusy = '';   // the run whose flow is laid out under the lane, and its steps

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
      // after the meters: before the header's own spacer (the harness) or its right-hand controls
      const anchor = header.querySelector('.hsp') || header.querySelector('.hdr-right');
      header.insertBefore(pill, anchor || null);
      // the panel is part of the page: under the header, in the column flow, above the tabs
      header.insertAdjacentElement('afterend', panel);
    } else {
      panel.classList.add('float');
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
    if (!panel.classList.contains('float')) return;   // in flow: the page lays it out
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
    panel.setAttribute('aria-hidden', String(!open));
    pill.classList.toggle('on', open);
    pill.setAttribute('aria-expanded', String(open));
    pill.setAttribute('aria-label', open ? 'Close recent Vera activity' : 'Open recent Vera activity');
    $('.vao-caret', pill).textContent = open ? '▴' : '▾';
    if (open) { place(); render(); scrollToNow(); $('.vao-chip')?.focus(); } else { flowKey = null; flowSteps = null; }
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
      e ? ((LABEL[e.kind] || e.kind || '') + ' · ' + String(e.title || '').slice(0, 60)) : 'Activity';
    $('.vao-count', pill).textContent = events.length ? String(events.length) : '';
  }

  function render() {
    const head = $('.vao-head');
    const counts = {}; events.forEach(e => { counts[e.kind] = (counts[e.kind] || 0) + 1; });
    head.innerHTML =
      `<span class="vao-chip${!filter ? ' on' : ''}" data-k="" tabindex="0" role="button" aria-pressed="${!filter}" style="--kc:var(--t3,#6b7480)"><i></i>all<span class="vao-n">${events.length}</span></span>` +
      kinds().map(k =>
        `<span class="vao-chip${filter === k ? ' on' : ''}" data-k="${esc(k)}" tabindex="0" role="button" aria-pressed="${filter === k}" style="--kc:${hue(k)};${filter === k ? 'color:' + hue(k) : ''}"><i></i>${esc(LABEL[k] || k)}<span class="vao-n">${counts[k] || 0}</span></span>`).join('') +
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
      $('.vao-flowhost').innerHTML = '';
      return;
    }
    track.innerHTML = `<div class="vao-lane">` + evs.map(e => {
      // Identity, not position — a refresh must not move the selected card.
      const k = e.kind, key = keyOf(e);
      const sum = (e.summary && e.summary !== e.title) ? String(e.summary) : '';
      const tags = tagsOf(e);
      return `<div class="vao-node" data-key="${esc(key)}">
        <div class="vao-time">${esc(fmtTs(e.ts))}</div>
        <div class="vao-card${flowKey === key ? ' on' : ''}" role="button" tabindex="0" aria-expanded="${flowKey === key}" aria-label="${esc((LABEL[k] || k) + ': ' + (e.title || ''))}" style="--kc:${hue(k)}" title="Open the whole flow">
          <div class="vao-kind"><i></i>${esc(LABEL[k] || k)}</div>
          <div class="vao-title">${esc(e.title || '')}</div>
          ${sum ? `<div class="vao-sum">${esc(sum)}</div>` : ''}
          ${tags.length ? `<div class="vao-meta">${tags.slice(0, 4).map(v => `<span class="vao-tag">${esc(v)}</span>`).join('')}</div>` : ''}
        </div></div>`;
    }).join('') +
      `<div class="vao-now"><span>now</span><span class="vao-now-tick">▸</span></div></div>`;

    track.querySelectorAll('.vao-node').forEach(n => {
      n.querySelector('.vao-card').addEventListener('click', ev => {
        ev.stopPropagation();
        const k = n.dataset.key;
        if (flowKey === k) { flowKey = null; flowSteps = null; render(); return; }
        flowKey = k; flowSteps = null; render();
        const e = events.find(x => keyOf(x) === k); if (e) loadFlow(e);
      });
      n.querySelector('.vao-card').addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ev.currentTarget.click(); }
      });
    });
    renderFlow();
  }

  const keyOf = e => (e.ts || '') + '|' + e.kind + '|' + String(e.title || '').slice(0, 40);
  // the tags a card wears: status, the capability, progress, the trace, the session, the run's evidence
  function tagsOf(e) {
    const k = e.kind, x = e.extra || {}, meta = [];
    if (e.status) meta.push(e.status);
    if (e.cap) meta.push(e.cap);
    if (x.progress != null) meta.push(Math.round(Number(x.progress) * 100) + '%');
    if (x.elapsed_s != null) meta.push(fmtMs(Number(x.elapsed_s) * 1000));
    if (x.trace_id) meta.push('trace ' + String(x.trace_id).slice(0, 12));
    if (e.session_id) meta.push('session ' + String(e.session_id).slice(0, 12));
    if (x.authoritative === false) meta.push('observed');
    const reconciliation = x.reconciliation || {}, recovery = x.recovery || {};
    if (k === 'run' && reconciliation.verified) meta.push('journal verified');
    const catalogRecovery = x.catalog_recovery || {};
    if (k === 'run' && catalogRecovery.attempted) { meta.push((catalogRecovery.recovered || 0) + ' recovered'); if (catalogRecovery.quarantined) meta.push(catalogRecovery.quarantined + ' quarantined'); }
    if (k === 'run' && recovery.retry_count) meta.push(recovery.retry_count + ' retries');
    if (k === 'run' && recovery.interrupted) meta.push('interrupted');
    const artifacts = Array.isArray(x.artifact_refs) ? x.artifact_refs : [];
    if (k === 'run' && artifacts.length) meta.push(artifacts.length + ' artifact refs');
    if (k === 'run' && artifacts.some(a => a.partial)) meta.push('partial output');
    const policy = x.policy_state || {};
    if (k === 'run' && policy.waiting_for_approval) meta.push('approval pending');
    if (k === 'run' && Array.isArray(policy.controls) && policy.controls.length) meta.push(policy.controls.length + ' control records');
    if (k === 'run' && x.workflow_id) meta.push('workflow ' + String(x.workflow_id).slice(0, 12));
    if (k === 'run' && x.projection) meta.push(String(x.projection));
    const telemetry = x.telemetry || {};
    if (k === 'run' && telemetry.event_count != null) meta.push(telemetry.event_count + ' trace events');
    if (k === 'run' && telemetry.orphan_event_count) meta.push(telemetry.orphan_event_count + ' orphan links');
    if (k === 'run' && telemetry.exported === false) meta.push('not exported');
    const intent = x.current_intent || {};
    if (k === 'narrator' && x.tier) meta.push(String(x.tier) + ' take');
    if (k === 'narrator' && intent.focus) meta.push('focus: ' + String(intent.focus).slice(0, 60));
    if (k === 'narrator' && intent.confidence) meta.push(String(intent.confidence) + ' confidence');
    if (k === 'program' && x.loops != null) meta.push(x.loops + ' loops');
    return meta;
  }
  const fmtMs = ms => { ms = Number(ms); if (!isFinite(ms) || ms <= 0) return '—'; return ms >= 60000 ? Math.round(ms / 60000) + 'm' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'; };
  const stateOf = s => { s = String(s || '').toLowerCase(); return /done|ok|complete|success|finished|merged|promoted/.test(s) ? 'done' : /run|active|start|progress|tail/.test(s) ? 'run' : /fail|error|cancel|interrupt|stale|refus/.test(s) ? 'fail' : /ask|wait_user|approval|decision|review/.test(s) ? 'ask' : 'wait'; };

  /* ── THE RUN FLOW: every run has a shape — what it was asked, what it read, what it did, what it left ── */
  // a loop's steps from its persisted events, through the graph families when the page has them (the same
  // reading the chat's context graph makes), else a plain pass over the events
  function stepsFromLoopEvents(evs) {
    const F = window.VeraGraphFamilies;
    if (F && typeof F.toDoc === 'function') {
      try {
        const d = F.toDoc('loop', { events: evs }); if (!d.error) {
          const byId = {}; d.nodes.forEach(n => { byId[n.id] = n; });
          const caps = {}, recs = {}; d.edges.forEach(ed => { if (ed.label === 'CALLS') (caps[ed.from] = caps[ed.from] || []).push(ed.to); else if (ed.label === 'RECORDS') (recs[ed.from] = recs[ed.from] || []).push(ed.to); });
          return d.nodes.filter(n => n.kind === 'step').map(n => { const cn = (caps[n.id] || []).map(id => byId[id]).filter(Boolean); const rn = (recs[n.id] || []).map(id => byId[id]).filter(Boolean);
            const io = []; cn.forEach(c => { (c.wires || []).slice(0, 2).forEach(w => io.push(['in', String(w).slice(0, 24)])); }); rn.slice(0, 2).forEach(r => io.push(['out', String(r.label || r.kind || '').slice(0, 24)]));
            return { n: String(n.label || n.id).slice(0, 30), d: cn.map(c => c.label).join(' · ') || String(n.rec && n.rec.summary || ''), io, ms: n.rec && n.rec.ms != null ? fmtMs(n.rec.ms) : '—', st: stateOf(n.status) }; }); }
      } catch (_) {}
    }
    const steps = [], idx = {};
    (evs || []).forEach(ev => { const t = String(ev.type || ''); const sid = ev.step_id != null ? ev.step_id : (ev.step != null ? ev.step : (ev.index != null ? ev.index : null)); if (sid == null) return;
      let s = idx[sid]; if (!s) { s = idx[sid] = { n: String(ev.title || ev.name || ('step ' + sid)).slice(0, 30), d: '', io: [], ms: '—', st: 'wait' }; steps.push(s); }
      if (/step_start$/.test(t)) s.st = 'run'; if (/step_done$/.test(t)) { s.st = 'done'; if (ev.ms != null) s.ms = fmtMs(ev.ms); } if (/step_(error|fail)/.test(t)) s.st = 'fail';
      if (/cap(_call|_start|\.start)$/.test(t) || t === 'tool_call') { const c = ev.tool || ev.cap || ev.name; if (c) s.d = s.d ? s.d + ' · ' + c : String(c); }
      if (/ask|wait_user|decision/.test(t)) s.st = 'ask'; });
    return steps;
  }
  async function loadFlow(e) {
    const k = e.kind, x = e.extra || {}, key = keyOf(e);
    let steps = null;
    try {
      if (k === 'run' && Array.isArray(x.children) && x.children.length) {
        steps = x.children.map((child, ci) => { const evs = child.events || []; const ev0 = evs[0] || {}, payload = ev0.payload || {}; const name = payload.capability || child.kind || 'action';
          const io = []; if (evs.length) io.push(['in', evs.length + ' events']); (child.artifacts || []).slice(0, 2).forEach(a => io.push(['out', String(a.kind || a.id || 'artifact').slice(0, 20)]));
          const ms = child.started_at && child.ended_at ? fmtMs(Date.parse(child.ended_at) - Date.parse(child.started_at)) : '—';
          return { n: String(child.task_id || (ci + 1)), d: name + (child.attempt > 1 ? ' · try ' + child.attempt : ''), io, ms, st: stateOf(child.status || 'created') }; });
      } else if (k === 'program' && Array.isArray(x.loop_plan) && x.loop_plan.length) {
        steps = x.loop_plan.map((lp, i) => ({ n: String(lp.name || ('loop ' + (i + 1))).slice(0, 30), d: String(lp.goal || ''), io: [['in', lp.profile || lp.engine || 'loop'], ['out', (lp.runs || 0) + ' runs']], ms: '—', st: stateOf(lp.status) }));
      } else if ((k === 'loop_live' || k === 'v8_loop') && e.session_id) {
        flowBusy = key; renderFlow();
        const r = await fetch(location.origin + '/workshop/agent_loop/session_state?session_id=' + encodeURIComponent(e.session_id) + '&since=0', { headers: { Accept: 'application/json' } });
        const j = await r.json(); const c = (j && j.content !== undefined) ? j.content : j;
        steps = stepsFromLoopEvents((c && c.events) || []);
        if (!steps.length && c && c.run) steps = [{ n: 'run', d: String(c.run.goal || e.title || ''), io: [], ms: c.run.elapsed_s ? fmtMs(c.run.elapsed_s * 1000) : '—', st: stateOf(c.run.status) }];
      }
    } catch (_) { steps = null; }
    if (!steps) {
      // every other kind: the event itself is the run — one step with what it carries
      const io = []; if (e.cap) io.push(['in', e.cap]); if (x.tier) io.push(['out', x.tier + ' take']); if (e.ref) io.push(['out', String(e.ref).slice(0, 20)]);
      steps = [{ n: LABEL[k] || k, d: String(e.summary && e.summary !== e.title ? e.summary : e.title || ''), io, ms: x.elapsed_s != null ? fmtMs(x.elapsed_s * 1000) : '—', st: stateOf(e.status || 'done') }];
    }
    flowBusy = ''; if (flowKey === key) { flowSteps = steps; renderFlow(); }
  }
  function renderFlow() {
    const host = $('.vao-flowhost'); if (!host) return;
    const e = flowKey ? events.find(x => keyOf(x) === flowKey) : null;
    if (!e) { host.innerHTML = ''; return; }
    const k = e.kind, x = e.extra || {}, ui = e.ui || {}, intent = x.current_intent || {};
    const openUrl = ui.run_id ? '/activity/panel#' + encodeURIComponent('run:' + ui.run_id) : (ui.program_id ? '/activity/panel#' + encodeURIComponent('program:' + ui.program_id) : '');
    const nativeUrl = k === 'run' && ui.native_url ? ui.native_url + (x.workflow_id ? '?workflow_id=' + encodeURIComponent(x.workflow_id) : '') : '';
    const loopUrl = ui.reattach || (e.session_id && (k === 'loop_live' || k === 'v8_loop') ? '/workshop/agent_loop/reattach?session_id=' + encodeURIComponent(e.session_id) : '');
    const steps = flowSteps || [];
    const sub = [fmtTs(e.ts), e.status, e.cap, e.session_id ? 'session ' + String(e.session_id).slice(0, 14) : ''].filter(Boolean).join(' · ');
    host.innerHTML = `<div class="vao-flow"><div class="vao-fh"><i style="background:${hue(k)}"></i>${esc(e.title || '')}<b>${esc(sub)}</b><span class="vao-spacer"></span>` +
      (loopUrl ? `<a class="vao-fc" href="${esc(loopUrl.replace('/reattach', '/reattach'))}" target="_blank" rel="noopener" title="Re-attach to this loop's output (replays, then tails)">Re-run · attach ↗</a>` : '') +
      /* the projection's identity links: the DAG workshop is the NATIVE authority for a run; the activity panel
         (#run:<id>) is the full Run timeline the ticker is a projection of - named for what they are */
      (nativeUrl ? `<a class="vao-fc" href="${esc(nativeUrl)}" target="_blank" rel="noopener" title="The run's native record - the DAG workshop is the authority the ticker projects">Open native DAG workshop ↗</a>` : '') +
      (openUrl ? `<a class="vao-fc" href="${esc(openUrl)}" target="_blank" rel="noopener" title="Every event of this run, in the activity panel">Open full Run timeline ↗</a>` : '') +
      `<button class="vao-fc" type="button" data-close="1">Close</button></div>` +
      (flowBusy === flowKey && !flowSteps ? `<div class="vao-fnote">Reading the run…</div>` :
        steps.length ? `<div class="vao-fsteps">` + steps.map(s => `<div class="vao-fs ${esc(s.st)}"><span class="vao-fdot"></span><span class="vao-fn" title="${esc(s.n)}">${esc(s.n)}</span><span class="vao-fd" title="${esc(s.d)}">${esc(s.d)}</span><span class="vao-fio">${(s.io || []).slice(0, 3).map(p => `<span class="${esc(p[0])}">${esc(p[1])}</span>`).join('')}</span><span class="vao-fms">${esc(s.ms || '—')}</span></div>`).join('') + `</div>`
        : `<div class="vao-fnote">Nothing recorded for this run yet.</div>`) +
      /* a narrator take carries its CURRENT intent - what it is watching and on what basis - which is metadata about
         now, not a step of any run: shown beside the flow, stamped with when it was current, never as history */
      (k === 'narrator' && (intent.evidence || intent.ts) ? `<div class="vao-fnote">${intent.evidence ? 'basis: ' + esc(intent.evidence) : ''}${intent.evidence && intent.ts ? ' · ' : ''}${intent.ts ? 'intent current as of ' + esc(fmtTs(intent.ts)) : ''}</div>` : '') +
      `</div>`;
    const cb = host.querySelector('[data-close]'); if (cb) cb.addEventListener('click', ev => { ev.stopPropagation(); flowKey = null; flowSteps = null; render(); });
  }

  mount();
})();

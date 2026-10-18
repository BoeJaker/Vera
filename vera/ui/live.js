/* vera/ui/live.js — window.VeraLive: the page's ONE live channel (DA-03b, DESK-SPINE-DESIGN.md §3.3).

     const sub = VeraLive.subscribe({ trace: tid }, (ev) => ...)     // or { types: ['research.'] }, { sid }, or {} for all
     sub.close();
     VeraLive.call(cap, args, { onEvent, onDone }) → Promise<answer>  // makes the trace id, subscribes to it, calls /mcp/call

   One /ws/mcp socket per page, opened with the first subscription and closed with the last. The server filters the
   stream (DA-03a: {sid, trace, types}, every given part must match), so the socket is told the widest filter that still
   covers every subscription - a part only when every subscription gives it (sids when they agree, traces and types as
   unions) - and each subscription is matched exactly here. Reconnects with backoff (1 s, doubling to 30 s), subscribes
   again, and fetches what it missed while down from the event stream (/events?since=<last ts>). Served at /ui/live.js. */
(function () {
  'use strict';
  const root = typeof window !== 'undefined' ? window : globalThis;
  if (root.VeraLive) return;
  const S = { ws: null, open: false, subs: new Map(), n: 0, calls: 0, backoff: 1000, timer: null, lastTs: '', sentFilter: null, seen: [], waiting: [] };
  const strs = (v) => (v == null ? [] : (Array.isArray(v) ? v : [v])).map((x) => String(x || '').trim()).filter(Boolean);
  const norm = (f) => { f = f || {}; return { sid: String(f.sid || '').trim(), trace: strs(f.trace), types: strs(f.types) }; };
  /* the server's rule, here: every given part must match */
  function match(ev, f) {
    if (!ev || typeof ev !== 'object') return false; if (!f) return true;
    if (f.sid && String(ev.sid || ev.session_id || '') !== f.sid) return false;
    if (f.trace && f.trace.length && f.trace.indexOf(String(ev.trace_id || ev.trace || '')) < 0) return false;
    if (f.types && f.types.length) { const t = String(ev.type || ''); if (!f.types.some((p) => t.indexOf(p) === 0)) return false; }
    return true;
  }
  /* the server filter for every subscription: one of them as it is; several as alternatives {any: [...]} (DA-03d - an
     older server ignores `any` and sends everything, matched here); a subscription with no filter is everything. When
     the alternatives would be too many, the widest single filter that covers them all. */
  function cover() {
    const all = [...S.subs.values()].map((s) => s.f); if (!all.length) return null;
    const bare = (f) => { const o = {}; if (f.sid) o.sid = f.sid; if (f.trace.length) o.trace = f.trace.slice(0, 200); if (f.types.length) o.types = f.types.slice(0, 50); return o; };
    if (all.some((f) => !f.sid && !f.trace.length && !f.types.length)) return {};
    const uniq = [...new Map(all.map((f) => { const b = bare(f); return [JSON.stringify(b), b]; })).values()];
    if (uniq.length === 1) return uniq[0];
    if (uniq.length <= 50) return { any: uniq };
    const out = {};
    const sids = new Set(all.map((f) => f.sid)); if (sids.size === 1 && !sids.has('')) out.sid = all[0].sid;
    if (all.every((f) => f.trace.length)) out.trace = [...new Set([].concat(...all.map((f) => f.trace)))].slice(0, 200);
    if (all.every((f) => f.types.length)) out.types = [...new Set([].concat(...all.map((f) => f.types)))].slice(0, 50);
    return out;
  }
  const wsUrl = () => { const l = root.location || { origin: 'http://localhost' }; return String(l.origin).replace(/^http/, 'ws') + '/ws/mcp'; };
  function send(o) { try { if (S.ws && S.open) S.ws.send(JSON.stringify(o)); } catch (_) { /* the reconnect sends it again */ } }
  function pushFilter() {
    const f = cover(); const k = JSON.stringify(f);
    if (!S.open || k === S.sentFilter) return; S.sentFilter = k; send({ action: 'subscribe_events', filter: f || {} });
  }
  let pushQ = false;
  function schedulePush() { if (pushQ) return; pushQ = true; Promise.resolve().then(() => { pushQ = false; pushFilter(); }); }
  /* one event to every subscription it matches, once (a catch-up and the socket may both bring it) */
  function dispatch(ev) {
    let body = ''; try { body = JSON.stringify(ev); } catch (_) { body = String(ev.type || ''); }
    const id = String(ev.ts || '') + '|' + body.length + '|' + body.slice(0, 160);   /* the same event, not merely the same moment */
    if (S.seen.indexOf(id) >= 0) return; S.seen.push(id); if (S.seen.length > 400) S.seen.splice(0, 200);
    if (ev.ts && String(ev.ts) > S.lastTs) S.lastTs = String(ev.ts);
    S.subs.forEach((s) => { if (match(ev, s.f)) { try { s.fn(ev); } catch (_) { /* a subscriber's own error */ } } });
  }
  async function catchUp(since) {
    if (!since || typeof root.fetch !== 'function') return;
    try { const r = await root.fetch('/events?limit=200&since=' + encodeURIComponent(since)); const list = await r.json();
      (Array.isArray(list) ? list : []).slice().reverse().forEach((ev) => { if (ev && String(ev.ts || '') > since) dispatch(ev); });
    } catch (_) { /* nothing to catch up on */ }
  }
  function connect() {
    if (S.ws || !S.subs.size || typeof root.WebSocket !== 'function') return;
    let ws; try { ws = new root.WebSocket(wsUrl()); } catch (_) { return later(); }
    S.ws = ws; S.open = false; S.sentFilter = null;
    ws.onopen = () => { S.open = true; const missed = S.lastTs; S.backoff = 1000; pushFilter(); catchUp(missed); const w = S.waiting.splice(0); w.forEach((f) => f()); };
    ws.onmessage = (m) => { let w; try { w = JSON.parse(m.data); } catch (_) { return; } if (w && w.type === 'event' && w.data) dispatch(w.data); };
    ws.onclose = () => { S.ws = null; S.open = false; later(); };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* closing */ } };
  }
  function later() {
    if (S.timer || !S.subs.size) return;
    const wait = S.backoff; S.backoff = Math.min(30000, S.backoff * 2);
    S.timer = setTimeout(() => { S.timer = null; connect(); }, wait);
  }
  function stop() { if (S.timer) { clearTimeout(S.timer); S.timer = null; } const ws = S.ws; S.ws = null; S.open = false; S.sentFilter = null; if (ws) { ws.onclose = null; try { ws.close(); } catch (_) { /* closed */ } } }

  function subscribe(filter, fn) {
    const id = ++S.n; S.subs.set(id, { f: norm(filter), fn: typeof fn === 'function' ? fn : () => {} });
    if (!S.ws) connect(); else schedulePush();
    let live = true;
    return { id, close() { if (!live) return; live = false; S.subs.delete(id); if (!S.subs.size) stop(); else schedulePush(); },
      update(f) { const s = S.subs.get(id); if (s) { s.f = norm(f); schedulePush(); } } };
  }
  /* resolves when the socket is open and told the current filter (at most ms - a socket that does not open never holds
     a caller); a page that makes its own call subscribes first, then awaits this, then calls */
  function ready(ms) { if (S.open) { pushFilter(); return Promise.resolve(true); }
    return new Promise((res) => { let done = false; const go = (v) => { if (!done) { done = true; res(v); } }; S.waiting.push(() => go(true)); setTimeout(() => go(false), ms == null ? 1500 : ms); }); }
  /* a call that is followed live: its trace id is made here and subscribed to before the call goes out */
  async function call(cap, args, opts) {
    opts = opts || {}; const sid = String(opts.sid || root.SID || 'page');
    const tid = opts.trace_id || ('t-' + sid + '-' + Date.now().toString(36) + '-' + (++S.calls));
    const sub = subscribe(Object.assign({ trace: [tid] }, opts.types ? { types: opts.types } : {}), (ev) => { if (opts.onEvent) opts.onEvent(ev); });
    /* the socket is open and told this trace before the call goes out - its first steps would be lost otherwise (a
       first connection has nothing to catch up from); a socket that does not open in 1.5 s does not hold the call */
    await ready(opts.wait);
    try {
      const r = await root.fetch('/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': (opts.surface || root.__VERA_SURFACE || 'chat') + '; trigger=' + (root.navigator && root.navigator.userActivation && root.navigator.userActivation.isActive ? 'person' : 'auto') }, body: JSON.stringify({ name: cap, arguments: args || {}, trace_id: tid }) });
      const j = await r.json(); const ans = j && j.type === 'tool_result' && 'content' in j ? j.content : j;
      if (opts.onDone) { try { opts.onDone(ans, tid); } catch (_) { /* the caller's */ } }
      return ans;
    } finally { setTimeout(() => sub.close(), opts.linger == null ? 1500 : opts.linger); }   // a step may trail the answer
  }
  root.VeraLive = { subscribe, call, ready, match, cover, state: () => ({ socket: !!S.ws, open: S.open, subs: S.subs.size, backoff: S.backoff, lastTs: S.lastTs, filter: S.sentFilter ? JSON.parse(S.sentFilter) : null }), version: 1 };
})();

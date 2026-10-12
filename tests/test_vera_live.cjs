// DA-03b: window.VeraLive - the page's one live channel (DESK-SPINE-DESIGN.md §3.3), with a fake socket, fetch and timers.
//   node tests/test_vera_live.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'live.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const sockets = [], timers = [], fetched = [];
class FakeWS { constructor(url) { this.url = url; this.sent = []; this.closed = false; sockets.push(this); }
  send(s) { this.sent.push(JSON.parse(s)); } close() { this.closed = true; if (this.onclose) this.onclose(); }
  open() { this.onopen && this.onopen(); } emit(ev) { this.onmessage && this.onmessage({ data: JSON.stringify({ type: 'event', data: ev }) }); }
  drop() { this.onclose && this.onclose(); } }
let fetchAnswer = () => [];
const win = { location: { origin: 'https://vera.local:8999' }, WebSocket: FakeWS, SID: 'chat-1',
  fetch: async (url, opt) => { fetched.push({ url, opt }); const body = typeof fetchAnswer === 'function' ? fetchAnswer(url, opt) : fetchAnswer; return { json: async () => body }; } };
const ctx = { window: win, globalThis: win, console, Promise, JSON, Date, encodeURIComponent,
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; } };
vm.createContext(ctx); vm.runInContext(src, ctx);
const L = win.VeraLive; const tick = () => new Promise((r) => setImmediate(r));
const last = (ws) => ws.sent[ws.sent.length - 1];

(async () => {
  t('VeraLive is on the page', L && typeof L.subscribe === 'function' && typeof L.call === 'function');
  t('no subscription, no socket', sockets.length === 0 && !L.state().socket);
  const got = { a: [], b: [] };
  const a = L.subscribe({ trace: ['t-1'] }, (e) => got.a.push(e.type));
  t('the first subscription opens the one socket, at /ws/mcp', sockets.length === 1 && sockets[0].url === 'wss://vera.local:8999/ws/mcp');
  const ws = sockets[0]; ws.open(); await tick();
  t('on open it asks the server for that subscription\'s events', JSON.stringify(last(ws)) === JSON.stringify({ action: 'subscribe_events', filter: { trace: ['t-1'] } }));
  const b = L.subscribe({ trace: ['t-2'], types: ['info.seek.'] }, (e) => got.b.push(e.type)); await tick();
  t('a second subscription: still one socket, the server told both as alternatives', sockets.length === 1 && JSON.stringify(last(ws).filter) === JSON.stringify({ any: [{ trace: ['t-1'] }, { trace: ['t-2'], types: ['info.seek.'] }] }), JSON.stringify(last(ws).filter));
{ const all = L.subscribe({}, () => {}); await tick(); t('a subscription with no filter: the server sends everything', JSON.stringify(last(ws).filter) === '{}'); all.close(); await tick(); }
  ws.emit({ type: 'cap.ok', trace_id: 't-1', ts: '2026-10-12T10:00:01' });
  ws.emit({ type: 'cap.ok', trace_id: 't-2', ts: '2026-10-12T10:00:02' });
  ws.emit({ type: 'info.seek.step', trace_id: 't-2', ts: '2026-10-12T10:00:03' });
  t('each subscription gets exactly its own events', JSON.stringify(got.a) === '["cap.ok"]' && JSON.stringify(got.b) === '["info.seek.step"]', JSON.stringify(got));
  ws.emit({ type: 'info.seek.step', trace_id: 't-2', ts: '2026-10-12T10:00:03' });
  t('an event is delivered once', got.b.length === 1);
  a.close(); await tick();
  t('closing one narrows the filter to the rest', JSON.stringify(last(ws).filter) === JSON.stringify({ trace: ['t-2'], types: ['info.seek.'] }));
  // the connection drops: back off, reconnect, subscribe again, catch up from the last event
  fetchAnswer = (url) => (url.indexOf('/events?') === 0 ? [{ type: 'info.seek.step', trace_id: 't-2', ts: '2026-10-12T10:00:09', stage: 'reading' }, { type: 'info.seek.step', trace_id: 't-2', ts: '2026-10-12T10:00:03' }] : []);
  ws.drop();
  t('a drop waits 1 s before reconnecting', timers.length && timers[timers.length - 1].ms === 1000 && L.state().backoff === 2000);
  timers[timers.length - 1].fn(); const ws2 = sockets[1]; t('then a new socket', !!ws2); ws2.open(); await tick(); await tick(); await tick();
  t('it subscribes again with the current filter', JSON.stringify(last(ws2)) === JSON.stringify({ action: 'subscribe_events', filter: { trace: ['t-2'], types: ['info.seek.'] } }));
  t('and fetches what it missed since the last event it saw', fetched.some((f) => f.url === '/events?limit=200&since=2026-10-12T10%3A00%3A03'));
  t('the missed event is delivered, the one already seen is not', JSON.stringify(got.b) === '["info.seek.step","info.seek.step"]' && L.state().lastTs === '2026-10-12T10:00:09', JSON.stringify(got.b));
  t('a successful open resets the backoff', L.state().backoff === 1000);
  ws2.drop(); timers[timers.length - 1].fn(); sockets[2].drop(); t('repeated drops back off further (1 s, 2 s, ...)', timers[timers.length - 1].ms === 2000);
  timers[timers.length - 1].fn(); sockets[3].open(); await tick();
  b.close(); await tick();
  t('the last subscription closes the socket and stops reconnecting', sockets[3].closed && !L.state().socket && L.state().subs === 0);
  // a call followed live: its trace is subscribed before the request goes out
  fetchAnswer = (url, opt) => (url === '/mcp/call' ? { type: 'tool_result', tool_name: 'web.research', trace_id: JSON.parse(opt.body).trace_id, content: { ok: true, n: 3 } } : []);
  const steps = [];
  const p = L.call('web.research', { query: 'x' }, { onEvent: (e) => steps.push(e.stage), linger: 0 });
  const ws5 = sockets[sockets.length - 1]; await tick();
  t('call() waits for the socket before it posts (its first steps would be lost)', !fetched.some((f) => f.url === '/mcp/call'));
  ws5.open(); await tick(); await tick();
  const posted = fetched.filter((f) => f.url === '/mcp/call'); const body = JSON.parse(posted[posted.length - 1].opt.body);
  t('call() makes a trace id, subscribes to it and posts /mcp/call with it', body.name === 'web.research' && /^t-chat-1-/.test(body.trace_id) && JSON.stringify(last(ws5).filter) === JSON.stringify({ trace: [body.trace_id] }));
  ws5.emit({ type: 'info.seek.step', trace_id: body.trace_id, stage: 'searching', ts: '2026-10-12T10:01:00' });
  ws5.emit({ type: 'info.seek.step', trace_id: 't-other', stage: 'not mine', ts: '2026-10-12T10:01:01' });
  const ans = await p;
  t('its steps reach onEvent, others do not; it answers with the content', JSON.stringify(steps) === '["searching"]' && ans && ans.n === 3, JSON.stringify(steps));
  timers[timers.length - 1].fn(); await tick();
  t('after the answer (and its linger) the call\'s subscription closes', L.state().subs === 0);
  t('match() is the server\'s rule', L.match({ type: 'a.b', sid: 's' }, { sid: 's', trace: [], types: ['a.'] }) && !L.match({ type: 'a.b', sid: 'x' }, { sid: 's', trace: [], types: [] }));
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();

// DO-6 (DESK-OUTPUT-REDESIGN §2.4; owner yes, 19 Oct): "Send to dashboard" (the desk item's ⋯) and "Open on desk" (a dashboard
// tile's ⋯) move the ITEM RECORD between surfaces - no separate tile record. The surfaces are different frames, so the record waits
// in the other surface's localStorage inbox (vera.dash.main.inbox / vera.desk.inbox) and that surface lands it: the main dashboard
// through its own placeRecord, the chat through canvas.add (kind widget). Checked live on :8997 (tools/snip_do6.js).
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const store = () => { const m = {}; return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; }, _m: m }; };
const fnText = (src, start) => { const a = src.indexOf(start); if (a < 0) throw new Error('missing ' + start); let d = 0, i = src.indexOf('{', a), q = null; for (; i < src.length; i++) { const ch = src[i];
  if (q) { if (ch === '\\') { i++; continue; } if (ch === q) q = null; continue; } if (ch === '"' || ch === "'" || ch === '`') { q = ch; continue; } if (ch === '{') d++; else if (ch === '}' && !--d) break; } return src.slice(a, i + 1); };

/* the desk: which items offer it, and what it writes */
const CV = R('vera/canvas/canvas_element.js');
const ls1 = store(); const c1 = { localStorage: ls1, JSON, Object, Array, Date, String }; vm.createContext(c1);
vm.runInContext(fnText(CV, 'function recordOfItem(c)') + '\n' + fnText(CV, 'function toDashInbox(rec, from)') + '\nthis.rec = recordOfItem; this.send = toDashInbox;', c1);
const certs = { form: 'element', title: 'Certificates', source: 'certs.list', read: { args: {} }, draw: { tag: 'vera-certs' }, data: { certs: [] } };
t('a widget item\'s record is its content (form or draw) or its content.record, with the item\'s title', c1.rec(certs) === certs && c1.rec({ record: certs, title: 'Certs' }).title === 'Certs' && c1.rec({ columns: ['a'], rows: [] }) === null);
t('Send to dashboard leaves the record in the main dashboard\'s inbox, the last 20 kept', c1.send(certs, { canvas: 'cv1', key: 'k' }) === true
  && JSON.parse(ls1.getItem('vera.dash.main.inbox'))[0].record.title === 'Certificates' && (() => { for (let i = 0; i < 30; i++) c1.send(certs, {}); return JSON.parse(ls1.getItem('vera.dash.main.inbox')).length === 20; })());
t('the ⋯ entry is there only for a widget item that carries a record, after the standard entries (MENU_ALL untouched)',
  CV.includes("if (b.type === 'widget' && recordOfItem(b.content)) h += '<hr>' + row('todash', 'Send to dashboard',") && CV.includes("if (act === 'todash') { const ok = toDashInbox(recordOfItem((this._blockOf(key) || {}).content)")
  && !R('vera/ui/vera_item.js').includes("'todash'"));

/* the dashboard: the tile's ⋯ writes the desk's inbox; the main dashboard drains its own */
const D = R('vera/chat/vera-dashboard.js');
const ls2 = store(); const c2 = { localStorage: ls2, JSON, Array, Date, key: 'main', recordOf: (w) => (w === 'rec-1' ? certs : null) }; vm.createContext(c2);
vm.runInContext("var DESK_INBOX = 'vera.desk.inbox';\n" + fnText(D, 'function toDesk(wid)') + '\nthis.toDesk = toDesk;', c2);
const sent = c2.toDesk('rec-1') && JSON.parse(ls2.getItem('vera.desk.inbox'))[0];
t('Open on desk writes the item contract to the desk\'s inbox: kind widget, the same record, its title in the content, where it came from',
  !!sent && sent.kind === 'widget' && sent.content.record === undefined ? false : (sent.kind === 'widget' && sent.content.record.title === 'Certificates' && sent.content.title === 'Certificates' && sent.source.dashboard === 'main' && sent.source.wid === 'rec-1'));
t('a tile with no record sends nothing', c2.toDesk('none') === false);
const placed = []; const ls3 = store(); ls3.setItem('vera.dash.main.inbox', JSON.stringify([{ record: certs }, { record: 'junk' }, null]));
const c3 = { localStorage: ls3, JSON, Array, SKEY: 'vera.dash.main', placeRecord: (r) => placed.push(r) }; vm.createContext(c3);
vm.runInContext("var IKEY = SKEY + '.inbox';\n" + fnText(D, 'function drainInbox()') + '\ndrainInbox(); drainInbox();', c3);
t('the main dashboard drains its inbox once: each record placed through placeRecord, junk left out, the inbox emptied', placed.length === 1 && placed[0].title === 'Certificates' && ls3.getItem('vera.dash.main.inbox') === null);
t('only the main dashboard listens, on load and on storage events; a record tile has the ⋯ with "Open on desk"',
  D.includes("if (key === 'main') { drainInbox(); window.addEventListener('storage', function (e) { if (e.key === IKEY && e.newValue) drainInbox(); }); }")
  && D.includes('<button class="w-iconbtn" data-vd-more title="More: open it on the desk">⋯</button>') && D.includes('>Open on desk</button>'));

/* the chat: lands the desk's inbox on this session's desk, once */
const CH = R('vera/chat/chat_panel.html');
const ls4 = store(); ls4.setItem('vera.desk.inbox', JSON.stringify([sent, { content: {} }]));
const calls = []; const c4 = { localStorage: ls4, JSON, Array, String, Date, Promise, SID: 'chat-1', document: { getElementById: (id) => (id === 'canvasColumnBody' ? {} : null) }, _capCall: (n, a) => { calls.push([n, a]); return Promise.resolve({ ok: true }); } };
vm.createContext(c4); vm.runInContext("const _DESK_INBOX='vera.desk.inbox';\n" + fnText(CH, 'function _deskInboxDrain()') + '\n_deskInboxDrain(); _deskInboxDrain();', c4);
t('the chat lands each record on its desk with canvas.add (kind widget, the same content), once, and empties the inbox',
  calls.length === 1 && calls[0][0] === 'canvas.add' && calls[0][1].session_id === 'chat-1' && calls[0][1].kind === 'widget' && calls[0][1].content.record.title === 'Certificates' && /^dash:rec-1:/.test(calls[0][1].key) && ls4.getItem('vera.desk.inbox') === null);
const c5 = Object.assign({}, c4, { document: { getElementById: () => null } }); const before = calls.length; ls4.setItem('vera.desk.inbox', JSON.stringify([sent])); vm.createContext(c5);
vm.runInContext("const _DESK_INBOX='vera.desk.inbox';\n" + fnText(CH, 'function _deskInboxDrain()') + '\n_deskInboxDrain();', c5);
t('a chat frame without the desk column leaves the inbox for the one that has it', calls.length === before && ls4.getItem('vera.desk.inbox') !== null);
t('the chat drains on storage events and when a session loads', CH.includes("window.addEventListener('storage', e=>{ if(e.key===_DESK_INBOX&&e.newValue) _deskInboxDrain(); });") && CH.includes("setTimeout(()=>{ try{ _deskInboxDrain(); }catch(_){} }, 1500);"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

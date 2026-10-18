// DU-E-kv: a record's field edited in place, only where the item says how a field is written (content.edit).
// Without `edit` the element draws exactly as before; with it, an edit control on the editable fields only, the input
// while editing, "saving…" while the update runs, the error on its row after a failure; both content shapes kept.
//   node tests/test_desk_kv_edit.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'kv:probe';
const FIELDS = { name: 'ct132', status: 'up', vram_used: 12, ok: true, tags: ['a', 'b'] };
const EDIT = { cap: 'demo.record.update', args: { id: '{field.name}' }, field_arg: 'field', value_arg: 'value', fields: ['status', 'vram_used', 'tags'] };
const host = (ui) => { const h = { dataset: {}, _live: {}, _views: {} }; if (ui) h._kvUi = { [KEY]: ui }; return h; };
const draw = (c, el) => mod.BLOCK.kv(JSON.parse(JSON.stringify(c)), 'l', KEY, el || host());
const edits = (h) => [...h.matchAll(/data-act="kvedit" data-kv-f="([^"]*)"/g)].map((m) => m[1]);

/* without `edit`: what the element drew before this change, byte for byte */
const BEFORE = '<div class="vc-kv" data-w="canvas.kv"><div class="vc-kv-g"><div class="kv-r"><span class="kv-k" title="name">Name</span><span class="kv-v">ct132</span></div>'
  + '<div class="kv-r"><span class="kv-k" title="status">Status</span><span class="kv-v">up</span></div><div class="kv-r"><span class="kv-k" title="vram_used">Vram used</span><span class="kv-v num">12</span></div>'
  + '<div class="kv-r"><span class="kv-k" title="ok">Ok</span><span class="kv-v yes">true</span></div><div class="kv-r"><span class="kv-k" title="tags">Tags</span><span class="kv-v">[&quot;a&quot;,&quot;b&quot;]</span></div></div></div>';
{ const h = draw({ fields: FIELDS });
  t('no edit: identical to before', h === BEFORE, h);
  t('no edit: no controls, no edit state', !/kv-pen|data-kv-|kvedit|kv-edit/.test(h));
  t('no edit, with leftover element state: still identical', draw({ fields: FIELDS }, host({ ed: 'status', draft: 'x', pend: { status: { v: 'y' } }, err: {} })) === BEFORE);
  t('an edit without a cap is no edit', draw({ fields: FIELDS, edit: { value_arg: 'v' } }) === BEFORE);
  t('pair list, no edit: as before', draw({ fields: Object.entries(FIELDS) }) === BEFORE); }
{ const h = draw({ fields: FIELDS, edit: EDIT });
  t('edit: controls only on the editable scalar fields (the list, an object field left out)', JSON.stringify(edits(h)) === '["status","vram_used"]', edits(h).join(','));
  t('edit: the editable value takes focus (Enter opens it)', /<span class="kv-v" tabindex="0" data-kv-f="status"/.test(h) && /<span class="kv-v num" tabindex="0" data-kv-f="vram_used"/.test(h));
  t('edit: a field not in the list is drawn as before', h.includes('<div class="kv-r"><span class="kv-k" title="name">Name</span><span class="kv-v">ct132</span></div>'));
  const all = draw({ fields: FIELDS, edit: Object.assign({}, EDIT, { fields: undefined }) });
  t('edit with no list: every scalar field', JSON.stringify(edits(all)) === '["name","status","vram_used","ok"]', edits(all).join(',')); }
{ const h = draw({ fields: FIELDS, edit: EDIT }, host({ ed: 'status', draft: 'down <b>', pend: {}, err: {} }));
  t('editing: the input with the draft (escaped), save and cancel', /<input class="kv-in" data-kv-in="status" value="down &lt;b&gt;"/.test(h) && h.includes('data-act="kvsave" data-kv-f="status"') && h.includes('data-act="kvcancel" data-kv-f="status"'));
  t('editing: one input only, the other field still has its pencil', (h.match(/class="kv-in"/g) || []).length === 1 && JSON.stringify(edits(h)) === '["vram_used"]'); }
{ const h = draw({ fields: FIELDS, edit: EDIT }, host({ ed: null, draft: '', pend: { status: { v: 'down' } }, err: {} }));
  t('pending: the new value shown at once, marked saving…', /kv-pend" data-kv-row="status"><span class="kv-k" title="status">Status<\/span><span class="kv-v" tabindex="0" data-kv-f="status" title="Enter to edit">down<span class="kv-st">saving…<\/span>/.test(h), h);
  t('pending: no pencil while it saves', JSON.stringify(edits(h)) === '["vram_used"]'); }
{ const h = draw({ fields: FIELDS, edit: EDIT }, host({ ed: null, draft: '', pend: {}, err: { vram_used: 'no such node' } }));
  t('error: on that row, the old value kept', /kv-bad" data-kv-row="vram_used">[\s\S]*?>12<\/span>[\s\S]*?<div class="kv-err">no such node<\/div><\/div>/.test(h));
  t('error: only that row', (h.match(/kv-err/g) || []).length === 1); }
{ const h = draw({ fields: Object.entries(FIELDS), edit: EDIT }, host({ ed: 'vram_used', draft: '13', pend: {}, err: {} }));
  t('pair list with edit: the same controls and input', h.includes('data-kv-in="vram_used"') && JSON.stringify(edits(h)) === '["status"]'); }
{ const h = draw({ data: FIELDS, edit: EDIT });
  t('fields under `data`: editable too', JSON.stringify(edits(h)) === '["status","vram_used"]'); }
/* the save path in the source: the answer's capability with the filled args, the shape kept, the old value back on failure */
t('save calls the item\'s capability with the filled args', C.includes('r = await this.callResult(ed.cap, args)') && C.includes("if (ed.field_arg) a[ed.field_arg] = name; a[ed.value_arg || 'value'] = value;"));
t('save keeps the content\'s shape (pair list stays pair list, object stays object)', C.includes('const nf = Array.isArray(f2) ? f2.map(') && C.includes(": p)) : Object.assign({}, f2, put);"));
t('save writes the item via canvas.update', C.includes("await this.call('canvas.update', Object.assign(this._ref(key), { content: nc }))"));
t('a failure clears the pending value and keeps the error', C.includes("delete ui.pend[name];") && C.includes("ui.err[name] = String((r && (r.error || r.message || r.detail)) || 'the update failed')"));
t('typing keeps the draft without a redraw', C.includes("if (ui.ed === q.dataset.kvIn) ui.draft = String(q.value || ''); });"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

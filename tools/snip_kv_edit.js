async (cv) => { const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  /* DU-E-kv on the real desk: a local kv item with an `edit` (writes, item records and bring-in held; callResult answered
     here - one success, one failure; canvas.update captured, not sent) - press edit, type key by key (focus kept), Enter
     (the update's args, the value kept, the captured canvas.update), a failure (old value back, the error on the row),
     Esc (nothing written). Then the item without `edit`: no controls. */
  await wait(1200); for (let i = 0; i < 80 && !(cv && cv._doc); i++) { await wait(500); cv = cv || document.querySelector('#canvasColumnBody vera-canvas');
    if (!cv && i === 20 && window.CH && typeof CH.togglePage === 'function') { try { CH.togglePage('canvas'); } catch (e) { /* optional */ } } }
  if (!cv) return { noCanvas: true };
  if (!cv._doc) return { noDoc: true };
  const keep = { refresh: cv.refresh, call: cv.call, callResult: cv.callResult, itemSet: cv._itemSet, saved: (cv._doc.blocks || []).slice(), bring: cv.getAttribute('bring-in') };
  const calls = [], asked = []; let answers = [];
  cv.refresh = () => {}; cv.call = async (n, a) => { calls.push({ n, a: JSON.parse(JSON.stringify(a || {})) }); return {}; }; cv._itemSet = () => {}; cv.setAttribute('bring-in', 'off');
  cv.callResult = async (n, a) => { asked.push({ n, a: JSON.parse(JSON.stringify(a || {})) }); await new Promise((r) => setTimeout(r, 400)); return answers.shift() || { ok: false, error: 'no answer' }; };
  const out = {};
  try {
    const KEY = 'kv:probe-' + Date.now().toString(36);
    const EDIT = { cap: 'demo.record.update', args: { id: '{field.name}', kind: 'node' }, field_arg: 'field', value_arg: 'value', fields: ['status', 'vram_used'] };
    cv._doc.blocks = [{ id: 'probe-kv', key: KEY, type: 'kv', state: 'now', size: 'l', anchors: [], content: { title: 'ct132', fields: [['name', 'ct132'], ['status', 'up'], ['vram_used', 12], ['notes', 'x']], edit: EDIT } }];
    cv._html = null; cv.render(cv._doc); await wait(2000);
    const sr = cv.shadowRoot; const it = () => [...sr.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === KEY);
    const val = (f) => { const r = it() && it().querySelector('[data-kv-row="' + f + '"]'); const v = r && r.querySelector('.kv-v'); return v ? v.textContent : null; };
    const inp = () => it() && it().querySelector('[data-kv-in]');
    out.card = !!it(); out.pens = [...(it() ? it().querySelectorAll('[data-act="kvedit"]') : [])].map((b) => b.dataset.kvF);
    const type = async (txt) => { for (const ch of txt) { const q = sr.activeElement && sr.activeElement.dataset && sr.activeElement.dataset.kvIn != null ? sr.activeElement : inp();
      const p = q.selectionStart; q.value = q.value.slice(0, p) + ch + q.value.slice(p); q.setSelectionRange(p + 1, p + 1); q.dispatchEvent(new Event('input', { bubbles: true, composed: true })); await wait(60); } };
    const key = (k) => { const q = inp(); q.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, composed: true, cancelable: true })); };
    const press = (f) => it().querySelector('[data-act="kvedit"][data-kv-f="' + f + '"]').dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true }));
    /* 1. success: status up -> down */
    answers = [{ ok: true, changed: { status: 'down' } }];
    press('status'); await wait(400);
    out.open = { input: !!inp(), focused: sr.activeElement === inp(), value: inp() && inp().value };
    const q0 = inp(); q0.setSelectionRange(0, q0.value.length); q0.value = ''; q0.dispatchEvent(new Event('input', { bubbles: true, composed: true })); await type('down');
    out.typed = { value: inp() && inp().value, focused: sr.activeElement === inp(), sameInput: inp() === q0 };
    key('Enter'); await wait(120);
    out.saving = { shown: val('status'), mark: !!(it() && it().querySelector('[data-kv-row="status"] .kv-st')) };
    await wait(1200);
    out.saved = { value: val('status'), asked: asked[0] || null, update: calls.find((c) => c.n === 'canvas.update') || null };
    /* 2. failure: vram_used 12 -> 99 */
    answers = [{ ok: false, error: 'node is read-only' }];
    press('vram_used'); await wait(400); const q1 = inp(); q1.value = ''; q1.dispatchEvent(new Event('input', { bubbles: true, composed: true })); await type('99'); key('Enter'); await wait(120);
    const pendShown = val('vram_used'); await wait(1200);
    const errEl = it() && it().querySelector('[data-kv-row="vram_used"] .kv-err');
    out.failed = { pendShown, value: val('vram_used'), error: errEl && errEl.textContent, asked: asked[1] || null, updates: calls.filter((c) => c.n === 'canvas.update').length };
    /* 3. Esc: nothing written */
    const nAsk = asked.length, nCall = calls.length;
    press('status'); await wait(400); await type('zz'); key('Escape'); await wait(400);
    out.esc = { input: !!inp(), value: val('status'), asked: asked.length - nAsk, written: calls.length - nCall };
    /* 4. no edit: drawn as before */
    const K2 = KEY + '-plain'; cv._doc.blocks.push({ id: 'probe-kv2', key: K2, type: 'kv', state: 'now', size: 'm', anchors: [], content: { fields: { name: 'ct132', status: 'up' } } });
    cv._html = null; cv.render(cv._doc); await wait(1200);
    const it2 = [...sr.querySelectorAll('.it[data-key]')].find((x) => x.dataset.key === K2);
    out.plain = { card: !!it2, controls: it2 ? it2.querySelectorAll('.kv-pen,[data-kv-f],[data-kv-row]').length : -1 };
  } catch (e) { out.err = String((e && e.stack) || e).slice(0, 400); }
  cv._doc.blocks = keep.saved; cv._html = null; cv.render(cv._doc); cv.refresh = keep.refresh; cv.call = keep.call; cv.callResult = keep.callResult; cv._itemSet = keep.itemSet;
  if (keep.bring == null) cv.removeAttribute('bring-in'); else cv.setAttribute('bring-in', keep.bring);
  return out; }

// The writable form (R.form in widget_element.js): fields over the record read, edits kept, Save only through the
// allowed capabilities.
//   node tests/test_widget_writable_form.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const W = fs.readFileSync(path.join(__dirname, '..', 'vera/widgets/widget_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

// run the renderer from the file's own source
const s = W.indexOf('  const FORM_WRITES = new Set('), e = W.indexOf('    return \'<div class="vw-fm">\'');
const end = W.indexOf('};', e) + 2;
t('the renderer is in the file', s > 0 && e > s && end > e);
const ctx = { R: { kv: () => 'KV-FACE' } };
vm.createContext(ctx);
vm.runInContext(`const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function pick(x, p) { return x == null ? undefined : String(p).split('.').reduce((c, k) => (c == null ? undefined : c[k]), x); }
  ${W.slice(s, end)}; this.FORM_WRITES = FORM_WRITES;`, ctx);
const agent = { id: 'a1', name: 'aide', system_prompt: 'be <brief>', temperature: 0.15, memory_inject: true, domain_caps: ['web.search', 'memory.search'], two_tier: 'fetched' };
const draw = { fields: [{ key: 'system_prompt', label: 'Prompt', type: 'textarea' }, { key: 'temperature', type: 'number' },
  { key: 'memory_inject', type: 'bool' }, { key: 'domain_caps', type: 'list' }, { key: 'two_tier', type: 'select', options: ['off', 'fetched', 'message'] }],
  save: { cap: 'agent.patch', from: { agent_id: 'id' }, edits_as: 'fields' } };
const html = ctx.R.form(agent, 300, { draw, ui: {} });
t('a field per entry, each with its current value', /data-vf="system_prompt"[^>]*>be &lt;brief&gt;<\/textarea>/.test(html)
  && /data-vf="temperature"[^>]*value="0.15"/.test(html) && /data-vf="memory_inject"[^>]* checked/.test(html)
  && /value="web.search, memory.search"/.test(html) && /<option value="fetched" selected>/.test(html));
t('Save is there and off until something changes', /data-vf-save disabled/.test(html));
const html2 = ctx.R.form(agent, 300, { draw, ui: { _edits: { temperature: 0.4 } } });
t('an edit is drawn over the record and marks its row', /vw-fm-row dirty"><span class="vw-fm-l">temperature/.test(html2) && /value="0.4"/.test(html2) && !/data-vf-save disabled/.test(html2) && /1 changed/.test(html2));
t('a form with no fields shows its values as before', ctx.R.form(agent, 300, { draw: {}, ui: {} }) === 'KV-FACE');
t('only agent.patch and agent.create may be saved through', [...ctx.FORM_WRITES].sort().join(',') === 'agent.create,agent.patch');

// the element's wiring
t('form is drawn by R.form, no longer an alias of kv', !/ALIAS = \{[^}]*\bform: 'kv'/.test(W) && /DRAWN = \{[\s\S]*?form: 'values'/.test(W));
t('a form to fill in is never a sample face', W.includes("const fillIn = canon(form) === 'form' && rec.draw && Array.isArray(rec.draw.fields)") && W.includes("&& !fillIn;") && W.includes('&& !editing &&'));
t('edits are kept without a re-render; Save is wired', W.includes("this._sh.querySelectorAll('[data-vf]').forEach((i) => { const on = () => this._formEdit(i);")
  && W.includes("if (sv) sv.addEventListener('click', (ev) => { ev.preventDefault(); this._formSave(); });"));
t('Save refuses a capability not on the list, before any request', /if \(!FORM_WRITES\.has\(cap\)\) \{ say\('saving through ' \+ \(cap \|\| 'nothing'\) \+ ' is not allowed'\); return; \}/.test(W));
t('Save sends only the edits (under edits_as) plus the record fields it names, then reads again', W.includes('if (sv.edits_as) args[String(sv.edits_as)] = ed; else Object.assign(args, ed);') && W.includes('this.read(true);'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

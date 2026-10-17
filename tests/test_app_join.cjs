// DA-07b: a composite's parts joined by field (view.join) - pick a row in one part, the joined parts light the rows that
// hold the same value (the others dim) and a joined part with no rows is outlined when its text names it.
//   node tests/test_app_join.cjs
'use strict';
const fs = require('fs'), path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const HOSTS = [{ name: 'ct126', ip: '10.0.0.26', state: 'up' }, { name: 'ct132', ip: '10.0.0.32', state: 'up' }, { name: 'gpu-1', ip: '10.0.0.40', state: 'down' }];
const CONTS = [{ id: 'a1', image: 'vera', host: 'CT132' }, { id: 'b2', image: 'redis', host: 'ct126' }, { id: 'c3', image: 'ollama', host: 'ct132' }];
const MAN = (join) => ({ panel: 'estate', title: 'Host', read: [{ as: 'hosts', cap: 'nodes.list' }, { as: 'containers', cap: 'docker.ps' }, { as: 'note', cap: 'notes.get' }],
  view: { kind: 'composite', layout: 'stack', join } });
const parts = () => [{ as: 'hosts', cap: 'nodes.list', args: {}, result: { rows: HOSTS } }, { as: 'containers', cap: 'docker.ps', args: {}, result: { containers: CONTS } },
  { as: 'note', cap: 'notes.get', args: {}, result: 'The vera container moved to ct132 last week; ct126 keeps redis.' }];
const draw = (join, pick) => V.BLOCK.app({ app: 'estate.host' }, 'l', 'app:h', { _apps: { 'app:h': { manifest: MAN(join), parts: parts(), result: null, at: Date.now(), ui: { pick } } } });
const sec = (h, as) => { const m = h.match(new RegExp('<section class="vc-app-part([^"]*)" data-app-part="' + as + '">([\\s\\S]*?)</section>')); return m ? { cls: m[1].trim(), body: m[2] } : null; };
const rows = (body) => [...body.matchAll(/<tr data-app-prow="(\d+)"(?: class="([^"]*)")?/g)].map((m) => m[2] || '-').join(' ');
const J = { 'hosts.name': 'containers.host', 'note.text': 'hosts.name' };

{ const h = draw(J, null);
  t('joined parts draw rows to pick', rows(sec(h, 'hosts').body) === '- - -' && rows(sec(h, 'containers').body) === '- - -' && /class="vc-table vc-app-t vc-app-jt" tabindex="0"/.test(h));
  t('a part with no join draws as before', !/data-app-prow/.test(draw({}, null))); }
{ const h = draw(J, { as: 'hosts', i: 1, row: HOSTS[1] });
  t('the picked row is marked, its part named as the source', rows(sec(h, 'hosts').body) === '- pk -' && /jsrc/.test(sec(h, 'hosts').cls));
  t('the joined part lights the rows holding the value (case and spaces aside), dims the rest', rows(sec(h, 'containers').body) === 'jn jd jn', rows(sec(h, 'containers').body));
  t('...and is outlined', /jfocus/.test(sec(h, 'containers').cls));
  t('a joined part with no rows is outlined when its text names the value', /jfocus/.test(sec(h, 'note').cls)); }
{ const h = draw(J, { as: 'containers', i: 1, row: CONTS[1] });
  t('the join works both ways (a container lights its host)', rows(sec(h, 'hosts').body) === 'jn jd jd', rows(sec(h, 'hosts').body)); }
{ const h = draw(J, { as: 'hosts', i: 2, row: HOSTS[2] });
  t('a value nothing holds: every row dims, nothing is outlined', rows(sec(h, 'containers').body) === 'jd jd jd' && !/jfocus/.test(sec(h, 'containers').cls) && !/jfocus/.test(sec(h, 'note').cls)); }
{ const h = draw([['hosts.name', 'containers.host']], { as: 'hosts', i: 0, row: HOSTS[0] });
  t('a join given as a list of pairs', rows(sec(h, 'containers').body) === 'jd jn jd'); }
// the pick: by a row press, the same again lets go, Esc lets go, it follows its row across a read, the first match brought into view
t('a row press picks (composite only)', S.includes("if (st && st.parts && t.closest('[data-app-prow]')) { ev.stopPropagation(); return this._appPick(key, t.closest('[data-app-part]').dataset.appPart, +t.closest('[data-app-prow]').dataset.appProw); }"));
t('the same row again lets go', S.includes('ui.pick = ui.pick && ui.pick.as === as && ui.pick.i === i ? null : { as, i, row: rows[i] };'));
t('Esc lets go', S.includes("if (ev.key !== 'Escape' || !t || !t.closest || !t.closest('.vc-app-comp')) return;") && S.includes('st.ui.pick = null; this._appDraw(it.dataset.key); });'));
t('the pick follows its row when its part is read again', S.includes("const want = appRowKey(st, pk.row); const i = (appRows(r) || []).findIndex((x) => appRowKey(st, x) === want);"));
t('the first lit row is brought into view inside its part (never the desk)', S.includes("const r = sec.querySelector('tr.jn'), pb = sec.querySelector('.vc-app-pb'); if (r && pb) pb.scrollTop = Math.max(0, r.offsetTop - pb.clientHeight / 3);"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

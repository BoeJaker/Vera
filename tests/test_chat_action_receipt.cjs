// DA-08b: an action run from the chat (no desk origin) leaves no item - its receipt is the turn's activity line, which
// goes with the turn's other calls; what it acted on shows the change. Command output, lists and long texts still land.
//   node tests/test_chat_action_receipt.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const cut = (a, b) => { const i = H.indexOf(a), j = H.indexOf(b, i + 1); if (i < 0 || j < 0) throw new Error('anchor missing: ' + (i < 0 ? a : b)); return H.slice(i, j); };

const ctx = {}; vm.createContext(ctx);
vm.runInContext(cut('  const _cvRowsOf=(c)=>{', '  /* A WEB RESULT IS A LIST OF PAGES') + cut('  const _CV_ACT_VERB=', '  /* A DECLARED ANSWER IS READ AS DECLARED') + ';this.f=_cvActionReceipt;', ctx);
const f = ctx.f;

// actions with receipts: no item
t('watchlist.add {ok, symbol}', f('markets.watchlist.add', { ok: true, symbol: 'ETH-USD', exchange: 'coinbase' }) === true);
t('cal.event.upsert {ok, event} (its month lands instead)', f('cal.event.upsert', { ok: true, event: { id: 'e1', title: 'Dentist', start: '2026-10-20T09:00' } }) === true);
t('sys.dev.restart, nothing said', f('sys.dev.restart', null) === true);
t('memory.feedback, a short line', f('memory.feedback', 'recorded') === true);
t('a verb with a tail: notes.add_tag', f('notes.add_tag', { ok: true }) === true);
t('one row is the thing it acted on', f('board.item.upsert', { ok: true, items: [{ id: 'x', title: 'y' }] }) === true);

// not receipts: these still land
t('a read is not an action (markets.watchlist.list)', f('markets.watchlist.list', { ok: true }) === false);
t('a run is not an action (exec.bash.run)', f('exec.bash.run', { ok: true }) === false);
t('command output is a terminal', f('sandbox.restart', { command: 'systemctl restart x', stdout: 'done', rc: 0 }) === false);
t('two or more things are something to look at', f('markets.watchlist.add', { ok: true, watchlist: [{ symbol: 'A' }, { symbol: 'B' }] }) === false);
t('a long text is something to read', f('docs.create', { ok: true, text: 'x'.repeat(700) }) === false);
t('a long string answer too', f('notes.save', 'y'.repeat(700)) === false);
t('a bare name is not an action', f('restart', null) === false);
t('an array answer lands', f('tags.add', [{ a: 1 }]) === false);

// wired into the reply's harvest, before the adapters, for a call that did not fail; desk calls untouched
const hook = cut("    cvHook('capability', '.cap-inline',", "    /* a tool card's insides");
const iR = hook.indexOf("if(cap&&!cap.err&&_cvActionReceipt(cname, cres)) return;"), iA = hook.indexOf('made = _cvAdapt(cname, cres, cap && cap.args)'), iC = hook.indexOf('_cvLandCalendar(cres.event');
t('the harvest skips a receipt before adapting it, after the calendar lands', iR > 0 && iA > iR && iC > 0 && iC < iR);
t('_cvAdapt itself is unchanged (the desk reads through it)', !cut('  function _cvAdapt(capName, result, args){', '  const _cvCapTerm=').includes('_cvActionReceipt'));
// and the receipt is seen: the call's activity line, which leaves once the turn's calls are done (a failure stays)
t('the call has its activity line', H.includes('return _cvActStart(el, capName, coercedArgs);') && H.includes("h.call.peek=_cvActPeek(content);"));
t('the activity item goes when all is done, stays on a failure', H.includes("if(!run&&!bad){ if(a.added){") && H.includes("canvas.remove',{ session_id:SID, key:'activity:'+mid }"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

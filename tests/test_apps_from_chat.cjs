// DA-06c: an app opens on the desk from the chat - [[app:<id> key=value]] in a reply, /app <id> key=value, and a panel
// inside the chat page (data-app on any element, through vera-panel.js) - the same act as the Canvas menu's.
//   node tests/test_apps_from_chat.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const P = fs.readFileSync(path.join(R, 'vera', 'vera-panel.js'), 'utf8');
const M = fs.readFileSync(path.join(R, 'vera', 'markets', 'markets_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the placeholder
const re = new RegExp((H.match(/const APP_PLACEHOLDER_RE=\/(.+)\/gi;/) || [])[1] || 'x', 'gi');
const m1 = [...'see [[app:markets.symbol symbol=ETH-USD]] and [[app:estate.nodes]]'.matchAll(re)];
t('[[app:id args]] and [[app:id]] are read', m1.length === 2 && m1[0][1] === 'markets.symbol' && m1[0][2] === 'symbol=ETH-USD' && m1[1][1] === 'estate.nodes');
// the args: key=value (quoted too) or JSON
const a0 = H.indexOf('  function _cvAppArgsOf(s){'), a1 = H.indexOf('  function _cvAppsPromptLine(){');
const ctx = {}; vm.createContext(ctx); vm.runInContext(H.slice(a0, a1) + ';this.f=_cvAppArgsOf;', ctx);
t('args as key=value', JSON.stringify(ctx.f('symbol=BTC-USD days=30')) === '{"symbol":"BTC-USD","days":"30"}');
t('quoted values', ctx.f('title="Q4 review" where=office').title === 'Q4 review');
t('or JSON', ctx.f('{"symbol":"ETH-USD"}').symbol === 'ETH-USD' && JSON.stringify(ctx.f('')) === '{}');
// the dispatcher, the stream, the prompt, /app
t('the reply dispatcher collects and runs it', H.includes("matches.push({idx:m.index,end:m.index+m[0].length,type:'app',groups:[m[1],m[2]||'']});") && H.includes("}else if(match.type==='app'){") && H.includes("_cvOpenApp(appId, appArgs).then(r=>placeholder.classList.add(r===false?'error':'done')).catch(()=>placeholder.classList.add('error'));"));
t('while streaming it is a badge, not markup', H.includes("t=t.replace(new RegExp(APP_PLACEHOLDER_RE.source,'gi'),(_,n)=>stash(_streamBadge('panel','app: '+n)));"));
t('the directive test knows app:', H.includes('/\\[\\[(?:cap:|dag:|panel:|app:|loop\\b|'));
t('the prompt says it can and lists the apps', H.includes('${_cvAppsPromptLine()}') && H.includes('open an APP on the desk'));
t('/app, with its argument checked and the list offered', H.includes("{name:'app', alias:['apps'], arg:'app',") && H.includes("appendSys('<code>'+esc(id)+'</code> needs '") && H.includes("if(type==='app'){ _cvAppsLoad();"));
t('closed canvas: opened first', H.includes("togglePage('canvas');") && H.includes("await _cvMenuAct({ a:'app', app:id, args:args||{} });"));
// a panel inside the chat page
t('the chat answers a panel that asks, and opens what it asks for', H.includes("if(d.type==='vera:app:hello'&&ev.source){ try{ ev.source.postMessage({ type:'vera:app:ready' }, location.origin); }catch(_){} return; }") && H.includes("if(d.type!=='vera:app:open'||!d.app) return;"));
t('the panel side: hidden until its host answers, a click asks', P.includes("css.textContent = 'html:not(.vp-desk) [data-app]{display:none!important}';") && P.includes("host.postMessage({ type: 'vera:app:hello' }, location.origin)") && P.includes("type: 'vera:app:open', app: b.getAttribute('data-app')"));
t('the markets Watchlist has its "on the desk" button', M.includes('data-app="markets.watchlist" title="Open the Watchlist app on the desk"'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

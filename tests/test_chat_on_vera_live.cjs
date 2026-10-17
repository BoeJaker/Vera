// DA-03c: the chat page's live listeners are on its one channel (window.VeraLive): a lookup follows its own call's trace
// (the page makes the trace id and sends it with the call), the memory-use verdicts are this session's, the graph bus
// subscribes per prefix. Without VeraLive each keeps its old socket.   node tests/test_chat_on_vera_live.cjs
const fs = require('node:fs'), path = require('node:path');
const R = path.join(__dirname, '..');
const C = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const G = fs.readFileSync(path.join(R, 'vera', 'vera_graph.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the chat page loads /ui/live.js after the widget surface', C.indexOf('<script src="/ui/live.js"></script>') > C.indexOf('<script src="/ui/widgets/widget_element.js"></script>') && C.indexOf('<script src="/ui/widgets/widget_element.js"></script>') > 0);
t('a call carries the page\'s own trace id', C.includes("const _tid='t-'+String(SID||'page')+'-'+Date.now().toString(36)+'-'+(++_capTraceN);") && C.includes("body:JSON.stringify({name:capName,arguments:coercedArgs,session_id:SID||'',trace_id:_tid})});"));
t('a lookup subscribes to that trace and waits for the socket before the call goes out', C.includes("_lookSub=VeraLive.subscribe({ trace:[_tid] }, (m)=>{ if((m.event||m.type)==='info.seek.step') _cvLookStep(_look, m); }); await VeraLive.ready(1500);"));
t('its subscription closes after the answer (a stage may trail it)', C.includes("if(_lookSub&&_runEnd!=='pending') setTimeout(()=>{ try{ _lookSub.close(); }catch(_){} }, 1500);"));   // a job's run keeps it till its last step (DA-04c)
t('with VeraLive the question-matching socket is not opened', C.includes("_cvLookDraw(h, true); if(!window.VeraLive) _cvLookListen(); return h;"));
t('the question-matching path leaves a traced lookup alone', C.includes("x.state==='running'&&!x.tid&&x.q&&"));
t('one step function for both paths', (C.match(/_cvLookStep\(/g) || []).length >= 3 && C.includes('function _cvLookStep(h, m){'));
t('memory-use verdicts: this session\'s, filtered on the server', C.includes("_ctxUseSub=VeraLive.subscribe({ types:['memory.feedback'], sid:SID }, (m)=>{ if(String(m.session_id||m.sid||'')===String(SID||'')) _ctxUseNote(m); }); return; }"));
t('the graph bus subscribes per prefix on VeraLive when the page has it', G.includes("var live = window.VeraLive.subscribe({ types: [prefix] }, function(ev){ try { cb(ev); } catch(_){} });") && G.includes('try { live.close(); } catch(_){}'));
t('without VeraLive the graph bus keeps its own socket', G.includes('ensureWs();') && G.includes("ws = new WebSocket(base + '/ws/mcp');"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

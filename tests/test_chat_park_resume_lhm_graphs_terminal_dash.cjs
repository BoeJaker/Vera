// 2026-10-03: a parked chat frame comes back with its transcript; the LHM's small graphs have no ground with blocks off
// and keep their controls behind ☰; the LHM terminal survives the menu's redraws; the dashboard's tiles lose the
// "fine" dot, gain a larger title, and scroll with quiet scrollbars only when there is something to scroll
//   node tests/test_chat_park_resume_lhm_graphs_terminal_dash.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), T = R('vera/remote/vera-terminal.js'), LG = R('vera/loop_graph_element.js'), W = R('vera/widgets/widget_element.js'), HAR = R('vera/capability_orchestration.html');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const i = H.indexOf('async function _resumeSession(){'), rs = H.slice(i, i + 2200);
t("a reloaded (parked) chat asks for its own session's messages, not the unawaited history list", rs.includes("if(empty && !known && SID){ try{ const r=await api('/memory/search','POST',{query:'',limit:3,record_type:'message',category:'chat',session_id:SID});")
  && rs.indexOf('known=(r&&Array.isArray(r.results)') < rs.indexOf('await loadSession(SID);'));

t('the terminal is torn down only if it is not back within a moment (a move keeps its shell)', T.includes("this._dropT = setTimeout(() => { this._dropT = 0; if (!this.isConnected) this.destroy(); }, 1500);")
  && T.includes("if (this._dropT) { clearTimeout(this._dropT); this._dropT = 0; this._doFit(true); if (this._hadFocus) this.focus(); return; }"));

t('blocks off: the docked graphs have no ground', H.includes('html[data-blocks="off"] #rightRail.lhm-host .lhm-quick .lhm-dk.dk-graph .lhm-dk-b{background:transparent!important;box-shadow:none!important}')
  && H.includes('html[data-blocks="off"] #rightRail.lhm-host .lhm-quick .lhm-dk.dk-graph vera-loop-graph{--lg-bg:transparent}'));
t('a docked graph keeps its controls behind ☰', H.includes("bg.className='dk-burger'; bg.textContent='\\u2630';") && H.includes(':not(.dk-tools) :is(#memGraphCtrl,#dagGraphCtrl,#memLegend,.vg-header,.vg-bottom-area,.vg-legend){display:none!important}'));
t('...the loop graph hides its own buttons on tools="hidden"', LG.includes(':host([tools="hidden"]) .bar .btns,:host([tools="hidden"]) .legend{display:none}') && H.includes("if(on) lg.removeAttribute('tools'); else lg.setAttribute('tools','hidden');"));

t('the dashboard drops the "fine" dot, keeps warn/err', HAR.includes('.dash-grid .w-head .w-dot:not(.warn):not(.err){display:none}'));
t('...titles a step larger', HAR.includes('.dash-grid .w-head .w-title{font-size:calc(var(--label-size,9px) + 1.5px)}'));
t('...thin, quiet scrollbars on the board and inside every widget', HAR.includes('.dash-grid,.dash-grid *{scrollbar-width:thin;') && W.includes('*{scrollbar-width:thin;scrollbar-color:var(--bd2,var(--border2,rgba(255,255,255,.16))) transparent}'));
t('...and a face a few pixels taller than its box does not scroll', W.includes("e.classList.toggle('vw-fit', over > 0 && over <= 6);") && W.includes('.vw-fit{overflow:hidden!important}'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

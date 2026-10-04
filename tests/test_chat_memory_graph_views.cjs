// The chat LHM's memory graph: its view picker and expand sit in its section head, always in reach.
//   node tests/test_chat_memory_graph_views.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera/chat/chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the memory section head gets its controls when docked', H.includes("if(p.id==='paneMemGraph') _memHeadControls(w); } }"));
t('the picker lists vera-graph\'s own registered modes after Graph', H.includes("return [{ id:'graph', label:'Graph' }].concat(ms.map(m=>({ id:String(m.id), label:String(m.label||m.id) })));"));
t('picking a view drives vera-graph\'s setMode and is kept per browser', H.includes("try{ localStorage.setItem(_MEMVIEW_KEY, id||'graph'); }catch(_){}") && H.includes("_memVG.setMode(id||'graph');"));
t('the kept view is applied after every draw', /pane\.classList\.add\('vg'\);\s*_memVGCount=nodes\.length;\s*_memViewApply\(\);\s*return true;/.test(H));
t('an empty session\'s graph stays plain (no parse error from a view with nothing to draw)', H.includes("if(!_memVGCount){ if(_memVG.getMode()!=='graph') _memVG.setMode('graph'); }")
  && H.includes("(_memVGCount||(id||'graph')==='graph')"));
t('expand grows the menu in place and shows memory in full', H.includes('_ctxGrow(true);') && H.includes("_ctxCol.mix('memory','all');"));
t('the controls are visible without ☰ (styled in the head, not in vera-graph\'s hidden header)', H.includes('#rightRail.lhm-host .lhm-quick .lhm-dk.dk-graph .dk-view{margin-left:auto;'));
t('colours only from the theme\'s names', !/\.dk-(view|grow)[^{]*\{[^}]*#[0-9a-f]{3,6}(?![^(]*\))/i.test(H.replace(/var\([^)]*\)/g, 'V')));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

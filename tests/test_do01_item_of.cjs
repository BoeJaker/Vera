// DO-0.1 step 1 (one resolver): an answer's desk item is made in ONE place - VeraWidget.itemOf in vera/widgets/present.js (moved
// unchanged from the chat page's _cvPresented; tools/probe_do01_itemof.py found the items identical on 117 real answers).
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const P = R('vera/widgets/present.js'), H = R('vera/chat/chat_panel.html');
t('present.js makes the item and exports it', P.includes('  function itemOf(capName, result, args, opts){') && P.includes('Object.assign(W, { present, presentations, presentationOf, itemOf, jsonItem, calMonth,'));
t('the chat asks it - its own copy is gone', H.includes('return W.itemOf(capName, result, args, opts);') && !H.includes("case 'records': { const items=listOf(r0.data,") && !H.includes("const widget=(r)=>({ kind:'widget', n:String(r.title||title)"));
t("the diary's month is shared too", P.includes('const calMonth=(evs)=>{') && H.includes('const _cvCalMonth=(evs)=>window.VeraWidget.calMonth(evs);'));
t('the moved code needs nothing of the chat page', (() => { const i = P.indexOf('function itemOf('), j = P.indexOf('Object.assign(W, {', i); return !/\b_cv\w+|\bwindow\b|\bdocument\b/.test(P.slice(i, j)); })());
// step 2a: the json tree item has one builder; the chat's json rule and itemOf both use it
t('step 2a: one json tree item - present.js builds it, the chat rule asks it', P.includes("const jsonItem=(cap, c)=>({ kind:'json', n:cap||'result', k:'json', col:'#94a3b8', d:'data', content:{ title:String(cap||''), data:c } });") &&
  P.includes("if(!W.presentationOf(cap)&&pr.records[0]&&pr.records[0].form==='json') return jsonItem(cap, result);") && H.includes("(c,ctx)=>window.VeraWidget.jsonItem((ctx&&ctx.cap)||'', c));") && !H.includes("(c,ctx)=>({ kind:'json', n:(ctx&&ctx.cap)||'result'"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

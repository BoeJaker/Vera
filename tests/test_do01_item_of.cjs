// DO-0.1 step 1 (one resolver): an answer's desk item is made in ONE place - VeraWidget.itemOf in vera/widgets/present.js (moved
// unchanged from the chat page's _cvPresented; tools/probe_do01_itemof.py found the items identical on 117 real answers).
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const P = R('vera/widgets/present.js'), H = R('vera/chat/chat_panel.html');
t('present.js makes the item and exports it', P.includes('  function itemOf(capName, result, args, opts){') && P.includes('Object.assign(W, { present, presentations, presentationOf, itemOf, calMonth,'));
t('the chat asks it - its own copy is gone', H.includes('return W.itemOf(capName, result, args, opts);') && !H.includes("case 'records': { const items=listOf(r0.data,") && !H.includes("const widget=(r)=>({ kind:'widget', n:String(r.title||title)"));
t("the diary's month is shared too", P.includes('const calMonth=(evs)=>{') && H.includes('const _cvCalMonth=(evs)=>window.VeraWidget.calMonth(evs);'));
t('the moved code needs nothing of the chat page', (() => { const i = P.indexOf('function itemOf('), j = P.indexOf('Object.assign(W, {', i); return !/\b_cv\w+|\bwindow\b|\bdocument\b/.test(P.slice(i, j)); })());
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

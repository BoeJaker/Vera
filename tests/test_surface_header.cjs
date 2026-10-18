// DO-0.5 (desk output redesign; vera-c7's usage counter B0.11 R0): calls say where they came from -
// X-Vera-Surface: <desk|dashboard|panel:<id>|chat|lhm>; trigger=<person|auto>.
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const C = R('vera/canvas/canvas_element.js');
t('the canvas names its surface: desk, or the Canvas tab', C.includes("const surfOf = (el) => { try { return el && el.deskAlign && !el.deskAlign() ? 'panel:canvas' : 'desk'; } catch (e) { return 'desk'; } };") && C.includes("const surfHdr = (el) => ({ 'X-Vera-Surface': surfOf(el) + '; trigger=' +"));
t('every /mcp/call from the canvas carries it', (C.match(/fetch\(base \+ '\/mcp\/call', \{ method: 'POST'(, keepalive: true)?, headers: Object\.assign\(\{ 'Content-Type': 'application\/json' \}, surfHdr\(this\)\)/g) || []).length === 5 && !/fetch\(base \+ '\/mcp\/call', \{ method: 'POST'(, keepalive: true)?, headers: \{ 'Content-Type'/.test(C));
t('the canvas poll carries it', C.includes("{ headers: Object.assign({ Accept: 'application/json' }, surfHdr(this)) }"));
t('trigger is the browser user activation', C.includes("root.navigator.userActivation.isActive ? 'person' : 'auto'"));
t('VeraLive calls carry it (the caller or the page says the surface)', R('vera/ui/live.js').includes("'X-Vera-Surface': (opts.surface || root.__VERA_SURFACE || 'chat') + '; trigger=' +"));
t('the market chart says desk inside the canvas, else the markets panel', R('vera/markets/markets_chart.js').includes("'panel:markets'") && R('vera/markets/markets_chart.js').includes("'X-Vera-Surface': surf + '; trigger=' +"));
const H = R('vera/chat/chat_panel.html'), O = R('vera/capability_orchestration.html');
t('the chat page api() carries chat', H.includes("'X-Vera-Surface':'chat; trigger='+"));
t('the chat page wrapper: lhm for ?only=menu, else chat; never overrides', H.includes(String.raw`var SURF = (/[?&]only=menu\b/.test(location.search) ? 'lhm' : 'chat');`) && H.includes("if (!h.has('X-Vera-Surface'))"));
t('the harness page wrapper says lhm', O.includes("var SURF = 'lhm';") && O.includes('wrapped._veraSurf = true; window.fetch = wrapped;'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

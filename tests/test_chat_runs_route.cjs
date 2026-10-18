// WSU-61: the chat's runs route well, reach the desk and are dotted (owner 2026-10-11). Measured on a harness scene
// (tools/snip_runs_scene.js): 4 runs, all to their items, 0 crossings, lanes between the text and the desk.
//   node tests/test_chat_runs_route.cjs
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('on the desk a run goes on from the edge to its item, along a band no other card is in, else stops at the edge',
  H.includes('const band=lx>ex+8?cands.find(y=>hFree(y, ex, lx)&&vFree(lx, y, ty)):null;')
  && H.includes('if(band!=null) pts=[{x:M.right, y:my},{x:gx, y:my},{x:gx, y:band},{x:lx, y:band},{x:lx, y:ty},{x:tx, y:ty}]'));
t('the lanes run where no reply\'s text is (from the replies\' right edge, never past the transcript)',
  H.includes("const chan0=Math.max(txtR, Math.min.apply(null, [edge0].concat(routes.map(r=>r.M.right)))), chan1="));
t('a run leaves its placeholder under the chip\'s row, not through the chip beside it', H.includes('r.my=Math.min(MB.bottom-1, q.bottom+3); r.fromChip=true;'));
t('the canvas runs are dotted', H.includes('stroke-linejoin="round" stroke-dasharray="1.5 4">'));
t('the context runs are dotted', H.includes('#ctxRunsOverlay line.run{stroke-dasharray:1.5 4}'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

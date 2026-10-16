const fs = require('node:fs');
const path = require('node:path');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

let failed = 0;
function test(name, condition) {
  console.log((condition ? 'ok - ' : 'FAIL - ') + name);
  if (!condition) failed++;
}

test('the desk is the new default while held and strict remain selectable',
  /cfgCvAlign:\['align','desk'\]/.test(src) &&
  src.includes('<option value="held">Held in view</option>') &&
  src.includes('<option value="strict">Strict — level with the turn</option>'));
test('all adaptive desk choices are explicit persisted canvas settings',
  ["cfgCvBack:['back','column']", "cfgCvPanel:['panel-layer','beside']", "cfgCvBring:['bring-in','on']",
   "cfgCvArrange:['desk-arrange','auto']", "cfgCvRoom:['panel-room','on']"].every((s) => src.includes(s)));
test('the floating chat has a visible grip and deterministic dock targets',
  src.includes('id="chatGrip"') && src.includes('id="chatDockHint"') &&
  src.includes("d.hint=(e.clientX-d.c.left<60)?'left':((d.c.right-e.clientX<60)?'right':'')"));
test('floating geometry is normalized in storage and projected into canvas coordinates',
  src.includes("const _CHAT_POS_KEY='vera_chat_float'") &&
  src.includes("cv.setAttribute('chat-rect',v)") && src.includes("localStorage.setItem(_CHAT_POS_KEY"));
test('graph-behind reuses the one context graph rather than cloning it',
  src.includes("if(_ctxCol&&_ctxCol.parentNode) _ctxCol.remove()") &&
  src.includes("if(_ctxGrown&&_deskBehind()) return _deskBackHost()"));
test('desk panels resolve their registered source before becoming a behind-layer item',
  src.includes("const p=_resolvePanel(id)") && src.includes("src=await _panelSrcAsync(p)") &&
  src.includes("layer?{ layer }:{}"));
test('turn relevance changes desk order only on the reply apply path',
  src.includes("if(o.apply!==false&&r&&r.ok&&cv&&typeof cv.bringIn==='function'") &&
  src.includes("_cvCfg('cfgCvBring')==='on'"));
test('split-frame menu requests open the canvas before refreshing it',
  src.includes("m.act==='pageOpen'") && src.includes("m.act==='canvasRefresh'"));
/* Explode on the desk is the turn scene (Cards, Front, Iso) like everywhere else: the desk lattice that took its place
   broke those modes (owner, 2026-10-10), and the design's exploded desk comes later as a mode of its own */
test('explode on the desk opens the turn scene, not a desk lattice in its place',
  !src.includes('_deskXpl') && !src.includes('id=\'deskXpl\'') &&
  src.includes("function _xplToggle(){ if(_xpl.on) _xplClose(); else explode(true); }"));
test('context and canvas activity runs stand down behind graph and panel layers',
  src.includes("document.body.dataset.graphBehind==='1'") &&
  src.includes(".it[data-desk-face^=\"layer-\"]"));

console.log(failed ? failed + ' FAILED' : '10 passed');
process.exit(failed ? 1 : 0);

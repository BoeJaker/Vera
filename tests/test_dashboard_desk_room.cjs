// The adaptive desk may place a dashboard panel behind its cards. The dashboard
// must consume only its parent frame's bounded obstacle message and restore its
// authored grid when the desk stops asking it to make room.
const fs = require('node:fs');
const path = require('node:path');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');

let failed = 0;
function test(name, condition) {
  console.log((condition ? 'ok - ' : 'FAIL - ') + name);
  if (!condition) failed++;
}

test('each initialized dashboard registers for late obstacle delivery',
  src.includes('ROOM_GRIDS.push(grid); roomLate(grid);'));
test('the shared layout kernel is loaded from Vera UI only when needed',
  src.includes("s.src = '/ui/desk-layout.js'") && src.includes('window.VeraDesk.makeRoom'));
test('only the embedding parent may publish desk obstacles',
  src.includes("d.type !== 'vera:desk:obstacles' || ev.source !== window.parent"));
test('obstacles are bounded frame shares converted into grid-local coordinates',
  src.includes('b.l * vw - g.left') && src.includes('b.t * vh - g.top') &&
  src.includes('b.r * vw - g.left') && src.includes('b.b * vh - g.top'));
test('the kernel receives stable tile homes rather than a dense reflow',
  src.includes('c: x.c, r: x.r') && src.includes('VeraDesk.makeRoom'));
test('clearing restores the authored row and column and removes transient classes',
  src.includes("w.style.gridColumn = w.dataset.roomCol || ''") &&
  src.includes("w.style.gridRow = w.dataset.roomRow || ''") &&
  src.includes("w.classList.remove('vd-room-compact', 'vd-room-moved')"));
test('desk placement remains transient and is not written to dashboard state',
  src.includes("grid.dataset.roomGrid = cols + ' cols") &&
  !/state\.(sizes|grid|order|hidden)[^\n]*room/.test(src));
test('roomFor is exposed for deterministic UI integration tests',
  /slotAt: slotAt, roomFor: roomFor \}/.test(src));

console.log(failed ? failed + ' FAILED' : '8 passed');
process.exit(failed ? 1 : 0);

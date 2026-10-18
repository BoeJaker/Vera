const fs = require('node:fs');
const path = require('node:path');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');

let failed = 0;
function test(name, condition) {
  console.log((condition ? 'ok - ' : 'FAIL - ') + name);
  if (!condition) failed++;
}

test('desk-specific inputs are explicit observed attributes',
  /'chat-rect', 'desk-arrange', 'panel-room'/.test(src));
test('a narrow desk falls back without pretending adaptive placement is active',
  src.includes('const DESK_MIN_W = 520') &&
  src.includes("this.toggleAttribute('desk-narrow', narrow)") &&
  /deskAlign\(\).*align.*desk.*desk-narrow/.test(src));
test('manual placement is stored as normalized coordinates scoped to the canvas',
  src.includes("'vera:canvas:deskpos:' + this.canvasId") &&
  src.includes('fx: +(d.cx / g.W).toFixed(4)') && src.includes('fy: +(d.cy / g.H).toFixed(4)'));
test('the layout kernel owns arrangement, collision separation, and responsive sizing',
  src.includes("D.arrange('orbit', R, info, desk)") && src.includes('D.layoutMode(mode, R, info, desk') && src.includes('D.separate(withStrip(bySize(plan)), R') && src.includes('D.sizeAt(R, it.x, it.y'));
test('panel obstacle messages contain normalized rectangles and no panel content',
  src.includes("type: 'vera:desk:obstacles', rects: shares, makeRoom: on") &&
  src.includes('l: +Math.max(0, (r.left - F.left) / F.width).toFixed(4)') &&
  src.includes('b: +Math.min(1, (r.bottom - F.top) / F.height).toFixed(4)'));
test('panel obstacle delivery targets only live panel frames',
  src.includes('Object.keys(this._live).forEach((key) =>') &&
  src.includes("lv.querySelector('iframe')") &&
  src.includes("fr.contentWindow.postMessage({ type: 'vera:desk:obstacles'"));
test('live overlays compensate for UI scale and clip to their owning card',
  src.includes('(B.width / body.offsetWidth)') && src.includes('el.style.clipPath') &&
  src.includes("const z = m.z != null ? m.z : (card.style.zIndex || '');"));
test('layer state is browser-local presentation state',
  src.includes("const LAYER_KEY = 'vera:canvas:layers'") &&
  src.includes('localStorage.setItem(LAYER_KEY') && src.includes('setLayer(key, mode)'));
test('the back slot is exposed beneath cards without changing canvas records',
  src.includes('<slot name="back"></slot>') && src.includes('part="back"'));

console.log(failed ? failed + ' FAILED' : '9 passed');
process.exit(failed ? 1 : 0);

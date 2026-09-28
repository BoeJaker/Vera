// The one ISO projection (UI redesign m1 foundations; vera/ui/iso.js) — the numeric checker the design carried:
// a projected point lands where the axonometry says; a box has three faces; a fitted scene never exceeds its frame;
// a route changes one ground coordinate per leg; the floor disc is a plain vertical squash.
//   node tests/test_iso_lib.mjs
import fs from 'node:fs'; import path from 'node:path'; import vm from 'node:vm'; import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8');
const ctx = { window: {}, console };
vm.runInNewContext(src, ctx);
const ISO = ctx.window.VeraISO;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

t('api surface', ['proj', 'box', 'face', 'scene', 'fit', 'px', 'shade', 'edgeOfBox', 'route', 'segs', 'ring', 'discTf', 'frame', 'isoFitK', 'isoScene'].every(k => typeof ISO[k] === 'function'));
// tilt 30 · azim 45 · k 10: u=1 → x = cos45·10 = 7.071, y = sin45·10·sin30 = 3.536
const P = ISO.proj(30, 45, 10);
const p = P(1, 0, 0);
t('proj u axis', near(p[0], 7.0710678, 1e-5) && near(p[1], 3.5355339, 1e-5), JSON.stringify(p));
const z = P(0, 0, 1);
t('proj z is up (negative screen y) by k·cos(tilt)', near(z[0], 0) && near(z[1], -10 * Math.cos(Math.PI / 6), 1e-6), JSON.stringify(z));
t('zpx keeps z in pixels', near(ISO.proj(30, 45, 10, true)(0, 0, 1)[1], -Math.cos(Math.PI / 6), 1e-6));
const b = ISO.box(P, 0, 0, 0, 1, 1, 1);
t('box has three faces of four points', ['left', 'right', 'top'].every(k => b[k].length === 4));
const f = ISO.face(b.top);
t('face is a bounding box with a local clip-path', f.w > 0 && f.h > 0 && /^polygon\(/.test(f.cp) && f.cp.split(',').length === 4);
const boxes = [{ u:0, v:0, z:0, w:1, d:1, h:1, col:'#123' }, { u:2, v:1, z:0, w:1, d:1, h:2, col:'#456' }];
const sc = ISO.scene(P, boxes);
t('scene paints three faces per box, back to front', sc.length === 6 && sc[0].k === 'l' && sc[2].k === 't');
t('faces are shaded by side', sc[0].col.includes('56%') && sc[1].col.includes('76%') && sc[2].col === '#123');
// fit centres the bounding box in W×H
const copy = sc.map(x => Object.assign({}, x)); ISO.fit(copy, 300, 200);
let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
copy.forEach(q => { x0 = Math.min(x0, q.x); y0 = Math.min(y0, q.y); x1 = Math.max(x1, q.x + q.w); y1 = Math.max(y1, q.y + q.h); });
t('fit centres', near((x0 + x1) / 2, 150, 1e-6) && near((y0 + y1) / 2, 100, 1e-6), [x0, x1, y0, y1].join(','));
t('px strings carry the paint order', ISO.px(copy)[3].i === 3 && /px$/.test(ISO.px(copy)[0].x));
// the fit checker: a fitted scene sits inside W×H, inside the margins, and the cap holds
const W = 276, H = 160;
const k2 = ISO.isoFitK(boxes, 27, W, H, null, 30, 45, {});
const fitted = ISO.scene(ISO.proj(30, 45, k2), boxes);
let fx0 = Infinity, fy0 = Infinity, fx1 = -Infinity, fy1 = -Infinity;
fitted.forEach(q => { fx0 = Math.min(fx0, q.x); fy0 = Math.min(fy0, q.y); fx1 = Math.max(fx1, q.x + q.w); fy1 = Math.max(fy1, q.y + q.h); });
t('isoFitK: the scene fills the frame inside its margins', (fx1 - fx0) <= W - 20 + 1e-6 && (fy1 - fy0) <= H - 8 - 18 + 1e-6 && (Math.abs((fx1 - fx0) - (W - 20)) < 1e-6 || Math.abs((fy1 - fy0) - (H - 26)) < 1e-6), [fx1 - fx0, fy1 - fy0].join(','));
t('isoFitK: the magnification cap holds', ISO.isoFitK([{ u:0, v:0, z:0, w:.1, d:.1, h:.1, col:'#000' }], 10, W, H, null, 30, 45, { max:3 }) === 30);
const sceneOut = ISO.isoScene(boxes, 27, W, H);
t('isoScene returns px-ready faces within the frame', sceneOut.length === 6 && sceneOut.every(q => parseFloat(q.x) >= 0 && parseFloat(q.x) + parseFloat(q.w) <= W + 1e-3));
// routes are isometric by construction: every leg changes exactly one of u, v, z
const legsOk = (pts) => pts.slice(1).every((q, i) => { const a = pts[i]; return [0, 1, 2].filter(j => Math.abs(q[j] - a[j]) > 1e-9).length <= 1; });
t('route: forward', legsOk(ISO.route({ u:0, v:0, z:0 }, { u:3, v:1, z:0 }, .1, 3)));
t('route: back', legsOk(ISO.route({ u:3, v:0, z:0 }, { u:0, v:1, z:0 }, .1, 3)));
t('route: climb', legsOk(ISO.route({ u:0, v:0, z:0 }, { u:3, v:1, z:2 }, .1, 3)));
t('segs', ISO.segs([[0, 0], [10, 0], [10, 10]]).map(s => Math.round(s.deg)).join(',') === '0,90');
t('discTf is a vertical squash by sin(tilt)', ISO.discTf(P) === 'scaleY(0.500)');
t('ring has n points on the ground circle', ISO.ring(P, 0, 0, 2, 8).length === 8 && near(ISO.ring(P, 0, 0, 2, 8)[2].u, 0, 1e-9));
t('edgeOfBox leaves the box on the way to the target', JSON.stringify(ISO.edgeOfBox([0, 0], 10, 5, [100, 0])) === '[10,0]');
const fr = ISO.frame(ISO.proj(30, 45, 1, true), { W:120, H:80, stand:true, cx:100, cy:100 });
t('frame: a standing screen has bezel faces and an affine transform', fr.faces.length === 6 && /^matrix\(/.test(fr.tf) && fr.w === '120px');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

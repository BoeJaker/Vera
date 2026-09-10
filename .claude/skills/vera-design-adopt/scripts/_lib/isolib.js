/*@iso-lib*/
/* ISO — the one projection, box, face and router every board shares.
   Defined here once and embedded into each artboard by _inc.mjs between the
   @iso-lib markers, so a fix lands everywhere and no board carries its own
   slightly different copy.

   Model: u,v on the ground, z up. proj() turns that into a screen point; box()
   turns a block into its three visible faces; face() turns a polygon into a
   positioned div with a clip-path, which is how every plate and column is
   actually drawn (CSS 3D never yields screen coordinates — this does).       */
const ISO = (() => {
  const RAD = Math.PI / 180;
  // an axonometric projection. tilt 0 = edge-on, 90 = straight down; azim
  // swings the ground plane. k is px per ground unit.
  // `zpx` = true means z is already in pixels (the chat and canvas scenes
  // measure their layer heights that way); otherwise z is in ground units.
  const proj = (tilt, azim, k, zpx) => {
    const TH = tilt * RAD, AZ = azim * RAD;
    const sT = Math.sin(TH), cT = Math.cos(TH), cA = Math.cos(AZ), sA = Math.sin(AZ);
    const zk = zpx ? 1 : k;
    const f = (u, v, z) => [(u * cA - v * sA) * k, (u * sA + v * cA) * k * sT - z * cT * zk];
    f.tilt = tilt; f.azim = azim; f.k = k;
    return f;
  };
  // the three faces of a box that face the viewer (with azim 25..65)
  const box = (P, u, v, z, w, d, h) => ({
    left:  [P(u, v + d, z), P(u + w, v + d, z), P(u + w, v + d, z + h), P(u, v + d, z + h)],
    right: [P(u + w, v, z), P(u + w, v + d, z), P(u + w, v + d, z + h), P(u + w, v, z + h)],
    top:   [P(u, v, z + h), P(u + w, v, z + h), P(u + w, v + d, z + h), P(u, v + d, z + h)]
  });
  // a screen polygon as a positioned div: bounding box + clip-path in local px
  const face = (pts) => {
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys);
    const x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys);
    return { x:x0, y:y0, w:Math.max(1, x1 - x0), h:Math.max(1, y1 - y0),
      cp:'polygon(' + pts.map((p, i) => (xs[i] - x0).toFixed(1) + 'px ' + (ys[i] - y0).toFixed(1) + 'px').join(',') + ')' };
  };
  const shade = (c, pct) => 'color-mix(in srgb, ' + c + ' ' + pct + '%, #000)';
  // a whole scene of boxes as faces, painted back to front. The default order
  // is by depth on the ground then height; a stacked scene (floors with things
  // on them) passes its own key so a lower floor and everything on it paints
  // before the floor above.
  const scene = (P, boxes, key) => {
    const out = [];
    const k = key || ((b) => (b.u + b.v) * 1000 + b.z);
    // a box may carry a class (an effect: hot, lamp, vu, mv, shine…) and an
    // ordinal `n` its animation is phased by; both ride on its faces
    boxes.slice().sort((a, b) => k(a) - k(b)).forEach((b) => {
      const f = box(P, b.u, b.v, b.z, b.w, b.d, b.h);
      const x = { t:b.t || '', cls:b.cls || '', n:b.n || 0 };
      out.push(Object.assign(face(f.left),  { col:shade(b.col, 56), k:'l' }, x));
      out.push(Object.assign(face(f.right), { col:shade(b.col, 76), k:'r' }, x));
      out.push(Object.assign(face(f.top),   { col:b.col,            k:'t' }, x));
    });
    return out;
  };
  // shift a set of faces/points so their bounding box is centred in W×H
  const fit = (faces, W, H, pad) => {
    pad = pad || 0;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    faces.forEach((f) => { x0 = Math.min(x0, f.x); y0 = Math.min(y0, f.y);
      x1 = Math.max(x1, f.x + f.w); y1 = Math.max(y1, f.y + f.h); });
    const dx = (W - (x1 - x0)) / 2 - x0, dy = (H - (y1 - y0)) / 2 - y0;
    faces.forEach((f) => { f.x += dx; f.y += dy; });
    return { dx, dy };
  };
  // faces with px strings on them, ready for a template
  // `i` is the face's place in paint order, for entrance animations
  const px = (faces) => faces.map((f, i) => Object.assign({}, f, { i,
    x:f.x.toFixed(1) + 'px', y:f.y.toFixed(1) + 'px', w:f.w.toFixed(1) + 'px', h:f.h.toFixed(1) + 'px' }));
  // n points evenly round a circle on the ground, projected
  const ring = (P, cu, cv, r, n, z) => Array.from({ length:n }, (_, i) => {
    const a = (i / n) * Math.PI * 2; return { a, u:cu + Math.cos(a) * r, v:cv + Math.sin(a) * r, p:P(cu + Math.cos(a) * r, cv + Math.sin(a) * r, z || 0) }; });
  // how a circle on the ground looks on screen: rotate by the azimuth, then
  // squash by the tilt — the transform for a CSS disc that lies on the floor
  const discTf = (P) => 'rotate(' + P.azim + 'deg) scaleY(' + Math.sin(P.tilt * Math.PI / 180).toFixed(3) + ')';
  // where a straight line from `from` toward `to` leaves a box centred on `from`
  const edgeOfBox = (from, hw, hh, to) => {
    const dx = to[0] - from[0], dy = to[1] - from[1];
    if (!dx && !dy) return from.slice();
    const s = Math.min(hw / Math.max(1e-6, Math.abs(dx)), hh / Math.max(1e-6, Math.abs(dy)));
    return [from[0] + dx * Math.min(1, s), from[1] + dy * Math.min(1, s)];
  };
  // an orthogonal run in ground coordinates between two anchors. Every leg
  // changes exactly one of u, v, z — so it is isometric by construction.
  // `off` is this run's lane; `vspan` is how deep the ground plane is.
  const route = (A, B, off, vspan) => {
    const back = B.u < A.u - 0.01, climb = Math.abs(A.z - B.z) > 0.5;
    if (climb){
      const vo = -1.15 + off, uc = (A.u + B.u) / 2 + off;
      return [[A.u, A.v, A.z], [A.u, vo, A.z], [uc, vo, A.z], [uc, vo, B.z], [B.u, vo, B.z], [B.u, B.v, B.z]];
    }
    if (back){
      const vo = vspan + 1.12 + Math.abs(off);
      return [[A.u, A.v, A.z], [A.u + 0.47, A.v, A.z], [A.u + 0.47, vo, A.z],
              [B.u - 0.47, vo, B.z], [B.u - 0.47, B.v, B.z], [B.u, B.v, B.z]];
    }
    const um = (A.u + B.u) / 2 + off;
    return [[A.u, A.v, A.z], [um, A.v, A.z], [um, B.v, B.z], [B.u, B.v, B.z]];
  };
  // a polyline of screen points as rotated segments (left/top/len/deg)
  const segs = (pts) => {
    const out = [];
    for (let i = 0; i < pts.length - 1; i++){
      const a = pts[i], b = pts[i + 1], dx = b[0] - a[0], dy = b[1] - a[1];
      out.push({ x:a[0], y:a[1], len:Math.hypot(dx, dy), deg:Math.atan2(dy, dx) * 180 / Math.PI });
    }
    return out;
  };
  // an ISO FRAME: a real HTML element laid onto the plane — a screen standing
  // on the plate (stand:true, the u–z face nearest the viewer) or a sheet lying
  // on it (the u–v face). The projection is affine, so a CSS matrix puts live
  // markup (a terminal, a page, a notebook, a panel) exactly on the face; the
  // bezel and foot under it are ordinary boxes. P is proj(tilt, azim, 1, true):
  // one unit = one px, z in px. `s` scales the whole object. Returns the bezel
  // faces (centred on cx,cy), the element's top-left, and its transform.
  const frame = (P, o) => {
    const s = o.s || 1, W = o.W * s, H = o.H * s;
    const pad = (o.pad == null ? 6 : o.pad) * s, dep = (o.dep == null ? 5 : o.dep) * s;
    const a = P(1, 0, 0), b = P(0, 1, 0), c = P(0, 0, 1);
    const boxes = [];
    let o0, m, corners;
    if (o.stand){
      const ft = (o.foot == null ? 10 : o.foot) * s, d0 = dep;
      boxes.push({ u:W / 2 - 12 * s, v:-3 * s, z:0, w:24 * s, d:12 * s, h:ft, col:o.col2 || 'var(--bd2)' });
      boxes.push({ u:-pad, v:0, z:ft, w:W + 2 * pad, d:d0, h:H + 2 * pad, col:o.col || 'var(--surf3)' });
      o0 = P(0, d0, ft + pad + H);
      m = [a[0], a[1], -c[0], -c[1]];
      corners = [P(0, d0, ft + pad + H), P(W, d0, ft + pad + H), P(W, d0, ft + pad), P(0, d0, ft + pad)];
    } else {
      boxes.push({ u:-pad, v:-pad, z:-dep, w:W + 2 * pad, d:H + 2 * pad, h:dep, col:o.col || 'var(--surf3)' });
      o0 = P(0, 0, 0);
      m = [a[0], a[1], b[0], b[1]];
      corners = [P(0, 0, 0), P(W, 0, 0), P(W, H, 0), P(0, H, 0)];
    }
    const faces = scene(P, boxes, (bx) => bx.z * 10 + bx.u + bx.v);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    faces.forEach((f) => { x0 = Math.min(x0, f.x); y0 = Math.min(y0, f.y); x1 = Math.max(x1, f.x + f.w); y1 = Math.max(y1, f.y + f.h); });
    corners.forEach((p) => { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); });
    const dx = (o.cx == null ? 0 : o.cx - (x0 + x1) / 2), dy = (o.cy == null ? 0 : o.cy - (y0 + y1) / 2);
    faces.forEach((f) => { f.x += dx; f.y += dy; });
    return { faces:px(faces), x:(o0[0] + dx).toFixed(1) + 'px', y:(o0[1] + dy).toFixed(1) + 'px',
      w:o.W + 'px', h:o.H + 'px', bw:x1 - x0, bh:y1 - y0,
      tf:'matrix(' + [m[0] * s, m[1] * s, m[2] * s, m[3] * s].map((v) => v.toFixed(4)).join(',') + ',0,0)' };
  };
  return { proj, box, face, scene, fit, px, shade, edgeOfBox, route, segs, ring, discTf, frame };
})();
/*@/iso-lib*/

/* The exploded scene — the chat's Explode (UI redesign: the Chat & canvas set, Notes/40 §6 P6; the board's
   "ONE EXPLODED SCENE"): not three panes side by side but one pipeline for the session — per turn (a station) what
   the turn READ, the EXCHANGE, what it PRODUCED and where it LANDED on the canvas — with the runs drawn between the
   actual entities. Cards · Front · Iso are three projections of this one scene, not three re-implementations:
     cards — every station as a row of its layers, the cards in flow, the runs between them;
     front — the selected station as a carousel of its layers in depth (the same construction the board uses);
     iso   — the lattice: u = station, v = the layer bands, the plates identical parallelograms in a row, projected
             through the shared ISO projection (/ui/iso.js) when it is there, the classic 30°/45° otherwise.
   Nothing here uses CSS 3D except the front carousel; plates, cards and runs sit at screen coordinates from one
   projection, so a run ends on a card, not near it. The composer stays docked under the scene (the host's).

   <vera-exploded>  API: setScene({turns:[{mid, who, t, text, reply, read:[card], say:[card], made:[card],
                    land:[card]}], sel}) · mode(name) · select(mid) · fit() · state()
   card = {n, d, col, kind, body?, score?, p?, m?, rows?}
   events: vera:xpl:pick {mid, layer, card} · vera:xpl:turn {mid} · vera:xpl:rendered {mode, stations}
   window.VeraExploded = { layout, LAYERS, version } — layout() is pure (node-testable).                        */
(function (root) {
  'use strict';
  const LAYERS = [
    { key: 'read', name: 'read', sub: 'what the turn read', col: 'var(--xp-dv1)' },
    { key: 'say', name: 'the exchange', sub: 'what it said', col: 'var(--xp-ac)' },
    { key: 'made', name: 'produced', sub: 'what it made', col: 'var(--xp-dv2)' },
    { key: 'land', name: 'session canvas', sub: 'where it landed', col: 'var(--xp-dv3)' }];
  const RAD = Math.PI / 180;
  const px = (v) => Math.round(v * 10) / 10;
  const isoP = (tilt, azim) => { const TH = tilt * RAD, AZ = azim * RAD; const sT = Math.sin(TH), cT = Math.cos(TH), cA = Math.cos(AZ), sA = Math.sin(AZ); return (u, v, z) => [u * cA - v * sA, (u * sA + v * cA) * sT - (z || 0) * cT]; };

  /* ── the layout, pure ─────────────────────────────────────────────────────────────────────────────── */
  function layout(scene, mode, W, H, o) {
    o = o || {}; const turns = (scene && scene.turns) || []; mode = mode === 'front' || mode === 'iso' ? mode : 'cards';
    const selIdx = Math.max(0, turns.findIndex((t) => t.mid === (scene && scene.sel))); const sel = turns.length ? Math.max(0, selIdx) : 0;
    const out = { mode, stations: turns.length, sel, plates: [], labels: [], cards: [], edges: [], panels: [], leaders: [], size: { w: W, h: H }, fit: { s: 1, x: 0, y: 0 } };
    const cardsOf = (t, k) => (k === 'say' ? (t.say && t.say.length ? t.say : [{ n: t.text || '', d: (t.who || 'you') + (t.t ? ' · ' + t.t : ''), col: 'var(--xp-ac)', kind: 'note' }].concat(t.reply ? [{ n: t.reply, d: 'aide' + (t.rt ? ' · ' + t.rt : ''), col: 'var(--xp-ac)', kind: 'note' }] : [])) : (t[k] || []));
    const edge = (a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; out.edges.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls: cls || '', title: title || '' }); };
    if (!turns.length) return out;
    if (mode === 'cards') {
      // every station a row: its four layers as columns, the cards in flow, the runs across the gutters
      const GUT = 26, PADX = 24, colW = Math.max(180, (W - PADX * 2 - GUT * 3) / 4), CH = 54, CG = 8; let y = 24;
      turns.forEach((t, si) => {
        const n = Math.max.apply(null, LAYERS.map((L) => cardsOf(t, L.key).length).concat([1]));
        const rowH = 34 + n * (CH + CG) + 18;
        out.plates.push({ si, x: px(PADX - 10), y: px(y - 8), w: px(W - PADX * 2 + 20), h: px(rowH), cls: si === sel ? 'on' : '', mid: t.mid });
        out.labels.push({ si, x: px(PADX), y: px(y), n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 60), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
        const pos = {};
        LAYERS.forEach((L, li) => {
          const x = PADX + li * (colW + GUT); const list = cardsOf(t, L.key);
          out.labels.push({ si, x: px(x), y: px(y + 18), n: L.name, k: String(list.length), cls: 'layer ' + L.key, col: L.col });
          pos[L.key] = list.map((c, ci) => { const cy = y + 34 + ci * (CH + CG); out.cards.push({ id: t.mid + ':' + L.key + ':' + ci, mid: t.mid, si, layer: L.key, ci, x: px(x), y: px(cy), w: px(colW), h: px(CH), card: c, col: c.col || L.col }); return { x: x, y: cy, w: colW, h: CH }; });
        });
        // the runs: every read into the exchange, the exchange into every product, every product to what landed
        const mid = (r) => ({ x: r.x + r.w, y: r.y + r.h / 2 }), left = (r) => ({ x: r.x, y: r.y + r.h / 2 });
        const ex = pos.say[0]; if (ex) { pos.read.forEach((r) => edge(mid(r), left(ex), 'var(--xp-dv1)', 'in', 'read by this turn')); pos.made.forEach((r) => edge(mid(ex), left(r), 'var(--xp-dv2)', 'out', 'produced by this turn')); }
        pos.made.forEach((r, i) => { const l = pos.land[i] || pos.land[0]; if (l) edge(mid(r), left(l), 'var(--xp-ac)', 'link', 'this became a canvas item'); });
        y += rowH + 14;
      });
      out.size = { w: W, h: y };
      return out;
    }
    if (mode === 'front') {
      // the selected station as a carousel of its layers in depth: the same construction the board uses
      const t = turns[sel]; const PDX = Math.max(300, Math.min(520, (W - 80) / 3.2)), PDZ = 126, li0 = o.layer == null ? 1 : o.layer;
      LAYERS.forEach((L, li) => { const off = li - li0, a = Math.abs(off); const list = cardsOf(t, L.key);
        out.panels.push({ layer: L.key, name: L.name, sub: L.sub, col: L.col, li, n: list.length, tf: 'translateX(' + (off * PDX).toFixed(0) + 'px) translateZ(' + (-a * PDZ).toFixed(0) + 'px) rotateY(26deg)', cls: (a === 0 ? 'on' : a === 1 ? 'near' : 'far') + (list.length > 5 ? ' many' : ''), d: (a * 0.07).toFixed(2) + 's', cards: list.map((c, ci) => ({ id: t.mid + ':' + L.key + ':' + ci, mid: t.mid, layer: L.key, ci, card: c, col: c.col || L.col })) }); });
      for (let i = 0; i + 1 < LAYERS.length; i++) { const oa = i - li0, ob = i + 1 - li0; const ax = oa * PDX, az = -Math.abs(oa) * PDZ, bx = ob * PDX, bz = -Math.abs(ob) * PDZ; out.leaders.push({ tf: 'translateX(' + ax.toFixed(0) + 'px) translateZ(' + az.toFixed(0) + 'px) rotateY(' + (Math.atan2(-(bz - az), bx - ax) * 180 / Math.PI).toFixed(1) + 'deg)', w: Math.sqrt((bx - ax) * (bx - ax) + (bz - az) * (bz - az)).toFixed(0) + 'px' }); }
      out.station = { mid: t.mid, who: t.who, t: t.t, text: t.text };
      return out;
    }
    // iso: u = station, v = the layer bands; the plates identical parallelograms in a row
    const P = o.proj || isoP(30, 45);
    const UP = 420, VB = 190, CARD_V = 56, PW = 400, PH = LAYERS.length * VB, pts = [];   // a plate holds two columns of 168px cards
    const proj = (u, v, z) => { const p = P(u, v, z || 0); return { x: p[0], y: p[1] }; };
    turns.forEach((t, si) => {
      const u0 = si * (UP + 60); const corners = [proj(u0, 0, 0), proj(u0 + PW, 0, 0), proj(u0 + PW, PH, 0), proj(u0, PH, 0)]; corners.forEach((c) => pts.push(c));
      out.plates.push({ si, mid: t.mid, cls: si === sel ? 'on' : '', poly: corners.map((c) => ({ x: c.x, y: c.y })) });
      const lb = proj(u0, -18, 0); out.labels.push({ si, x: lb.x, y: lb.y, n: (t.who || 'you') + ' · ' + (t.t || ''), k: String(t.text || '').slice(0, 40), cls: 'station' + (si === sel ? ' on' : ''), col: 'var(--xp-t2)', mid: t.mid });
      const pos = {};
      LAYERS.forEach((L, li) => {
        const v0 = li * VB; const list = cardsOf(t, L.key); const ll = proj(u0 + PW + 8, v0 + 10, 0); out.labels.push({ si, x: ll.x, y: ll.y, n: L.name, k: String(list.length), cls: 'layer sm ' + L.key, col: L.col });
        pos[L.key] = list.map((c, ci) => { const p = proj(u0 + 110 + (ci % 2) * 190, v0 + 30 + Math.floor(ci / 2) * CARD_V, 0); pts.push(p); out.cards.push({ id: t.mid + ':' + L.key + ':' + ci, mid: t.mid, si, layer: L.key, ci, x: p.x, y: p.y, w: 168, h: 40, card: c, col: c.col || L.col, anchored: true }); return p; });
      });
      const ex = pos.say[0]; if (ex) { pos.read.forEach((r) => edge(r, ex, 'var(--xp-dv1)', 'in', 'read by this turn')); pos.made.forEach((r) => edge(ex, r, 'var(--xp-dv2)', 'out', 'produced by this turn')); }
      pos.made.forEach((r, i) => { const l = pos.land[i] || pos.land[0]; if (l) edge(r, l, 'var(--xp-ac)', 'link', 'this became a canvas item'); });
    });
    // fit the scene into the frame: scale and shift, cards counter-scale in the DOM
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; pts.forEach((p) => { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); });
    const s = Math.max(0.3, Math.min(1.4, Math.min((W - 80) / Math.max(1, x1 - x0 + 200), (H - 120) / Math.max(1, y1 - y0 + 140))));
    const dx = W / 2 - s * (x0 + x1) / 2, dy = H / 2 - s * (y0 + y1) / 2 + 20;
    out.fit = { s: +s.toFixed(3), x: px(dx), y: px(dy) };
    const T = (p) => ({ x: px(p.x * s + dx), y: px(p.y * s + dy) });
    out.plates.forEach((pl) => { pl.poly = pl.poly.map(T); });
    out.labels.forEach((l) => { const q = T(l); l.x = q.x; l.y = q.y; });
    out.cards.forEach((c) => { const q = T(c); c.x = q.x; c.y = q.y; });
    out.edges = []; // re-derive at screen scale
    turns.forEach((t) => { const byL = {}; out.cards.filter((c) => c.mid === t.mid).forEach((c) => { (byL[c.layer] = byL[c.layer] || []).push(c); });
      const ex = (byL.say || [])[0]; if (ex) { (byL.read || []).forEach((r) => edge(r, ex, 'var(--xp-dv1)', 'in', 'read by this turn')); (byL.made || []).forEach((r) => edge(ex, r, 'var(--xp-dv2)', 'out', 'produced by this turn')); }
      (byL.made || []).forEach((r, i) => { const l = (byL.land || [])[i] || (byL.land || [])[0]; if (l) edge(r, l, 'var(--xp-ac)', 'link', 'this became a canvas item'); }); });
    out.size = { w: W, h: H };
    return out;
  }

  const CSS = `
vera-exploded{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative;overflow:hidden;--xp-bg:var(--bg0,#0e0f12);--xp-s1:var(--bg1,#15171c);--xp-s2:var(--bg2,#1b1e25);--xp-bd:var(--border,#2a2e37);--xp-t1:var(--fg,#e6e6e6);--xp-t2:var(--dim,#aaa);--xp-t3:var(--dim2,#777);--xp-ac:var(--acc,#7c9cff);--xp-ac2:var(--acc2,#5ec9a0);--xp-dv1:#a78bfa;--xp-dv2:#fb923c;--xp-dv3:#38bdf8;--xp-mono:var(--mono,ui-monospace,monospace);font-size:10.5px;color:var(--xp-t1);background:var(--xp-bg)}
vera-exploded .xp-ctl{position:absolute;left:14px;top:10px;z-index:30;display:flex;align-items:center;gap:5px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-ctl .c{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--xp-t3);margin-right:3px}
vera-exploded .xp-ctl button{font:inherit;font-size:10px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 10px;border-radius:999px}
vera-exploded .xp-ctl button.on{background:var(--xp-ac);color:var(--xp-bg)}
vera-exploded .xp-ctl .sep{width:1px;height:14px;background:var(--xp-bd);margin:0 3px}
vera-exploded .xp-wrap{flex:1;min-height:0;position:relative;overflow:hidden;cursor:grab;touch-action:none;user-select:none}
vera-exploded .xp-wrap.dragging{cursor:grabbing}vera-exploded .xp-wrap.dragging .xp-view{transition:none}
vera-exploded .xp-view{position:absolute;inset:0;transform-origin:50% 50%;transition:transform .38s cubic-bezier(.22,.68,.18,1)}
vera-exploded .xp-view.scroll{overflow:auto;transition:none}
vera-exploded .xp-pl{position:absolute;pointer-events:none;background:color-mix(in srgb,var(--xp-ac) 5%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--xp-ac) 20%,transparent);border-radius:8px}
vera-exploded .xp-pl.on{background:color-mix(in srgb,var(--xp-ac) 9%,transparent);box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--xp-ac) 45%,transparent)}
vera-exploded .xp-pl.iso{border-radius:0;clip-path:var(--cp);box-shadow:none;background:color-mix(in srgb,var(--xp-ac) 8%,transparent)}
vera-exploded .xp-lb{position:absolute;font-size:10px;letter-spacing:.12em;text-transform:uppercase;white-space:nowrap;display:flex;gap:8px;align-items:baseline;pointer-events:none;color:var(--xp-t3)}
vera-exploded .xp-lb b{font-family:var(--xp-mono);font-size:9.5px;letter-spacing:0;font-weight:400;text-transform:none;color:var(--xp-t3);max-width:320px;overflow:hidden;text-overflow:ellipsis}
vera-exploded .xp-lb.station{font-size:11px;color:var(--xp-t2);pointer-events:auto;cursor:pointer}vera-exploded .xp-lb.station.on{color:var(--xp-t1)}vera-exploded .xp-lb.station:hover{text-decoration:underline;text-underline-offset:3px}
vera-exploded .xp-lb.sm{font-size:8.5px}
vera-exploded .xp-e{position:absolute;height:2px;transform-origin:0 50%;z-index:6;pointer-events:none;border-radius:2px;background:var(--ec);opacity:.75;box-shadow:0 0 7px -2px var(--ec)}
vera-exploded .xp-e.link{height:0;border-top:2px dashed var(--ec);background:none;box-shadow:none}
vera-exploded .xp-it{position:absolute;border-radius:6px;background:var(--xp-s2);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd);cursor:pointer;z-index:10;padding:6px 10px;display:flex;flex-direction:column;gap:2px;box-sizing:border-box;overflow:hidden;transition:box-shadow .15s ease}
vera-exploded .xp-it:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-t3),0 14px 28px -18px rgba(0,0,0,.95);z-index:22}
vera-exploded .xp-it.open{height:auto!important;z-index:26;background:color-mix(in srgb,var(--xp-s1) 97%,transparent);box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1.5px var(--xp-ac),0 26px 46px -18px rgba(0,0,0,.98)}
vera-exploded .xp-it.anch{transform:translate(-50%,-50%) scale(var(--inv,1));transform-origin:50% 50%}vera-exploded .xp-it.anch.open{transform:translate(-50%,-50%) scale(var(--inv,1))}
vera-exploded .xp-it .n{font-size:11px;color:var(--xp-t1);line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}vera-exploded .xp-it.open .n{white-space:normal}
vera-exploded .xp-it .d{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}
vera-exploded .xp-it .b{display:none;margin-top:4px;font-size:10px;color:var(--xp-t2);line-height:1.4}vera-exploded .xp-it.open .b{display:block;max-height:170px;overflow:auto}
vera-exploded .xp-it .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap;color:var(--xp-t2)}
vera-exploded .xp-it .b .bar{height:5px;border-radius:3px;background:var(--xp-bd);overflow:hidden}vera-exploded .xp-it .b .bar i{display:block;height:100%;background:var(--cc)}
vera-exploded .xp-it .b .pm b{font-family:var(--xp-mono);margin-right:8px}vera-exploded .xp-it .b .pm .p{color:var(--xp-ac2)}vera-exploded .xp-it .b .pm .m{color:#e06c75}
vera-exploded .xp-it .b .kv{display:grid;grid-template-columns:auto 1fr;gap:1px 8px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-it .b .kv b{color:var(--xp-t3);font-weight:400}
vera-exploded .xp-it .b .steps span{display:flex;gap:6px;font-family:var(--xp-mono);font-size:9px}vera-exploded .xp-it .b .steps i{width:6px;height:6px;border-radius:50%;background:var(--xp-t3);align-self:center;flex-shrink:0}vera-exploded .xp-it .b .steps span.ok i{background:var(--xp-ac2)}vera-exploded .xp-it .b .steps span.run i{background:var(--xp-ac)}vera-exploded .xp-it .b .steps span.fail i{background:#e06c75}
vera-exploded .xp-dots{position:absolute;right:12px;top:10px;z-index:32;display:flex;flex-direction:column;align-items:flex-end;gap:6px;padding:8px 7px;border-radius:8px;max-height:calc(100% - 40px);overflow:hidden}
vera-exploded .xp-dots:hover{background:color-mix(in srgb,var(--xp-s1) 92%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}
vera-exploded .xp-dot{display:flex;align-items:center;gap:8px;cursor:pointer;height:12px}vera-exploded .xp-dot i{width:6px;height:6px;border-radius:2px;background:var(--xp-t3);opacity:.5;flex-shrink:0;order:2}vera-exploded .xp-dot:hover i{opacity:1}vera-exploded .xp-dot.on i{opacity:1;background:var(--xp-ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--xp-ac) 22%,transparent)}
vera-exploded .xp-dot .l{order:1;display:none;font-family:var(--xp-mono);font-size:9px;color:var(--xp-t2);white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}vera-exploded .xp-dots:hover .xp-dot .l{display:block}
vera-exploded .xp-pz{position:absolute;left:14px;bottom:12px;z-index:34;display:flex;align-items:center;gap:3px;padding:4px 6px;border-radius:8px;background:color-mix(in srgb,var(--xp-s1) 90%,transparent);box-shadow:0 0 0 1px var(--xp-bd);opacity:.6}vera-exploded .xp-pz:hover{opacity:1}
vera-exploded .xp-pz button{font:inherit;font-family:var(--xp-mono);font-size:11px;color:var(--xp-t2);background:none;border:0;cursor:pointer;padding:3px 7px;border-radius:4px}vera-exploded .xp-pz button:hover{background:var(--xp-s2);color:var(--xp-t1)}vera-exploded .xp-pz .z{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3);min-width:34px;text-align:center}
/* FRONT: the carousel */
vera-exploded .xp-car{position:absolute;inset:0;perspective:2800px;perspective-origin:50% 48%;overflow:hidden}
vera-exploded .xp-track{position:absolute;left:50%;top:50%;width:0;height:0;transform-style:preserve-3d;transition:transform .5s cubic-bezier(.2,.85,.25,1.02)}
vera-exploded .xp-cp{position:absolute;left:-200px;top:-236px;width:400px;height:472px;border-radius:8px;cursor:pointer;display:flex;flex-direction:column;gap:9px;padding:14px;box-sizing:border-box;background:color-mix(in srgb,var(--pc) 8%,var(--xp-s1));box-shadow:0 0 0 1px color-mix(in srgb,var(--pc) 32%,transparent),0 12px 30px -14px rgba(0,0,0,.9);transition:transform .64s cubic-bezier(.2,.85,.25,1.04),opacity .46s ease;animation:xp-cp-in .62s cubic-bezier(.2,.8,.25,1) backwards;animation-delay:var(--d,0s)}
@keyframes xp-cp-in{from{opacity:0;transform:translateX(0) translateZ(-140px) rotateY(-6deg)}}
vera-exploded .xp-cp.on{background:var(--xp-s1);box-shadow:0 0 0 1.5px color-mix(in srgb,var(--pc) 62%,transparent),0 22px 40px -26px #000}vera-exploded .xp-cp.near{opacity:.92}vera-exploded .xp-cp.far{opacity:.55}
vera-exploded .xp-cp-h{display:flex;align-items:baseline;gap:7px;font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:color-mix(in srgb,var(--pc) 90%,var(--xp-t3));flex-shrink:0;cursor:pointer}vera-exploded .xp-cp-h i{width:7px;height:7px;border-radius:2px;background:var(--pc);align-self:center}vera-exploded .xp-cp-h b{font-family:var(--xp-mono);font-size:9px;letter-spacing:0;color:var(--xp-t3);font-weight:400;text-transform:none;margin-left:auto}
vera-exploded .xp-cp-b{flex:1;min-height:0;display:flex;flex-direction:column;gap:10px;overflow:auto;padding:0 6px 2px}
vera-exploded .xp-cp.many .xp-cp-b{display:grid;grid-template-columns:1fr 1fr;align-content:start}
vera-exploded .xp-rc{position:relative;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:6px;background:var(--xp-s2);cursor:pointer;box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-bd)}vera-exploded .xp-rc:hover{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-t3)}vera-exploded .xp-rc.open{box-shadow:inset 3px 0 0 0 var(--cc),0 0 0 1px var(--xp-ac)}
vera-exploded .xp-rc .n{font-size:11px;color:var(--xp-t1)}vera-exploded .xp-rc .d{font-family:var(--xp-mono);font-size:9px;color:var(--xp-t3)}vera-exploded .xp-rc .b{display:none;margin-top:4px;font-size:10px;color:var(--xp-t2)}vera-exploded .xp-rc.open .b{display:block}vera-exploded .xp-rc .b pre{margin:0;font-family:var(--xp-mono);font-size:9px;white-space:pre-wrap}
vera-exploded .xp-spine{position:absolute;left:-2600px;top:-1px;width:5200px;height:2px;pointer-events:none;background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--xp-t3) 26%,transparent) 20%,color-mix(in srgb,var(--xp-t3) 26%,transparent) 80%,transparent)}
vera-exploded .xp-lead{position:absolute;left:0;top:0;height:0;transform-origin:0 50%;border-top:1px dashed var(--xp-bd);pointer-events:none}
vera-exploded .xp-nav{position:absolute;top:50%;transform:translateY(-50%);z-index:30;width:30px;height:52px;border:0;border-radius:6px;cursor:pointer;font-size:17px;line-height:1;color:var(--xp-t2);background:color-mix(in srgb,var(--xp-s1) 84%,transparent);box-shadow:0 0 0 1px var(--xp-bd)}vera-exploded .xp-nav:hover{color:var(--xp-t1);background:var(--xp-s2)}vera-exploded .xp-nav.l{left:16px}vera-exploded .xp-nav.r{right:16px}
vera-exploded .xp-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--xp-t3);text-align:center;padding:20px;line-height:1.5}
`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-exploded-css')) return; const s = doc.createElement('style'); s.id = 'vera-exploded-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  function cardBody(c) {
    const k = c.kind || ''; let h = '';
    if (c.score != null) h += '<div class="bar"><i style="width:' + Math.round(Math.max(0, Math.min(1, +c.score)) * 100) + '%"></i></div>';
    if (k === 'diff' && (c.p != null || c.m != null)) h += '<div class="pm"><b class="p">+' + esc(c.p || 0) + '</b><b class="m">−' + esc(c.m || 0) + '</b></div>';
    if (Array.isArray(c.rows) && c.rows.length) h += '<div class="kv">' + c.rows.slice(0, 8).map((r) => '<b>' + esc(r.k) + '</b><span>' + esc(r.v) + '</span>').join('') + '</div>';
    if (Array.isArray(c.steps) && c.steps.length) h += '<div class="steps">' + c.steps.slice(0, 12).map((s) => '<span class="' + esc(s.status || '') + '"><i></i>' + esc(s.label || s.n || '') + (s.cap ? ' · ' + esc(s.cap) : '') + '</span>').join('') + '</div>';
    if (c.body) h += '<pre>' + esc(String(c.body).slice(0, 600)) + '</pre>';
    return h;
  }

  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-exploded')) {
    class VeraExploded extends HTMLElement {
      constructor() { super(); this._S = { scene: { turns: [], sel: '' }, mode: 'cards', layer: 1, open: null, pan: { x: 0, y: 0, z: 1 } }; this._raf = 0; }
      connectedCallback() {
        ensureCss(this.ownerDocument); if (this._built) { this._schedule(); return; } this._built = true;
        const m = this.getAttribute('mode'); if (m) this._S.mode = m;
        this.innerHTML = '<div class="xp-ctl"><span class="c">explode</span><button data-m="cards">Cards</button><button data-m="front">Front</button><button data-m="iso">Iso</button><span class="sep"></span><button data-a="fit" title="Back to the whole scene">Fit</button><button data-a="close" title="Back to the flat transcript">Flatten</button></div><div class="xp-dots" data-r="dots"></div><div class="xp-wrap" data-r="wrap"><div class="xp-view" data-r="view"></div></div><div class="xp-pz"><button data-a="zout">−</button><span class="z" data-r="zoom">100%</span><button data-a="zin">+</button></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this.addEventListener('click', (e) => this._click(e));
        const wrap = this._r.wrap;
        wrap.addEventListener('wheel', (e) => { if (this._S.mode === 'cards') return; e.preventDefault(); const p = this._S.pan; const nz = Math.max(0.4, Math.min(3, p.z * (e.deltaY > 0 ? 0.9 : 1.12))); const r = wrap.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._applyPan(); }, { passive: false });
        wrap.addEventListener('pointerdown', (e) => { if (e.button || this._S.mode === 'cards' || (e.target.closest && e.target.closest('.xp-it,.xp-rc,.xp-cp,button,.xp-lb.station'))) return; this._drag = { x0: e.clientX, y0: e.clientY, px: this._S.pan.x, py: this._S.pan.y, id: e.pointerId, moved: false }; });
        wrap.addEventListener('pointermove', (e) => { const g = this._drag; if (!g) return; const dx = e.clientX - g.x0, dy = e.clientY - g.y0; if (!g.moved && Math.abs(dx) + Math.abs(dy) > 4) { g.moved = true; wrap.classList.add('dragging'); try { wrap.setPointerCapture(g.id); } catch (_) {} } if (g.moved) { this._S.pan.x = g.px + dx; this._S.pan.y = g.py + dy; this._applyPan(); } });
        const up = () => { if (this._drag) { wrap.classList.remove('dragging'); this._drag = null; } }; wrap.addEventListener('pointerup', up); wrap.addEventListener('pointercancel', up);
        if (root.ResizeObserver) { this._ro = new ResizeObserver(() => this._schedule()); this._ro.observe(wrap); }
        this._schedule();
      }
      disconnectedCallback() { if (this._ro) { try { this._ro.disconnect(); } catch (_) {} } }
      setScene(scene) { this._S.scene = scene && scene.turns ? scene : { turns: [], sel: '' }; if (!this._S.scene.sel && this._S.scene.turns.length) this._S.scene.sel = this._S.scene.turns[this._S.scene.turns.length - 1].mid; this._schedule(); }
      mode(name) { if (name && /^(cards|front|iso)$/.test(name)) { this._S.mode = name; this._S.pan = { x: 0, y: 0, z: 1 }; this._S.open = null; this._schedule(); } return this._S.mode; }
      select(mid) { this._S.scene.sel = mid; this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); }
      fit() { this._S.pan = { x: 0, y: 0, z: 1 }; this._applyPan(); }
      state() { return this._S; }
      _applyPan() { const p = this._S.pan; if (this._r.view) this._r.view.style.transform = this._S.mode === 'cards' ? 'none' : 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')'; if (this._r.zoom) this._r.zoom.textContent = Math.round(p.z * 100) + '%'; this.querySelectorAll('.xp-it.anch').forEach((el) => { el.style.setProperty('--inv', (1 / Math.max(0.5, Math.min(2.2, (this._last ? this._last.fit.s : 1) * p.z))).toFixed(3)); }); }
      _click(e) {
        const t = e.target; const mb = t.closest && t.closest('button[data-m]'); if (mb) { this.mode(mb.dataset.m); return; }
        const ab = t.closest && t.closest('[data-a]'); if (ab) { const k = ab.dataset.a; if (k === 'fit') this.fit(); else if (k === 'close') this.dispatchEvent(new CustomEvent('vera:xpl:close', { bubbles: true })); else if (k === 'zin' || k === 'zout') { const p = this._S.pan; p.z = Math.max(0.4, Math.min(3, p.z * (k === 'zin' ? 1.2 : 0.83))); this._applyPan(); } else if (k === 'prev' || k === 'next') { this._S.layer = Math.max(0, Math.min(LAYERS.length - 1, this._S.layer + (k === 'next' ? 1 : -1))); this._schedule(); } return; }
        const dot = t.closest && t.closest('.xp-dot'); if (dot) { this.select(dot.dataset.mid); return; }
        const st = t.closest && t.closest('.xp-lb.station'); if (st) { this.select(st.dataset.mid); return; }
        const ph = t.closest && t.closest('.xp-cp-h'); if (ph) { this._S.layer = +ph.closest('.xp-cp').dataset.li; this._schedule(); return; }
        const card = t.closest && t.closest('.xp-it,.xp-rc'); if (card) { const id = card.dataset.id; this._S.open = this._S.open === id ? null : id; const [mid, layer] = id.split(':'); this._schedule(); this.dispatchEvent(new CustomEvent('vera:xpl:pick', { detail: { mid, layer, card: id }, bubbles: true })); if (layer === 'say') this.dispatchEvent(new CustomEvent('vera:xpl:turn', { detail: { mid }, bubbles: true })); return; }
        const cp = t.closest && t.closest('.xp-cp'); if (cp) { this._S.layer = +cp.dataset.li; this._schedule(); }
      }
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _render() {
        const S = this._S, wrap = this._r.wrap, view = this._r.view; const W = wrap.clientWidth || 800, H = wrap.clientHeight || 600;
        const proj = (root.VeraISO && typeof root.VeraISO.proj === 'function') ? root.VeraISO.proj(30, 45, 1, true) : null;
        const o = layout(S.scene, S.mode, W, H, { layer: S.layer, proj }); this._last = o;
        this.querySelectorAll('.xp-ctl button[data-m]').forEach((b) => b.classList.toggle('on', b.dataset.m === o.mode));
        this._r.dots.innerHTML = (S.scene.turns || []).map((t, i) => '<div class="xp-dot' + (i === o.sel ? ' on' : '') + '" data-mid="' + esc(t.mid) + '" title="' + esc((t.who || 'you') + ' · ' + (t.t || '') + ' · ' + String(t.text || '').slice(0, 80)) + '"><span class="l">' + esc((t.who || 'you') + ' ' + (i + 1) + ' · ' + String(t.text || '').slice(0, 30)) + '</span><i></i></div>').join('');
        view.classList.toggle('scroll', o.mode === 'cards');
        const st = (x, y) => 'left:' + x + 'px;top:' + y + 'px;';
        const itHtml = (c, anch) => '<div class="xp-it' + (anch ? ' anch' : '') + (S.open === c.id ? ' open' : '') + '" data-id="' + esc(c.id) + '" title="' + esc(c.card.n || '') + (c.card.d ? ' — ' + esc(c.card.d) : '') + '" style="' + st(c.x, c.y) + 'width:' + c.w + 'px;height:' + c.h + 'px;--cc:' + esc(c.col) + '"><span class="n">' + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span><div class="b">' + cardBody(c.card) + '</div></div>';
        let h = '';
        if (!(S.scene.turns || []).length) { view.innerHTML = '<div class="xp-empty">Nothing to explode yet — the scene is the session\'s turns: what each read, what it said, what it made, where it landed.</div>'; view.style.transform = 'none'; this._emit(o); return; }
        if (o.mode === 'front') {
          const nav = '<button class="xp-nav l" data-a="prev">‹</button><button class="xp-nav r" data-a="next">›</button>';
          h = '<div class="xp-car"><div class="xp-track"><div class="xp-spine"></div>' + o.leaders.map((l) => '<div class="xp-lead" style="transform:' + l.tf + ';width:' + l.w + '"></div>').join('')
            + o.panels.map((p) => '<div class="xp-cp ' + p.cls + '" data-li="' + p.li + '" style="--pc:' + esc(p.col) + ';--d:' + p.d + ';transform:' + p.tf + '"><div class="xp-cp-h"><i></i>' + esc(p.name) + '<span style="letter-spacing:0;text-transform:none;opacity:.7"> · ' + esc(p.sub) + '</span><b>' + p.n + '</b></div><div class="xp-cp-b">'
              + p.cards.map((c) => '<div class="xp-rc' + (S.open === c.id ? ' open' : '') + '" data-id="' + esc(c.id) + '" style="--cc:' + esc(c.col) + '"><span class="n">' + esc(c.card.n || '') + '</span><span class="d">' + esc(c.card.d || '') + '</span><div class="b">' + cardBody(c.card) + '</div></div>').join('')
              + (p.cards.length ? '' : '<div class="d" style="color:var(--xp-t3);font-family:var(--xp-mono);font-size:9px">nothing here for this turn</div>') + '</div></div>').join('') + '</div>' + nav + '</div>';
          view.innerHTML = h; view.style.transform = 'none'; this._emit(o); return;
        }
        if (o.mode === 'iso') {
          o.plates.forEach((p) => { const xs = p.poly.map((q) => q.x), ys = p.poly.map((q) => q.y); const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys); const cp = 'polygon(' + p.poly.map((q) => (q.x - x0).toFixed(1) + 'px ' + (q.y - y0).toFixed(1) + 'px').join(',') + ')'; h += '<div class="xp-pl iso' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(x0, y0) + 'width:' + (x1 - x0).toFixed(1) + 'px;height:' + (y1 - y0).toFixed(1) + 'px;--cp:' + cp + '"></div>'; });
        } else { o.plates.forEach((p) => { h += '<div class="xp-pl' + (p.cls ? ' ' + p.cls : '') + '" style="' + st(p.x, p.y) + 'width:' + p.w + 'px;height:' + p.h + 'px"></div>'; }); }
        o.edges.forEach((e) => { h += '<div class="xp-e ' + e.cls + '" title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;--ec:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.labels.forEach((l) => { h += '<div class="xp-lb ' + l.cls + '" data-mid="' + esc(l.mid || '') + '" style="' + st(l.x, l.y) + 'color:' + esc(l.col) + '">' + esc(l.n) + '<b>' + esc(l.k) + '</b></div>'; });
        o.cards.forEach((c) => { h += itHtml(c, !!c.anchored); });
        if (o.mode === 'cards') h = '<div style="position:relative;width:' + o.size.w + 'px;height:' + o.size.h + 'px">' + h + '</div>';
        view.innerHTML = h; this._applyPan(); this._emit(o);
      }
      _emit(o) { this.dispatchEvent(new CustomEvent('vera:xpl:rendered', { detail: { mode: o.mode, stations: o.stations, cards: o.cards.length, panels: o.panels.length }, bubbles: true })); }
    }
    root.customElements.define('vera-exploded', VeraExploded);
  }
  const api = { layout, LAYERS, ensureCss, version: 1 };
  root.VeraExploded = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);

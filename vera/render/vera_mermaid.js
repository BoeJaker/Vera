/* ═══════════════════════════════════════════════════════════════════════════
   <vera-mermaid> — Vera's own Mermaid diagram renderer
   ───────────────────────────────────────────────────────────────────────────
   A dependency-free (no CDN, no mermaid.js) renderer for the Mermaid subsets
   an LLM actually produces:

     • flowchart / graph  (TD|TB|LR|RL|BT, node shapes, edge labels, chains,
                           `&` fan-out, subgraphs)
     • sequenceDiagram    (participants/actors, sync/async/dotted arrows,
                           notes, loop/alt/opt frames)
     • stateDiagram[-v2]  ([*] start/end, transitions with labels)
     • pie                (title + slices)

   Theme-aware: drawn in the live theme's tokens (the design tokens --s1/--s2/--t1/--t2/--t3/--ac/--ac2/--bd/--bd2,
   the older --bg0/--bg2/--border/--acc/--text/--dim2 as fallbacks — read at render time), re-drawn when the theme
   changes (data-theme · data-style on <html>, the vera:theme message veraUI posts), and `el.themeVariables = {…}`
   (or the attribute, JSON) overrides any token for one element. Sized to its width: the viewport takes the height
   the diagram has when it fills the width (up to max-height, 720), so a diagram keeps its aspect instead of shrinking
   into a strip; a [fill] host fits the box it is given. Pans and zooms with the pointer, exports SVG/PNG, and shows a
   friendly error banner (with the source) when the input can't be parsed rather than throwing.

   API
   ───
     el.render(code)      — parse + draw (also: set attribute `code`, or put
                            the source as the element's text content)
     el.stream(code)      — the same, while the code is still ARRIVING: every
                            complete line (closed by its newline) is placed; the
                            unfinished tail line is shown as a ghost (dashed node,
                            dashed edge), never guessed; a badge counts nodes ·
                            edges · pending; a parse that fails on a half-written
                            structure keeps the last good drawing instead of an
                            error banner. render() on the closing fence settles
                            the layout once.
     el.getSvg()          — serialised <svg> string ('' if nothing rendered)
     el.fit()             — re-fit the diagram to the viewport
     el.naturalHeight(w)  — the height the drawing takes at width w (its aspect kept; 0 before a render)
     el.themeVariables    — {bg, card, line, soft, text, dim, acc, acc2, acc3, warn, err} overrides (property or attribute)
     VeraMermaid.theme()  — the token set a render reads, as the page has it now
     attribute `title`    — toolbar label
     attribute `max-height` — the viewport's ceiling when not [fill] (720)
     attribute `bare`     — no toolbar / border (embed mode)

   Events:  vm:rendered {detail:{type,nodes,edges,width,height,natural}}   vm:error {detail:{message}}
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  if (window.customElements && customElements.get('vera-mermaid')) return;

  /* ── helpers ─────────────────────────────────────────────────────────── */
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  // Unquote + translate mermaid text conventions (<br/>, #quot;, "…")
  function clean(txt) {
    let t = String(txt == null ? '' : txt).trim();
    if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'")))
      t = t.slice(1, -1);
    return t.replace(/<br\s*\/?>/gi, '\n').replace(/#quot;/g, '"')
            .replace(/&quot;/g, '"').replace(/&amp;/g, '&').trim();
  }

  // Wrap a label into lines of ~n chars (respecting explicit \n)
  function wrap(txt, n) {
    n = n || 18;
    const out = [];
    for (const seg of String(txt).split('\n')) {
      let line = '';
      for (const w of seg.split(/\s+/)) {
        if (!w) continue;
        if (line && (line + ' ' + w).length > n) { out.push(line); line = w; }
        else line = line ? line + ' ' + w : w;
      }
      if (line) out.push(line);
      if (!seg.trim()) out.push('');
    }
    return out.length ? out : [''];
  }

  const TXT_W = 6.4;           // ≈ px per char at font-size 11
  const LINE_H = 15;

  function labelSize(lines) {
    const w = Math.max(24, ...lines.map(l => l.length * TXT_W));
    return { w: w + 24, h: lines.length * LINE_H + 16 };
  }

  /* ═══ FLOWCHART PARSER ═════════════════════════════════════════════════ */
  // node shape delimiters, longest-first so ((…)) wins over (…)
  const SHAPES = [
    ['((', '))', 'circle'], ['([', '])', 'stadium'], ['[[', ']]', 'subroutine'],
    ['[(', ')]', 'cylinder'], ['[/', '/]', 'parallelogram'], ['[\\', '\\]', 'parallelogram'],
    ['{{', '}}', 'hexagon'], ['[', ']', 'rect'], ['(', ')', 'round'],
    ['{', '}', 'diamond'], ['>', ']', 'flag'],
  ];

  function parseFlow(code) {
    const g = { type: 'flow', dir: 'TD', nodes: new Map(), edges: [], subs: [] };
    const lines = code.split('\n');
    let cur = null;                              // current subgraph
    const subStack = [];

    const first = (lines.find(l => l.trim()) || '').trim();
    const dm = first.match(/^(?:graph|flowchart)\s+(TD|TB|LR|RL|BT)/i);
    if (dm) g.dir = dm[1].toUpperCase() === 'TB' ? 'TD' : dm[1].toUpperCase();

    function node(id, label, shape) {
      id = id.trim();
      if (!id) return null;
      let n = g.nodes.get(id);
      if (!n) { n = { id, label: id, shape: 'rect', sub: cur }; g.nodes.set(id, n); }
      if (label != null && label !== '') n.label = clean(label);
      if (shape) n.shape = shape;
      if (cur && !n.sub) n.sub = cur;
      return n;
    }

    // parse one endpoint token like  B{Is it?}  |  C  |  D[/data/]
    function endpoint(tok) {
      tok = tok.trim();
      const m = tok.match(/^([A-Za-z0-9_.:-]+)\s*(.*)$/s);
      if (!m) return null;
      const id = m[1]; const rest = m[2] || '';
      if (rest) {
        for (const [o, c, shape] of SHAPES) {
          if (rest.startsWith(o) && rest.endsWith(c) && rest.length >= o.length + c.length) {
            return node(id, rest.slice(o.length, rest.length - c.length), shape);
          }
        }
      }
      return node(id, null, null);
    }

    // split a chain "A -->|x| B --> C & D" into segments
    const EDGE_RE = /\s*(<?)(-{2,3}|={2,3}|-\.+-?)(>?)(?:\|([^|]*)\|)?\s*/g;

    for (let raw of lines) {
      let line = raw.replace(/%%.*$/, '').trim();  // strip comments
      if (!line) continue;
      if (/^(?:graph|flowchart)\b/i.test(line)) continue;
      if (/^(classDef|class|style|linkStyle|click|direction|accTitle|accDescr)\b/i.test(line)) continue;
      const sg = line.match(/^subgraph\s+(.+)$/i);
      if (sg) {
        const t = clean(sg[1].replace(/\[.*\]$/, m0 => m0.slice(1, -1)));
        const sid = 'sub' + g.subs.length;
        g.subs.push({ id: sid, label: t, parent: cur });
        subStack.push(cur); cur = sid; continue;
      }
      if (/^end\s*$/i.test(line)) { cur = subStack.pop() || null; continue; }

      // edge chains — split by edge tokens. Text inside a node's brackets or quotes ("digest == stored?",
      // "a -- b") is masked first so its own ==/-- never reads as an edge.
      const masks = [];
      const masked = line.replace(/"[^"]*"|\[[^\]]*\]|\([^)]*\)|\{[^}]*\}/g, s => { masks.push(s); return '\u0001' + (masks.length - 1) + '\u0001'; });
      const unmask = s => s.replace(/\u0001(\d+)\u0001/g, (_, i) => masks[+i]);
      EDGE_RE.lastIndex = 0;
      const parts = []; const ops = [];
      let last = 0; let m;
      while ((m = EDGE_RE.exec(masked)) !== null) {
        // ignore matches inside label text: crude guard — require non-empty left side
        const left = masked.slice(last, m.index);
        if (!left.trim() && parts.length === 0) { continue; }
        parts.push(unmask(left));
        ops.push({ back: m[1] === '<', body: m[2], arrow: m[3] === '>', label: unmask(m[4] || '') });
        last = EDGE_RE.lastIndex;
      }
      parts.push(unmask(masked.slice(last)));

      if (ops.length === 0) { endpoint(line); continue; }

      // "A -- label --> B" form: body may swallow label; handle "-- text -->"
      for (let i = 0; i < ops.length; i++) {
        const srcs = parts[i].split('&').map(s => endpoint(s)).filter(Boolean);
        const dsts = parts[i + 1].split('&').map(s => endpoint(s)).filter(Boolean);
        for (const s of srcs) for (const d of dsts) {
          const dotted = ops[i].body.indexOf('.') >= 0;
          const thick = ops[i].body[0] === '=';
          if (ops[i].back) g.edges.push({ from: d.id, to: s.id, label: clean(ops[i].label), dotted, thick });
          else g.edges.push({ from: s.id, to: d.id, label: clean(ops[i].label), dotted, thick });
        }
      }
    }
    if (!g.nodes.size) throw new Error('no nodes found — is this a flowchart?');
    return g;
  }

  /* ═══ STATE DIAGRAM → reuse flow graph ════════════════════════════════ */
  function parseState(code) {
    const g = { type: 'flow', dir: 'TD', nodes: new Map(), edges: [], subs: [] };
    let starts = 0, ends = 0;
    function node(id, label, shape) {
      let n = g.nodes.get(id);
      if (!n) { n = { id, label: label || id, shape: shape || 'stadium', sub: null }; g.nodes.set(id, n); }
      if (label) n.label = label;
      return n;
    }
    for (let raw of code.split('\n')) {
      const line = raw.replace(/%%.*$/, '').trim();
      if (!line || /^stateDiagram/i.test(line) || /^direction\b/i.test(line)) continue;
      const tr = line.match(/^(\[\*\]|[\w.-]+)\s*-->\s*(\[\*\]|[\w.-]+)\s*(?::\s*(.*))?$/);
      if (tr) {
        let a = tr[1], b = tr[2];
        if (a === '[*]') { a = '__start' + (starts++); node(a, ' ', 'dot'); }
        if (b === '[*]') { b = '__end' + (ends++); node(b, ' ', 'dotend'); }
        node(a); node(b);
        g.edges.push({ from: a, to: b, label: clean(tr[3] || ''), dotted: false, thick: false });
        continue;
      }
      const st = line.match(/^state\s+"([^"]+)"\s+as\s+([\w.-]+)/i) || line.match(/^([\w.-]+)\s*:\s*(.+)$/);
      if (st) {
        if (st[2] && /^state\s/i.test(line)) node(st[2], clean(st[1]));
        else node(st[1], node(st[1]).label + '\n' + clean(st[2]));
      }
    }
    if (!g.nodes.size) throw new Error('no states found');
    return g;
  }

  /* ═══ FLOWCHART LAYOUT — layered, barycenter ordering ═════════════════ */
  function layoutFlow(g) {
    const ids = [...g.nodes.keys()];
    const idx = new Map(ids.map((id, i) => [id, i]));
    const N = ids.length;
    const out = Array.from({ length: N }, () => []);
    const inn = Array.from({ length: N }, () => []);
    for (const e of g.edges) {
      const a = idx.get(e.from), b = idx.get(e.to);
      if (a == null || b == null || a === b) continue;
      out[a].push(b); inn[b].push(a);
    }
    // longest-path layering (cycle-safe: bounded iterations)
    const layer = new Array(N).fill(0);
    for (let pass = 0; pass < N; pass++) {
      let changed = false;
      for (const e of g.edges) {
        const a = idx.get(e.from), b = idx.get(e.to);
        if (a == null || b == null || a === b) continue;
        if (layer[b] < layer[a] + 1) { layer[b] = layer[a] + 1; changed = true; }
        if (layer[b] > N) { changed = false; break; }   // cycle guard
      }
      if (!changed) break;
    }
    const L = Math.max(0, ...layer) + 1;
    const layers = Array.from({ length: L }, () => []);
    ids.forEach((id, i) => layers[layer[i]].push(i));

    // node metrics
    const meta = ids.map(id => {
      const n = g.nodes.get(id);
      const lines = wrap(n.label, 20);
      let { w, h } = labelSize(lines);
      if (n.shape === 'diamond') { w += 26; h += 18; }
      if (n.shape === 'circle') { w = h = Math.max(w * 0.8, h + 14); }
      if (n.shape === 'dot' || n.shape === 'dotend') { w = h = 16; }
      return { lines, w, h };
    });

    // barycenter ordering sweeps
    const pos = new Array(N).fill(0);
    layers.forEach(l => l.forEach((v, i) => pos[v] = i));
    for (let sweep = 0; sweep < 4; sweep++) {
      const down = sweep % 2 === 0;
      for (let li = down ? 1 : L - 2; down ? li < L : li >= 0; down ? li++ : li--) {
        const ref = v => {
          const nb = down ? inn[v] : out[v];
          if (!nb.length) return pos[v];
          return nb.reduce((s, u) => s + pos[u], 0) / nb.length;
        };
        layers[li].sort((a, b) => ref(a) - ref(b));
        layers[li].forEach((v, i) => pos[v] = i);
      }
    }

    // coordinates
    const horiz = g.dir === 'LR' || g.dir === 'RL';
    const GAP_MAIN = 64, GAP_CROSS = 28;
    const layerSize = layers.map(l =>
      Math.max(0, ...l.map(v => horiz ? meta[v].w : meta[v].h)));
    const layerOff = [];
    let acc = 0;
    for (let i = 0; i < L; i++) { layerOff.push(acc); acc += layerSize[i] + GAP_MAIN; }

    const coord = new Array(N);
    for (let li = 0; li < L; li++) {
      let cross = 0;
      for (const v of layers[li]) {
        const m = meta[v];
        const main = layerOff[li] + layerSize[li] / 2;
        const c = cross + (horiz ? m.h : m.w) / 2;
        coord[v] = horiz ? { x: main, y: c } : { x: c, y: main };
        cross += (horiz ? m.h : m.w) + GAP_CROSS;
      }
    }
    // centre each layer against the widest
    const total = layers.map(l => l.reduce((s, v) => s + (horiz ? meta[v].h : meta[v].w) + GAP_CROSS, -GAP_CROSS));
    const maxTotal = Math.max(0, ...total);
    layers.forEach((l, li) => {
      const off = (maxTotal - total[li]) / 2;
      for (const v of l) { if (horiz) coord[v].y += off; else coord[v].x += off; }
    });
    // reverse for RL / BT
    if (g.dir === 'RL') { const mx = Math.max(...coord.map(c => c.x)); coord.forEach(c => c.x = mx - c.x); }
    if (g.dir === 'BT') { const my = Math.max(...coord.map(c => c.y)); coord.forEach(c => c.y = my - c.y); }

    ids.forEach((id, i) => {
      const n = g.nodes.get(id);
      n.x = coord[i].x; n.y = coord[i].y;
      n.w = meta[i].w; n.h = meta[i].h; n.lines = meta[i].lines;
    });
  }

  /* ═══ SVG BUILDING ════════════════════════════════════════════════════ */
  /* the theme a render reads: the design tokens the estate's themes set (--s1 ground · --s2 card · --t1 text ·
     --t2/--t3 dim · --ac/--ac2/--ac3 accents · --bd/--bd2 lines), the older vars where a page has only those, and safe
     fallbacks last — read at RENDER time, so a theme change re-draws in the new tokens; `overrides` (an element's
     themeVariables) win over all of it */
  const THEME = (overrides) => {
    const cs = getComputedStyle(document.documentElement);
    const v = (names, fb) => { for (const n of names) { const x = (cs.getPropertyValue(n) || '').trim(); if (x) return x; } return fb; };
    const t = {
      bg:    v(['--s1', '--bg1', '--bg0'], '#101012'),
      card:  v(['--s2', '--bg2'], '#1a1c20'),
      line:  v(['--bd2', '--border2'], 'rgba(255,255,255,.22)'),
      soft:  v(['--bd', '--border'], 'rgba(255,255,255,.09)'),
      text:  v(['--t1', '--text', '--fg'], '#d8dce4'),
      dim:   v(['--t2', '--dim2', '--fg2'], '#8a92a0'),
      acc:   v(['--ac', '--acc'], '#5a9e8f'),
      acc2:  v(['--ac2', '--acc2'], '#8fb87a'),
      acc3:  v(['--ac3', '--acc3'], '#d4a96a'),
      warn:  v(['--warn', '--ac3'], '#c9a35a'),
      err:   v(['--err'], '#c96b6b'),
    };
    if (overrides && typeof overrides === 'object') for (const k of Object.keys(t)) if (overrides[k]) t[k] = String(overrides[k]);
    return t;
  };
  /* every connected element re-draws when the theme changes: the attributes veraUI sets on <html> (data-theme ·
     data-style · data-den · data-blocks) and the vera:theme message it posts between frames — one observer, all elements */
  const LIVE = new Set(); let _themeT = 0;
  const rethemeAll = () => { clearTimeout(_themeT); _themeT = setTimeout(() => { LIVE.forEach((el) => { try { el.retheme(); } catch (_) {} }); }, 60); };
  try { new MutationObserver(rethemeAll).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-style', 'data-den', 'data-blocks', 'class'] }); } catch (_) {}
  try { window.addEventListener('message', (e) => { const d = e && e.data; if (d && typeof d === 'object' && d.type === 'vera:theme') rethemeAll(); }); } catch (_) {}
  const PALETTE = t => [t.acc, t.acc2, t.acc3, '#a78bfa', '#e07a9a', '#5ab0d8', '#c9a35a', '#7ac9b0'];

  function nodeSvg(n, t) {
    const x = n.x - n.w / 2, y = n.y - n.h / 2;
    const common = n.ghost ? `fill="none" stroke="${t.dim}" stroke-width="1.2" stroke-dasharray="3 3"`
                           : `fill="${t.card}" stroke="${t.line}" stroke-width="1.2"`;
    let shape = '';
    switch (n.shape) {
      case 'round':
      case 'stadium':
        shape = `<rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.shape === 'stadium' ? n.h / 2 : 10}" ${common}/>`; break;
      case 'circle': {
        const r = Math.max(n.w, n.h) / 2;
        shape = `<circle cx="${n.x}" cy="${n.y}" r="${r}" ${common}/>`; break;
      }
      case 'diamond':
        shape = `<polygon points="${n.x},${y} ${x + n.w},${n.y} ${n.x},${y + n.h} ${x},${n.y}" ${common}/>`; break;
      case 'hexagon': {
        const c = Math.min(16, n.w / 4);
        shape = `<polygon points="${x + c},${y} ${x + n.w - c},${y} ${x + n.w},${n.y} ${x + n.w - c},${y + n.h} ${x + c},${y + n.h} ${x},${n.y}" ${common}/>`; break;
      }
      case 'cylinder': {
        const ry = 7;
        shape = `<path d="M${x},${y + ry} a${n.w / 2},${ry} 0 0 1 ${n.w},0 v${n.h - 2 * ry} a${n.w / 2},${ry} 0 0 1 -${n.w},0 z" ${common}/>` +
                `<ellipse cx="${n.x}" cy="${y + ry}" rx="${n.w / 2}" ry="${ry}" fill="none" stroke="${t.line}" stroke-width="1.2"/>`; break;
      }
      case 'subroutine':
        shape = `<rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="3" ${common}/>` +
                `<line x1="${x + 5}" y1="${y}" x2="${x + 5}" y2="${y + n.h}" stroke="${t.line}"/>` +
                `<line x1="${x + n.w - 5}" y1="${y}" x2="${x + n.w - 5}" y2="${y + n.h}" stroke="${t.line}"/>`; break;
      case 'parallelogram': {
        const sk = 12;
        shape = `<polygon points="${x + sk},${y} ${x + n.w},${y} ${x + n.w - sk},${y + n.h} ${x},${y + n.h}" ${common}/>`; break;
      }
      case 'flag':
        shape = `<polygon points="${x},${y} ${x + n.w},${y} ${x + n.w},${y + n.h} ${x},${y + n.h} ${x + 12},${n.y}" ${common}/>`; break;
      case 'dot':
        return `<circle cx="${n.x}" cy="${n.y}" r="7" fill="${t.text}"/>`;
      case 'dotend':
        return `<circle cx="${n.x}" cy="${n.y}" r="8" fill="none" stroke="${t.text}" stroke-width="1.5"/>` +
               `<circle cx="${n.x}" cy="${n.y}" r="4.5" fill="${t.text}"/>`;
      default:
        shape = `<rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="6" ${common}/>`;
    }
    const ty = n.y - ((n.lines.length - 1) * LINE_H) / 2;
    const txt = n.lines.map((l, i) =>
      `<text x="${n.x}" y="${ty + i * LINE_H}" text-anchor="middle" dominant-baseline="central" font-size="11" fill="${n.ghost ? t.dim : t.text}">${esc(l)}</text>`).join('');
    return shape + txt;
  }

  // intersection of a line from node centre toward (tx,ty) with the node border
  function anchor(n, tx, ty) {
    const dx = tx - n.x, dy = ty - n.y;
    if (!dx && !dy) return { x: n.x, y: n.y };
    const hw = n.w / 2 + 4, hh = n.h / 2 + 4;
    const sx = dx !== 0 ? hw / Math.abs(dx) : Infinity;
    const sy = dy !== 0 ? hh / Math.abs(dy) : Infinity;
    const s = Math.min(sx, sy);
    return { x: n.x + dx * s, y: n.y + dy * s };
  }

  function flowSvg(g, t) {
    layoutFlow(g);
    const parts = [];
    // subgraph frames first (behind)
    for (const sub of g.subs) {
      const members = [...g.nodes.values()].filter(n => n.sub === sub.id);
      if (!members.length) continue;
      const x0 = Math.min(...members.map(n => n.x - n.w / 2)) - 14;
      const y0 = Math.min(...members.map(n => n.y - n.h / 2)) - 26;
      const x1 = Math.max(...members.map(n => n.x + n.w / 2)) + 14;
      const y1 = Math.max(...members.map(n => n.y + n.h / 2)) + 12;
      parts.push(`<rect x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" rx="9" fill="${t.soft}" fill-opacity=".28" stroke="${t.soft}"/>` +
        `<text x="${x0 + 10}" y="${y0 + 14}" font-size="10" fill="${t.dim}" font-weight="600">${esc(sub.label)}</text>`);
    }
    // edges
    for (const e of g.edges) {
      const a = g.nodes.get(e.from), b = g.nodes.get(e.to);
      if (!a || !b) continue;
      const p1 = anchor(a, b.x, b.y), p2 = anchor(b, a.x, a.y);
      const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
      const horiz = g.dir === 'LR' || g.dir === 'RL';
      const c1 = horiz ? `${mx},${p1.y}` : `${p1.x},${my}`;
      const c2 = horiz ? `${mx},${p2.y}` : `${p2.x},${my}`;
      const dash = (e.dotted || e.ghost) ? ' stroke-dasharray="4 4"' : '';
      const width = e.thick ? 2.4 : 1.4;
      parts.push(`<path d="M${p1.x},${p1.y} C${c1} ${c2} ${p2.x},${p2.y}" fill="none" stroke="${e.ghost ? t.dim : t.line}" stroke-width="${width}"${dash} marker-end="url(#vmArrow)"/>`);
      if (e.label) {
        const lw = e.label.length * TXT_W + 10;
        parts.push(`<rect x="${mx - lw / 2}" y="${my - 9}" width="${lw}" height="18" rx="4" fill="${t.bg}" fill-opacity=".92"/>` +
          `<text x="${mx}" y="${my}" text-anchor="middle" dominant-baseline="central" font-size="10" fill="${t.dim}">${esc(e.label)}</text>`);
      }
    }
    for (const n of g.nodes.values()) parts.push(nodeSvg(n, t));
    return { body: parts.join(''), count: { nodes: g.nodes.size, edges: g.edges.length } };
  }

  /* ═══ SEQUENCE DIAGRAM ════════════════════════════════════════════════ */
  function parseSeq(code) {
    const d = { type: 'seq', actors: [], msgs: [] };
    const order = new Map();
    const actor = (name, label, isActor) => {
      name = name.trim();
      if (!order.has(name)) {
        order.set(name, d.actors.length);
        d.actors.push({ id: name, label: clean(label || name), actor: !!isActor });
      } else if (label) d.actors[order.get(name)].label = clean(label);
      return name;
    };
    for (let raw of code.split('\n')) {
      const line = raw.replace(/%%.*$/, '').trim();
      if (!line || /^sequenceDiagram/i.test(line) || /^(autonumber|activate|deactivate)\b/i.test(line)) continue;
      let m = line.match(/^(participant|actor)\s+([\w.-]+)(?:\s+as\s+(.+))?$/i);
      if (m) { actor(m[2], m[3], /^actor$/i.test(m[1])); continue; }
      m = line.match(/^[Nn]ote\s+(?:over|left of|right of)\s+([\w.,\s-]+?)\s*:\s*(.*)$/);
      if (m) {
        const who = m[1].split(',').map(s => actor(s.trim()));
        d.msgs.push({ note: true, over: who, text: clean(m[2]) }); continue;
      }
      m = line.match(/^(loop|alt|opt|par|else)\b\s*(.*)$/i);
      if (m) { d.msgs.push({ frame: m[1].toLowerCase(), text: clean(m[2]) }); continue; }
      if (/^end\s*$/i.test(line)) { d.msgs.push({ frameEnd: true }); continue; }
      m = line.match(/^([\w.-]+)\s*(-{1,2})(>{1,2}|[x)])\s*([\w.-]+)\s*:\s*(.*)$/);
      if (m) {
        d.msgs.push({
          from: actor(m[1]), to: actor(m[4]), text: clean(m[5]),
          dotted: m[2] === '--', open: m[3] === '>', cross: m[3] === 'x',
        }); continue;
      }
    }
    if (!d.actors.length) throw new Error('no participants found — is this a sequenceDiagram?');
    return d;
  }

  function seqSvg(d, t) {
    const AW = 40;      // min gap around actor labels
    // actor x positions
    let x = 30;
    for (const a of d.actors) {
      a.w = Math.max(70, a.label.length * TXT_W + 26);
      a.x = x + a.w / 2; x += a.w + AW;
    }
    const rows = [];
    let y = 66;
    const frames = [];
    for (const m of d.msgs) {
      if (m.frameEnd) { const f = frames.pop(); if (f) f.y1 = y + 6; continue; }
      if (m.frame) { frames.push({ label: m.frame + (m.text ? ': ' + m.text : ''), y0: y, y1: 0 }); rows.push({ frameTitle: frames[frames.length - 1], y }); y += 26; continue; }
      m.y = y;
      rows.push(m);
      y += m.note ? 40 : 34;
    }
    frames.forEach(f => { if (!f.y1) f.y1 = y; });
    const H = y + 40;

    const parts = [];
    // lifelines + actor boxes
    for (const a of d.actors) {
      parts.push(`<line x1="${a.x}" y1="46" x2="${a.x}" y2="${H - 26}" stroke="${t.soft}" stroke-width="1.2"/>`);
      for (const yy of [12, H - 26]) {
        parts.push(`<rect x="${a.x - a.w / 2}" y="${yy}" width="${a.w}" height="30" rx="7" fill="${t.card}" stroke="${t.line}"/>` +
          `<text x="${a.x}" y="${yy + 15}" text-anchor="middle" dominant-baseline="central" font-size="11" font-weight="600" fill="${t.text}">${esc(a.label)}</text>`);
      }
    }
    // frames (behind messages, above lifelines)
    const xMin = d.actors[0].x - d.actors[0].w / 2 - 10;
    const xMax = d.actors[d.actors.length - 1].x + d.actors[d.actors.length - 1].w / 2 + 10;
    for (const f of frames.concat()) {
      parts.push(`<rect x="${xMin}" y="${f.y0 - 4}" width="${xMax - xMin}" height="${f.y1 - f.y0}" rx="6" fill="none" stroke="${t.warn}" stroke-dasharray="5 4" stroke-opacity=".55"/>` +
        `<text x="${xMin + 8}" y="${f.y0 + 9}" font-size="9.5" fill="${t.warn}" font-weight="600">${esc(f.label)}</text>`);
    }
    const ax = id => (d.actors.find(a => a.id === id) || d.actors[0]).x;
    for (const m of d.msgs) {
      if (m.frame || m.frameEnd) continue;
      if (m.note) {
        const xs = m.over.map(ax);
        const x0 = Math.min(...xs) - 30, x1 = Math.max(...xs) + 30;
        parts.push(`<rect x="${x0}" y="${m.y - 12}" width="${x1 - x0}" height="26" rx="4" fill="${t.warn}" fill-opacity=".14" stroke="${t.warn}" stroke-opacity=".5"/>` +
          `<text x="${(x0 + x1) / 2}" y="${m.y + 1}" text-anchor="middle" dominant-baseline="central" font-size="10" fill="${t.text}">${esc(m.text)}</text>`);
        continue;
      }
      const x1 = ax(m.from), x2 = ax(m.to);
      const self = m.from === m.to;
      const dash = m.dotted ? ' stroke-dasharray="4 4"' : '';
      if (self) {
        parts.push(`<path d="M${x1},${m.y - 6} h34 v14 h-34" fill="none" stroke="${t.acc}"${dash} marker-end="url(#vmArrowA)"/>`);
        parts.push(`<text x="${x1 + 42}" y="${m.y + 1}" font-size="10" fill="${t.text}">${esc(m.text)}</text>`);
      } else {
        parts.push(`<line x1="${x1}" y1="${m.y}" x2="${x2}" y2="${m.y}" stroke="${t.acc}"${dash} marker-end="url(#vmArrowA)"/>`);
        const mx = (x1 + x2) / 2;
        parts.push(`<text x="${mx}" y="${m.y - 8}" text-anchor="middle" font-size="10" fill="${t.text}">${esc(m.text)}</text>`);
        if (m.cross) parts.push(`<text x="${x2 + (x2 > x1 ? -8 : 8)}" y="${m.y}" font-size="11" fill="${t.err}" text-anchor="middle" dominant-baseline="central">✕</text>`);
      }
    }
    return { body: parts.join(''), count: { nodes: d.actors.length, edges: d.msgs.length } };
  }

  /* ═══ PIE ═════════════════════════════════════════════════════════════ */
  function parsePie(code) {
    const d = { type: 'pie', title: '', slices: [] };
    for (let raw of code.split('\n')) {
      const line = raw.replace(/%%.*$/, '').trim();
      if (!line || /^pie\b/i.test(line) && !/title/i.test(line)) {
        const tm0 = line.match(/^pie\s+(?:showData\s+)?title\s+(.*)$/i);
        if (tm0) d.title = clean(tm0[1]);
        continue;
      }
      const tm = line.match(/^title\s+(.*)$/i);
      if (tm) { d.title = clean(tm[1]); continue; }
      const sm = line.match(/^"([^"]+)"\s*:\s*([\d.]+)/);
      if (sm) d.slices.push({ label: clean(sm[1]), value: parseFloat(sm[2]) });
    }
    if (!d.slices.length) throw new Error('no pie slices found');
    return d;
  }

  function pieSvg(d, t) {
    const R = 110, CX = 150, CY = 140;
    const total = d.slices.reduce((s, x) => s + x.value, 0) || 1;
    const pal = PALETTE(t);
    let ang = -Math.PI / 2;
    const parts = [];
    if (d.title) parts.push(`<text x="${CX}" y="16" text-anchor="middle" font-size="13" font-weight="600" fill="${t.text}">${esc(d.title)}</text>`);
    d.slices.forEach((s, i) => {
      const a2 = ang + (s.value / total) * Math.PI * 2;
      const large = (a2 - ang) > Math.PI ? 1 : 0;
      const x1 = CX + R * Math.cos(ang), y1 = CY + R * Math.sin(ang);
      const x2 = CX + R * Math.cos(a2), y2 = CY + R * Math.sin(a2);
      parts.push(`<path d="M${CX},${CY} L${x1},${y1} A${R},${R} 0 ${large} 1 ${x2},${y2} z" fill="${pal[i % pal.length]}" fill-opacity=".85" stroke="${t.bg}" stroke-width="1.5"/>`);
      const mid = (ang + a2) / 2;
      const pct = Math.round((s.value / total) * 100);
      if (pct >= 5) parts.push(`<text x="${CX + (R * .62) * Math.cos(mid)}" y="${CY + (R * .62) * Math.sin(mid)}" text-anchor="middle" dominant-baseline="central" font-size="10.5" font-weight="600" fill="${t.bg}">${pct}%</text>`);
      ang = a2;
    });
    // legend
    d.slices.forEach((s, i) => {
      const ly = 40 + i * 20;
      parts.push(`<rect x="${CX + R + 34}" y="${ly - 8}" width="11" height="11" rx="3" fill="${pal[i % pal.length]}"/>` +
        `<text x="${CX + R + 51}" y="${ly - 2}" font-size="11" dominant-baseline="central" fill="${t.text}">${esc(s.label)} — ${s.value}</text>`);
    });
    return { body: parts.join(''), count: { nodes: d.slices.length, edges: 0 } };
  }

  /* ═══ THE ELEMENT ═════════════════════════════════════════════════════ */
  const CSS = `
    :host{display:block;min-height:60px;font-family:system-ui,Segoe UI,Roboto,sans-serif}
    :host([bare]){min-height:0}
    :host([fill]){height:100%}
    .wrap{border:1px solid var(--border,rgba(255,255,255,.09));border-radius:8px;
      background:var(--bg1,rgba(0,0,0,.14));overflow:hidden;display:flex;flex-direction:column;height:100%}
    :host([bare]) .wrap{border:none;background:transparent}
    :host([fill]) .vp{height:100%}
    .bar{display:flex;align-items:center;gap:4px;padding:4px 8px;
      border-bottom:1px solid var(--border,rgba(255,255,255,.09));
      font-size:9px;color:var(--dim2,#8a92a0);user-select:none;flex:0 0 auto}
    :host([bare]) .bar{display:none}
    .bar .ttl{flex:1;font-weight:600;letter-spacing:.5px;text-transform:uppercase;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--acc,#5a9e8f)}
    .bar button{background:transparent;border:1px solid var(--border,rgba(255,255,255,.12));
      color:var(--dim2,#8a92a0);border-radius:4px;font-size:9px;padding:1px 7px;cursor:pointer;
      font-family:inherit;line-height:1.5}
    .bar button:hover{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
    /* the viewport's height is its own (flex-basis auto — a basis of 0 made the height property inert, so the drawing sat
       in a 150px strip whatever it asked for); a [fill] host hands it the box instead */
    .vp{position:relative;overflow:hidden;flex:1 1 auto;min-height:80px;cursor:grab;touch-action:none;background:var(--s1,var(--bg0,transparent))}
    :host([fill]) .vp{flex:1 1 0%;height:auto;min-height:0}
    :host([bare]) .vp{min-height:40px;background:transparent}
    .vp.panning{cursor:grabbing}
    .vp svg{display:block}
    .err{padding:10px 12px;font-size:11px;color:var(--err,#c96b6b);font-family:ui-monospace,monospace;white-space:pre-wrap}
    .badge{position:absolute;right:8px;top:6px;font-family:ui-monospace,monospace;font-size:8.5px;color:var(--dim2,#8a92a0);
      background:var(--bg2,#1a1c20);border:1px solid var(--border,rgba(255,255,255,.09));border-radius:99px;padding:2px 7px;
      display:flex;gap:5px;align-items:center;pointer-events:none;z-index:2}
    .badge i{width:5px;height:5px;border-radius:50%;background:var(--warn,#c9a35a);animation:vmbp 1.2s ease-in-out infinite}
    .badge.done i{background:var(--acc2,#8fb87a);animation:none}
    @keyframes vmbp{0%,100%{opacity:1}50%{opacity:.3}}
    .ghost{display:flex;align-items:center;gap:12px;padding:22px 16px;font-size:10px;color:var(--dim2,#8a92a0);font-family:ui-monospace,monospace}
    .ghost i{width:110px;height:32px;border:1px dashed var(--dim2,#8a92a0);border-radius:6px;display:inline-block;flex-shrink:0;opacity:.7}
    .err pre{margin:6px 0 0;padding:8px;background:rgba(0,0,0,.25);border-radius:5px;
      font-size:10px;color:var(--dim2,#8a92a0);max-height:180px;overflow:auto}
    .src{display:none;margin:0;padding:8px 10px;background:rgba(0,0,0,.22);font-size:10.5px;
      font-family:ui-monospace,monospace;color:var(--text,#d8dce4);max-height:220px;overflow:auto;
      border-top:1px solid var(--border,rgba(255,255,255,.09));white-space:pre-wrap;flex:0 0 auto}
    .src.on{display:block}
  `;

  class VeraMermaid extends HTMLElement {
    constructor() {
      super();
      this._sh = this.attachShadow({ mode: 'open' });
      this._code = '';
      this._svgEl = null;
      this._view = { x: 0, y: 0, k: 1 };
      this._bounds = { w: 100, h: 100 };
      this._sh.innerHTML = `<style>${CSS}</style>
        <div class="wrap">
          <div class="bar">
            <span class="ttl"></span>
            <button data-a="fit" title="Fit to view">⛶</button>
            <button data-a="src" title="Show source">src</button>
            <button data-a="copy" title="Copy mermaid source">copy</button>
            <button data-a="svg" title="Download SVG">svg</button>
            <button data-a="png" title="Download PNG">png</button>
            <button data-a="pop" title="Float over the UI">⧉</button>
          </div>
          <div class="vp"></div>
          <pre class="src"></pre>
        </div>`;
      this._vp = this._sh.querySelector('.vp');
      this._sh.querySelector('.bar').addEventListener('click', e => {
        const a = e.target && e.target.dataset && e.target.dataset.a;
        if (a === 'fit') this.fit();
        else if (a === 'src') this._sh.querySelector('.src').classList.toggle('on');
        else if (a === 'copy') { try { navigator.clipboard.writeText(this._code); } catch (_) { } }
        else if (a === 'svg') this._download('svg');
        else if (a === 'png') this._download('png');
        else if (a === 'pop') this.dispatchEvent(new CustomEvent('vm:popout', {
          bubbles: true, composed: true,
          detail: { code: this._code, title: this.getAttribute('title') || 'Diagram' },
        }));
      });
      this._initPanZoom();
    }

    connectedCallback() {
      // The pop-out button only works when a host wired vm:popout (the chat
      // panel does) — hide it unless the `popout` attribute opts in. Checked
      // here (not the constructor) because the attribute is set after
      // createElement but before insertion.
      const pb = this._sh.querySelector('[data-a="pop"]');
      if (pb) pb.style.display = this.hasAttribute('popout') ? '' : 'none';
      const attr = this.getAttribute('code');
      const txt = (this.textContent || '').trim();
      if (attr) this.render(attr);
      else if (txt) { this.textContent = ''; this.render(txt); }
      this._sh.querySelector('.ttl').textContent = this.getAttribute('title') || 'diagram';
      LIVE.add(this);
    }
    static get observedAttributes() { return ['code', 'title', 'theme-variables', 'max-height']; }
    attributeChangedCallback(name, _o, v) {
      if (name === 'code' && v != null && v !== this._code) this.render(v);
      if (name === 'title') this._sh.querySelector('.ttl').textContent = v || 'diagram';
      if (name === 'theme-variables') { try { this._themeVars = v ? JSON.parse(v) : null; } catch (_) { this._themeVars = null; } this.retheme(); }
      if (name === 'max-height' && this._svgEl) this._size();
    }
    disconnectedCallback() { LIVE.delete(this); }
    /* an element's own token overrides (themeVariables — the name mermaid.js gives the same idea) */
    get themeVariables() { return this._themeVars || null; }
    set themeVariables(v) { this._themeVars = v && typeof v === 'object' ? Object.assign({}, v) : null; this.retheme(); }
    _theme() { return THEME(this._themeVars); }
    static theme() { return THEME(); }
    /* the same drawing in the theme as it is now; the view (a pan, a zoom) is kept */
    retheme() { if (!this._code || !this._svgEl) return; const keep = Object.assign({}, this._view); this._held = this._held || false; try { this.render(this._code); } catch (_) { return; } if (this._held) { this._view = keep; this._apply(); } }
    /* the height the drawing takes at a width — its aspect kept, at most 1.6× its own size, never under 80 */
    naturalHeight(width) { const b = this._bounds; if (!b || !b.w || !this._svgEl) return 0; const w = Math.max(1, Number(width) || this._vp.clientWidth || 400); const k = Math.min(w / b.w, 1.25); return Math.max(80, Math.round(b.h * k)); }

    /* what kind of diagram the source is, and the graph for the flow-like kinds */
    static _kind(code) {
      const head = (String(code || '').split('\n').find(l => l.trim()) || '').trim().toLowerCase();
      if (/^sequencediagram/.test(head)) return 'sequence';
      if (/^pie\b/.test(head)) return 'pie';
      if (/^statediagram/.test(head)) return 'state';
      return 'flowchart';
    }
    static _graph(code, kind) {
      if (kind === 'state') return parseState(code);
      const head = (String(code || '').split('\n').find(l => l.trim()) || '').trim().toLowerCase();
      return parseFlow(/^(graph|flowchart)\b/.test(head) ? code : 'graph TD\n' + code);
    }
    _build(code, t) {
      const type = VeraMermaid._kind(code);
      if (type === 'sequence') return { type, result: seqSvg(parseSeq(code), t) };
      if (type === 'pie') return { type, result: pieSvg(parsePie(code), t) };
      return { type, result: flowSvg(VeraMermaid._graph(code, type), t) };
    }
    _badge(text, live) {
      if (!this._badgeEl) { this._badgeEl = document.createElement('span'); this._badgeEl.className = 'badge'; }
      const b = this._badgeEl;
      if (text == null) { b.remove(); return; }
      b.className = 'badge' + (live ? '' : ' done'); b.innerHTML = '<i></i>' + esc(text);
      if (b.parentNode !== this._vp) this._vp.appendChild(b);
    }

    /* main entry */
    render(code) {
      this._code = String(code || '').trim();
      this._sh.querySelector('.src').textContent = this._code;
      const t = this._theme(); this._held = false;
      let result, type;
      try {
        ({ type, result } = this._build(this._code, t));
      } catch (err) {
        this._svgEl = null;
        this._vp.innerHTML = `<div class="err">⚠ mermaid parse failed: ${esc(err && err.message || err)}<pre>${esc(this._code.slice(0, 1200))}</pre></div>`;
        this.dispatchEvent(new CustomEvent('vm:error', { detail: { message: String(err && err.message || err) } }));
        return;
      }
      this._paint(result, type);
      // the closing fence settles the layout once: the badge says so, then goes
      if (this._live) { this._live = false; this._badge(`${result.count.nodes || 0} nodes · ${result.count.edges || 0} edges · settled`, false); clearTimeout(this._badgeT); this._badgeT = setTimeout(() => this._badge(null), 2500); }
    }

    /* the same, while the source is still arriving (see the header) */
    stream(code) {
      code = String(code || '').replace(/\r\n?/g, '\n');
      this._live = true;
      const lines = code.split('\n');
      const partial = /\n$/.test(code) ? '' : (lines.pop() || '');
      if (/\n$/.test(code)) lines.pop();
      const full = lines.join('\n');
      this._code = (full + (partial ? '\n' + partial : '')).trim();
      this._sh.querySelector('.src').textContent = this._code;
      const t = this._theme();
      const kind = VeraMermaid._kind(this._code);
      const pending = partial.trim() ? 1 : 0;
      let result, type;
      try {
        if (kind === 'sequence' || kind === 'pie') { ({ type, result } = this._build(full, t)); }
        else {
          let gA = null, gB = null;
          try { gA = VeraMermaid._graph(full, kind); } catch (_) { gA = null; }
          if (pending) { try { gB = VeraMermaid._graph(full + '\n' + partial, kind); } catch (_) { gB = null; } }
          if (gB && gA) {
            for (const [id, n] of gB.nodes) if (!gA.nodes.has(id)) n.ghost = true;
            for (let i = gA.edges.length; i < gB.edges.length; i++) gB.edges[i].ghost = true;
          } else if (gB && !gA) {
            for (const n of gB.nodes.values()) n.ghost = true;
            for (const e of gB.edges) e.ghost = true;
          }
          const g = gB || gA;
          if (!g) throw new Error('waiting for a complete line');
          type = kind; result = flowSvg(g, t);
        }
      } catch (err) {
        // nothing whole yet — a ghost frame, never an error banner; a previous drawing stays
        if (!this._svgEl) this._vp.innerHTML = '<div class="ghost"><i></i>lines parse as they close · a line is complete at its newline</div>';
        this._badge('0 nodes · waiting for a complete line', true);
        return;
      }
      this._paint(result, type);
      this._badge(`${result.count.nodes || 0} nodes · ${result.count.edges || 0} edges${pending ? ' · ' + pending + ' pending' : ''}`, true);
      this.dispatchEvent(new CustomEvent('vm:stream', { detail: { type, pending, ...result.count } }));
    }

    _paint(result, type) {
      const svgNs = 'http://www.w3.org/2000/svg';
      this._vp.innerHTML = '';
      const svg = document.createElementNS(svgNs, 'svg');
      svg.setAttribute('xmlns', svgNs);
      const t2 = this._theme();
      svg.innerHTML = `<defs>
          <marker id="vmArrow" markerWidth="9" markerHeight="9" refX="7.5" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 z" fill="${t2.line}"/></marker>
          <marker id="vmArrowA" markerWidth="9" markerHeight="9" refX="7.5" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 z" fill="${t2.acc}"/></marker>
        </defs><g class="root">${result.body}</g>`;
      this._vp.appendChild(svg);
      this._svgEl = svg;
      // measure content, size viewport, fit
      const g = svg.querySelector('g.root');
      let bb;
      try { bb = g.getBBox(); } catch (_) { bb = { x: 0, y: 0, width: 400, height: 200 }; }
      this._blind = !(bb.width > 0 && bb.height > 0);   // measured while folded or unshown (a zero box): drawn again when it has one
      this._bounds = { x: bb.x - 16, y: bb.y - 16, w: bb.width + 32, h: bb.height + 32 };
      const natural = this._size();
      this.fit();
      if (this._badgeEl && this._badgeEl.isConnected === false && this._live) this._vp.appendChild(this._badgeEl);
      this.dispatchEvent(new CustomEvent('vm:rendered', { detail: { type, ...result.count, width: this._vp.clientWidth || 0, height: this._vp.clientHeight || 0, natural } }));
    }
    /* the viewport's height: the drawing's own at this width (its aspect kept), up to max-height — a [fill] host
       or one given a height keeps the box it has */
    _size() {
      const maxH = parseInt(this.getAttribute('max-height') || '720', 10) || 720;
      const natural = this.naturalHeight(this._vp.clientWidth || 400);
      if (!this.style.height && !this.hasAttribute('fill')) this._vp.style.height = Math.min(maxH, natural) + 'px';
      return natural;
    }

    fit() {
      if (!this._svgEl) return;
      const vw = this._vp.clientWidth || 400, vh = this._vp.clientHeight || 240;
      const b = this._bounds;
      const k = Math.min(vw / b.w, vh / b.h, 1.6);
      this._held = false;   // the fitted view: a resize fits again; a pan or a zoom holds it
      this._view.k = k > 0 && isFinite(k) ? k : 1;
      this._view.x = (vw - b.w * this._view.k) / 2 - b.x * this._view.k;
      this._view.y = (vh - b.h * this._view.k) / 2 - b.y * this._view.k;
      this._apply();
    }
    _apply() {
      if (!this._svgEl) return;
      const { x, y, k } = this._view;
      this._svgEl.setAttribute('width', this._vp.clientWidth || 400);
      this._svgEl.setAttribute('height', this._vp.clientHeight || 240);
      const g = this._svgEl.querySelector('g.root');
      if (g) g.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
    }
    _initPanZoom() {
      let drag = null;
      this._vp.addEventListener('pointerdown', e => {
        if (e.button !== 0) return;
        drag = { x: e.clientX, y: e.clientY, vx: this._view.x, vy: this._view.y }; this._held = true;
        this._vp.classList.add('panning');
        try { this._vp.setPointerCapture(e.pointerId); } catch (_) { }
      });
      this._vp.addEventListener('pointermove', e => {
        if (!drag) return;
        this._view.x = drag.vx + (e.clientX - drag.x);
        this._view.y = drag.vy + (e.clientY - drag.y);
        this._apply();
      });
      const up = () => { drag = null; this._vp.classList.remove('panning'); };
      this._vp.addEventListener('pointerup', up);
      this._vp.addEventListener('pointercancel', up);
      this._vp.addEventListener('wheel', e => {
        if (!this._svgEl) return;
        e.preventDefault();
        const r = this._vp.getBoundingClientRect();
        const mx = e.clientX - r.left, my = e.clientY - r.top;
        const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        const k2 = Math.max(.15, Math.min(6, this._view.k * f)); this._held = true;
        this._view.x = mx - (mx - this._view.x) * (k2 / this._view.k);
        this._view.y = my - (my - this._view.y) * (k2 / this._view.k);
        this._view.k = k2;
        this._apply();
      }, { passive: false });
      // the width changes (a column resized, a slot placed): the viewport takes the drawing's height at the new width and
      // fits again — unless a pan or a zoom is held, which only follows the box
      if (window.ResizeObserver) { let lastW = 0; new ResizeObserver(() => { if (!this._svgEl) return; const w = this._vp.clientWidth; if (w && this._blind && this._code) { this.render(this._code); lastW = w; return; } if (w && w !== lastW) { lastW = w; this._size(); } if (this._held) this._apply(); else this.fit(); }).observe(this._vp); }
    }

    getSvg() {
      if (!this._svgEl) return '';
      const cl = this._svgEl.cloneNode(true);
      const b = this._bounds;
      cl.setAttribute('viewBox', `${b.x} ${b.y} ${b.w} ${b.h}`);
      cl.setAttribute('width', Math.round(b.w)); cl.setAttribute('height', Math.round(b.h));
      const g = cl.querySelector('g.root'); if (g) g.removeAttribute('transform');
      const t = this._theme();
      const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      bgRect.setAttribute('x', b.x); bgRect.setAttribute('y', b.y);
      bgRect.setAttribute('width', b.w); bgRect.setAttribute('height', b.h);
      bgRect.setAttribute('fill', t.bg);
      cl.insertBefore(bgRect, cl.firstChild.nextSibling);
      return new XMLSerializer().serializeToString(cl);
    }
    _download(fmt) {
      const src = this.getSvg(); if (!src) return;
      if (fmt === 'svg') {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([src], { type: 'image/svg+xml' }));
        a.download = 'diagram.svg'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        return;
      }
      const img = new Image();
      const b = this._bounds;
      img.onload = () => {
        const cv = document.createElement('canvas');
        cv.width = Math.round(b.w * 2); cv.height = Math.round(b.h * 2);
        const ctx = cv.getContext('2d');
        ctx.drawImage(img, 0, 0, cv.width, cv.height);
        const a = document.createElement('a');
        a.href = cv.toDataURL('image/png'); a.download = 'diagram.png'; a.click();
      };
      img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(src)));
    }
  }
  VeraMermaid.retheme = rethemeAll;
  window.VeraMermaid = VeraMermaid;
  customElements.define('vera-mermaid', VeraMermaid);
})();

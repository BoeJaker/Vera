/**
 * sparkline_element.js — <vera-sparkline>
 * ============================================================================
 * A small canvas history chart for dashboard tiles (CPU/RAM/proxmox/docker/
 * temp/queue over time). Forked/simplified from the markets module's QChart
 * engine (vera/markets/markets_studio_panel.html.blk0.js) — keeps its
 * ResizeObserver sizing, requestAnimationFrame + dirty-flag redraw loop, and
 * MutationObserver theme repaint, but strips everything QChart has that this
 * doesn't need (candles, overlays, multi-pane, drawing tools) down to: up to
 * a few line/area series with a live legend, min/max shading, and a hover
 * crosshair + tooltip with the exact value/time for every series at once.
 *
 * Public API:
 *   el.setSeries(points, label)      — single series, points: [{t,v}], oldest→newest.
 *                                        label is shown in the legend (always
 *                                        rendered — a chart with no legend at
 *                                        all leaves no way to tell what it is).
 *   el.setMultiSeries([{label,color,unit,points}, ...])  — up to ~3 series with
 *                                        a live legend (current value per series)
 *   el.setUnit(text)                 — single-series hover unit, e.g. '%', '°C'
 *
 * Always draws a scale: a top/bottom gridline labelled with the actual min/max
 * value of the visible range (not just an unlabelled squiggle), and reserves
 * real vertical space for the legend row so it never sits on top of the line.
 *
 * Zero backend dependency — callers fetch /sysmon/history (or similar) once and
 * hand the mapped series to each mounted instance; this component only draws.
 */
(function () {
  'use strict';
  if (window.customElements && window.customElements.get('vera-sparkline')) return;

  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, f) => { const v = cs.getPropertyValue(n).trim(); return v || f; };
    return {
      ink:   g('--ink', '#d9e1ed'),
      dim2:  g('--dim2', '#7e8ba0'),
      acc:   g('--acc', '#5b8cff'),
      acc2:  g('--acc2', '#8fb87a'),
      warn:  g('--warn', '#c9a45a'),
      bg0:   g('--bg0', '#0b0f17'),
      border:g('--border', '#2a3140'),
      mono:  g('--mono', 'ui-monospace,Consolas,monospace'),
    };
  }
  function hexA(hex, a) {
    if (!/^#/.test(hex)) return hex;
    const n = parseInt(hex.slice(1), 16), r = n >> 16 & 255, g = n >> 8 & 255, b = n & 255;
    return `rgba(${r},${g},${b},${a})`;
  }

  class VeraSparkline extends HTMLElement {
    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._series = [];        // [{label, color, unit, points:[{t,v}]}]
      this._unit = this.getAttribute('unit') || '';
      this._hoverIdx = null;
      this._dirty = true;
      this._raf = 0;
      this._col = readTheme();
      this._loop = this._loop.bind(this);
      this._build();
    }

    connectedCallback() {
      this._ro = new ResizeObserver(() => { this._resize(); this._dirty = true; });
      this._ro.observe(this);
      this._mo = new MutationObserver(() => { this._col = readTheme(); this._dirty = true; });
      this._mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style'] });
      this._resize();
      this._raf = requestAnimationFrame(this._loop);
    }
    disconnectedCallback() {
      try { this._ro.disconnect(); } catch (_) {}
      try { this._mo.disconnect(); } catch (_) {}
      cancelAnimationFrame(this._raf);
    }

    setSeries(points, label) {
      this._series = [{ label: label || '', color: 'acc', unit: this._unit, points: Array.isArray(points) ? points.slice() : [] }];
      this._dirty = true;
    }
    setMultiSeries(series) {
      this._series = (series || []).map(s => ({
        label: s.label || '', color: s.color || 'acc', unit: s.unit || '',
        points: Array.isArray(s.points) ? s.points.slice() : [],
      }));
      this._dirty = true;
    }
    setUnit(u) { this._unit = u || ''; if (this._series[0]) this._series[0].unit = u; this._dirty = true; }

    _build() {
      this._sr.innerHTML = `
        <style>
          :host{display:block;width:100%;height:100%;min-height:32px;position:relative}
          canvas{width:100%;height:100%;display:block;cursor:crosshair}
          .legend{position:absolute;top:1px;left:2px;display:flex;gap:6px;flex-wrap:wrap;
                  font-size:8.5px;font-family:var(--mono,monospace);pointer-events:none;z-index:1}
          .legend span{background:var(--bg0,#0b0f17);padding:0 3px;border-radius:2px;opacity:.95}
          .legend b{font-weight:700}
          .tip{position:absolute;pointer-events:none;top:1px;right:2px;font-size:9.5px;
               font-family:var(--mono,monospace);white-space:pre;text-align:right;
               background:var(--bg0,#0b0f17);border:1px solid var(--border,#2a3140);
               border-radius:3px;padding:1px 6px;opacity:0;transition:opacity .1s;
               color:var(--ink,#d9e1ed);z-index:2}
        </style>
        <canvas></canvas>
        <div class="legend"></div>
        <div class="tip"></div>
      `;
      this._cv = this._sr.querySelector('canvas');
      this._ctx = this._cv.getContext('2d');
      this._legendEl = this._sr.querySelector('.legend');
      this._tip = this._sr.querySelector('.tip');
      this._cv.addEventListener('mousemove', e => this._onHover(e));
      this._cv.addEventListener('mouseleave', () => {
        this._hoverIdx = null; this._tip.style.opacity = 0; this._dirty = true;
      });
    }

    _resize() {
      const r = this.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this._w = Math.max(20, r.width); this._h = Math.max(20, r.height);
      this._cv.width = this._w * dpr; this._cv.height = this._h * dpr;
      this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    _xAt(i, n) {
      const plotW = this._plotW != null ? this._plotW : this._w;
      return n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW;
    }
    _maxLen() { return this._series.reduce((m, s) => Math.max(m, s.points.length), 0); }

    _onHover(e) {
      const n = this._maxLen(); if (!n) return;
      const rect = this._cv.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const plotW = this._plotW != null ? this._plotW : this._w;
      let idx = Math.round((x / plotW) * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));
      this._hoverIdx = idx;
      const lines = [];
      let ts = null;
      this._series.forEach(s => {
        const p = s.points[idx];
        if (!p) return;
        if (ts == null) ts = p.t;
        lines.push(`${s.label ? s.label + ': ' : ''}${p.v == null ? '—' : p.v}${s.unit || ''}`);
      });
      if (ts != null) {
        const d = new Date(ts * 1000);
        const hh = String(d.getHours()).padStart(2, '0'),
              mm = String(d.getMinutes()).padStart(2, '0'),
              ss = String(d.getSeconds()).padStart(2, '0');
        lines.push(`@ ${hh}:${mm}:${ss}`);
        this._tip.textContent = lines.join('\n');
        this._tip.style.opacity = 1;
      } else {
        this._tip.style.opacity = 0;
      }
      this._dirty = true;
    }

    _loop() {
      if (this._dirty) { this._draw(); this._dirty = false; }
      this._raf = requestAnimationFrame(this._loop);
    }

    _lastVal(pts) {
      for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v != null) return pts[i].v;
      return null;
    }

    _draw() {
      const ctx = this._ctx, w = this._w, h = this._h, col = this._col;
      ctx.clearRect(0, 0, w, h);
      const n = this._maxLen();
      const anyData = this._series.some(s => s.points.some(p => p.v != null));
      const hasLegend = this._series.some(s => s.label) && this._series.length;
      if (!n || !anyData) {
        ctx.fillStyle = col.dim2; ctx.font = '10px ' + col.mono; ctx.textAlign = 'center';
        ctx.fillText('no data', w / 2, h / 2 + 3);
        this._legendEl.innerHTML = '';
        return;
      }

      // shared y-range across all series so multi-series lines are comparable
      let lo = Infinity, hi = -Infinity;
      this._series.forEach(s => s.points.forEach(p => {
        if (p.v != null) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
      }));
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (lo === hi) { lo -= 1; hi += 1; }
      // Reserve real space at the top for the legend row (rather than just
      // overlaying it) so the line/area never visually collides with the
      // text, and at the bottom/right for the axis scale labels.
      const padTop = hasLegend ? 13 : 4, padBot = 11, padR = 30;
      const plotW = w - padR;
      this._plotW = plotW;   // _xAt() (used here and by _onHover) reads this
      const yOf = v => h - padBot - ((v - lo) / (hi - lo)) * (h - padTop - padBot);

      // min/max shading band (once, using the shared range)
      ctx.fillStyle = 'rgba(126,139,160,.07)';
      ctx.fillRect(0, yOf(hi), plotW, Math.max(0, yOf(lo) - yOf(hi)));

      // Scale: an actual labelled axis, not just an unlabelled squiggle — a
      // top gridline at the max, a bottom one at the min, each with its real
      // value printed on the right edge.
      const unitOf = this._series[0] && this._series[0].unit || '';
      ctx.strokeStyle = 'rgba(126,139,160,.18)'; ctx.lineWidth = 1;
      ctx.font = '8px ' + col.mono; ctx.fillStyle = col.dim2; ctx.textAlign = 'left';
      [{ v: hi, y: yOf(hi) }, { v: lo, y: yOf(lo) }].forEach(g => {
        ctx.beginPath(); ctx.moveTo(0, g.y + 0.5); ctx.lineTo(plotW, g.y + 0.5); ctx.stroke();
        const label = (Number.isInteger(g.v) ? g.v : g.v.toFixed(1)) + unitOf;
        ctx.fillText(label, plotW + 3, Math.min(h - 2, Math.max(8, g.y + 3)));
      });

      this._series.forEach((s, si) => {
        const color = col[s.color] || col.acc;
        const pts = s.points;
        ctx.beginPath();
        let started = false, lx = null, ly = null;
        pts.forEach((p, i) => {
          if (p.v == null) { started = false; return; }
          const x = this._xAt(i, n), y = yOf(p.v);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
          lx = x; ly = y;
        });
        ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();
        // area fill only for the first/primary series, to keep multi-series legible
        if (si === 0) {
          const lastIdx = pts.length - 1;
          ctx.lineTo(this._xAt(lastIdx, n), h);
          ctx.lineTo(this._xAt(0, n), h);
          ctx.closePath();
          const grad = ctx.createLinearGradient(0, 0, 0, h);
          grad.addColorStop(0, hexA(color, 0.20));
          grad.addColorStop(1, hexA(color, 0));
          ctx.fillStyle = grad; ctx.fill();
        }
        if (lx != null) { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(lx, ly, 1.8, 0, Math.PI * 2); ctx.fill(); }
      });

      // hover crosshair
      if (this._hoverIdx != null) {
        const x = this._xAt(this._hoverIdx, n);
        ctx.strokeStyle = col.dim2; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); ctx.setLineDash([]);
        this._series.forEach(s => {
          const p = s.points[this._hoverIdx];
          if (p && p.v != null) {
            const color = col[s.color] || col.acc;
            ctx.fillStyle = color;
            ctx.beginPath(); ctx.arc(x, yOf(p.v), 2.5, 0, Math.PI * 2); ctx.fill();
          }
        });
      }

      // legend (only worth showing when there's a label to explain the color)
      if (this._series.some(s => s.label)) {
        this._legendEl.innerHTML = this._series.map(s => {
          const v = this._lastVal(s.points);
          const color = col[s.color] || col.acc;
          return `<span style="color:${color}">${s.label} <b>${v == null ? '—' : v}${s.unit || ''}</b></span>`;
        }).join('');
      } else {
        this._legendEl.innerHTML = '';
      }
    }
  }

  customElements.define('vera-sparkline', VeraSparkline);
})();

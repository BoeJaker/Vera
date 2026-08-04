/**
 * sparkline_element.js — <vera-sparkline>
 * ============================================================================
 * A small canvas history sparkline for dashboard tiles (CPU/RAM/proxmox/docker/
 * temp over time). Forked/simplified from the markets module's QChart engine
 * (vera/markets/markets_studio_panel.html.blk0.js) — keeps its ResizeObserver
 * sizing, requestAnimationFrame + dirty-flag redraw loop, and MutationObserver
 * theme repaint, but strips everything QChart has that a sparkline doesn't need
 * (candles, overlays, multi-pane, drawing tools) down to: one line/area series,
 * min/max shading, and a hover crosshair + tooltip with the exact value/time.
 *
 * Public API:
 *   el.setSeries(points)   — points: [{t: epochSeconds, v: number|null}, ...],
 *                            oldest → newest
 *   el.setLabel(text)      — optional, currently unused by the default paint
 *                            (host tiles show their own label; kept for future use)
 *   el.setUnit(text)       — appended to the hover tooltip value, e.g. '%', '°C'
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
      this._pts = [];          // [{t,v}], oldest → newest
      this._label = this.getAttribute('label') || '';
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

    setSeries(points) { this._pts = Array.isArray(points) ? points.slice() : []; this._dirty = true; }
    setLabel(t) { this._label = t || ''; this._dirty = true; }
    setUnit(u) { this._unit = u || ''; this._dirty = true; }

    _build() {
      this._sr.innerHTML = `
        <style>
          :host{display:block;width:100%;height:100%;min-height:32px;position:relative}
          canvas{width:100%;height:100%;display:block;cursor:crosshair}
          .tip{position:absolute;pointer-events:none;top:1px;font-size:10px;
               font-family:var(--mono,monospace);white-space:nowrap;
               background:var(--bg0,#0b0f17);border:1px solid var(--border,#2a3140);
               border-radius:3px;padding:1px 5px;opacity:0;transition:opacity .1s;
               color:var(--ink,#d9e1ed);z-index:2}
        </style>
        <canvas></canvas>
        <div class="tip"></div>
      `;
      this._cv = this._sr.querySelector('canvas');
      this._ctx = this._cv.getContext('2d');
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
    _xAt(i, n) { return n <= 1 ? this._w / 2 : (i / (n - 1)) * this._w; }

    _onHover(e) {
      if (!this._pts.length) return;
      const rect = this._cv.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const n = this._pts.length;
      let idx = Math.round((x / this._w) * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));
      this._hoverIdx = idx;
      const p = this._pts[idx];
      if (p && p.v != null) {
        const d = new Date(p.t * 1000);
        const hh = String(d.getHours()).padStart(2, '0'),
              mm = String(d.getMinutes()).padStart(2, '0'),
              ss = String(d.getSeconds()).padStart(2, '0');
        this._tip.textContent = `${p.v}${this._unit} @ ${hh}:${mm}:${ss}`;
        this._tip.style.left = Math.min(this._w - 74, Math.max(2, this._xAt(idx, n) - 30)) + 'px';
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

    _draw() {
      const ctx = this._ctx, w = this._w, h = this._h, col = this._col;
      ctx.clearRect(0, 0, w, h);
      const withVal = this._pts.filter(p => p.v != null);
      if (withVal.length < 2) {
        ctx.fillStyle = col.dim2; ctx.font = '10px ' + col.mono; ctx.textAlign = 'center';
        ctx.fillText('no data', w / 2, h / 2 + 3);
        return;
      }
      let lo = Math.min(...withVal.map(p => p.v)), hi = Math.max(...withVal.map(p => p.v));
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = 3;
      const yOf = v => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);
      const n = this._pts.length;

      // min/max shading band
      ctx.fillStyle = 'rgba(126,139,160,.07)';
      ctx.fillRect(0, yOf(hi), w, Math.max(0, yOf(lo) - yOf(hi)));

      // line
      ctx.beginPath();
      let started = false;
      this._pts.forEach((p, i) => {
        if (p.v == null) { started = false; return; }
        const x = this._xAt(i, n), y = yOf(p.v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = col.acc; ctx.lineWidth = 1.4; ctx.stroke();

      // area fill under the line
      const lastIdx = this._pts.length - 1;
      ctx.lineTo(this._xAt(lastIdx, n), h);
      ctx.lineTo(this._xAt(0, n), h);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, hexA(col.acc, 0.22));
      grad.addColorStop(1, hexA(col.acc, 0));
      ctx.fillStyle = grad; ctx.fill();

      // hover crosshair
      if (this._hoverIdx != null) {
        const x = this._xAt(this._hoverIdx, n);
        ctx.strokeStyle = col.dim2; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); ctx.setLineDash([]);
        const p = this._pts[this._hoverIdx];
        if (p && p.v != null) {
          ctx.fillStyle = col.acc;
          ctx.beginPath(); ctx.arc(x, yOf(p.v), 2.5, 0, Math.PI * 2); ctx.fill();
        }
      }
    }
  }

  customElements.define('vera-sparkline', VeraSparkline);
})();

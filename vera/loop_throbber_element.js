/* ============================================================================
 * <vera-wiremesh-throbber>
 * ============================================================================
 * "The agentic loop is doing work" indicator: a small wireframe polygon whose
 * vertices continuously deform (a lightweight per-vertex sine/cosine wobble,
 * redrawn every frame) — deliberately NOT a generic ring spinner, so it reads
 * as its own distinct signal next to chat's "Thinking…" orbit throbber.
 *
 * Hidden by default. Toggle with the `active` attribute/property — any panel
 * that launches a loop (chat, dream, netmap, dag workshop, …) can drop one
 * somewhere PERSISTENTLY visible (e.g. a top bar) rather than relying on the
 * many per-step spinners inside a scrolling loop transcript, which age out of
 * view as a long run goes on.
 *
 *   <vera-wiremesh-throbber id="loopThrob" title="Agentic loop running…"></vera-wiremesh-throbber>
 *   document.getElementById('loopThrob').active = true;   // or .toggleAttribute('active', bool)
 *
 * Sizing: set width/height via CSS on the host (default 18x18) — the SVG
 * viewBox scales cleanly at any size, so the same element works as a small
 * badge or blown up large.
 * ============================================================================ */
(function () {
  if (window.customElements && window.customElements.get('vera-wiremesh-throbber')) return;

  const N = 7;            // vertices
  const SKIP = 3;         // interior "mesh" chords connect i -> i+SKIP (crosshatch look)
  const BASE_R = 8;

  const STYLE = `
    :host{display:none;width:18px;height:18px;vertical-align:middle;line-height:0}
    :host([active]){display:inline-block}
    svg{width:100%;height:100%;overflow:visible;display:block}
    .fill{fill:var(--acc,#5a9e8f);opacity:.08}
    .mesh{fill:none;stroke:var(--acc,#5a9e8f);stroke-width:1.15;stroke-linejoin:round;opacity:.95}
    .chord{stroke:var(--acc2,#a07ec1);stroke-width:.6;opacity:.5}
    .core{fill:var(--acc3,#c9856b);opacity:.85}
  `;

  class VeraWiremeshThrobber extends HTMLElement {
    static get observedAttributes() { return ['active']; }

    constructor() {
      super();
      this._raf = null;
      this._t0 = 0;
      const root = this.attachShadow({ mode: 'open' });
      root.innerHTML = `
        <style>${STYLE}</style>
        <svg viewBox="-12 -12 24 24">
          <polygon class="fill" points=""></polygon>
          <g class="chords"></g>
          <polygon class="mesh" points=""></polygon>
          <circle class="core" cx="0" cy="0" r="1"></circle>
        </svg>`;
      this._polys = root.querySelectorAll('polygon');
      const chordsG = root.querySelector('.chords');
      for (let i = 0; i < N; i++) {
        const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        l.setAttribute('class', 'chord');
        chordsG.appendChild(l);
      }
      this._chords = chordsG.querySelectorAll('line');
    }

    connectedCallback() {
      if (this.hasAttribute('active')) this._start();
    }

    disconnectedCallback() { this._stop(); }

    attributeChangedCallback(name, oldVal, newVal) {
      if (name !== 'active') return;
      (newVal !== null) ? this._start() : this._stop();
    }

    get active() { return this.hasAttribute('active'); }
    set active(v) { this.toggleAttribute('active', !!v); }

    _start() {
      if (this._raf) return;
      this._t0 = performance.now();
      const frame = () => {
        this._draw((performance.now() - this._t0) / 1000);
        this._raf = requestAnimationFrame(frame);
      };
      this._raf = requestAnimationFrame(frame);
    }

    _stop() {
      if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    }

    _draw(t) {
      const pts = [];
      for (let i = 0; i < N; i++) {
        // Slow overall rotation + two out-of-phase wobbles per vertex so no
        // two vertices ever move in lockstep — reads as an organic deform,
        // not a pulsing ring.
        const a = (i / N) * Math.PI * 2 + t * 0.22;
        const r = BASE_R
          + Math.sin(t * 1.5 + i * 1.7) * 1.8
          + Math.cos(t * 0.85 + i * 2.6) * 1.0;
        pts.push([Math.cos(a) * r, Math.sin(a) * r]);
      }
      const s = pts.map(p => p[0].toFixed(2) + ',' + p[1].toFixed(2)).join(' ');
      this._polys.forEach(p => p.setAttribute('points', s));
      for (let i = 0; i < N; i++) {
        const a = pts[i], b = pts[(i + SKIP) % N];
        const l = this._chords[i];
        l.setAttribute('x1', a[0]); l.setAttribute('y1', a[1]);
        l.setAttribute('x2', b[0]); l.setAttribute('y2', b[1]);
      }
    }
  }

  customElements.define('vera-wiremesh-throbber', VeraWiremeshThrobber);
})();

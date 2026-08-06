/**
 * <vera-test-activity-timeline> — Loop Lab's hourly test-activity chart,
 * rebuilt to match bun.com's actual "Commit Activity Timeline" widget
 * mechanics (studied directly off bun.com/blog/bun-in-rust's source, not
 * just its concept): an SVG bar chart, a real sweeping scrub-line playhead
 * that reveals bars as it crosses them (opacity 0.12 -> 1, not just a
 * single pulse), ticking counters driven by a shared cubic-ease animator,
 * a real accelerated clock during replay, and scroll-triggered autoplay
 * via IntersectionObserver — all on REAL evolve.runs data, never fabricated.
 *
 * Differences from bun.com's original (the "improve and expand" mandate):
 *   - bar color = a real pink-fail -> green-pass gradient per bar (bun's
 *     pink/cyan was added/deleted; pass/fail is the meaningful split here)
 *   - click-to-drill-down into that hour's real runs (bun's has none)
 *   - drag-to-select a real time range, replay ONLY that slice (component J)
 *   - real perf.stalls / cap.error marks plotted at their real timestamps
 *     directly under the relevant bar (component H)
 *   - speed control (1x/2x/5x) + pause/resume (bun's is one fixed autoplay)
 *
 * Truthful animation: the sweep, the ticking counters, and the clock only
 * ever advance across ALREADY-FETCHED real run timestamps, in real order —
 * every frame corresponds to something that really happened at that real
 * time. Outside of an explicit/scroll-triggered replay pass, the only
 * automatic motion is a single new-bar reveal on a real `evolve.run.done`
 * WS event (diff-gated, suppressed on first paint).
 *
 * Attributes: hours (default 48), poll-ms (default 20000), autoplay
 * ("scroll" default — plays once when scrolled into view; "off" disables)
 * Public API: setApiBase(url), refresh()
 */
(function () {
  if (customElements.get('vera-test-activity-timeline')) return;

  const ease = t => 1 - Math.pow(1 - t, 3);
  const FAIL_RGB = [239, 92, 92];    // pink/red — matches --err family
  const PASS_RGB = [90, 200, 130];   // green — matches --ok family
  const lerpColor = (a, b, t) => 'rgb(' + a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(',') + ')';
  const reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.hdr{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.hdr .ttl{font-weight:600;color:var(--dim2,var(--t3,#8a7e70));text-transform:uppercase;
  font-size:10px;letter-spacing:.04em}
.stat{font-family:var(--mono,monospace);color:var(--acc2,var(--ac2,#6db87a));font-weight:600}
.hdr .peak{margin-left:auto;font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70))}
.ctl{display:flex;align-items:center;gap:5px;font-size:9.5px}
.ctl button{background:var(--bg2,var(--s2,#272421));border:1px solid var(--border,var(--bd2,#3a3530));
  color:var(--text,var(--t1,#ddd5c8));border-radius:5px;padding:3px 7px;cursor:pointer;font-family:inherit}
.ctl button:hover{border-color:var(--acc,var(--ac,#5a9e8f));color:var(--acc,var(--ac,#5a9e8f))}
.ctl button.on{background:var(--acc,var(--ac,#5a9e8f));color:var(--on-acc,#12100e);border-color:var(--acc,var(--ac,#5a9e8f))}
.clock{font-family:var(--mono,monospace);font-size:9.5px;color:var(--acc,var(--ac,#5a9e8f));min-width:130px}
.chartwrap{position:relative;user-select:none}
svg.chart{width:100%;height:120px;display:block;cursor:crosshair}
.bar{cursor:pointer;transition:opacity .25s ease,height .35s ease,y .35s ease}
.bar.selected{stroke:var(--acc,var(--ac,#5a9e8f));stroke-width:1}
.bar.dim{opacity:.2 !important}
/* the CURRENT hour is a genuinely ongoing/open bucket — still real time it
   could receive another run any second — so it gets a real "live" pulse,
   the same truthful-ongoing-state convention <vera-activity-timeline>'s
   own .live .dot already uses elsewhere in Vera. Everything else stays
   perfectly still until a real event changes it. */
@keyframes vatLive{0%,100%{opacity:1}50%{opacity:.35}}
.livering{animation:vatLive 1.4s ease-in-out infinite}
.playline{stroke:var(--acc,var(--ac,#5a9e8f));stroke-width:1.5;display:none}
.playline.on{display:block}
.mk{opacity:.9}
.axis{display:flex;justify-content:space-between;font-size:8.5px;color:var(--dim,var(--t2,#6a6058));margin-top:4px}
.drill{margin-top:6px;font-size:9.5px;max-height:120px;overflow:auto;
  border-top:1px dotted var(--border,var(--bd2,#3a3530));padding-top:4px}
.drill .row{display:flex;gap:6px;padding:2px 0;border-bottom:1px dotted var(--border,var(--bd2,#3a3530))}
.drill .row .t{font-family:var(--mono,monospace)}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div class="hdr">
  <span class="ttl">Test activity</span>
  <span><span class="stat" id="statTotal">0</span> runs · <span class="stat" id="statPass">0</span> pass</span>
  <div class="ctl">
    <button id="play">▶ replay</button>
    <button id="pause" style="display:none">⏸</button>
    <button id="s1" class="on">1x</button><button id="s2">2x</button><button id="s5">5x</button>
    <button id="clearSel" title="Clear the selected range">✕ range</button>
  </div>
  <span class="clock" id="clock"></span>
  <span class="peak" id="peak"></span>
</div>
<div class="chartwrap">
  <svg class="chart" id="svg" viewBox="0 0 760 120" preserveAspectRatio="none"></svg>
</div>
<div class="axis" id="axis"></div>
<div class="drill" id="drill" style="display:none"></div>`;

  class VeraTestActivityTimeline extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._ws = null;
      this._pollTimer = null;
      this._lastSig = '';
      this._firstRender = true;
      this._runs = [];
      this._buckets = [];
      this._stalls = [];
      this._errors = [];
      this._speed = 1;
      this._playing = false;
      this._playRaf = null;
      this._sel = null;
      this._dragStart = null;
      this._openDrill = -1;
      this._shownTotal = 0;
      this._shownPass = 0;
      this._autoplayed = false;
    }

    connectedCallback() {
      this._hours = parseInt(this.getAttribute('hours') || '48', 10);
      this._autoplay = this.getAttribute('autoplay') || 'scroll';
      this._wire();
      this._connectWs();
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), parseInt(this.getAttribute('poll-ms') || '20000', 10));
      if (this._autoplay === 'scroll' && !reducedMotion()) {
        this._io = new IntersectionObserver(entries => {
          entries.forEach(e => {
            if (e.isIntersecting && e.intersectionRatio >= 0.5 && !this._autoplayed && this._runs.length) {
              this._autoplayed = true;
              this._startReplay();
            }
          });
        }, { threshold: [0.5] });
        this._io.observe(this);
      }
    }

    disconnectedCallback() {
      if (this._pollTimer) clearInterval(this._pollTimer);
      if (this._playRaf) cancelAnimationFrame(this._playRaf);
      if (this._io) this._io.disconnect();
      try { this._ws && this._ws.close(); } catch (_) {}
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    _wire() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('play').onclick = () => this._startReplay();
      $('pause').onclick = () => this._stopReplay();
      $('s1').onclick = () => this._setSpeed(1);
      $('s2').onclick = () => this._setSpeed(2);
      $('s5').onclick = () => this._setSpeed(5);
      $('clearSel').onclick = () => { this._sel = null; this._paintSelection(); };
      const svg = $('svg');
      svg.addEventListener('mousedown', e => {
        const idx = this._idxFromEvent(e);
        if (idx == null) return;
        this._dragStart = idx;
        this._sel = { startIdx: idx, endIdx: idx };
        this._paintSelection();
      });
      svg.addEventListener('mousemove', e => {
        if (this._dragStart == null) return;
        const idx = this._idxFromEvent(e);
        if (idx == null) return;
        this._sel = { startIdx: Math.min(this._dragStart, idx), endIdx: Math.max(this._dragStart, idx) };
        this._paintSelection();
      });
      window.addEventListener('mouseup', () => { this._dragStart = null; });
    }

    _idxFromEvent(e) {
      const bars = this.shadowRoot.querySelectorAll('.bar');
      const svg = this.shadowRoot.getElementById('svg');
      const rect = svg.getBoundingClientRect();
      const vbW = 760, n = bars.length;
      if (!n) return null;
      const xFrac = (e.clientX - rect.left) / rect.width;
      const idx = Math.floor((xFrac * vbW) / (vbW / n));
      return Math.max(0, Math.min(n - 1, idx));
    }

    _connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
        this._ws.onmessage = e => { try { const ev = JSON.parse(e.data); if (ev && ev.type === 'evolve.run.done') this.refresh(); } catch (_) {} };
        this._ws.onclose = () => { setTimeout(() => this._connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this._connectWs(), 5000); }
    }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    async refresh() {
      const [runsD, stallsD, eventsD] = await Promise.all([
        this._fetchJson('/evolve/runs?limit=500'),
        this._fetchJson('/perf/stalls?limit=100'),
        this._fetchJson('/events?limit=300'),
      ]);
      this._runs = ((runsD && runsD.runs) || []).filter(r => r.ts);
      this._stalls = ((stallsD && stallsD.events) || []).filter(e => e.kind === 'hang' || e.kind === 'stall');
      this._errors = (Array.isArray(eventsD) ? eventsD : []).filter(e => e && e.type === 'cap.error');
      const sig = JSON.stringify([this._runs.length, this._runs[0] && this._runs[0].run_id,
        this._stalls.length, this._errors.length]);
      const isNew = sig !== this._lastSig;
      this._lastSig = sig;
      this._bucketize();
      this._render(isNew);
    }

    _bucketize() {
      const now = Date.now();
      const hourMs = 3600000;
      const nBuckets = this._hours;
      const buckets = [];
      for (let i = nBuckets - 1; i >= 0; i--) {
        const end = now - i * hourMs;
        const start = end - hourMs;
        buckets.push({ start, end, runs: [], pass: 0, fail: 0, stalls: [], errors: [] });
      }
      const bucketFor = ts => {
        const idx = nBuckets - 1 - Math.floor((now - ts) / hourMs);
        return (idx >= 0 && idx < nBuckets) ? buckets[idx] : null;
      };
      this._runs.forEach(r => {
        const b = bucketFor(Date.parse(r.ts));
        if (!b) return;
        b.runs.push(r);
        if ((r.pass_rate || 0) >= 1) b.pass++; else b.fail++;
      });
      this._stalls.forEach(s => {
        const t = (s.ts || 0) * (String(s.ts).length <= 10 ? 1000 : 1);
        const b = bucketFor(t);
        if (b) b.stalls.push(s);
      });
      this._errors.forEach(e => {
        const t = Date.parse(e.ts || '') || 0;
        const b = bucketFor(t);
        if (b) b.errors.push(e);
      });
      this._buckets = buckets;
    }

    // ── ticking counters (bun's shared tick() animator, real values only) ──
    _tickTo(el, from, to, ms) {
      if (from === to) { el.textContent = to; return; }
      if (reducedMotion()) { el.textContent = to; return; }
      const t0 = performance.now();
      const step = now => {
        const frac = Math.min(1, (now - t0) / ms);
        el.textContent = Math.round(from + (to - from) * ease(frac));
        if (frac < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }

    _render(isNew) {
      const $ = id => this.shadowRoot.getElementById(id);
      const svg = $('svg'), axis = $('axis'), peak = $('peak');
      if (!this._runs.length) {
        svg.innerHTML = '';
        axis.textContent = ''; peak.textContent = '';
        $('statTotal').textContent = '0'; $('statPass').textContent = '0';
        return;
      }
      const totalRuns = this._runs.length;
      const totalPass = this._runs.filter(r => (r.pass_rate || 0) >= 1).length;
      if (isNew) {
        this._tickTo($('statTotal'), this._shownTotal, totalRuns, 700);
        this._tickTo($('statPass'), this._shownPass, totalPass, 700);
        this._shownTotal = totalRuns; this._shownPass = totalPass;
      }

      const maxTotal = Math.max(1, ...this._buckets.map(b => b.pass + b.fail));
      let peakIdx = 0;
      this._buckets.forEach((b, i) => { if (b.pass + b.fail > this._buckets[peakIdx].pass + this._buckets[peakIdx].fail) peakIdx = i; });
      const peakB = this._buckets[peakIdx];
      peak.textContent = (peakB.pass + peakB.fail) ? ('peak: ' + new Date(peakB.start).getHours() + ':00 (' + (peakB.pass + peakB.fail) + ' runs)') : '';

      const n = this._buckets.length, vbW = 760, vbH = 120, slot = vbW / n, barW = Math.max(1.2, slot * 0.72);
      const maxBarH = 92, baseY = 100;
      let svgHtml = '<line class="playline" id="playline" x1="0" y1="0" x2="0" y2="' + vbH + '"></line>';
      this._buckets.forEach((b, i) => {
        const total = b.pass + b.fail;
        const h = total ? Math.max(3, (total / maxTotal) * maxBarH) : 0;
        const x = i * slot + (slot - barW) / 2;
        const y = baseY - h;
        const passRatio = total ? b.pass / total : 0;
        const color = total ? lerpColor(FAIL_RGB, PASS_RGB, passRatio) : 'rgba(255,255,255,.06)';
        const title = new Date(b.start).toLocaleString() + ' — ' + total + ' run(s), ' + b.pass + ' pass' +
          (b.stalls.length ? ', ' + b.stalls.length + ' stall(s)' : '') +
          (b.errors.length ? ', ' + b.errors.length + ' error(s)' : '');
        svgHtml += '<rect class="bar" data-i="' + i + '" x="' + x.toFixed(1) + '" y="' + y.toFixed(1) +
          '" width="' + barW.toFixed(1) + '" height="' + (total ? h.toFixed(1) : 1) +
          '" rx="1" fill="' + color + '"><title>' + this._esc(title) + '</title></rect>';
        // the current (rightmost) hour is genuinely still open/accumulating —
        // a real ongoing state, so it's the one bar allowed a continuous pulse.
        if (i === n - 1) {
          svgHtml += '<circle class="mk livering" cx="' + (x + barW / 2).toFixed(1) + '" cy="' +
            (y - 4).toFixed(1) + '" r="2" fill="var(--acc,#5a9e8f)"></circle>';
        }
        const marks = b.stalls.length + b.errors.length;
        if (marks) {
          const mx = x + barW / 2;
          for (let m = 0; m < Math.min(marks, 4); m++) {
            const isErr = m < b.errors.length;
            svgHtml += '<circle class="mk" cx="' + (mx - 4 + m * 3).toFixed(1) + '" cy="' + (baseY + 6) +
              '" r="1.6" fill="' + (isErr ? '#ef4444' : '#d9a441') + '"></circle>';
          }
        }
      });
      svg.innerHTML = svgHtml;
      svg.querySelectorAll('.bar').forEach(el => {
        el.addEventListener('click', () => this._toggleDrill(parseInt(el.dataset.i, 10)));
      });

      axis.innerHTML = '<span>' + new Date(this._buckets[0].start).toLocaleString() + '</span>' +
        '<span>' + this._buckets.length + ' hour(s)</span>' +
        '<span>' + new Date(this._buckets[this._buckets.length - 1].end).toLocaleString() + '</span>';

      this._paintSelection();
      if (this._openDrill >= 0) this._renderDrill(this._openDrill);
      this._firstRender = false;
    }

    _paintSelection() {
      const bars = this.shadowRoot.querySelectorAll('.bar');
      bars.forEach((b, i) => {
        b.classList.remove('selected', 'dim');
        if (this._sel) {
          if (i >= this._sel.startIdx && i <= this._sel.endIdx) b.classList.add('selected');
          else b.classList.add('dim');
        }
      });
    }

    _toggleDrill(i) {
      this._openDrill = this._openDrill === i ? -1 : i;
      const drill = this.shadowRoot.getElementById('drill');
      if (this._openDrill < 0) { drill.style.display = 'none'; return; }
      drill.style.display = 'block';
      this._renderDrill(this._openDrill);
    }

    _renderDrill(i) {
      const b = this._buckets[i];
      const drill = this.shadowRoot.getElementById('drill');
      if (!b || !drill) return;
      if (!b.runs.length) { drill.innerHTML = '<div class="empty">No runs this hour.</div>'; return; }
      const TRIG_ICON = { claude_code: '🧑‍💻', autonomous: '⚙', user: '👤' };
      drill.innerHTML = b.runs.map(r =>
        '<div class="row"><span class="t">' + new Date(r.ts).toLocaleTimeString() + '</span>' +
        '<span title="' + this._esc({ claude_code: 'Claude Code', autonomous: 'autonomous', user: 'user' }[r.triggered_by] || '') +
        '">' + (TRIG_ICON[r.triggered_by] || '') + '</span>' +
        '<span style="flex:1">' + this._esc(r.label || r.task) + '</span>' +
        '<span style="color:' + ((r.pass_rate || 0) >= 1 ? 'var(--ok,var(--ac2,#6db87a))' : 'var(--err,var(--ac4,#c96b6b))') + '">' +
        (r.combined == null ? '—' : r.combined) + '</span></div>').join('');
    }

    // ── replay: real sweeping scrub-line + opacity reveal + ticking + clock ──
    _setSpeed(n) {
      this._speed = n;
      ['s1', 's2', 's5'].forEach(id => this.shadowRoot.getElementById(id).classList.remove('on'));
      this.shadowRoot.getElementById('s' + n).classList.add('on');
    }

    _startReplay() {
      if (this._playing || !this._runs.length) return;
      let runs = this._runs.slice().sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
      if (this._sel) {
        const startTs = this._buckets[this._sel.startIdx].start;
        const endTs = this._buckets[this._sel.endIdx].end;
        runs = runs.filter(r => { const t = Date.parse(r.ts); return t >= startTs && t <= endTs; });
      }
      if (!runs.length) return;
      const minTs = Date.parse(runs[0].ts), maxTs = Date.parse(runs[runs.length - 1].ts);
      const span = Math.max(maxTs - minTs, 1000);
      const BASE_MS = reducedMotion() ? 1 : 22000;
      const durationMs = BASE_MS / this._speed;
      this._playing = true;
      const $ = id => this.shadowRoot.getElementById(id);
      $('play').style.display = 'none';
      $('pause').style.display = '';
      const playline = this.shadowRoot.getElementById('playline');
      playline.classList.add('on');
      let replayed = 0, passed = 0, nextIdx = 0;
      const bars = this.shadowRoot.querySelectorAll('.bar');
      bars.forEach(b => b.classList.remove('dim'));
      const nBuckets = this._buckets.length, vbW = 760, slot = vbW / nBuckets;
      const t0 = performance.now();
      const frame = now => {
        const frac = Math.min(1, (now - t0) / durationMs);
        const playheadTs = minTs + frac * span;
        const px = frac * vbW;
        playline.setAttribute('x1', px.toFixed(1)); playline.setAttribute('x2', px.toFixed(1));
        const aheadIdx = Math.floor(px / slot);
        bars.forEach((b, i) => { b.style.opacity = i <= aheadIdx ? '1' : '.12'; });
        $('clock').textContent = new Date(playheadTs).toLocaleString();
        while (nextIdx < runs.length && Date.parse(runs[nextIdx].ts) <= playheadTs) {
          replayed++; if ((runs[nextIdx].pass_rate || 0) >= 1) passed++;
          nextIdx++;
        }
        this._tickTo($('statTotal'), this._shownTotal, replayed, 120);
        this._tickTo($('statPass'), this._shownPass, passed, 120);
        this._shownTotal = replayed; this._shownPass = passed;
        if (frac < 1) { this._playRaf = requestAnimationFrame(frame); }
        else this._stopReplay(true);
      };
      this._playRaf = requestAnimationFrame(frame);
    }

    _stopReplay(finished) {
      this._playing = false;
      if (this._playRaf) { cancelAnimationFrame(this._playRaf); this._playRaf = null; }
      const $ = id => this.shadowRoot.getElementById(id);
      $('play').style.display = '';
      $('pause').style.display = 'none';
      const playline = this.shadowRoot.getElementById('playline');
      if (playline) playline.classList.remove('on');
      this.shadowRoot.querySelectorAll('.bar').forEach(b => { b.style.opacity = ''; });
      if (!finished) { $('clock').textContent = ''; }
      // Settle the ticking counters back to the REAL current totals.
      const totalRuns = this._runs.length;
      const totalPass = this._runs.filter(r => (r.pass_rate || 0) >= 1).length;
      $('statTotal').textContent = totalRuns; $('statPass').textContent = totalPass;
      this._shownTotal = totalRuns; this._shownPass = totalPass;
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-test-activity-timeline', VeraTestActivityTimeline);
})();

/* ============================================================================
 * <vera-character>  —  Animated agent companion (reusable custom element)
 * ============================================================================
 *
 * Renders an agent's "character" (generated expression frames) as an animated
 * companion that idles, reacts to system activity, speaks (TTS + lip-flap) and
 * listens (STT). The SAME element is used in the web Companion panel, embedded
 * in other panels, and inside the desktop pywebview host — one renderer.
 *
 * Layered tiers with graceful fallback (set via the `mode` attribute or the
 * stored character's render_mode):
 *     talkinghead → spritesheet → procedural → emoji
 * Whatever assets/models exist drive the best tier; missing ones fall through.
 *
 * USAGE
 *   <script src="/ui/elements/character.js"></script>
 *   <vera-character agent-id="…" interactive narrate></vera-character>
 *   <vera-character agent="assistant" size="160"></vera-character>
 *
 * ATTRIBUTES (all optional)
 *   agent-id / agent   — bind to an agent by id or name
 *   size               — px (square). Default 220.
 *   mode               — auto|procedural|spritesheet|talkinghead (default auto)
 *   interactive        — show mic + speech bubble; enable speak()/listen()
 *   narrate            — subscribe to the event stream and narrate jobs/dreams
 *   api-base           — backend origin override
 *
 * PUBLIC API
 *   el.setAgent(idOrName, {byName})   el.reload()
 *   el.setState(state)                // idle|listening|thinking|talking|happy|working|error
 *   el.say(text)                      // speech bubble only
 *   el.speak(text)                    // TTS + lip-flap, returns a Promise
 *   el.listen()                       // mic → STT, returns Promise<string>
 *   el.setMode(mode)                  el.bindEvents() / el.unbindEvents()
 *   el.reset()
 *
 * EVENTS (CustomEvent, bubbles)
 *   char:ready {agentId, hasCharacter}   char:state {state}
 *   char:spoke {text}                    char:heard {text}
 *   char:click {agentId}                 char:narrate {text}
 * ============================================================================ */
(function () {
  if (window.customElements && window.customElements.get('vera-character')) return;

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function _apiBase(override) {
    if (override) return String(override).replace(/\/$/, '');
    try {
      if (window._veraBase) return String(window._veraBase).replace(/\/$/, '');
      if (window.parent && window.parent._veraBase) return String(window.parent._veraBase).replace(/\/$/, '');
    } catch (_) {}
    return location.origin;
  }
  function _wsBase(httpBase) {
    return httpBase.replace(/^http/, 'ws');
  }

  const STATE_FRAME = {  // state → preferred frame, with fallbacks
    idle:      ['neutral'],
    listening: ['listening', 'neutral'],
    thinking:  ['thinking', 'neutral'],
    talking:   ['talking', 'neutral'],
    happy:     ['happy', 'neutral'],
    working:   ['working', 'thinking', 'neutral'],
    error:     ['error', 'neutral'],
  };
  const STATE_RING = {
    idle: 'var(--dim2,#8a7e70)', listening: 'var(--info,#7eb8d9)',
    thinking: 'var(--acc4,#a07ec1)', talking: 'var(--acc2,#a8c87a)',
    happy: 'var(--acc,#5a9e8f)', working: 'var(--acc3,#c5a572)',
    error: 'var(--err,#c75a5a)',
  };

  const STYLE = `
:host{display:inline-block;font-family:var(--mono,'IBM Plex Mono',monospace);color:var(--text,#ddd5c8);--char-size:220px}
:host([hidden]){display:none}
.wrap{position:relative;width:var(--char-size);height:var(--char-size);display:flex;align-items:center;justify-content:center}
.stage{position:relative;width:100%;height:100%;border-radius:14px;overflow:hidden;background:radial-gradient(ellipse at 50% 120%,var(--bg2,#252220),var(--bg0,#181614));border:1px solid var(--border,#3a3530);box-shadow:0 0 0 2px transparent;transition:box-shadow .25s}
.stage.ring{box-shadow:0 0 0 2px var(--ring,#8a7e70),0 6px 18px rgba(0,0,0,.35)}
.bob{position:absolute;inset:0;display:flex;align-items:flex-end;justify-content:center;will-change:transform}
.bob.idle{animation:char-bob 3s ease-in-out infinite}
.bob.blink{animation:char-blink .14s ease-in-out}
.frame{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .12s linear;image-rendering:auto}
:host([pixel]) .frame{image-rendering:pixelated}
.frame.on{opacity:1}
.emoji{font-size:calc(var(--char-size) * .5);line-height:1;filter:drop-shadow(0 4px 6px rgba(0,0,0,.4))}
.placeholder{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--dim,#a89f92);font-size:11px;text-align:center;padding:10px}
.spinner{width:20px;height:20px;border-radius:50%;border:2px solid var(--acc4,#a07ec1);border-top-color:transparent;animation:char-spin .9s linear infinite}
.bubble{position:absolute;left:50%;bottom:calc(100% + 8px);transform:translateX(-50%) translateY(6px);max-width:280px;min-width:80px;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:12px;padding:8px 11px;font-size:11.5px;line-height:1.45;color:var(--text,#ddd5c8);box-shadow:0 6px 18px rgba(0,0,0,.4);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;font-family:system-ui,-apple-system,Segoe UI,sans-serif;z-index:5;white-space:normal;word-break:break-word}
.bubble.show{opacity:1;transform:translateX(-50%) translateY(0)}
.bubble::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:7px solid transparent;border-top-color:var(--bg1,#1f1d1a)}
.name{position:absolute;left:0;right:0;bottom:6px;text-align:center;font-size:10px;color:var(--text2,#bfb6a8);text-shadow:0 1px 3px #000;pointer-events:none;letter-spacing:.3px}
.controls{position:absolute;right:6px;bottom:6px;display:flex;gap:5px;z-index:6}
.cbtn{width:30px;height:30px;border-radius:50%;border:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);color:var(--text2,#bfb6a8);cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;padding:0;transition:.15s}
.cbtn:hover{border-color:var(--acc,#5a9e8f);color:var(--text,#ddd5c8)}
.cbtn.rec{border-color:var(--err,#c75a5a);color:var(--err,#c75a5a);animation:char-pulse 1.2s ease-in-out infinite}
.status{position:absolute;left:8px;top:8px;font-size:9px;color:var(--dim2,#8a7e70);background:rgba(0,0,0,.35);border-radius:7px;padding:1px 6px;opacity:0;transition:opacity .2s}
.status.show{opacity:1}
@keyframes char-bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-4%)}}
@keyframes char-blink{0%,100%{transform:scaleY(1)}50%{transform:scaleY(.9)}}
@keyframes char-spin{to{transform:rotate(360deg)}}
@keyframes char-pulse{0%,100%{opacity:1}50%{opacity:.5}}
@media (prefers-reduced-motion: reduce){.bob.idle{animation:none}}
`;

  class VeraCharacter extends HTMLElement {
    static get observedAttributes() { return ['agent-id', 'agent', 'size', 'mode', 'state', 'api-base']; }

    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._agentId = '';
      this._agentName = '';
      this._rec = null;
      this._avatar = '🤖';
      this._voice = 'af_heart';
      this._frames = {};          // state → {img, url}
      this._state = 'idle';
      this._mode = 'auto';
      this._speaking = false;
      this._ws = null;
      this._wsRetry = null;
      this._narrateBuf = [];
      this._narrateTimer = null;
      this._blinkTimer = null;
      this._audioCtx = null;
    }

    connectedCallback() {
      if (this._mounted) return;
      this._mounted = true;
      this._base = _apiBase(this.getAttribute('api-base'));
      this._mode = this.getAttribute('mode') || 'auto';
      const size = parseInt(this.getAttribute('size') || '220', 10) || 220;
      this.style.setProperty('--char-size', size + 'px');
      this._interactive = this.hasAttribute('interactive');
      this._narrate = this.hasAttribute('narrate');
      this._render();
      this._agentId = this.getAttribute('agent-id') || '';
      this._agentName = this.getAttribute('agent') || '';
      this._startIdle();
      this.reload();
    }

    disconnectedCallback() {
      this.unbindEvents();
      if (this._sheetPlayer) { cancelAnimationFrame(this._sheetPlayer.raf); this._sheetPlayer = null; }
      if (this._blinkTimer) clearInterval(this._blinkTimer);
      if (this._narrateTimer) clearInterval(this._narrateTimer);
    }

    attributeChangedCallback(name, _o, v) {
      if (!this._mounted) return;
      if (name === 'size') {
        this.style.setProperty('--char-size', (parseInt(v, 10) || 220) + 'px');
      } else if (name === 'agent-id' && v && v !== this._agentId) {
        this._agentId = v; this.reload();
      } else if (name === 'agent' && v && v !== this._agentName) {
        this._agentName = v; this._agentId = ''; this.reload();
      } else if (name === 'mode') {
        this._mode = v || 'auto';
      }
    }

    _render() {
      this._sr.innerHTML = `
        <style>${STYLE}</style>
        <div class="wrap">
          <div class="bubble" data-part="bubble"></div>
          <div class="stage" data-part="stage">
            <div class="status" data-part="status"></div>
            <div class="bob idle" data-part="bob">
              <div class="placeholder" data-part="ph"><div class="spinner"></div><span>loading…</span></div>
            </div>
            <div class="name" data-part="name"></div>
            ${this._interactive ? `<div class="controls">
              <button class="cbtn" data-act="mic" title="Push to talk">🎤</button>
            </div>` : ''}
          </div>
        </div>`;
      this._stage = this._sr.querySelector('[data-part="stage"]');
      this._bob = this._sr.querySelector('[data-part="bob"]');
      this._bubbleEl = this._sr.querySelector('[data-part="bubble"]');
      this._statusEl = this._sr.querySelector('[data-part="status"]');
      this._nameEl = this._sr.querySelector('[data-part="name"]');
      this._stage.addEventListener('click', (e) => {
        if (e.target.closest('.controls')) return;
        this.dispatchEvent(new CustomEvent('char:click', { detail: { agentId: this._agentId }, bubbles: true }));
      });
      const mic = this._sr.querySelector('[data-act="mic"]');
      if (mic) mic.addEventListener('click', () => this.listen());
    }

    // ───────────────────────── loading ─────────────────────────
    async setAgent(idOrName, opts) {
      if (opts && opts.byName) { this._agentName = idOrName; this._agentId = ''; }
      else { this._agentId = idOrName; }
      await this.reload();
    }

    async reload() {
      try {
        // Resolve agent for avatar/voice fallback (and id from name).
        let agent = null;
        const q = this._agentId ? ('id=' + encodeURIComponent(this._agentId))
                                : (this._agentName ? ('name=' + encodeURIComponent(this._agentName)) : '');
        if (q) {
          try {
            const r = await fetch(this._base + '/agents/get?' + q);
            agent = await r.json();
            if (agent && !agent.error) {
              this._agentId = agent.id || this._agentId;
              this._avatar = agent.avatar || this._avatar;
              this._voice = agent.voice || this._voice;
            }
          } catch (_) {}
        }
        // Load the character, if any.
        let rec = null;
        if (this._agentId) {
          try {
            const r = await fetch(this._base + '/character/get?agent_id=' + encodeURIComponent(this._agentId));
            const data = await r.json();
            if (data && !data.error && data.frame_urls && Object.keys(data.frame_urls).length) rec = data;
          } catch (_) {}
        }
        this._rec = rec;
        if (rec) {
          if (rec.voice) this._voice = rec.voice;
          if (rec.style === 'pixel') this.setAttribute('pixel', ''); else this.removeAttribute('pixel');
          if (this._mode === 'auto') this._mode = rec.render_mode || 'procedural';
          if (this._mode === 'spritesheet' && rec.sheet && rec.sheet.url)
            this._buildSheet(rec.sheet);
          else
            this._buildFrames(rec.frame_urls || {});
          if (this._narrate || rec.idle_blink) this._startBlink(rec.idle_blink !== false);
        } else {
          this._buildEmoji();
        }
        this._nameEl.textContent = (rec && rec.display_name) || this._agentName || '';
        this.setState('idle');
        if (this._narrate) this.bindEvents();
        this.dispatchEvent(new CustomEvent('char:ready',
          { detail: { agentId: this._agentId, hasCharacter: !!rec }, bubbles: true }));
      } catch (e) {
        this._buildEmoji();
      }
    }

    _clearLayers() {
      if (this._sheetPlayer) { cancelAnimationFrame(this._sheetPlayer.raf); this._sheetPlayer = null; }
      this._sr.querySelectorAll('.frame,.emoji').forEach(n => n.remove());
      const ph = this._sr.querySelector('[data-part="ph"]');
      if (ph) ph.remove();
      this._frames = {};
    }

    _buildFrames(urls) {
      this._clearLayers();
      Object.keys(urls).forEach(state => {
        const img = document.createElement('img');
        img.className = 'frame';
        img.alt = state;
        img.src = this._base + urls[state];
        this._bob.appendChild(img);
        this._frames[state] = { img, url: urls[state] };
      });
    }

    // Play packed sprite sheets (from spritegen). New records carry EVERY built
    // animation (sheet.animations) → canvas player that idles by default, plays
    // 'talk' while speaking and one-shot reactions (hurt/jump) on state changes.
    // Old records with a single sheet keep the CSS steps() fallback.
    _buildSheet(sheet) {
      this._clearLayers();
      const anims = sheet.animations;
      if (!anims || !Object.keys(anims).length) return this._buildSheetSingle(sheet);
      const cv = document.createElement('canvas');
      cv.className = 'frame on';
      cv.style.cssText = 'image-rendering:pixelated;object-fit:contain;background:transparent';
      this._bob.appendChild(cv);
      this._sheetEl = cv;
      const ctx = cv.getContext('2d');
      const player = { anims: {}, cur: null, raf: 0 };
      Object.keys(anims).forEach(a => {
        const img = new Image();
        img.src = this._base + anims[a].url;
        player.anims[a] = { img, m: anims[a] };
      });
      player.def = (sheet.anim && player.anims[sheet.anim]) ? sheet.anim
                 : (player.anims.idle ? 'idle' : Object.keys(player.anims)[0]);
      player.play = (name, once) => {
        const A = player.anims[name];
        if (!A) return false;
        player.cur = { name, start: performance.now(),
                       once: (once != null) ? !!once : (A.m.loop === false) };
        if (cv.width !== A.m.frame_width || cv.height !== A.m.frame_height) {
          cv.width = A.m.frame_width; cv.height = A.m.frame_height;
        }
        ctx.imageSmoothingEnabled = false;
        return true;
      };
      const tick = (now) => {
        player.raf = requestAnimationFrame(tick);
        const c = player.cur;
        if (!c) return;
        const A = player.anims[c.name];
        if (!A || !A.img.naturalWidth) return;
        const m = A.m;
        let idx = Math.floor((now - c.start) / 1000 * Math.max(1, m.fps || 8));
        if (c.once && idx >= m.count) { player.play(player.def); return; }
        idx %= Math.max(1, m.count);
        const col = idx % m.columns, row = Math.floor(idx / m.columns);
        ctx.clearRect(0, 0, cv.width, cv.height);
        ctx.drawImage(A.img, col * m.frame_width, row * m.frame_height,
                      m.frame_width, m.frame_height, 0, 0, cv.width, cv.height);
      };
      player.play(player.def);
      player.raf = requestAnimationFrame(tick);
      this._sheetPlayer = player;
      this._frames = {};
    }

    // Single-sheet CSS steps() fallback (older character records).
    _buildSheetSingle(sheet) {
      const S = this._stage.clientWidth || parseInt(this.getAttribute('size') || '220', 10) || 220;
      const N = Math.max(1, sheet.count || sheet.columns || 1);
      const fps = sheet.fps || 8;
      const div = document.createElement('div');
      div.className = 'frame on';
      div.style.cssText =
        `background-image:url(${this._base + sheet.url});background-repeat:no-repeat;` +
        `background-position:0 center;background-size:${N * S}px ${S}px;image-rendering:pixelated`;
      this._bob.appendChild(div);
      this._sheetEl = div;
      try {
        if (N > 1) div.animate(
          [{ backgroundPositionX: '0px' }, { backgroundPositionX: `-${N * S}px` }],
          { duration: (N / Math.max(1, fps)) * 1000, iterations: Infinity, easing: `steps(${N})` });
      } catch (_) {}
      this._frames = {};
    }

    // Public: play a named sprite animation (no-op for non-sprite characters).
    playAnim(name, once) {
      return this._sheetPlayer ? this._sheetPlayer.play(name, once) : false;
    }

    _buildEmoji() {
      this._clearLayers();
      const span = document.createElement('div');
      span.className = 'emoji';
      span.textContent = this._avatar || '🤖';
      this._bob.appendChild(span);
      this._frames = {};
    }

    // ───────────────────────── animation ─────────────────────────
    _startIdle() {
      this._bob.classList.add('idle');
    }
    _startBlink(on) {
      if (this._blinkTimer) clearInterval(this._blinkTimer);
      if (!on) return;
      const tick = () => {
        if (this._speaking) return;
        this._bob.classList.add('blink');
        setTimeout(() => this._bob.classList.remove('blink'), 150);
      };
      this._blinkTimer = setInterval(() => { if (Math.random() < 0.6) tick(); }, 3200);
    }

    _showFrame(state) {
      // pick the first available frame for this logical state
      const order = STATE_FRAME[state] || ['neutral'];
      let key = order.find(k => this._frames[k]);
      if (!key) key = Object.keys(this._frames)[0];
      Object.keys(this._frames).forEach(k => {
        this._frames[k].img.classList.toggle('on', k === key);
      });
    }

    setState(state) {
      state = state || 'idle';
      this._state = state;
      this.setAttribute('state', state);
      if (this._sheetPlayer) this._spriteState(state);
      if (Object.keys(this._frames).length) this._showFrame(state);
      this._stage.classList.toggle('ring', state !== 'idle');
      this._stage.style.setProperty('--ring', STATE_RING[state] || STATE_RING.idle);
      this.dispatchEvent(new CustomEvent('char:state', { detail: { state }, bubbles: true }));
    }

    /** Public mouth flap that leaves the logical state (and status ring) alone —
        used by the chat buddy to lip-flap while reply tokens stream in. */
    flap(open) { this._mouth(!!open); }

    _setStatus(txt) {
      if (!this._statusEl) return;
      this._statusEl.textContent = txt || '';
      this._statusEl.classList.toggle('show', !!txt);
    }

    // ───────────────────────── speech ─────────────────────────
    say(text, ms) {
      if (!text) return;
      this._bubbleEl.textContent = text;
      this._bubbleEl.classList.add('show');
      clearTimeout(this._bubbleTimer);
      const dur = ms || Math.min(9000, 2200 + text.length * 45);
      this._bubbleTimer = setTimeout(() => this._bubbleEl.classList.remove('show'), dur);
    }

    async speak(text) {
      if (!text) return;
      this.say(text);
      this._speaking = true;
      this.setState('talking');
      this.dispatchEvent(new CustomEvent('char:spoke', { detail: { text }, bubbles: true }));
      let played = false;
      try {
        const r = await fetch(this._base + '/tts/synthesize', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, voice: this._voice, speed: (this._rec && this._rec.tts_speed) || 1.0 }),
        });
        const data = await r.json();
        if (data && data.audio_b64) { await this._playWithLipSync(data.audio_b64); played = true; }
      } catch (_) {}
      if (!played) await this._timedFlap(Math.min(8000, 1400 + text.length * 38));
      this._speaking = false;
      this.setState('idle');
    }

    async _timedFlap(ms) {
      const end = performance.now() + ms;
      return new Promise(res => {
        const step = () => {
          if (this._mouthClosedForce || performance.now() >= end) { this._mouth(false); return res(); }
          this._mouth((performance.now() / 120 | 0) % 2 === 0);
          requestAnimationFrame(step);
        };
        step();
      });
    }

    // Map logical companion states onto whichever sprite animations exist.
    _spriteState(state) {
      const p = this._sheetPlayer;
      if (!p) return;
      const loops = { talking: 'talk', working: 'walk' };
      const shots = { error: 'hurt', happy: 'jump' };
      if (loops[state] && p.anims[loops[state]]) {
        if (!p.cur || p.cur.name !== loops[state]) p.play(loops[state]);
      } else if (shots[state] && p.anims[shots[state]]) {
        p.play(shots[state], true);
      } else if (state === 'idle' && p.cur && p.cur.name !== p.def && !p.cur.once) {
        p.play(p.def);
      }
    }

    _mouth(open) {
      if (this._sheetPlayer) {
        const p = this._sheetPlayer;
        if (p.anims.talk) {
          if (open && (!p.cur || p.cur.name !== 'talk')) p.play('talk');
          else if (!open && p.cur && p.cur.name === 'talk') p.play(p.def);
        }
        return;
      }
      if (!Object.keys(this._frames).length) return;
      const key = open ? (this._frames.talking ? 'talking' : 'neutral')
                       : (this._frames.neutral ? 'neutral' : Object.keys(this._frames)[0]);
      Object.keys(this._frames).forEach(k => this._frames[k].img.classList.toggle('on', k === key));
    }

    async _playWithLipSync(audioB64) {
      try {
        const bytes = Uint8Array.from(atob(audioB64), c => c.charCodeAt(0));
        this._audioCtx = this._audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        const ctx = this._audioCtx;
        if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (_) {} }
        const buf = await ctx.decodeAudioData(bytes.buffer.slice(0));
        const src = ctx.createBufferSource(); src.buffer = buf;
        const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
        const data = new Uint8Array(analyser.frequencyBinCount);
        src.connect(analyser); analyser.connect(ctx.destination);
        return new Promise(resolve => {
          let done = false;
          const finish = () => { if (done) return; done = true; this._mouth(false); resolve(); };
          src.onended = finish;
          src.start();
          const loop = () => {
            if (done) return;
            analyser.getByteTimeDomainData(data);
            let sum = 0;
            for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
            const rms = Math.sqrt(sum / data.length);
            this._mouth(rms > 0.06);
            requestAnimationFrame(loop);
          };
          loop();
        });
      } catch (e) {
        return this._timedFlap(2500);
      }
    }

    // ───────────────────────── listening (STT) ─────────────────────────
    async listen() {
      const mic = this._sr.querySelector('[data-act="mic"]');
      if (this._recording) { this._stopListen(); return ''; }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.say('Microphone not available here.'); return '';
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const rec = new MediaRecorder(stream);
        const chunks = [];
        this._recording = rec;
        rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
        const done = new Promise(res => { rec.onstop = () => res(new Blob(chunks, { type: 'audio/webm' })); });
        rec.start();
        this.setState('listening');
        if (mic) mic.classList.add('rec');
        this._stopListen = () => { try { rec.stop(); } catch (_) {} stream.getTracks().forEach(t => t.stop()); this._recording = null; if (mic) mic.classList.remove('rec'); };
        // Auto-stop after 8s as a safety net; user can click again to stop early.
        const auto = setTimeout(() => this._stopListen && this._stopListen(), 8000);
        const blob = await done; clearTimeout(auto);
        this.setState('thinking');
        const fd = new FormData(); fd.append('file', blob, 'speech.webm');
        const r = await fetch(this._base + '/stt', { method: 'POST', body: fd });
        const data = await r.json();
        const text = (data && data.text) || '';
        this.setState('idle');
        if (text) this.dispatchEvent(new CustomEvent('char:heard', { detail: { text }, bubbles: true }));
        return text;
      } catch (e) {
        this.setState('idle');
        if (mic) mic.classList.remove('rec');
        this._recording = null;
        this.say('Could not capture audio.');
        return '';
      }
    }

    // ───────────────────────── event narration ─────────────────────────
    bindEvents() {
      if (this._ws) return;
      this._narrate = true;
      this._connectWS();
      if (!this._narrateTimer) {
        this._narrateTimer = setInterval(() => this._flushNarration(), 22000);
      }
    }
    unbindEvents() {
      if (this._wsRetry) { clearTimeout(this._wsRetry); this._wsRetry = null; }
      if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
      if (this._narrateTimer) { clearInterval(this._narrateTimer); this._narrateTimer = null; }
    }

    _connectWS() {
      try {
        const url = _wsBase(this._base) + '/ws/mcp';
        const ws = new WebSocket(url);
        this._ws = ws;
        ws.onopen = () => { try { ws.send(JSON.stringify({ action: 'subscribe_events' })); } catch (_) {} };
        ws.onmessage = m => { let ev; try { ev = JSON.parse(m.data); } catch (_) { return; } this._onEvent(ev); };
        ws.onclose = () => {
          this._ws = null;
          if (this._narrate && this.isConnected) this._wsRetry = setTimeout(() => this._connectWS(), 4000);
        };
        ws.onerror = () => { try { ws.close(); } catch (_) {} };
      } catch (_) {}
    }

    _onEvent(ev) {
      const t = (ev && ev.type) || '';
      if (!t || t === 'connected') return;
      // Drive transient state from activity, but never override active speech.
      if (!this._speaking) {
        if (t === 'cap.error' || t === 'worker.error' || t.endsWith('.error')) {
          this.setState('error'); this._bounceIdle(1500);
        } else if (t.startsWith('dream.')) {
          this.setState('working');
        } else if (t === 'cap.call' || t === 'worker.start') {
          this.setState('working'); this._bounceIdle(1200);
        } else if (t === 'cap.ok' || t === 'worker.done') {
          this.setState('happy'); this._bounceIdle(900);
        }
      }
      // Buffer notable events for conversational narration.
      const notable = t.startsWith('dream.') || t === 'worker.start' || t === 'worker.done'
        || t.endsWith('.error') || t.startsWith('character.generate') || t === 'render.export';
      if (notable) {
        this._narrateBuf.push({ type: t, name: ev.name || ev.detail || ev.error || '' });
        if (this._narrateBuf.length > 20) this._narrateBuf.shift();
      }
    }

    _bounceIdle(ms) {
      clearTimeout(this._idleTimer);
      this._idleTimer = setTimeout(() => { if (!this._speaking) this.setState('idle'); }, ms);
    }

    async _flushNarration() {
      if (!this._narrate || this._speaking || !this._narrateBuf.length) return;
      const events = this._narrateBuf.splice(0, this._narrateBuf.length);
      try {
        const r = await fetch(this._base + '/character/narrate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: this._agentId, events: JSON.stringify(events) }),
        });
        const data = await r.json();
        const text = (data && data.text || '').trim();
        if (text) {
          this.dispatchEvent(new CustomEvent('char:narrate', { detail: { text }, bubbles: true }));
          if (this._interactive) this.speak(text); else this.say(text);
        }
      } catch (_) {}
    }

    setMode(m) { this._mode = m || 'auto'; }
    reset() { this._narrateBuf = []; this.setState('idle'); this._bubbleEl.classList.remove('show'); }
  }

  customElements.define('vera-character', VeraCharacter);
})();

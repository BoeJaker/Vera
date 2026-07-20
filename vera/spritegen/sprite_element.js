/* ============================================================================
 * <vera-sprite>  —  animated sprite-sheet player (reusable custom element)
 * ============================================================================
 *
 * Plays the packed sheets a spritegen character produced, straight from the
 * atlas metadata: idle loops automatically, non-looping actions play once and
 * fall back to idle. Optional keyboard control (walk/run/action) and pointer
 * dragging turn it into a controllable in-page companion.
 *
 * USAGE
 *   <script src="/ui/elements/sprite.js"></script>
 *   <vera-sprite char-id="abc123"></vera-sprite>                    auto-idle
 *   <vera-sprite char-id="abc123" anim="walk" scale="3"></vera-sprite>
 *   <vera-sprite char-id="abc123" roam draggable controls></vera-sprite>
 *
 * ATTRIBUTES (all optional except char-id)
 *   char-id     — spritegen character id (required)
 *   anim        — initial animation (default: idle, else first available)
 *   scale       — integer CSS upscale of the sprite pixels (default 3)
 *   fps         — override every animation's fps
 *   controls    — keyboard drives the sprite. Which key does what comes from the
 *                 character's own bindings (dirmap = movement keys, keymap = action
 *                 keys → any pack animation); falls back to WASD/arrows + space.
 *   draggable   — drag the sprite around with the pointer (implies roam)
 *   roam        — absolutely positioned inside its offsetParent, clamped
 *   auto        — force tamagotchi autonomy on (wander/emote/rest when idle),
 *                 regardless of the character's stored autonomy config
 *   api-base    — backend origin override
 *
 * BINDINGS (read from /spritegen/get, all optional — sensible defaults otherwise)
 *   dirmap   {key:  'up'|'down'|'left'|'right'}   movement keys
 *   keymap   {key:  animationName}                action keys → one-shot animation
 *   events   {slot: animationName}                activity slots (see trigger());
 *            slots: idle, move, run, talk, think, work, happy, error, sleep,
 *                   greet, emote — plus any custom slot you invent
 *   autonomy {enabled, wander, emote, idle_after, min_ms, max_ms}   tamagotchi
 *
 * PUBLIC API
 *   el.play(name, {once})   el.stop()   el.reload()
 *   el.trigger(slot, {once}) — play the animation bound to an activity slot (or a
 *                              raw animation name); used by the chat buddy to react
 *   el.animations           — array of available animation names
 *   el.setFlip(bool)
 *
 * EVENTS (bubble): sprite:ready {charId, animations}, sprite:anim {name}
 * ============================================================================ */
(function () {
  if (window.customElements && window.customElements.get('vera-sprite')) return;

  function _apiBase(override) {
    if (override) return String(override).replace(/\/$/, '');
    try {
      if (window._veraBase) return String(window._veraBase).replace(/\/$/, '');
      if (window.parent && window.parent._veraBase) return String(window.parent._veraBase).replace(/\/$/, '');
    } catch (_) {}
    return location.origin;
  }

  // Built-in fallbacks — used only for keys/slots the character hasn't bound and
  // whose target animation actually exists in the pack.
  const DEFAULT_DIRMAP = {
    ArrowLeft: 'left', a: 'left', A: 'left',
    ArrowRight: 'right', d: 'right', D: 'right',
    ArrowUp: 'up', w: 'up', W: 'up',
    ArrowDown: 'down', s: 'down', S: 'down',
  };
  const DEFAULT_KEYMAP = { ' ': 'attack', j: 'attack', k: 'cast', l: 'jump' };
  const DEFAULT_EVENTS = {
    idle: 'idle', move: 'walk', run: 'run', talk: 'talk', think: 'idle',
    work: 'walk', happy: 'jump', error: 'hurt', sleep: 'idle', greet: 'jump', emote: 'jump',
  };
  const DEFAULT_AUTONOMY = {
    enabled: false, wander: true, emote: true, idle_after: 4000, min_ms: 2500, max_ms: 6000,
  };

  class VeraSprite extends HTMLElement {
    static get observedAttributes() { return ['char-id', 'anim', 'scale', 'fps', 'controls', 'auto', 'api-base']; }

    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._sheets = {};      // anim → {img, meta:{columns,rows,frame_width,frame_height,count,fps,loop}}
      this._cur = null;       // {name, start, once}
      this._fallback = 'idle';
      this._flip = false;
      this._keys = {};
      this._pos = null;       // {x,y} when roaming
      this._vel = { walk: 90, run: 200 };   // px/s at scale 1 (scaled below)
      this._raf = 0;
      this._lastT = 0;
      // binding maps (resolved from the record in reload → _computeMaps)
      this._dirmap = DEFAULT_DIRMAP;
      this._keymap = {};
      this._events = {};
      this._auto = Object.assign({}, DEFAULT_AUTONOMY);
      // tamagotchi AI state
      this._lastInput = 0;    // perf.now() of the last user action (keeps AI quiet)
      this._aiState = null;   // null | 'walk'
      this._aiTarget = null;  // {x,y} wander goal
      this._aiNext = 0;       // perf.now() when the next autonomous behaviour may fire
    }

    connectedCallback() {
      if (this._mounted) return;
      this._mounted = true;
      this._base = _apiBase(this.getAttribute('api-base'));
      this._sr.innerHTML = `
        <style>
          :host{display:inline-block;line-height:0;touch-action:none;user-select:none}
          :host([roam]){position:absolute;z-index:20}
          :host([draggable]){cursor:grab}
          :host(.dragging){cursor:grabbing}
          canvas{image-rendering:pixelated;image-rendering:crisp-edges;display:block}
        </style>
        <canvas></canvas>`;
      this._cv = this._sr.querySelector('canvas');
      this._ctx = this._cv.getContext('2d');
      this._ctx.imageSmoothingEnabled = false;
      if (this.hasAttribute('draggable')) this._bindDrag();
      if (this.hasAttribute('controls')) this._bindKeys();
      this._lastInput = performance.now();    // settle before the first autonomous act
      this.reload();
      this._raf = requestAnimationFrame(t => this._tick(t));
    }

    disconnectedCallback() {
      cancelAnimationFrame(this._raf);
      this._unbindKeys();
    }

    attributeChangedCallback(name, _o, v) {
      if (!this._mounted) return;
      if (name === 'char-id' && v) this.reload();
      else if (name === 'anim' && v && this._sheets[v]) this.play(v);
      else if (name === 'scale') this._applyScale();
      else if (name === 'controls') { v == null ? this._unbindKeys() : this._bindKeys(); }
      else if (name === 'auto') { this._auto.enabled = v != null; }   // runtime toggle is authoritative (record autonomy still applies on reload)
    }

    get animations() { return Object.keys(this._sheets); }
    get charId() { return this.getAttribute('char-id') || ''; }
    setFlip(b) { this._flip = !!b; }
    stop() { this._cur = null; this._clear(); }

    async reload() {
      const id = this.charId;
      if (!id) return;
      let rec = null;
      try {
        const r = await fetch(this._base + '/spritegen/get?char_id=' + encodeURIComponent(id));
        rec = await r.json();
      } catch (_) {}
      if (!rec || rec.error) return;
      const urls = (rec.urls && rec.urls.sheets) || {};
      const sheets = {};
      const loads = [];
      Object.keys(rec.sheets || {}).forEach(anim => {
        const meta = rec.sheets[anim];
        const u = urls[anim] && urls[anim].png;
        if (!u || !meta || !meta.count) return;
        const img = new Image();
        loads.push(new Promise(res => { img.onload = res; img.onerror = res; }));
        img.src = this._base + u;
        sheets[anim] = { img, meta };
      });
      await Promise.all(loads);
      this._sheets = sheets;
      this._rec = rec;
      this._computeMaps(rec);
      const names = this.animations;
      this._fallback = names.includes('idle') ? 'idle' : names[0] || '';
      const want = this.getAttribute('anim');
      this.play((want && sheets[want]) ? want : this._fallback);
      // Roaming sprites take their spawn position now (bottom-left), not on
      // the first key/drag — otherwise they render at the layer's top-left
      // and visibly teleport on the first input. rAF so the canvas has been
      // sized by play() and offsetWidth/Height are real before we clamp.
      if (this.hasAttribute('roam')) requestAnimationFrame(() => this._roamBy(0, 0));
      this.dispatchEvent(new CustomEvent('sprite:ready',
        { detail: { charId: id, animations: names }, bubbles: true }));
    }

    // Resolve the character's binding maps against the animations that actually
    // exist in the pack — record bindings win, built-in defaults fill the gaps,
    // and any target animation that isn't present is dropped so nothing plays a
    // missing sheet.
    _computeMaps(rec) {
      const have = (n) => !!(n && this._sheets[n]);
      const filt = (m) => { const o = {}; for (const k in m) if (have(m[k])) o[k] = m[k]; return o; };
      const recKm = (rec && rec.keymap) || {};
      const recDm = (rec && rec.dirmap) || {};
      const recEv = (rec && rec.events) || {};
      // events: prefer a bound-and-present anim, else the default if present; also
      // keep any custom slots the user added.
      const ev = {};
      for (const slot in DEFAULT_EVENTS) {
        if (have(recEv[slot])) ev[slot] = recEv[slot];
        else if (have(DEFAULT_EVENTS[slot])) ev[slot] = DEFAULT_EVENTS[slot];
      }
      for (const slot in recEv) if (have(recEv[slot])) ev[slot] = recEv[slot];
      this._events = ev;
      this._keymap = Object.keys(recKm).length ? filt(recKm) : filt(DEFAULT_KEYMAP);
      this._dirmap = Object.keys(recDm).length ? recDm : DEFAULT_DIRMAP;
      this._auto = Object.assign({}, DEFAULT_AUTONOMY, (rec && rec.autonomy) || {});
      if (this.hasAttribute('auto')) this._auto.enabled = true;
    }

    _animDurMs(name) {
      const sh = this._sheets[name];
      const m = sh && sh.meta;
      return m ? (m.count / Math.max(1, m.fps || 8)) * 1000 : 400;
    }

    // Play the animation bound to an activity slot (idle/talk/think/work/happy/…),
    // or a raw animation name. Non-idle activity also keeps the tamagotchi AI quiet
    // so the buddy reacts to the conversation instead of wandering off mid-reply.
    trigger(slot, opts) {
      const name = this._events[slot] || (this._sheets[slot] ? slot : null);
      if (!name || !this._sheets[name]) return false;
      if (slot !== 'idle' && slot !== 'sleep') this._lastInput = performance.now();
      const loop = this._sheets[name].meta.loop !== false;
      const once = (opts && 'once' in opts) ? !!opts.once : !loop;
      if (this._cur && this._cur.name === name && !once) return true;   // already looping it
      this.play(name, { once });
      if (once) this._actionUntil = performance.now() + this._animDurMs(name);
      return true;
    }

    play(name, opts) {
      const sh = this._sheets[name];
      if (!sh) return false;
      const loop = sh.meta.loop !== false;
      this._cur = { name, start: performance.now(),
                    once: (opts && 'once' in opts) ? !!opts.once : !loop };
      // Size the canvas to this animation's cell (cells can differ per anim).
      if (this._cv.width !== sh.meta.frame_width || this._cv.height !== sh.meta.frame_height) {
        // Cells SHOULD be uniform (the pipeline aligns them), but older packs
        // differ per animation. When the cell changes, keep the sprite's
        // bottom-centre anchor fixed so switching animations doesn't teleport
        // the character (the canvas otherwise grows right/down in place).
        const hadCell = !!this._cellSized;
        const s = this._scale || Math.max(1, parseInt(this.getAttribute('scale') || '3', 10) || 3);
        const dw = (this._cv.width - sh.meta.frame_width) * s;
        const dh = (this._cv.height - sh.meta.frame_height) * s;
        this._cv.width = sh.meta.frame_width;
        this._cv.height = sh.meta.frame_height;
        this._cellSized = true;
        this._ctx.imageSmoothingEnabled = false;
        this._applyScale();
        if (hadCell && this._pos && (dw || dh)) {
          this._pos.x += dw / 2;   // keep the horizontal centre…
          this._pos.y += dh;       // …and the feet where they were
          this._roamBy(0, 0);      // re-clamp inside the stage
        }
      }
      this.dispatchEvent(new CustomEvent('sprite:anim', { detail: { name }, bubbles: true }));
      return true;
    }

    _applyScale() {
      const s = Math.max(1, parseInt(this.getAttribute('scale') || '3', 10) || 3);
      this._scale = s;
      this._cv.style.width = (this._cv.width * s) + 'px';
      this._cv.style.height = (this._cv.height * s) + 'px';
    }

    _clear() { this._ctx.clearRect(0, 0, this._cv.width, this._cv.height); }

    // ── render + movement loop ──────────────────────────────────────────────
    _tick(now) {
      this._raf = requestAnimationFrame(t => this._tick(t));
      const dt = Math.min(0.1, (now - (this._lastT || now)) / 1000);
      this._lastT = now;
      this._move(dt);
      this._aiUpdate(now, dt);
      const cur = this._cur;
      if (!cur) return;
      const sh = this._sheets[cur.name];
      if (!sh || !sh.img.naturalWidth) return;
      const m = sh.meta;
      const fps = parseFloat(this.getAttribute('fps')) || m.fps || 8;
      let idx = Math.floor((now - cur.start) / 1000 * Math.max(1, fps));
      if (cur.once && idx >= m.count) {           // action finished → fall back
        if (cur.name !== this._fallback && this._sheets[this._fallback]) {
          this._actionUntil = 0;
          this.play(this._fallback);
        }
        idx = m.count - 1;
      }
      idx = idx % Math.max(1, m.count);
      const c = idx % m.columns, r = Math.floor(idx / m.columns);
      const ctx = this._ctx;
      this._clear();
      ctx.save();
      if (this._flip) { ctx.translate(this._cv.width, 0); ctx.scale(-1, 1); }
      ctx.drawImage(sh.img, c * m.frame_width, r * m.frame_height,
                    m.frame_width, m.frame_height,
                    0, 0, m.frame_width, m.frame_height);
      ctx.restore();
    }

    // ── keyboard control ────────────────────────────────────────────────────
    // A key resolves via the character's own maps: dirmap → a movement direction,
    // keymap → a one-shot action animation. Both maps fall back to WASD/arrows +
    // space when the character hasn't bound anything. Space is matched as ' '.
    _dir(e) { return this._dirmap[e.key] || (e.key.length === 1 ? this._dirmap[e.key.toLowerCase()] : null); }
    _act(e) {
      return this._keymap[e.key]
        || (e.key.length === 1 ? this._keymap[e.key.toLowerCase()] : null)
        || (e.code === 'Space' ? this._keymap[' '] : null);
    }
    _bindKeys() {
      if (this._kd) return;
      this._kd = (e) => {
        if (e.target && /^(input|textarea|select)$/i.test(e.target.tagName || '')) return;
        const dir = this._dir(e);
        if (dir) {
          e.preventDefault();
          this._userActive();
          this._keys[dir] = true;
          this._keys.run = e.shiftKey;
          return;
        }
        const anim = this._act(e);
        if (!anim || !this._sheets[anim]) return;
        e.preventDefault();
        this._userActive();
        this._actionUntil = performance.now() + this._animDurMs(anim);
        this.play(anim, { once: true });
      };
      this._ku = (e) => {
        const dir = this._dir(e);
        if (dir) this._keys[dir] = false;
        this._keys.run = e.shiftKey;
        this._userActive();
      };
      window.addEventListener('keydown', this._kd);
      window.addEventListener('keyup', this._ku);
    }
    _unbindKeys() {
      if (!this._kd) return;
      window.removeEventListener('keydown', this._kd);
      window.removeEventListener('keyup', this._ku);
      this._kd = this._ku = null;
      this._keys = {};
    }

    _move(dt) {
      if (!this._kd) return;                    // controls not enabled
      const k = this._keys;
      let dx = (k.right ? 1 : 0) - (k.left ? 1 : 0);
      let dy = (k.down ? 1 : 0) - (k.up ? 1 : 0);
      const moving = dx || dy;
      const inAction = this._actionUntil && performance.now() < this._actionUntil;
      const runAnim = this._events.run, walkAnim = this._events.move;
      if (moving) {
        this._userActive();
        if (dx) this._flip = dx < 0;
        if (this.hasAttribute('roam')) {
          const sp = (k.run ? this._vel.run : this._vel.walk) * (this._scale || 3) / 3;
          const len = Math.hypot(dx, dy) || 1;
          this._roamBy(dx / len * sp * dt, dy / len * sp * dt);
        }
        if (!inAction) {
          const want = (k.run && runAnim) ? runAnim : (walkAnim || runAnim || this._fallback);
          if (want && this._cur && this._cur.name !== want) this.play(want);
        }
      } else if (!inAction && !this._aiState && this._cur
                 && this._cur.name !== this._fallback && !this._cur.once) {
        this.play(this._fallback);
      }
    }

    // ── tamagotchi autonomy ─────────────────────────────────────────────────
    // When the character's autonomy is enabled (record or the `auto` attribute)
    // and the sprite has been left alone for `idle_after` ms, it drives itself:
    // wander to a random spot (needs `roam`), play a random emote, or rest. Any
    // user input, drag, or one-shot action suppresses it.
    _userActive() { this._lastInput = performance.now(); this._aiState = null; this._aiTarget = null; }
    _rand(a, b) { return a + Math.random() * (b - a); }

    _aiUpdate(now, dt) {
      const a = this._auto;
      if (!a || !a.enabled) return;
      if (this.classList.contains('dragging')) { this._lastInput = now; return; }
      if ((now - (this._lastInput || 0)) < a.idle_after) return;
      if (this._actionUntil && now < this._actionUntil) return;
      const k = this._keys || {};
      if (k.left || k.right || k.up || k.down) return;

      if (this._aiState === 'walk' && this._aiTarget && this.hasAttribute('roam')) {
        this._roamInit();
        const dx = this._aiTarget.x - this._pos.x, dy = this._aiTarget.y - this._pos.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 3) {
          this._aiState = null; this._aiTarget = null;
          const rest = this._events.idle || this._fallback;
          if (rest && (!this._cur || this._cur.name !== rest)) this.play(rest);
          this._aiNext = now + this._rand(a.min_ms, a.max_ms);
        } else {
          const sp = this._vel.walk * (this._scale || 3) / 3;
          const step = Math.min(dist, sp * dt);
          if (Math.abs(dx) > 0.5) this._flip = dx < 0;
          this._roamBy(dx / dist * step, dy / dist * step);
          const wa = this._events.move || this._events.run;
          if (wa && (!this._cur || this._cur.name !== wa)) this.play(wa);
        }
        return;
      }
      if (this._aiNext && now < this._aiNext) return;
      this._aiPick(now);
    }

    _aiPick(now) {
      const a = this._auto;
      const roll = Math.random();
      const canWander = a.wander && this.hasAttribute('roam');
      if (canWander && roll < 0.55) {
        this._roamInit();
        const p = this.offsetParent || this.parentElement || document.body;
        const maxX = Math.max(0, (p.clientWidth || 0) - this.offsetWidth);
        const maxY = Math.max(0, (p.clientHeight || 0) - this.offsetHeight);
        this._aiTarget = { x: Math.random() * maxX, y: Math.random() * maxY };
        this._aiState = 'walk';
        return;
      }
      if (a.emote && roll < 0.85) {
        const cands = [this._events.emote, this._events.happy, this._events.greet].filter(n => n && this._sheets[n]);
        const actions = this.animations.filter(n => this._sheets[n].meta.loop === false);
        const pool = cands.length ? cands : actions;
        const pick = pool.length ? pool[Math.floor(Math.random() * pool.length)] : null;
        if (pick) { this.play(pick, { once: true }); this._actionUntil = performance.now() + this._animDurMs(pick); }
      } else {
        const rest = this._events.sleep || this._events.idle || this._fallback;
        if (rest && (!this._cur || this._cur.name !== rest)) this.play(rest);
      }
      this._aiState = null; this._aiNext = now + this._rand(a.min_ms, a.max_ms);
    }

    // ── roaming + dragging ──────────────────────────────────────────────────
    _roamInit() {
      if (this._pos) return;
      const p = this.offsetParent || this.parentElement || document.body;
      // Default spawn: bottom-LEFT with a small margin. (It used to default to
      // bottom-centre, and only on the first move — so the sprite sat wherever
      // it rendered until a keypress teleported it. _roamInit is now also
      // called as soon as the sheets load, so the spawn point is where the
      // sprite actually stands and the first move is a step, not a jump.)
      this._pos = { x: this.offsetLeft || 14,
                    y: this.offsetTop || Math.max(0, p.clientHeight - this.offsetHeight - 8) };
      this._roamBy(0, 0);
    }
    _roamBy(dx, dy) {
      this._roamInit();
      const p = this.offsetParent || this.parentElement || document.body;
      const maxX = Math.max(0, (p.clientWidth || 0) - this.offsetWidth);
      const maxY = Math.max(0, (p.clientHeight || 0) - this.offsetHeight);
      this._pos.x = Math.min(maxX, Math.max(0, this._pos.x + dx));
      this._pos.y = Math.min(maxY, Math.max(0, this._pos.y + dy));
      this.style.left = this._pos.x + 'px';
      this.style.top = this._pos.y + 'px';
    }

    _bindDrag() {
      if (!this.hasAttribute('roam')) this.setAttribute('roam', '');
      let start = null;
      this.addEventListener('pointerdown', (e) => {
        this._roamInit();
        start = { x: e.clientX, y: e.clientY, px: this._pos.x, py: this._pos.y };
        this.classList.add('dragging');
        this.setPointerCapture(e.pointerId);
      });
      this.addEventListener('pointermove', (e) => {
        if (!start) return;
        this._pos.x = start.px; this._pos.y = start.py;
        this._roamBy(e.clientX - start.x, e.clientY - start.y);
        start.px = this._pos.x; start.py = this._pos.y;
        start.x = e.clientX; start.y = e.clientY;
      });
      const end = (e) => {
        if (!start) return;
        start = null;
        this.classList.remove('dragging');
        try { this.releasePointerCapture(e.pointerId); } catch (_) {}
      };
      this.addEventListener('pointerup', end);
      this.addEventListener('pointercancel', end);
    }
  }

  customElements.define('vera-sprite', VeraSprite);
})();

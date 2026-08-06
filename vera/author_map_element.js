/**
 * <vera-author-map> — real authorship map: every recent commit to this
 * repo, tagged with WHO/WHAT actually produced it. Answers directly:
 * "can I see which changes were made by Claude Code vs Vera's own
 * autonomous agent vs a direct human commit, mapped to branches?"
 *
 * Data: GET /evolve/authors?hours=&branch= (new capability) — which itself
 * only ever tags a commit from REAL correlated data: a Claude Code session
 * whose real ingested transcript time-window covered that commit (via
 * ide.claude_sessions.list_sessions' own git-log join), or a Vera
 * evolve.ide.improve run whose real `engine` field (claude|vera-agent) and
 * commit-window covered it, or — if neither correlates — left as "direct"
 * rather than guessed. Never fabricated attribution.
 *
 * Visual language borrows bun.com's adversarial-review chat-bubble pattern
 * (colored avatar circle + bordered card per entry) applied to real commit
 * data instead: an avatar per AGENT (🧑‍💻 Claude Code, ⚙ Vera agent, ● direct
 * human), a monospace commit hash, the real message, and — for
 * Claude-Code-attributed commits — the real session id so it's traceable
 * back to the exact transcript.
 *
 * Truthful animation: new commits (poll-diffed against the previously seen
 * hash set) reveal with a one-shot staggered fade+slide — never looping,
 * never re-animating commits already shown. Idle = zero motion.
 *
 * Attributes: hours (default 72), branch (optional — filter to one branch's log)
 * Public API: setApiBase(url), setBranch(b), refresh()
 */
(function () {
  if (customElements.get('vera-author-map')) return;

  const AGENT_META = {
    claude: { icon: '🧑‍💻', color: '#fbf0df', label: 'Claude Code' },
    'vera-agent': { icon: '⚙', color: '#5a9e8f', label: 'Vera agent' },
    direct: { icon: '●', color: '#6a6058', label: 'direct' },
  };

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;font-family:var(--sans,system-ui,sans-serif);font-size:11px;
  color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.hdr{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.hdr .ttl{font-weight:600;color:var(--dim2,var(--t3,#8a7e70));text-transform:uppercase;
  font-size:10px;letter-spacing:.04em}
.legend{display:flex;gap:10px;margin-left:auto;font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70))}
.legend span{display:inline-flex;align-items:center;gap:3px}
.list{display:flex;flex-direction:column;gap:6px;max-height:340px;overflow:auto}
.row{display:flex;gap:8px;align-items:flex-start;opacity:1}
.row.enter{animation:amEnter .38s ease-out}
@keyframes amEnter{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.av{width:22px;height:22px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;font-size:11px;background:var(--bg2,var(--s2,#272421));
  border:1.5px solid var(--dim,var(--t2,#6a6058))}
.card{flex:1;min-width:0;background:var(--bg1,var(--s1,#1f1d1a));border-radius:7px;
  border-left:3px solid var(--dim,var(--t2,#6a6058));padding:6px 9px}
.card .top{display:flex;gap:8px;align-items:baseline}
.card .hash{font-family:var(--mono,monospace);font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70))}
.card .who{font-weight:600;font-size:9.5px}
.card .flex{flex:1}
.card .when{font-size:9px;color:var(--dim,var(--t2,#6a6058))}
.card .msg{font-size:10.5px;margin-top:2px;word-break:break-word}
.card .sub{font-size:8.5px;color:var(--dim2,var(--t3,#8a7e70));margin-top:2px}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
.hdr select{background:var(--bg2,var(--s2,#272421));border:1px solid var(--border,var(--bd2,#3a3530));
  color:var(--text,var(--t1,#ddd5c8));border-radius:5px;font-size:10px;padding:2px 6px}
</style>
<div class="hdr">
  <span class="ttl">Authorship map</span>
  <select id="hoursSel">
    <option value="24">24h</option>
    <option value="72" selected>72h</option>
    <option value="168">7d</option>
  </select>
  <div class="legend">
    <span>🧑‍💻 Claude Code</span><span>⚙ Vera agent</span><span>● direct</span>
  </div>
</div>
<div class="list" id="list"></div>`;

  class VeraAuthorMap extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._pollTimer = null;
      this._knownHashes = new Set();
      this._firstRender = true;
    }

    connectedCallback() {
      this._hours = parseInt(this.getAttribute('hours') || '72', 10);
      this._branch = this.getAttribute('branch') || '';
      const sel = this.shadowRoot.getElementById('hoursSel');
      sel.value = String(this._hours);
      sel.addEventListener('change', () => { this._hours = parseInt(sel.value, 10); this.refresh(); });
      this.refresh();
      this._pollTimer = setInterval(() => this.refresh(), 20000);
    }

    disconnectedCallback() { if (this._pollTimer) clearInterval(this._pollTimer); }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }
    setBranch(b) { this._branch = b || ''; this.refresh(); }

    async refresh() {
      const qs = new URLSearchParams({ hours: this._hours });
      if (this._branch) qs.set('branch', this._branch);
      let d;
      try {
        const r = await fetch(this._getBase() + '/evolve/authors?' + qs.toString());
        d = await r.json();
      } catch (_) { d = null; }
      const commits = (d && d.commits) || [];
      this._render(commits);
    }

    _relTime(ts) {
      const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
      if (s < 60) return s + 's ago';
      if (s < 3600) return Math.floor(s / 60) + 'm ago';
      if (s < 86400) return Math.floor(s / 3600) + 'h ago';
      return Math.floor(s / 86400) + 'd ago';
    }

    _render(commits) {
      const list = this.shadowRoot.getElementById('list');
      if (!commits.length) {
        list.innerHTML = '<div class="empty">No commits in this window.</div>';
        this._knownHashes = new Set();
        this._firstRender = false;
        return;
      }
      const isNewSet = !this._firstRender;
      list.innerHTML = commits.map((c, i) => {
        const meta = AGENT_META[c.agent] || AGENT_META.direct;
        const isNewRow = isNewSet && !this._knownHashes.has(c.hash);
        const sub = c.agent === 'claude'
          ? (c.session_id ? 'session ' + this._esc(String(c.session_id).slice(0, 12)) : '')
          : (c.agent === 'vera-agent' ? (c.task ? 'run · ' + this._esc(c.task) : '') : '');
        return '<div class="row' + (isNewRow ? ' enter' : '') + '" style="animation-delay:' +
          (isNewRow ? Math.min(i, 8) * 60 : 0) + 'ms">' +
          '<div class="av" style="border-color:' + meta.color + '">' + meta.icon + '</div>' +
          '<div class="card" style="border-left-color:' + meta.color + '">' +
          '<div class="top"><span class="who" style="color:' + meta.color + '">' + meta.label + '</span>' +
          '<span class="hash">' + this._esc((c.hash || '').slice(0, 8)) + '</span>' +
          '<span class="flex"></span><span class="when">' + this._relTime(c.ts) + '</span></div>' +
          '<div class="msg">' + this._esc(c.message || '') + '</div>' +
          (sub ? '<div class="sub">' + sub + ' · git author: ' + this._esc(c.author || '') + '</div>' : '') +
          '</div></div>';
      }).join('');
      this._knownHashes = new Set(commits.map(c => c.hash));
      this._firstRender = false;
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  }

  customElements.define('vera-author-map', VeraAuthorMap);
})();

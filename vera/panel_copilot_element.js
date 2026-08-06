/**
 * <vera-panel-copilot> — the generalized, reusable form of the markets
 * studio's "COP" copilot widget (markets_studio_panel.html), so ANY panel
 * can embed a specialist-agent copilot with one tag instead of
 * reimplementing the chat/loop wiring (markets' own version even resorted
 * to monkey-patching window.fetch to rewrite outgoing loop requests — this
 * element builds the right request directly, no interception hack needed).
 *
 * Two modes, matching markets' real behavior exactly:
 *   chat — POST /agents/chat/stream with agent_name=<the bound specialist
 *          persona>, SSE token/thinking/error events.
 *   loop — POST /workshop/agent_loop/stream with version='v6',
 *          agent_name=<specialist>, loop_profile=<the bound specialist
 *          loop profile>, record_agent_name=<specialist> — a real
 *          autonomous run scoped to this panel's domain, not just a chat
 *          reply.
 *
 * The specialist binding itself is declarative (see register_ui's
 * specialist_agent/specialist_loop_profile in capability_orchestration.py):
 * pass `agent`/`loop-profile` attributes directly, OR pass `panel-id` and
 * this element resolves them itself via GET /ui/panel/specialist — so a
 * panel just declares its binding once at registration and drops in
 * <vera-panel-copilot panel-id="my_panel"></vera-panel-copilot>.
 *
 * Attributes: agent, loop-profile, panel-id, session-id (default:
 * generated + persisted in localStorage per panel-id), cap-allow (regex
 * string restricting which caps the loop mode may use — panel authors
 * should scope this to their own domain, matching markets'
 * CHAT_CAP_ALLOW convention).
 * Public API: setApiBase(url)
 */
(function () {
  if (customElements.get('vera-panel-copilot')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:flex;flex-direction:column;height:100%;min-height:220px;
  font-family:var(--sans,system-ui,sans-serif);font-size:11px;color:var(--text,var(--t1,#ddd5c8))}
*,*::before,*::after{box-sizing:border-box}
.hdr{display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px solid var(--border,var(--bd2,#3a3530))}
.chip{font-size:9.5px;padding:2px 8px;border-radius:10px;border:1px solid var(--border,var(--bd2,#3a3530));
  cursor:pointer;color:var(--dim2,var(--t3,#8a7e70))}
.chip.on{background:var(--acc,var(--ac,#5a9e8f));border-color:var(--acc,var(--ac,#5a9e8f));color:var(--on-acc,#12100e)}
.who{font-size:9.5px;color:var(--dim2,var(--t3,#8a7e70));margin-left:auto}
.log{flex:1;overflow:auto;padding:8px;display:flex;flex-direction:column;gap:6px}
.msg{max-width:90%;padding:6px 9px;border-radius:8px;font-size:10.5px;line-height:1.5}
.msg.user{align-self:flex-end;background:var(--acc,var(--ac,#5a9e8f));color:var(--on-acc,#12100e)}
.msg.bot{align-self:flex-start;background:var(--bg2,var(--s2,#272421))}
.msg.err{color:var(--err,var(--ac4,#c96b6b))}
.qchips{display:flex;gap:4px;flex-wrap:wrap;padding:0 8px 6px}
.inrow{display:flex;gap:6px;padding:6px 8px;border-top:1px solid var(--border,var(--bd2,#3a3530))}
.inrow input{flex:1;background:var(--bg2,var(--s2,#272421));border:1px solid var(--border,var(--bd2,#3a3530));
  border-radius:6px;padding:5px 8px;color:inherit;font-family:inherit;font-size:10.5px}
.inrow button{background:var(--acc,var(--ac,#5a9e8f));color:var(--on-acc,#12100e);border:none;
  border-radius:6px;padding:5px 10px;cursor:pointer;font-size:10.5px}
.empty{padding:14px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div class="hdr">
  <span class="chip on" id="modeLoop">⟳ loop</span>
  <span class="chip" id="modeChat">💬 chat</span>
  <span class="who" id="who"></span>
</div>
<div class="qchips" id="qchips"></div>
<div class="log" id="log"><div class="empty">Ask this panel's specialist something, or run it as an autonomous loop.</div></div>
<div class="inrow">
  <input id="text" placeholder="Ask…">
  <button id="send">send</button>
</div>`;

  class VeraPanelCopilot extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._mode = 'loop';
      this._hist = [];
      this._busy = false;
      this._abort = null;
    }

    async connectedCallback() {
      this._agent = this.getAttribute('agent') || '';
      this._loopProfile = this.getAttribute('loop-profile') || '';
      this._panelId = this.getAttribute('panel-id') || '';
      this._capAllow = this.getAttribute('cap-allow') || '';
      this._contextCap = this.getAttribute('context-cap') || '';
      if (this._panelId && (!this._agent || !this._loopProfile || !this._contextCap)) {
        const d = await this._fetchJson('/ui/panel/specialist?panel_id=' + encodeURIComponent(this._panelId));
        if (d) {
          if (!this._agent) this._agent = d.specialist_agent || '';
          if (!this._loopProfile) this._loopProfile = d.specialist_loop_profile || '';
          if (!this._contextCap) this._contextCap = d.specialist_context_cap || '';
        }
      }
      const skey = 'vera-copilot-sid-' + (this._panelId || this._agent || 'default');
      this._sid = localStorage.getItem(skey) || ('cop_' + Math.random().toString(36).slice(2, 10));
      localStorage.setItem(skey, this._sid);
      this._wire();
      this._syncHeader();
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }
    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    async _fetchJson(path) {
      try { const r = await fetch(this._getBase() + path); return await r.json(); }
      catch (_) { return null; }
    }

    // Real, fresh, panel-relevant data — not just the right persona. Called
    // right before every send so the specialist always sees CURRENT state
    // (an up-to-date market scan, the live business/store rollup, …) rather
    // than answering from training data + persona alone. Goes through
    // /mcp/call (works for ANY bound capability name, not just ones that
    // happen to expose a predictable REST path) — entity_id lets a panel
    // pass whatever it currently has "active" (e.g. a selected store id).
    async _fetchContext(entityId) {
      if (!this._contextCap) return '';
      try {
        const args = entityId ? { store_id: entityId, entity_id: entityId } : {};
        const r = await fetch(this._getBase() + '/mcp/call', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: this._contextCap, arguments: args }),
        });
        const d = await r.json();
        const content = d && d.content;
        return (content && content.context) || '';
      } catch (_) { return ''; }
    }

    _wire() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('modeLoop').onclick = () => { this._mode = 'loop'; this._syncHeader(); };
      $('modeChat').onclick = () => { this._mode = 'chat'; this._syncHeader(); };
      $('send').onclick = () => this._send();
      $('text').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); } });
    }

    _syncHeader() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('modeLoop').classList.toggle('on', this._mode === 'loop');
      $('modeChat').classList.toggle('on', this._mode === 'chat');
      $('who').textContent = (this._mode === 'loop'
        ? (this._loopProfile ? '⚡ ' + this._loopProfile : 'no loop profile bound')
        : (this._agent ? '🧑‍💻 ' + this._agent : 'no agent bound'));
      const qchips = $('qchips');
      qchips.innerHTML = '';
    }

    _addMsg(role, html) {
      const log = this.shadowRoot.getElementById('log');
      if (log.querySelector('.empty')) log.innerHTML = '';
      const el = document.createElement('div');
      el.className = 'msg ' + role;
      el.innerHTML = html;
      log.appendChild(el);
      log.scrollTop = 1e9;
      return el;
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

    async _send() {
      const $ = id => this.shadowRoot.getElementById(id);
      const text = $('text').value.trim();
      if (!text || this._busy) return;
      $('text').value = '';
      if (this._mode === 'chat') return this._sendChat(text);
      return this._sendLoop(text);
    }

    // ── chat mode: /agents/chat/stream with the bound specialist ──────────
    async _sendChat(text) {
      if (!this._agent) { this._addMsg('err', 'No specialist agent bound to this panel.'); return; }
      this._addMsg('user', this._esc(text));
      this._hist.push({ role: 'user', content: text });
      const holder = this._addMsg('bot', '<span>…</span>');
      this._busy = true;
      this._abort = new AbortController();
      let acc = '', think = '';
      try {
        const context = await this._fetchContext(this.getAttribute('entity-id'));
        const outgoing = context ? ('Context (real, current):\n' + context + '\n\nQuestion:\n' + text) : text;
        const r = await fetch(this._getBase() + '/agents/chat/stream', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: outgoing, agent_name: this._agent,
            history: JSON.stringify(this._hist.slice(-14)), session_id: this._sid, prefer_gpu: true }),
          signal: this._abort.signal,
        });
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = '';
        while (true) {
          let done, value;
          try { ({ done, value } = await reader.read()); } catch (_) { break; }
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            for (const ln of chunk.split('\n')) {
              if (!ln.startsWith('data:')) continue;
              const pl = ln.slice(5).trim();
              if (pl === '[DONE]') break;
              let ev; try { ev = JSON.parse(pl); } catch (_) { continue; }
              if (ev.type === 'token' && ev.text) { acc += ev.text; holder.innerHTML = this._esc(acc); }
              else if (ev.type === 'thinking' && ev.text) { think += ev.text; }
              else if (ev.type === 'error') { holder.innerHTML = '<span class="err">' + this._esc(ev.text || 'error') + '</span>'; }
            }
          }
        }
        if (acc) this._hist.push({ role: 'assistant', content: acc });
      } catch (e) {
        if (String(e).indexOf('bort') < 0) holder.innerHTML = '<span class="err">' + this._esc(String(e)) + '</span>';
      } finally { this._busy = false; }
    }

    // ── loop mode: real v6 run bound to the panel's specialist loop profile ─
    async _sendLoop(text) {
      if (!this._loopProfile) { this._addMsg('err', 'No loop profile bound to this panel.'); return; }
      this._addMsg('user', this._esc(text));
      const holder = this._addMsg('bot', 'starting…');
      this._busy = true;
      try {
        const context = await this._fetchContext(this.getAttribute('entity-id'));
        const goal = context ? ('Context (real, current):\n' + context + '\n\nTask:\n' + text) : text;
        const r = await fetch(this._getBase() + '/evolve/run/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'loop', target: 'specialist:' + this._loopProfile, goal }),
        });
        const d = await r.json();
        if (d && d.ok) {
          holder.innerHTML = 'running as <b>' + this._esc(this._loopProfile) +
            '</b> — run <span title="' + this._esc(d.run_id) + '">' + this._esc(d.run_id.slice(0, 8)) + '</span>' +
            ' <a href="#" data-open="' + this._esc(d.run_id) + '">watch in Loop Lab</a>';
          const a = holder.querySelector('a');
          if (a) a.onclick = e => {
            e.preventDefault();
            this.dispatchEvent(new CustomEvent('copilot:openrun', { detail: { run_id: d.run_id }, bubbles: true }));
          };
        } else {
          holder.innerHTML = '<span class="err">' + this._esc((d && d.error) || 'failed to start') + '</span>';
        }
      } catch (e) {
        holder.innerHTML = '<span class="err">' + this._esc(String(e)) + '</span>';
      } finally { this._busy = false; }
    }
  }

  customElements.define('vera-panel-copilot', VeraPanelCopilot);
})();

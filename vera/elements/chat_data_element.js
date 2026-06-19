/**
 * <vera-chat-data> — Injectable "Chat with Data" custom element
 *
 * A general-purpose conversational widget that talks to the Vera assistant
 * agent (which has tool access to the data fabric, capabilities, and memory).
 * Drop it into any dashboard grid slot via the widget loader, or embed it
 * standalone — it derives the backend base from window.location.origin and
 * needs no host globals.
 *
 * Backend: POST /agents/chat  { message, agent_name, history, session_id }
 *
 * Attributes:
 *   agent      — agent name to chat with (default "assistant")
 *   placeholder— input placeholder text
 *
 * Public API:
 *   el.setApiBase(url)   — override backend URL
 *   el.reset()           — clear the conversation
 *
 * Events dispatched: vcd:sent, vcd:reply
 */
(function () {
  if (customElements.get('vera-chat-data')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;height:100%;overflow:hidden;color:var(--text,#ddd5c8);font-family:var(--sans,'Inter',system-ui,sans-serif);font-size:12px;background:transparent}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
.wrap{display:flex;flex-direction:column;height:100%;min-height:0}
.toolbar{padding:5px 8px;border-bottom:1px solid var(--border,#3a3530);display:flex;align-items:center;gap:6px;background:var(--bg1,#1f1d1a);flex-shrink:0}
.title{font-size:10px;font-weight:600;color:var(--text,#ddd5c8)}
.sub{font-size:8px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace)}
.btn{background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);cursor:pointer;padding:2px 8px;border-radius:3px;font-size:9px;font-family:inherit;transition:.12s}
.btn:hover{border-color:var(--acc,#5a9e8f);color:var(--text,#ddd5c8)}
.btn.primary{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}
.btn:disabled{opacity:.5;cursor:default}
.log{flex:1;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:8px;min-height:0}
.msg{display:flex;flex-direction:column;gap:2px;max-width:92%}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.bot{align-self:flex-start}
.role{font-size:7.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace)}
.bubble{padding:6px 9px;border-radius:8px;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.user .bubble{background:var(--acc,#5a9e8f);color:var(--bg0,#181614)}
.bot .bubble{background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8)}
.bot .bubble.err{border-color:var(--err,#c96b6b);color:var(--err,#c96b6b)}
.hint{color:var(--dim,#6a6058);font-size:9.5px;padding:10px;text-align:center;line-height:1.6}
.inbar{border-top:1px solid var(--border,#3a3530);padding:6px 8px;display:flex;gap:6px;align-items:flex-end;background:var(--bg1,#1f1d1a);flex-shrink:0}
textarea{flex:1;height:34px;max-height:120px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);padding:5px 7px;border-radius:5px;font-size:11px;font-family:inherit;resize:none}
textarea:focus{outline:none;border-color:var(--acc,#5a9e8f)}
.typing{font-size:9px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace);padding:0 8px 4px}
</style>
<div class="wrap">
  <div class="toolbar">
    <span class="title">Chat with Data</span>
    <span class="sub" id="agentLbl"></span>
    <button class="btn" id="resetBtn" style="margin-left:auto">Reset</button>
  </div>
  <div class="log" id="log">
    <div class="hint" id="hint">Ask a question about your data, capabilities, or memory.<br>The assistant can query the data fabric to answer.</div>
  </div>
  <div class="typing" id="typing" style="display:none">assistant is thinking…</div>
  <div class="inbar">
    <textarea id="input" placeholder="Ask about your data…"></textarea>
    <button class="btn primary" id="sendBtn">Send</button>
  </div>
</div>`;

  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  class VeraChatData extends HTMLElement {
    constructor() {
      super();
      this._base = window.location.origin;
      this._history = [];
      this._session = 'cwd-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      this._busy = false;
      this.attachShadow({ mode: 'open' }).appendChild(TMPL.content.cloneNode(true));
    }

    connectedCallback() {
      const $ = (id) => this.shadowRoot.getElementById(id);
      this._el = { log: $('log'), hint: $('hint'), input: $('input'), send: $('sendBtn'), typing: $('typing') };
      const agent = this.getAttribute('agent') || 'assistant';
      $('agentLbl').textContent = '· ' + agent;
      const ph = this.getAttribute('placeholder');
      if (ph) this._el.input.placeholder = ph;
      this._el.send.onclick = () => this._send();
      $('resetBtn').onclick = () => this.reset();
      this._el.input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
      });
    }

    setApiBase(url) { if (url) this._base = String(url).replace(/\/$/, ''); }

    reset() {
      this._history = [];
      this._session = 'cwd-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      this._el.log.innerHTML = '';
      this._el.log.appendChild(this._el.hint);
      this._el.hint.style.display = '';
    }

    _bubble(role, text, isErr) {
      if (this._el.hint && this._el.hint.parentNode) this._el.hint.style.display = 'none';
      const wrap = document.createElement('div');
      wrap.className = 'msg ' + (role === 'user' ? 'user' : 'bot');
      wrap.innerHTML = '<span class="role">' + (role === 'user' ? 'you' : 'assistant') + '</span>'
        + '<div class="bubble' + (isErr ? ' err' : '') + '">' + esc(text) + '</div>';
      this._el.log.appendChild(wrap);
      this._el.log.scrollTop = this._el.log.scrollHeight;
      return wrap.querySelector('.bubble');
    }

    async _send() {
      if (this._busy) return;
      const msg = (this._el.input.value || '').trim();
      if (!msg) return;
      this._el.input.value = '';
      this._bubble('user', msg);
      this._history.push({ role: 'user', content: msg });
      this.dispatchEvent(new CustomEvent('vcd:sent', { detail: { message: msg } }));

      this._busy = true;
      this._el.send.disabled = true;
      this._el.typing.style.display = '';
      const agent = this.getAttribute('agent') || 'assistant';
      try {
        const r = await fetch(this._base + '/agents/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: msg,
            agent_name: agent,
            history: JSON.stringify(this._history.slice(0, -1)),
            session_id: this._session,
          }),
        });
        const data = await r.json().catch(() => null);
        const reply = data && (data.text || data.answer || data.response || data.content || data.message);
        if (reply) {
          this._bubble('bot', reply);
          this._history.push({ role: 'assistant', content: reply });
          this.dispatchEvent(new CustomEvent('vcd:reply', { detail: { reply, raw: data } }));
        } else {
          this._bubble('bot', (data && data.error) || 'No response from agent.', true);
        }
      } catch (e) {
        this._bubble('bot', 'Request failed: ' + (e.message || e), true);
      } finally {
        this._busy = false;
        this._el.send.disabled = false;
        this._el.typing.style.display = 'none';
        this._el.input.focus();
      }
    }
  }

  customElements.define('vera-chat-data', VeraChatData);
})();

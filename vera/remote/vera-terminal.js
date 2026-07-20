/* vera-terminal.js — <vera-terminal> custom element
 * ===================================================
 * A self-contained xterm.js terminal that speaks Vera's terminal WebSocket
 * protocol (see remote_capabilities.py). Reused by the Remote panel, the
 * Workspaces panel, and any dashboard "terminal" widget.
 *
 * Usage:
 *   const t = document.createElement('vera-terminal');
 *   t.setAttribute('ws', '/remote/ssh/term/ws/<host_id>?shell=');
 *   container.appendChild(t);           // auto-connects on connectedCallback
 *   // …or programmatically:
 *   t.connect('/remote/docker/term/ws/local/mycontainer?shell=sh');
 *
 * Wire protocol:
 *   client → server : {"d":"<keys>"} to write, {"r":[cols,rows]} to resize
 *   server → client : raw binary output bytes
 */
(function () {
  if (window.customElements && customElements.get('vera-terminal')) return;

  const XTERM_JS  = 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js';
  const XTERM_CSS = 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css';
  const FIT_JS    = 'https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js';

  let _loading = null;
  function loadXterm() {
    if (window.Terminal && window.FitAddon) return Promise.resolve();
    if (_loading) return _loading;
    _loading = new Promise((resolve, reject) => {
      // CSS (once)
      if (!document.querySelector('link[data-vera-xterm]')) {
        const l = document.createElement('link');
        l.rel = 'stylesheet'; l.href = XTERM_CSS; l.setAttribute('data-vera-xterm', '1');
        document.head.appendChild(l);
      }
      const addScript = (src) => new Promise((res, rej) => {
        const existing = document.querySelector(`script[src="${src}"]`);
        if (existing) { existing.addEventListener('load', res); existing.addEventListener('error', rej);
                        if (existing.getAttribute('data-loaded')) res(); return; }
        const s = document.createElement('script');
        s.src = src;
        s.onload = () => { s.setAttribute('data-loaded', '1'); res(); };
        s.onerror = rej;
        document.head.appendChild(s);
      });
      addScript(XTERM_JS)
        .then(() => addScript(FIT_JS))
        .then(resolve)
        .catch(reject);
    });
    return _loading;
  }

  class VeraTerminal extends HTMLElement {
    constructor() {
      super();
      this.term = null; this.fit = null; this.ws = null;
      this._ro = null; this._wsPath = ''; this._connected = false;
    }

    connectedCallback() {
      this.style.display = 'block';
      this.style.position = 'relative';
      if (!this.style.height) this.style.height = '100%';
      this.style.background = '#000';
      const p = this.getAttribute('ws');
      if (p && !this._connected) this.connect(p);
    }

    disconnectedCallback() { this.destroy(); }

    async connect(wsPath) {
      this._wsPath = wsPath || this._wsPath;
      if (!this._wsPath) return;
      this._connected = true;
      try { await loadXterm(); }
      catch (e) { this._fail('Terminal library failed to load (CDN blocked?).'); return; }
      if (!window.Terminal) { this._fail('xterm.js unavailable.'); return; }

      this.innerHTML = '';
      const mount = document.createElement('div');
      mount.style.cssText = 'position:absolute;inset:0;';
      this.appendChild(mount);

      const theme = this._readTheme();
      this.term = new window.Terminal({
        cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        theme,
      });
      try { this.fit = new window.FitAddon.FitAddon(); this.term.loadAddon(this.fit); }
      catch (e) { this.fit = null; }
      this.term.open(mount);
      this._doFit();

      const base = location.origin.replace(/^http/, 'ws');
      const sep = this._wsPath.includes('?') ? '&' : '?';
      const url = base + this._wsPath + sep + 'cols=' + this.term.cols + '&rows=' + this.term.rows;
      const ws = new WebSocket(url);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      const dec = new TextDecoder();

      ws.onopen = () => { this._sendResize(); this.term.focus(); };
      ws.onmessage = (ev) => {
        try {
          if (typeof ev.data === 'string') this.term.write(ev.data);
          else this.term.write(new Uint8Array(ev.data));
        } catch (e) {}
      };
      ws.onclose = () => { try { this.term.write('\r\n\x1b[90m[disconnected]\x1b[0m\r\n'); } catch (e) {} };
      ws.onerror = () => { try { this.term.write('\r\n\x1b[91m[socket error]\x1b[0m\r\n'); } catch (e) {} };

      this.term.onData((d) => {
        if (ws.readyState === 1) { try { ws.send(JSON.stringify({ d })); } catch (e) {} }
      });

      // Auto-fit on resize.
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => this._doFit(true));
        this._ro.observe(this);
      }
      window.addEventListener('resize', this._onWinResize = () => this._doFit(true));
    }

    _readTheme() {
      // Pull a couple of vars from the active Vera theme so terminals match.
      try {
        const cs = getComputedStyle(document.documentElement);
        const bg = (cs.getPropertyValue('--bg0') || '#0d0f12').trim();
        const fg = (cs.getPropertyValue('--fg') || '#d6dde6').trim();
        return { background: bg || '#000', foreground: fg || '#d6dde6' };
      } catch (e) { return { background: '#000' }; }
    }

    _doFit(sendAfter) {
      if (!this.term) return;
      try { this.fit && this.fit.fit(); } catch (e) {}
      if (sendAfter) this._sendResize();
    }

    _sendResize() {
      if (!this.term || !this.ws || this.ws.readyState !== 1) return;
      try { this.ws.send(JSON.stringify({ r: [this.term.cols, this.term.rows] })); } catch (e) {}
    }

    _fail(msg) {
      this.innerHTML = '<div style="color:#e88;font:13px/1.5 ui-monospace,monospace;padding:12px">'
        + msg + '</div>';
    }

    fit() { this._doFit(true); }
    focus() { try { this.term && this.term.focus(); } catch (e) {} }

    destroy() {
      try { this._ro && this._ro.disconnect(); } catch (e) {}
      try { this._onWinResize && window.removeEventListener('resize', this._onWinResize); } catch (e) {}
      try { this.ws && this.ws.close(); } catch (e) {}
      try { this.term && this.term.dispose(); } catch (e) {}
      this.ws = null; this.term = null; this._connected = false;
    }
  }

  customElements.define('vera-terminal', VeraTerminal);
})();

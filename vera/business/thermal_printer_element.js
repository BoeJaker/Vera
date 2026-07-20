/* thermal_printer_element.js — <vera-thermal-printer>
 *
 * A self-contained Web Serial bridge for USB thermal printers, used by the
 * Business panel (and reusable anywhere). It lets a *web client* drive a
 * printer that is plugged into that client's machine — the "serial/USB
 * forwarding from web clients to the server" path described in the printer
 * module: the server builds ESC/POS bytes, the browser writes them.
 *
 * Public API (call on the element instance):
 *   el.connect()             → prompts the user to pick a serial port
 *   el.disconnect()
 *   el.printB64(base64)      → writes raw ESC/POS bytes to the port
 *   el.isConnected()         → bool
 *   attribute [baud="9600"]  → serial baud rate
 *
 * Emits CustomEvents: 'tp:connect', 'tp:disconnect', 'tp:print', 'tp:error'.
 *
 * Web Serial is Chromium-only and needs a secure context (https/localhost);
 * the element degrades gracefully with a clear message if unavailable.
 */
(function () {
  if (window.customElements && customElements.get('vera-thermal-printer')) return;

  const b64ToBytes = (b64) => {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };

  class VeraThermalPrinter extends HTMLElement {
    constructor() {
      super();
      this._port = null;
      this._writer = null;
      this.attachShadow({ mode: 'open' });
    }

    connectedCallback() { this._render(); }

    get baud() { return parseInt(this.getAttribute('baud') || '9600', 10) || 9600; }
    isConnected() { return !!this._port; }

    _emit(name, detail) {
      this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
    }

    async connect() {
      if (!('serial' in navigator)) {
        this._status('Web Serial unavailable (use Chrome/Edge over https or localhost)', 'err');
        this._emit('tp:error', { error: 'no-web-serial' });
        return false;
      }
      try {
        this._port = await navigator.serial.requestPort();
        await this._port.open({ baudRate: this.baud });
        this._writer = this._port.writable.getWriter();
        const info = this._port.getInfo ? this._port.getInfo() : {};
        this._status('Connected' + (info.usbVendorId ? ` (vid ${info.usbVendorId.toString(16)})` : ''), 'ok');
        this._emit('tp:connect', { info });
        this._render();
        return true;
      } catch (e) {
        this._status('Connect cancelled/failed: ' + (e.message || e), 'err');
        this._emit('tp:error', { error: String(e) });
        return false;
      }
    }

    async disconnect() {
      try {
        if (this._writer) { try { this._writer.releaseLock(); } catch (_) {} this._writer = null; }
        if (this._port) { await this._port.close(); }
      } catch (_) {}
      this._port = null;
      this._status('Disconnected', '');
      this._emit('tp:disconnect', {});
      this._render();
    }

    async printB64(b64) {
      if (!this._port || !this._writer) {
        const ok = await this.connect();
        if (!ok) return false;
      }
      try {
        await this._writer.write(b64ToBytes(b64));
        this._status('Printed ' + b64ToBytes(b64).length + ' bytes', 'ok');
        this._emit('tp:print', { bytes: b64ToBytes(b64).length });
        return true;
      } catch (e) {
        this._status('Print failed: ' + (e.message || e), 'err');
        this._emit('tp:error', { error: String(e) });
        return false;
      }
    }

    async _testPrint() {
      // Ask the server to build a demo receipt (transport webserial → bytes back).
      try {
        const r = await fetch('/print/text', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: 'VERA', text: 'Thermal printer test\n' + new Date().toLocaleString() +
              '\n\nWeb Serial bridge OK.', align: 'center', cut: true })
        });
        const j = await r.json();
        if (j.escpos_b64) await this.printB64(j.escpos_b64);
      } catch (e) {
        this._status('Test failed: ' + (e.message || e), 'err');
      }
    }

    _status(msg, kind) {
      const s = this.shadowRoot.getElementById('st');
      if (s) { s.textContent = msg; s.className = 'st ' + (kind || ''); }
    }

    _render() {
      const connected = this.isConnected();
      this.shadowRoot.innerHTML = `
        <style>
          :host{display:block;font-family:'Inter',system-ui,sans-serif;font-size:12px;color:var(--text,#ddd5c8)}
          .wrap{border:1px solid var(--border,#38332f);border-radius:6px;padding:12px;background:var(--bg1,#1b1917)}
          .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
          h4{font-size:13px;margin:0 0 8px;display:flex;align-items:center;gap:6px}
          .dot{width:8px;height:8px;border-radius:50%;background:${connected ? '#3fd0a4' : '#6a6058'}}
          button{background:var(--bg2,#242120);border:1px solid var(--border2,#4a4540);color:var(--text,#ddd5c8);
            border-radius:4px;padding:6px 12px;font-size:12px;cursor:pointer}
          button:hover{background:var(--bg3,#2e2a28)}
          button.pri{background:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f);color:#0d0f12;font-weight:600}
          .st{margin-top:8px;font-size:11px;color:var(--dim2,#8a7e70);font-family:'JetBrains Mono',monospace}
          .st.ok{color:#6db87a}.st.err{color:#c96b6b}
          .hint{margin-top:6px;font-size:10px;color:var(--dim,#6a6058)}
        </style>
        <div class="wrap">
          <h4><span class="dot"></span> USB Thermal Printer <span style="color:var(--dim,#6a6058);font-weight:400">(this device)</span></h4>
          <div class="row">
            ${connected
              ? `<button id="dc">Disconnect</button><button class="pri" id="tp">Test print</button>`
              : `<button class="pri" id="cn">Connect printer…</button>`}
          </div>
          <div class="st" id="st">${connected ? 'Connected' : 'Not connected'}</div>
          <div class="hint">Plug the printer into this computer's USB, click Connect, pick the serial port. Chrome/Edge over https or localhost.</div>
        </div>`;
      const $ = (id) => this.shadowRoot.getElementById(id);
      if ($('cn')) $('cn').onclick = () => this.connect();
      if ($('dc')) $('dc').onclick = () => this.disconnect();
      if ($('tp')) $('tp').onclick = () => this._testPrint();
    }
  }

  customElements.define('vera-thermal-printer', VeraThermalPrinter);
})();

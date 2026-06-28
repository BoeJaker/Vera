/* ───────────────────────────────────────────────────────────────────────────
   <vera-sandbox-controls>  —  shared exec-sandbox policy editor
   ============================================================================
   A single, self-contained custom element that reads and writes the ONE exec
   sandbox policy (exec.sandbox.get / exec.sandbox.set, persisted to
   ~/.vera_exec_sandbox.json on the backend). Mount it in any panel — the Exec
   panel and the IDE panel both use it — and because they all edit the same
   server-side policy file, the controls are "linked together": a change made
   in one surfaces in the other on its next refresh.

   The element gates BOTH the Exec panel's bash/ps/code runners AND the IDE's
   Run terminal (which now routes through /ide-api/exec/run, sandbox-checked by
   the same backend helper).

   Usage
   ─────
     <script src="/ui/elements/sandbox_controls.js"></script>
     <vera-sandbox-controls></vera-sandbox-controls>

   Same-origin panels need nothing more. Panels served inside the harness
   iframe (where the backend lives on another origin, e.g. :8999) should set
   the API base:

     const el = document.querySelector('vera-sandbox-controls');
     el.setApiBase(window.parent._veraBase || '');
     el.refresh();

   Public API
   ──────────
     el.setApiBase(url)   — point requests at a backend base (default '')
     el.refresh()         — re-fetch the policy from the server
     el.save()            — push the current form to the server
     el.resetDefaults()   — restore shipped defaults (with confirm)
     el.getPolicy()       — last-loaded policy object

   Events
   ──────
     sandbox:loaded  detail:{policy, path}
     sandbox:saved   detail:{policy}
     sandbox:error   detail:{error}
   ─────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  if (window.customElements && customElements.get('vera-sandbox-controls')) return;

  // List-valued policy fields. Sent to the server as JSON arrays (NOT
  // comma-joined) so regex patterns that contain commas — e.g. `a{2,3}` — and
  // paths survive intact.
  const LIST_FIELDS = [
    'languages', 'allow_paths', 'deny_paths',
    'command_blocklist', 'command_allowlist',
  ];

  const STYLE = `
    :host{
      /* Resolve against the Exec panel's var names, the IDE panel's, OR the
         Workers panel's (--text/--acc/--err), falling back to dark defaults. */
      --sb-bg:     var(--bg,  var(--bg0, #0e0f12));
      --sb-s1:     var(--s1,  var(--bg1, var(--bg2, #14171d)));
      --sb-s2:     var(--s2,  var(--bg3, #1b1f27));
      --sb-bd:     var(--bd,  var(--border, rgba(255,255,255,.10)));
      --sb-bd2:    var(--bd2, var(--border2, var(--border, rgba(255,255,255,.18))));
      --sb-t1:     var(--t1,  var(--text0, var(--text, #d4dae4)));
      --sb-t2:     var(--t2,  var(--text2, var(--dim2, #8a93a3)));
      --sb-t3:     var(--t3,  var(--text3, var(--dim, #5a6473)));
      --sb-ac:     var(--ac,  var(--accent, var(--acc, #6ea8d8)));
      --sb-ok:     var(--ac2, var(--success, var(--acc2, #5ec9a0)));
      --sb-warn:   var(--ac3, var(--warning, var(--acc3, #e09a55)));
      --sb-err:    var(--ac4, var(--danger,  var(--err, #e06060)));
      display:block; height:100%; overflow:hidden;
      font:12px/1.5 ui-monospace,'SFMono-Regular','Menlo',monospace;
      color:var(--sb-t1);
    }
    *{box-sizing:border-box}
    .wrap{display:flex;flex-direction:column;height:100%;background:var(--sb-bg);overflow:hidden}
    .hdr{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--sb-s1);
         border-bottom:1px solid var(--sb-bd);flex-shrink:0}
    .hdr .ttl{color:var(--sb-ac);font-weight:600;letter-spacing:.03em}
    .hdr .dot{width:8px;height:8px;border-radius:50%;background:var(--sb-t3);flex-shrink:0}
    .hdr .dot.on{background:var(--sb-ok)}
    .hdr .dot.off{background:var(--sb-err)}
    .hdr .pathwrap{flex:1;min-width:0;color:var(--sb-t3);font-size:10px;text-align:right;
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .hdr button{font:inherit;cursor:pointer;border:1px solid var(--sb-bd);background:var(--sb-s2);
         color:var(--sb-t2);border-radius:4px;padding:4px 10px;font-size:11px}
    .hdr button:hover{color:var(--sb-t1);border-color:var(--sb-bd2)}

    .body{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:14px}
    .body::-webkit-scrollbar{width:7px}
    .body::-webkit-scrollbar-thumb{background:var(--sb-bd2);border-radius:3px}

    .master{display:flex;align-items:center;gap:12px;padding:11px 13px;background:var(--sb-s1);
            border:1px solid var(--sb-bd);border-radius:6px}
    .master .m-txt{flex:1}
    .master .m-txt b{display:block;color:var(--sb-t1);font-size:12px}
    .master .m-txt span{color:var(--sb-t3);font-size:10px}

    /* toggle switch */
    .sw{position:relative;width:38px;height:20px;flex-shrink:0;cursor:pointer}
    .sw input{opacity:0;width:0;height:0;position:absolute}
    .sw .track{position:absolute;inset:0;background:var(--sb-s2);border:1px solid var(--sb-bd2);
               border-radius:20px;transition:.15s}
    .sw .knob{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;
              background:var(--sb-t2);transition:.15s}
    .sw input:checked + .track{background:color-mix(in srgb,var(--sb-ok) 35%,transparent);
              border-color:var(--sb-ok)}
    .sw input:checked + .track + .knob,
    .sw input:checked ~ .knob{transform:translateX(18px);background:var(--sb-ok)}

    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    @media(max-width:560px){.grid{grid-template-columns:1fr}}
    .field{display:flex;flex-direction:column;gap:4px;min-width:0}
    .field.full{grid-column:1/-1}
    .field label{font-size:9px;color:var(--sb-t3);letter-spacing:.07em;text-transform:uppercase}
    .field .hint{font-size:9px;color:var(--sb-t3);font-style:italic}
    .field textarea,.field input{background:var(--sb-s2);border:1px solid var(--sb-bd);
            border-radius:4px;padding:7px 9px;color:var(--sb-t1);font:11px/1.45 ui-monospace,'Menlo',monospace;
            outline:none;resize:vertical;width:100%}
    .field textarea{min-height:62px;white-space:pre;overflow-wrap:normal;overflow-x:auto}
    .field textarea:focus,.field input:focus{border-color:var(--sb-ac)}
    .field .num{width:120px}
    .rowline{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
    .chk{display:flex;align-items:center;gap:6px;color:var(--sb-t2);font-size:11px;cursor:pointer}
    .chk input{accent-color:var(--sb-ac)}

    .sect-ttl{font-size:9px;color:var(--sb-t3);letter-spacing:.08em;text-transform:uppercase;
              border-bottom:1px solid var(--sb-bd);padding-bottom:4px}

    .test{background:var(--sb-s1);border:1px solid var(--sb-bd);border-radius:6px;padding:11px 13px;
          display:flex;flex-direction:column;gap:7px}
    .test .trow{display:flex;gap:7px}
    .test input{flex:1;background:var(--sb-s2);border:1px solid var(--sb-bd);border-radius:4px;
            padding:6px 9px;color:var(--sb-t1);font:11px ui-monospace,'Menlo',monospace;outline:none}
    .test input:focus{border-color:var(--sb-ac)}
    .test button{cursor:pointer;border:1px solid var(--sb-bd);background:var(--sb-s2);color:var(--sb-t1);
            border-radius:4px;padding:6px 12px;font:inherit;font-size:11px}
    .test button:hover{border-color:var(--sb-bd2)}
    .test .verdict{font-size:11px;min-height:16px}
    .test .verdict.ok{color:var(--sb-ok)}
    .test .verdict.no{color:var(--sb-err)}
    .test .verdict .why{color:var(--sb-t3);font-size:10px}

    .foot{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--sb-s1);
          border-top:1px solid var(--sb-bd);flex-shrink:0}
    .foot .msg{flex:1;font-size:10px;color:var(--sb-t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .foot .msg.ok{color:var(--sb-ok)} .foot .msg.err{color:var(--sb-err)}
    .foot button{cursor:pointer;font:inherit;font-size:11px;border-radius:4px;padding:6px 14px;border:1px solid var(--sb-bd)}
    .foot .ghost{background:var(--sb-s2);color:var(--sb-t2)}
    .foot .ghost:hover{color:var(--sb-t1);border-color:var(--sb-bd2)}
    .foot .save{background:var(--sb-ac);color:#06121c;border-color:var(--sb-ac);font-weight:600}
    .foot .save:hover{filter:brightness(1.08)}
    .foot button:disabled{opacity:.5;cursor:not-allowed}
  `;

  const HTML = `
    <div class="wrap">
      <div class="hdr">
        <span class="dot" data-dot></span>
        <span class="ttl">Exec Sandbox</span>
        <span class="pathwrap" data-path></span>
        <button data-refresh title="Reload policy from server">↻ Refresh</button>
      </div>
      <div class="body">
        <div class="master">
          <label class="sw">
            <input type="checkbox" data-f="enabled">
            <span class="track"></span><span class="knob"></span>
          </label>
          <div class="m-txt">
            <b>Sandbox enforcement</b>
            <span>When off, every command runs unrestricted. When on, the rules below gate bash, PowerShell, code runs and the IDE Run terminal.</span>
          </div>
        </div>

        <div class="rowline">
          <div class="field" style="flex:0 0 auto">
            <label>Max timeout (s) · 0 = uncapped</label>
            <input class="num" type="number" min="0" step="1" data-f="max_timeout">
          </div>
          <label class="chk"><input type="checkbox" data-f="network"> allow network (advisory)</label>
        </div>

        <div class="sect-ttl">Filesystem scope</div>
        <div class="grid">
          <div class="field">
            <label>Allow paths · cwd must live under one</label>
            <textarea data-f="allow_paths" spellcheck="false" placeholder="(empty = anywhere)&#10;/home/me/projects"></textarea>
            <span class="hint">one path per line · empty means no restriction</span>
          </div>
          <div class="field">
            <label>Deny paths · never touch these roots</label>
            <textarea data-f="deny_paths" spellcheck="false" placeholder="/etc&#10;/var"></textarea>
            <span class="hint">one path per line</span>
          </div>
        </div>

        <div class="sect-ttl">Artifacts · where agent-generated files land</div>
        <div class="rowline">
          <div class="field" style="flex:1">
            <label>Artifact root · empty = ~/.vera_artifacts</label>
            <input class="num" style="width:100%" type="text" data-f="artifact_root" spellcheck="false" placeholder="~/.vera_artifacts">
          </div>
          <div class="field" style="flex:0 0 auto">
            <label>Scope</label>
            <select class="num" data-f="artifact_scope">
              <option value="artifact">per artifact</option>
              <option value="session">per session</option>
              <option value="project">per project</option>
              <option value="workspace">per workspace</option>
            </select>
          </div>
        </div>
        <span class="hint">the resolved per-run folder is always writable (sandbox-allowed); deny paths still override</span>

        <div class="sect-ttl">Languages &amp; commands</div>
        <div class="grid">
          <div class="field full">
            <label>Language whitelist · empty = all allowed</label>
            <textarea data-f="languages" spellcheck="false" placeholder="(empty = all)&#10;python&#10;node"></textarea>
            <span class="hint">one id per line — python, node, ruby, go, …</span>
          </div>
          <div class="field">
            <label>Command blocklist · regex, any match denies</label>
            <textarea data-f="command_blocklist" spellcheck="false" placeholder="rm\\s+-rf?\\s+/"></textarea>
            <span class="hint">one regex per line · case-insensitive</span>
          </div>
          <div class="field">
            <label>Command allowlist · if set, must match one</label>
            <textarea data-f="command_allowlist" spellcheck="false" placeholder="(empty = no allowlist)"></textarea>
            <span class="hint">one regex per line · case-insensitive</span>
          </div>
        </div>

        <div class="sect-ttl">Test a command against this policy</div>
        <div class="test">
          <div class="trow">
            <input data-test-cmd spellcheck="false" placeholder="e.g. rm -rf /  ·  python build.py">
            <button data-test-run>Test</button>
          </div>
          <div class="verdict" data-test-verdict></div>
          <div class="hint" style="color:var(--sb-t3);font-size:9px">Client-side preview of the blocklist/allowlist + deny-path rules. The server is the source of truth.</div>
        </div>
      </div>
      <div class="foot">
        <span class="msg" data-msg></span>
        <button class="ghost" data-reset>Reset to defaults</button>
        <button class="save" data-save>Save policy</button>
      </div>
    </div>
  `;

  class VeraSandboxControls extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._base = '';
      this._policy = null;
      this._path = '';
      this._busy = false;
    }

    setApiBase(url) { this._base = url || ''; return this; }
    getPolicy() { return this._policy; }

    connectedCallback() {
      if (!this._mounted) {
        if (this.hasAttribute('api-base')) this._base = this.getAttribute('api-base') || '';
        this.shadowRoot.innerHTML = `<style>${STYLE}</style>${HTML}`;
        this._wire();
        this._mounted = true;
      }
      this.refresh();
    }

    _el(sel) { return this.shadowRoot.querySelector(sel); }
    _field(name) { return this.shadowRoot.querySelector(`[data-f="${name}"]`); }
    _api(path) { return (this._base || '') + path; }

    _wire() {
      this._el('[data-refresh]').onclick = () => this.refresh();
      this._el('[data-save]').onclick = () => this.save();
      this._el('[data-reset]').onclick = () => this.resetDefaults();
      this._field('enabled').addEventListener('change', () => this._reflectEnabled());
      this._el('[data-test-run]').onclick = () => this._runTest();
      this._el('[data-test-cmd]').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); this._runTest(); }
      });
    }

    _reflectEnabled() {
      const on = !!this._field('enabled').checked;
      const dot = this._el('[data-dot]');
      dot.classList.toggle('on', on);
      dot.classList.toggle('off', !on);
    }

    _msg(text, kind) {
      const m = this._el('[data-msg]');
      m.textContent = text || '';
      m.className = 'msg' + (kind ? ' ' + kind : '');
    }

    // ── load ────────────────────────────────────────────────────────────────
    async refresh() {
      this._msg('loading…');
      try {
        const r = await fetch(this._api('/exec/sandbox'), { method: 'GET' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const j = await r.json();
        this._policy = j.policy || {};
        this._path = j.path || '';
        this._populate(this._policy);
        this._el('[data-path]').textContent = this._path;
        this._el('[data-path]').title = this._path;
        this._msg('');
        this.dispatchEvent(new CustomEvent('sandbox:loaded',
          { bubbles: true, composed: true, detail: { policy: this._policy, path: this._path } }));
      } catch (e) {
        this._msg('load failed: ' + e.message, 'err');
        this.dispatchEvent(new CustomEvent('sandbox:error',
          { bubbles: true, composed: true, detail: { error: String(e) } }));
      }
    }

    _populate(pol) {
      this._field('enabled').checked = pol.enabled !== false;
      this._field('network').checked = !!pol.network;
      this._field('max_timeout').value = Number(pol.max_timeout || 0);
      const ar = this._field('artifact_root'); if (ar) ar.value = pol.artifact_root || '';
      const as = this._field('artifact_scope'); if (as) as.value = pol.artifact_scope || 'session';
      for (const f of LIST_FIELDS) {
        this._field(f).value = (Array.isArray(pol[f]) ? pol[f] : []).join('\n');
      }
      this._reflectEnabled();
    }

    _collect() {
      const lines = (name) => this._field(name).value
        .split('\n').map((s) => s.trim()).filter(Boolean);
      const body = {
        enabled: !!this._field('enabled').checked,
        network: !!this._field('network').checked,
        max_timeout: Math.max(0, parseInt(this._field('max_timeout').value, 10) || 0),
        artifact_root: (this._field('artifact_root')?.value || '').trim(),
        artifact_scope: this._field('artifact_scope')?.value || 'session',
      };
      for (const f of LIST_FIELDS) body[f] = lines(f);
      return body;
    }

    // ── save ────────────────────────────────────────────────────────────────
    async save() {
      if (this._busy) return;
      this._busy = true;
      this._el('[data-save]').disabled = true;
      this._msg('saving…');
      try {
        const r = await fetch(this._api('/exec/sandbox/set'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this._collect()),
        });
        const j = await r.json();
        if (!r.ok || j.ok === false) {
          const detail = (j.details && j.details.length) ? ' — ' + j.details.join('; ') : '';
          throw new Error((j.error || ('HTTP ' + r.status)) + detail);
        }
        this._policy = j.policy || this._policy;
        if (j.path) { this._path = j.path; this._el('[data-path]').textContent = j.path; }
        this._populate(this._policy);
        this._msg('saved · applies to all runs immediately', 'ok');
        this.dispatchEvent(new CustomEvent('sandbox:saved',
          { bubbles: true, composed: true, detail: { policy: this._policy } }));
      } catch (e) {
        this._msg('save failed: ' + e.message, 'err');
        this.dispatchEvent(new CustomEvent('sandbox:error',
          { bubbles: true, composed: true, detail: { error: String(e) } }));
      } finally {
        this._busy = false;
        this._el('[data-save]').disabled = false;
      }
    }

    async resetDefaults() {
      if (!window.confirm('Restore the shipped default sandbox policy? This overwrites the current rules.')) return;
      this._busy = true; this._el('[data-save]').disabled = true; this._msg('resetting…');
      try {
        const r = await fetch(this._api('/exec/sandbox/set'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reset: true }),
        });
        const j = await r.json();
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        this._policy = j.policy || {};
        this._populate(this._policy);
        this._msg('reset to defaults', 'ok');
        this.dispatchEvent(new CustomEvent('sandbox:saved',
          { bubbles: true, composed: true, detail: { policy: this._policy } }));
      } catch (e) {
        this._msg('reset failed: ' + e.message, 'err');
      } finally {
        this._busy = false; this._el('[data-save]').disabled = false;
      }
    }

    // ── client-side test (mirrors the server's _sandbox_check ordering) ──────
    _runTest() {
      const cmd = this._el('[data-test-cmd]').value;
      const v = this._el('[data-test-verdict]');
      if (!cmd.trim()) { v.className = 'verdict'; v.textContent = ''; return; }
      const pol = this._collect(); // test against the UNSAVED form, not just last load
      const res = this._evaluate(cmd, pol);
      if (res.allowed) {
        v.className = 'verdict ok';
        v.innerHTML = '✓ allowed';
      } else {
        v.className = 'verdict no';
        v.innerHTML = '✗ blocked <span class="why">— ' + this._esc(res.reason) + '</span>';
      }
    }

    _evaluate(text, pol) {
      if (!pol.enabled) return { allowed: true };
      // deny paths (literal substring, matching the server)
      for (const d of pol.deny_paths || []) {
        if (d && text.indexOf(d) !== -1) return { allowed: false, reason: 'references denied path: ' + d };
      }
      // blocklist
      for (const pat of pol.command_blocklist || []) {
        let rx; try { rx = new RegExp(pat, 'i'); } catch (e) { continue; }
        if (rx.test(text)) return { allowed: false, reason: 'matches blocked pattern: ' + pat };
      }
      // allowlist
      const allow = pol.command_allowlist || [];
      if (allow.length) {
        const hit = allow.some((pat) => { try { return new RegExp(pat, 'i').test(text); } catch (e) { return false; } });
        if (!hit) return { allowed: false, reason: 'does not match any allowlist pattern' };
      }
      return { allowed: true };
    }

    _esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
  }

  customElements.define('vera-sandbox-controls', VeraSandboxControls);
})();

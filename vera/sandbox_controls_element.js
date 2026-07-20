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

    .sbx{background:var(--sb-s1);border:1px solid var(--sb-bd);border-radius:6px;
         padding:11px 13px;display:flex;flex-direction:column;gap:7px}
    .sbx .s-head{display:flex;align-items:center;gap:8px}
    .sbx .s-head .cnt{flex:1;color:var(--sb-t3);font-size:10px}
    .sbx .s-head button{cursor:pointer;border:1px solid var(--sb-bd);background:var(--sb-s2);
         color:var(--sb-t2);border-radius:4px;padding:3px 9px;font:inherit;font-size:10px}
    .sbx .s-head button:hover{color:var(--sb-t1);border-color:var(--sb-bd2)}
    .sbx .s-row{display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--sb-s2);
         border:1px solid var(--sb-bd);border-radius:4px;font-size:10px}
    .sbx .s-row .sid{font-weight:600;color:var(--sb-t1)}
    .sbx .s-row .meta{flex:1;color:var(--sb-t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .sbx .s-row .badge{font-size:9px;padding:1px 6px;border-radius:8px;border:1px solid var(--sb-bd2)}
    .sbx .s-row .badge.on{color:var(--sb-ok);border-color:var(--sb-ok)}
    .sbx .s-row .badge.off{color:var(--sb-t3)}
    .sbx .s-row button{cursor:pointer;border:1px solid var(--sb-bd);background:var(--sb-s1);
         color:var(--sb-t2);border-radius:3px;padding:2px 7px;font:inherit;font-size:9px}
    .sbx .s-row button:hover{color:var(--sb-t1);border-color:var(--sb-bd2)}
    .sbx .s-row button.danger:hover{color:var(--sb-err);border-color:var(--sb-err)}
    .sbx .s-empty{color:var(--sb-t3);font-size:10px}

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

        <div class="sect-ttl">Container sandboxes · per-session Docker isolation</div>
        <div class="sbx">
          <div class="s-cfg" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;padding-bottom:7px;border-bottom:1px solid var(--sb-bd)">
            <div class="field" style="flex:0 0 auto;min-width:150px">
              <label>Docker host · where containers run</label>
              <select class="num" style="width:100%" data-sbx-host></select>
            </div>
            <div class="field" style="flex:1;min-width:140px">
              <label>Base image</label>
              <input class="num" style="width:100%" type="text" data-sbx-image spellcheck="false" placeholder="python:3.12-slim">
            </div>
            <div class="field" style="flex:0 0 auto">
              <label>Idle sleep (min) · 0 = off</label>
              <input class="num" style="width:80px" type="number" min="0" step="1" data-sbx-idle>
            </div>
            <label class="chk" title="System-wide default: any session that executes shell/code or writes artifacts gets its own container automatically — chat, agent loops, dream cycles, goals. Off = containers only where explicitly started."><input type="checkbox" data-sbx-autocreate> containers on by default</label>
            <label class="chk" title="Keep agent-generated files in /workspace: HOME, temp and cache dirs are redirected into the workspace volume and the exec cwd defaults there, so 'pipe to /tmp' output lands in the browsable, synced workspace. Reads elsewhere in the container still work."><input type="checkbox" data-sbx-confine> confine writes to /workspace</label>
            <label class="chk" title="When a session container is stopped, snapshot its /workspace to the blob store (Garage) and commit the image so the session can be fully restored later. Off = stop just removes the container (the /workspace volume is still kept)."><input type="checkbox" data-sbx-archive> archive on stop</label>
            <button data-sbx-cfg-save title="Save the sandbox defaults (host, image, auto-create, idle sleep, archive)">Save defaults</button>
          </div>
          <div class="s-head">
            <span class="cnt" data-sbx-count>—</span>
            <button data-sbx-refresh>↻ Refresh</button>
          </div>
          <div data-sbx-list style="display:flex;flex-direction:column;gap:5px"></div>
          <div class="hint" style="font-size:9px">While a sandbox is ACTIVE, its session's shell, code &amp; file IO run inside that container instead of this host (sandbox.session.*). Sleeping containers wake automatically on next use. Provision a dedicated desktopless Docker host with the <code>sandbox.host.provision</code> cap (Proxmox LXC + Docker).</div>
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
      this._el('[data-sbx-refresh]').onclick = () => this._sbxRefresh();
      this._el('[data-sbx-cfg-save]').onclick = () => this._sbxCfgSave();
    }

    async _sbxCfgLoad() {
      // Docker host list + current sandbox defaults, loaded side by side.
      try {
        const [cr, hr] = await Promise.all([
          fetch(this._api('/remote/sandbox/config'), { method: 'GET' }),
          fetch(this._api('/workers/docker/hosts'), { method: 'GET' }),
        ]);
        const cfg = cr.ok ? await cr.json() : {};
        const hosts = hr.ok ? ((await hr.json()).hosts || []) : [];
        const sel = this._el('[data-sbx-host]');
        if (sel) {
          const cur = cfg.docker_host_id || 'local';
          sel.innerHTML = (hosts.length ? hosts : [{ id: 'local', label: 'local' }])
            .map((h) => `<option value="${this._esc(h.id)}"${h.id === cur ? ' selected' : ''}>${this._esc(h.label || h.id)}${h.kind ? ' · ' + this._esc(h.kind) : ''}</option>`)
            .join('');
          if (![...sel.options].some((o) => o.value === cur)) {
            sel.insertAdjacentHTML('afterbegin',
              `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>`);
          }
        }
        const img = this._el('[data-sbx-image]');
        if (img) img.value = cfg.base_image || '';
        const idle = this._el('[data-sbx-idle]');
        if (idle) idle.value = Number(cfg.idle_sleep_minutes ?? 30);
        const ac = this._el('[data-sbx-autocreate]');
        if (ac) ac.checked = cfg.auto_create !== false;
        const cf = this._el('[data-sbx-confine]');
        if (cf) cf.checked = cfg.confine_writes !== false;
        const cb = this._el('[data-sbx-archive]');
        if (cb) cb.checked = cfg.archive_on_stop !== false;
      } catch (e) { /* module may not be loaded */ }
    }

    async _sbxCfgSave() {
      try {
        const body = {
          docker_host_id: this._el('[data-sbx-host]')?.value || 'local',
          base_image: (this._el('[data-sbx-image]')?.value || '').trim(),
          idle_sleep_minutes: Math.max(0, parseInt(this._el('[data-sbx-idle]')?.value, 10) || 0),
          auto_create: !!this._el('[data-sbx-autocreate]')?.checked,
          confine_writes: !!this._el('[data-sbx-confine]')?.checked,
          archive_on_stop: !!this._el('[data-sbx-archive]')?.checked,
        };
        const r = await fetch(this._api('/remote/sandbox/config/set'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        this._msg('sandbox defaults saved', 'ok');
      } catch (e) { this._msg('sandbox defaults save failed: ' + e.message, 'err'); }
    }

    // ── container sandboxes (per-session Docker isolation) ──────────────────
    async _sbxRefresh() {
      const list = this._el('[data-sbx-list]');
      const cnt = this._el('[data-sbx-count]');
      if (!list) return;
      this._sbxCfgLoad();   // archive-on-stop checkbox loads in parallel
      let rows = [];
      try {
        const r = await fetch(this._api('/remote/sandbox/list'), { method: 'GET' });
        if (r.ok) rows = (await r.json()).sandboxes || [];
      } catch (e) { /* module may not be loaded — leave list empty */ }
      cnt.textContent = rows.length
        ? rows.length + ' session sandbox' + (rows.length === 1 ? '' : 'es') +
          ' · ' + rows.filter((s) => s.active).length + ' active'
        : 'no session sandboxes yet';
      if (!rows.length) {
        list.innerHTML = '<div class="s-empty">Nothing here — a sandbox appears when a session starts one (or via Docker → Session Sandboxes → ➕).</div>';
        return;
      }
      list.innerHTML = rows.map((s) => {
        const running = (s.state || '') === 'running';
        const stateBadge = s.state
          ? `<span class="badge ${running ? 'on' : 'off'}">${this._esc(s.state)}</span>` : '';
        const kind = s.kind && s.kind !== 'session'
          ? `<span class="badge" style="color:var(--sb-ac);border-color:var(--sb-ac)">${this._esc(s.kind)}</span>` : '';
        const sleepWake = running
          ? `<button data-sbx-sleep="${this._esc(s.session_id)}" title="docker-stop the container (kept + auto-wakes on next use)">Sleep</button>`
          : `<button data-sbx-wake="${this._esc(s.session_id)}" title="Start the container back up (installed packages survive)">Wake</button>`;
        return `
        <div class="s-row">
          <span class="sid" title="${this._esc(s.session_id)}">${this._esc(s.label || s.session_id)}</span>
          ${kind}
          <span class="meta">${this._esc(s.container || '')} · ${this._esc(s.image || '')} · ${this._esc(s.docker_host_id || 'local')}</span>
          ${stateBadge}
          <span class="badge ${s.active ? 'on' : 'off'}">${s.active ? 'active' : 'inactive'}</span>
          <button data-sbx-toggle="${this._esc(s.session_id)}" title="${s.active ? 'Stop routing this session into the container' : 'Route this session’s exec/code into the container'}">${s.active ? 'Deactivate' : 'Activate'}</button>
          ${sleepWake}
          <button class="danger" data-sbx-stop="${this._esc(s.session_id)}" title="Stop the container (archived first when archiving is on)">Stop</button>
        </div>`;
      }).join('');
      list.querySelectorAll('[data-sbx-toggle]').forEach((b) => {
        b.onclick = () => this._sbxToggle(b.getAttribute('data-sbx-toggle'));
      });
      list.querySelectorAll('[data-sbx-stop]').forEach((b) => {
        b.onclick = () => this._sbxStop(b.getAttribute('data-sbx-stop'));
      });
      list.querySelectorAll('[data-sbx-sleep]').forEach((b) => {
        b.onclick = () => this._sbxSleep(b.getAttribute('data-sbx-sleep'));
      });
      list.querySelectorAll('[data-sbx-wake]').forEach((b) => {
        b.onclick = () => this._sbxWake(b.getAttribute('data-sbx-wake'));
      });
    }

    async _sbxSleep(sid) {
      try {
        const r = await fetch(this._api('/remote/sandbox/sleep'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid }),
        });
        const j = await r.json();
        if (j.ok === false) this._msg('sleep failed: ' + (j.error || ''), 'err');
        else this._msg('container sleeping — wakes on next use', 'ok');
      } catch (e) { this._msg('sleep failed: ' + e.message, 'err'); }
      this._sbxRefresh();
    }

    async _sbxWake(sid) {
      try {
        const r = await fetch(this._api('/remote/sandbox/start'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid, enable: true }),
        });
        const j = await r.json();
        if (j.ok === false) this._msg('wake failed: ' + (j.error || ''), 'err');
      } catch (e) { this._msg('wake failed: ' + e.message, 'err'); }
      this._sbxRefresh();
    }

    async _sbxToggle(sid) {
      const rowActive = !!(this.shadowRoot.querySelector(`[data-sbx-toggle="${CSS.escape(sid)}"]`)?.textContent === 'Deactivate');
      try {
        // Activate ensures the container exists + is running (start); deactivate
        // only flips the routing flag (set_active) — no docker run/rm side effects.
        const r = rowActive
          ? await fetch(this._api('/remote/sandbox/set_active'), {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: sid, active: false }),
            })
          : await fetch(this._api('/remote/sandbox/start'), {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: sid, enable: true }),
            });
        const j = await r.json();
        if (j.ok === false) this._msg('sandbox toggle failed: ' + (j.error || ''), 'err');
      } catch (e) { this._msg('sandbox toggle failed: ' + e.message, 'err'); }
      this._sbxRefresh();
    }

    async _sbxStop(sid) {
      const arch = this._el('[data-sbx-archive]')?.checked;
      if (!window.confirm('Stop sandbox "' + sid + '"?' + (arch
        ? ' It is archived first (workspace → blob store, image commit) so it can be restored later.'
        : ' Archiving is OFF — the container is removed (its /workspace volume is kept).'))) return;
      try {
        // sync/commit omitted on purpose: the server applies the global
        // archive_on_stop config as the default.
        const r = await fetch(this._api('/remote/sandbox/stop'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid, remove: true }),
        });
        const j = await r.json();
        if (j.ok === false) this._msg('sandbox stop failed: ' + (j.error || ''), 'err');
      } catch (e) { this._msg('sandbox stop failed: ' + e.message, 'err'); }
      this._sbxRefresh();
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
      this._sbxRefresh();   // container-sandbox list loads in parallel
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

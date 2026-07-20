"use strict";
/**
 * Vera VS Code extension
 * ----------------------
 * Brings Vera's IDE features into VS Code / code-server:
 *   • a Vera sidebar (connection status, remote instances, work queue, enqueue
 *     a Claude Code task on this workspace, and an "Ask Vera" capability runner);
 *   • "Vera: Connect this workspace" — downloads Vera's MCP bridge, writes it +
 *     a project .mcp.json, and runs `claude mcp add vera …` so THIS workspace's
 *     Claude Code can call back into Vera (memory, fabric, IDE tools).
 *
 * Node built-ins only (http/https, fs, path, child_process) — no runtime deps.
 */
const vscode = require("vscode");
const http = require("http");
const https = require("https");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFile, spawn } = require("child_process");

function cfg() {
  const c = vscode.workspace.getConfiguration("vera");
  return {
    base: (c.get("baseUrl") || "http://127.0.0.1:8000").replace(/\/+$/, ""),
    allow: c.get("allow") || "ide.,fabric.,memory.,dream.,project.,web.",
    askCap: (c.get("askCapability") || "").trim(),
    clientMode: !!c.get("clientMode"),
    clientLabel: (c.get("clientLabel") || "").trim(),
    clientToken: (c.get("clientToken") || "").trim(),
  };
}

function workspaceDir() {
  const f = vscode.workspace.workspaceFolders;
  return f && f.length ? f[0].uri.fsPath : "";
}

/* ---- tiny HTTP client (JSON + text) ---- */
function request(url, method, body, asText) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(url); } catch (e) { return reject(e); }
    const lib = u.protocol === "https:" ? https : http;
    const data = body ? Buffer.from(JSON.stringify(body)) : null;
    const req = lib.request(u, {
      method,
      headers: Object.assign({ "Content-Type": "application/json" },
        data ? { "Content-Length": data.length } : {}),
      timeout: 120000,
    }, (res) => {
      let chunks = "";
      res.on("data", (d) => chunks += d);
      res.on("end", () => {
        if (asText) return resolve(chunks);
        try { resolve(JSON.parse(chunks)); }
        catch (e) { resolve({ error: "bad response", raw: chunks.slice(0, 300) }); }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("timeout")); });
    if (data) req.write(data);
    req.end();
  });
}
const apiGet  = (p, asText) => request(cfg().base + p, "GET", null, asText);
const apiPost = (p, body)   => request(cfg().base + p, "POST", body, false);
/** Invoke any Vera capability by name via /mcp/call. */
const call    = (name, args) => apiPost("/mcp/call", { name, arguments: args || {} });

/* ---- Connect this workspace: deploy the MCP bridge + register with Claude Code ---- */
async function connectWorkspace() {
  const dir = workspaceDir();
  if (!dir) { vscode.window.showErrorMessage("Vera: open a folder/workspace first."); return; }
  const { base, allow } = cfg();
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Vera: connecting workspace…" },
    async (progress) => {
      try {
        progress.report({ message: "downloading MCP bridge" });
        const src = await apiGet("/ide/remote/bridge/source", true);
        if (!src || src.startsWith("# bridge source unavailable")) {
          throw new Error("could not fetch bridge from " + base);
        }
        const veraDir = path.join(dir, ".vera");
        fs.mkdirSync(veraDir, { recursive: true });
        const bridgePath = path.join(veraDir, "vera_mcp_bridge.py");
        fs.writeFileSync(bridgePath, src, "utf8");

        progress.report({ message: "writing .mcp.json" });
        const mcp = {
          mcpServers: {
            vera: {
              command: "python3",
              args: [bridgePath, "--url", base, "--allow", allow],
            },
          },
        };
        const mcpPath = path.join(dir, ".mcp.json");
        let existing = {};
        try { existing = JSON.parse(fs.readFileSync(mcpPath, "utf8")); } catch (e) {}
        existing.mcpServers = Object.assign(existing.mcpServers || {}, mcp.mcpServers);
        fs.writeFileSync(mcpPath, JSON.stringify(existing, null, 2), "utf8");

        progress.report({ message: "registering with Claude Code" });
        await new Promise((resolve) => {
          execFile("claude", ["mcp", "add", "vera", "--", "python3", bridgePath,
            "--url", base, "--allow", allow], { cwd: dir }, (err) => {
            // Non-fatal: the .mcp.json alone makes it work for project-scoped runs.
            resolve(err);
          });
        });
        vscode.window.showInformationMessage(
          "Vera: workspace connected. Claude Code in this folder can now call Vera (" + base + ").");
      } catch (e) {
        vscode.window.showErrorMessage("Vera: connect failed — " + e.message);
      }
    });
}

/* ---- Enqueue a Claude Code task for this workspace ---- */
async function enqueueTask(preset) {
  const task = preset || await vscode.window.showInputBox({
    prompt: "Task for Claude Code to run on a remote Vera instance",
    placeHolder: "e.g. Add tests for the auth module",
  });
  if (!task) return;
  const insts = await apiGet("/ide/remote/instances");
  const list = (insts.instances || []);
  let instance_id = "any";
  if (list.length) {
    const pick = await vscode.window.showQuickPick(
      [{ label: "any free instance", id: "any" }].concat(
        list.map((i) => ({ label: i.label + " (" + i.kind + ")", id: i.id }))),
      { placeHolder: "Which instance?" });
    if (!pick) return;
    instance_id = pick.id;
  }
  const r = await apiPost("/ide/remote/queue/add", { task, instance_id, engine: "claude", source: "user" });
  vscode.window.showInformationMessage(r.ok ? "Vera: task enqueued." : "Vera: " + (r.error || "error"));
}

/* ════════════════════════════════════════════════════════════════════════
 * CLIENT MODE — register this window as a Vera instance (kind=vscode-client)
 * and long-poll for pushed actions: open files, run VS Code commands, drive
 * the terminal, type text, run Claude Code tasks (with THIS machine's
 * sign-in), show notifications. Results are posted back so Vera's work
 * queue can await them like any other remote run.
 * ════════════════════════════════════════════════════════════════════════ */
const client = { running: false, id: "", status: null, lastErr: "" };

function clientId(ctx) {
  let id = ctx.globalState.get("vera.clientId");
  if (!id) {
    id = "vsc-" + os.hostname().toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 24)
       + "-" + Math.random().toString(16).slice(2, 8);
    ctx.globalState.update("vera.clientId", id);
  }
  return id;
}

/** JSON-safe copy (VS Code command results can hold live objects). */
function jsonSafe(v) {
  try { return v === undefined ? null : JSON.parse(JSON.stringify(v)); }
  catch (e) { return String(v); }
}

function runClaudeTask(args) {
  return new Promise((resolve) => {
    const dir = workspaceDir() || undefined;
    const pm = String(args.permission_mode || "acceptEdits");
    const argv = ["-p", "--output-format", "json", "--permission-mode", pm];
    let child;
    try {
      // prompt goes via stdin, so no shell-quoting worries on any platform
      child = spawn("claude", argv, {
        cwd: dir, env: process.env, windowsHide: true,
        shell: process.platform === "win32",
      });
    } catch (e) { return resolve({ ok: false, error: "spawn claude failed: " + e.message }); }
    let out = "", err = "";
    child.stdout.on("data", (d) => out += d);
    child.stderr.on("data", (d) => err += d);
    const timer = setTimeout(() => { try { child.kill(); } catch (e) {} },
                             (Number(args.timeout_s) || 1700) * 1000);
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: "claude CLI not runnable: " + e.message });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      let summary = "", result = null;
      for (const line of out.trim().split("\n").reverse()) {
        const s = line.trim();
        if (s.startsWith("{")) {
          try { result = JSON.parse(s); summary = result.result || result.text || ""; break; }
          catch (e) {}
        }
      }
      if (!summary) summary = (out || err).slice(-1200);
      resolve({ ok: code === 0, summary, result: jsonSafe(result),
                error: code === 0 ? "" : ("exit " + code + ": " + err.slice(-400)) });
    });
    child.stdin.write(String(args.task || ""));
    child.stdin.end();
  });
}

async function execClientAction(a) {
  const args = a.args || {};
  switch (a.action) {
    case "notify":
      vscode.window.showInformationMessage("Vera: " + String(args.message || ""));
      return { ok: true };
    case "open_file": {
      const p = path.isAbsolute(String(args.path || ""))
        ? String(args.path) : path.join(workspaceDir() || "", String(args.path || ""));
      const doc = await vscode.workspace.openTextDocument(p);
      await vscode.window.showTextDocument(doc, { preview: false });
      return { ok: true, summary: "opened " + p };
    }
    case "run_command": {
      const extra = Array.isArray(args.args) ? args.args
        : (args.args !== undefined && args.args !== null ? [args.args] : []);
      const res = await vscode.commands.executeCommand(String(args.command || ""), ...extra);
      return { ok: true, result: jsonSafe(res) };
    }
    case "terminal": {
      const t = vscode.window.activeTerminal || vscode.window.createTerminal("Vera");
      t.show(true);
      t.sendText(String(args.text || ""), args.enter !== false);
      return { ok: true };
    }
    case "type_text": {
      const ed = vscode.window.activeTextEditor;
      if (!ed) return { ok: false, error: "no active editor" };
      const okEdit = await ed.edit((b) =>
        ed.selections.forEach((sel) => b.insert(sel.active, String(args.text || ""))));
      return { ok: okEdit };
    }
    case "claude_task": {
      if (client.status) client.status.text = "$(sync~spin) Vera: Claude task…";
      const r = await runClaudeTask(args);
      if (client.status) client.status.text = "$(radio-tower) Vera client";
      return r;
    }
    default:
      return { ok: false, error: "unknown action: " + a.action };
  }
}

async function registerClient(ctx) {
  const { clientLabel, clientToken } = cfg();
  const id = clientId(ctx);
  const label = clientLabel ||
    (os.hostname() + " · " + (path.basename(workspaceDir() || "") || "no folder"));
  const body = { id, label, kind: "vscode-client", workdir: workspaceDir() || "" };
  if (clientToken) body.token = clientToken;
  const r = await apiPost("/ide/remote/register", body);
  if (!r || r.ok === false) throw new Error((r && r.error) || "register failed");
  return id;
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

async function clientLoop(ctx) {
  if (client.running) return;
  client.running = true;
  client.status = client.status || vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right, 90);
  client.status.command = "vera.toggleClientMode";
  client.status.text = "$(radio-tower) Vera client";
  client.status.tooltip = "Vera can control this window (click to disable)";
  client.status.show();

  while (client.running && cfg().clientMode) {
    try {
      if (!client.id) client.id = await registerClient(ctx);
      const r = await apiPost("/ide-api/remote/client/poll",
        { instance_id: client.id, token: cfg().clientToken, wait: 25 });
      if (r && r.error) {
        client.lastErr = r.error;
        if (/not found/i.test(r.error)) client.id = "";   // re-register next round
        await sleep(15000);
        continue;
      }
      const actions = (r && r.actions) || [];
      for (const a of actions) {
        // run detached so a long Claude task doesn't starve the poll loop
        (async () => {
          let res;
          try { res = await execClientAction(a); }
          catch (e) { res = { ok: false, error: e.message }; }
          await apiPost("/ide-api/remote/client/result", {
            instance_id: client.id, token: cfg().clientToken,
            action_id: a.id, ...res,
          }).catch(() => {});
        })();
      }
      client.lastErr = "";
    } catch (e) {
      client.lastErr = e.message;
      client.status.text = "$(warning) Vera client";
      await sleep(10000);
      client.status.text = "$(radio-tower) Vera client";
    }
  }
  client.running = false;
  if (client.status) client.status.hide();
}

function stopClient() {
  client.running = false;
  if (client.status) client.status.hide();
}

async function toggleClientMode() {
  const c = vscode.workspace.getConfiguration("vera");
  const next = !c.get("clientMode");
  await c.update("clientMode", next, vscode.ConfigurationTarget.Global);
  vscode.window.showInformationMessage(
    "Vera client mode " + (next ? "enabled — Vera can now control this window."
                                : "disabled."));
}

/* ---- Sidebar webview ---- */
class VeraSidebar {
  constructor(ctx) { this.ctx = ctx; this.view = null; }
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(async (msg) => {
      try {
        if (msg.t === "refresh") this.push();
        else if (msg.t === "connect") await connectWorkspace();
        else if (msg.t === "enqueue") await enqueueTask(msg.task);
        else if (msg.t === "ask") {
          const cap = cfg().askCap;
          if (!cap) { this.post({ t: "ask-result", text: "Set vera.askCapability in settings first." }); return; }
          const r = await call(cap, { query: msg.q, q: msg.q, text: msg.q });
          this.post({ t: "ask-result", text: JSON.stringify(r && r.content !== undefined ? r.content : r, null, 2) });
        }
      } catch (e) { this.post({ t: "error", text: e.message }); }
    });
    this.push();
  }
  post(m) { if (this.view) this.view.webview.postMessage(m); }
  async push() {
    const { base } = cfg();
    let instances = [], queue = { items: [], autopilot: false };
    try { instances = (await apiGet("/ide/remote/instances")).instances || []; } catch (e) {}
    try { queue = await apiGet("/ide/remote/queue/list"); } catch (e) {}
    this.post({ t: "state", base, workspace: workspaceDir(), instances, queue,
                client: { on: client.running, id: client.id, err: client.lastErr } });
  }
  html() {
    return `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
 body{font-family:var(--vscode-font-family);font-size:12px;color:var(--vscode-foreground);padding:8px}
 h4{margin:12px 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;opacity:.7}
 button{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:none;
   padding:6px 10px;border-radius:4px;cursor:pointer;width:100%;margin:3px 0;font-size:12px}
 button.sec{background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground)}
 input,textarea{width:100%;box-sizing:border-box;background:var(--vscode-input-background);
   color:var(--vscode-input-foreground);border:1px solid var(--vscode-input-border);border-radius:4px;padding:5px;margin:3px 0}
 .row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--vscode-panel-border)}
 .dim{opacity:.6} .ok{color:var(--vscode-testing-iconPassed)} .bad{color:var(--vscode-testing-iconFailed)}
 pre{white-space:pre-wrap;word-break:break-word;background:var(--vscode-textCodeBlock-background);padding:6px;border-radius:4px;max-height:180px;overflow:auto}
</style></head><body>
 <div id="conn" class="dim">connecting…</div>
 <button onclick="send({t:'connect'})">🔗 Connect this workspace to Vera</button>
 <h4>Run</h4>
 <textarea id="task" rows="2" placeholder="Claude Code task for this project…"></textarea>
 <button onclick="enqueue()">＋ Enqueue Claude Code task</button>
 <h4>Ask Vera</h4>
 <input id="ask" placeholder="query Vera memory / capabilities">
 <button class="sec" onclick="ask()">Ask</button>
 <pre id="askOut" style="display:none"></pre>
 <h4>Instances</h4><div id="insts" class="dim">—</div>
 <h4>Work queue <span id="ap" class="dim"></span></h4><div id="queue" class="dim">—</div>
<script>
 const vscode=acquireVsCodeApi();
 const send=m=>vscode.postMessage(m);
 function enqueue(){const t=document.getElementById('task').value.trim();if(t){send({t:'enqueue',task:t});document.getElementById('task').value='';}}
 function ask(){const q=document.getElementById('ask').value.trim();if(q)send({t:'ask',q});}
 const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
 window.addEventListener('message',e=>{const m=e.data;
   if(m.t==='state'){
     const cl=m.client||{};
     document.getElementById('conn').innerHTML='Vera: <b>'+esc(m.base)+'</b><br><span class="dim">workspace: '+esc(m.workspace||'(none)')+'</span><br><span class="'+(cl.on?'ok':'dim')+'">client mode: '+(cl.on?'ON — Vera can control this window ('+esc(cl.id)+')':'off')+'</span>'+(cl.err?'<br><span class="bad">'+esc(cl.err)+'</span>':'');
     document.getElementById('insts').innerHTML=(m.instances||[]).map(i=>'<div class="row"><span>'+esc(i.label)+' <span class="dim">'+i.kind+'</span></span><span class="'+(i.status==='running'?'ok':'dim')+'">'+esc(i.status||'')+'</span></div>').join('')||'<span class="dim">none</span>';
     const q=m.queue||{}; document.getElementById('ap').textContent=q.autopilot?'· autopilot ON':'';
     document.getElementById('queue').innerHTML=((q.items||[]).slice(-8).reverse()).map(i=>'<div class="row"><span>'+esc((i.task||'').slice(0,32))+'</span><span class="dim">'+esc(i.status)+'</span></div>').join('')||'<span class="dim">empty</span>';
   } else if(m.t==='ask-result'){const o=document.getElementById('askOut');o.style.display='block';o.textContent=m.text;}
   else if(m.t==='error'){const o=document.getElementById('askOut');o.style.display='block';o.textContent='Error: '+m.text;}
 });
 send({t:'refresh'});
</script></body></html>`;
  }
}

function activate(ctx) {
  const sidebar = new VeraSidebar(ctx);
  ctx.subscriptions.push(
    vscode.window.registerWebviewViewProvider("vera.sidebar", sidebar),
    vscode.commands.registerCommand("vera.connectWorkspace", connectWorkspace),
    vscode.commands.registerCommand("vera.enqueueTask", () => enqueueTask()),
    vscode.commands.registerCommand("vera.toggleClientMode", toggleClientMode),
    vscode.commands.registerCommand("vera.refresh", () => sidebar.push()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("vera.clientMode")) {
        if (cfg().clientMode) clientLoop(ctx);
        else stopClient();
        sidebar.push();
      }
    }),
  );
  if (cfg().clientMode) clientLoop(ctx);
}
function deactivate() { stopClient(); }
module.exports = { activate, deactivate };

const vscode = require('vscode');

const SNIPPET_KEY = 'inputAutomator.snippets';

let extContext = null;
let lastEditor = null;
let runSeq = 0; // bumping this cancels any in-flight job
const webviews = new Set();

function activate(context) {
  extContext = context;
  lastEditor = vscode.window.activeTextEditor || null;

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((ed) => {
      if (ed) lastEditor = ed;
    }),

    vscode.commands.registerCommand('inputAutomator.open', () => {
      const panel = vscode.window.createWebviewPanel(
        'inputAutomator.panel',
        'Input Automator',
        vscode.ViewColumn.Beside,
        { enableScripts: true, retainContextWhenHidden: true }
      );
      wire(panel.webview);
      panel.onDidDispose(() => webviews.delete(panel.webview));
    }),

    vscode.window.registerWebviewViewProvider(
      'inputAutomator.view',
      {
        resolveWebviewView(view) {
          view.webview.options = { enableScripts: true };
          wire(view.webview);
          view.onDidDispose(() => webviews.delete(view.webview));
        }
      },
      { webviewOptions: { retainContextWhenHidden: true } }
    )
  );
}

function wire(webview) {
  webviews.add(webview);
  webview.html = getHtml();
  webview.onDidReceiveMessage(async (msg) => {
    try {
      switch (msg.type) {
        case 'ready':
          sendSnippets(webview);
          break;
        case 'run':
          await runJob(msg.spec || {}, webview);
          break;
        case 'stop':
          runSeq++;
          status(webview, 'Stopped.', false);
          break;
        case 'saveSnippet':
          await saveSnippet(msg.name, msg.spec);
          break;
        case 'deleteSnippet':
          await deleteSnippet(msg.name);
          break;
      }
    } catch (err) {
      status(webview, 'Error: ' + (err && err.message ? err.message : String(err)), false);
    }
  });
}

// ---------------------------------------------------------------- job runner

async function runJob(spec, webview) {
  const my = ++runSeq;
  const cancelled = () => runSeq !== my;

  const startDelay = clampInt(spec.startDelay, 0, 600000);
  const charDelay = clampInt(spec.charDelay, 0, 60000);
  const repeat = clampInt(spec.repeat, 1, 100000);
  const interval = clampInt(spec.interval, 0, 600000);

  if (startDelay > 0) {
    status(webview, 'Starting in ' + (startDelay / 1000).toFixed(1) + 's - focus your target...', true);
    await sleep(startDelay);
    if (cancelled()) return;
  }

  for (let i = 0; i < repeat; i++) {
    if (cancelled()) return;
    status(webview, repeat > 1 ? 'Sending ' + (i + 1) + '/' + repeat + '...' : 'Sending...', true);

    switch (spec.target) {
      case 'editor-type':
        await typeIntoEditor(String(spec.text || ''), charDelay, cancelled);
        break;
      case 'editor-insert':
        await insertIntoEditor(String(spec.text || ''));
        break;
      case 'terminal':
        sendToTerminal(String(spec.text || ''), !!spec.enter);
        break;
      case 'command':
        await runCommand(spec.commandId, spec.commandArgs);
        break;
      default:
        throw new Error('Unknown target: ' + spec.target);
    }

    if (cancelled()) return;
    if (i < repeat - 1 && interval > 0) await sleep(interval);
  }

  status(webview, 'Done.', false);
}

async function typeIntoEditor(text, charDelay, cancelled) {
  await focusLastEditor();
  if (charDelay > 0) {
    // Character by character through the real input pipeline, so
    // autocomplete, auto-close brackets etc. fire just like manual typing.
    for (const ch of Array.from(text)) {
      if (cancelled()) return;
      await vscode.commands.executeCommand('type', { text: ch });
      await sleep(charDelay);
    }
  } else {
    await vscode.commands.executeCommand('type', { text });
  }
}

async function insertIntoEditor(text) {
  await focusLastEditor();
  const ed = vscode.window.activeTextEditor;
  if (!ed) throw new Error('No active editor.');
  await ed.edit((b) => {
    for (const sel of ed.selections) b.replace(sel, text);
  });
}

async function focusLastEditor() {
  if (lastEditor && !lastEditor.document.isClosed) {
    await vscode.window.showTextDocument(lastEditor.document, {
      viewColumn: lastEditor.viewColumn,
      preserveFocus: false
    });
    return;
  }
  const ed = vscode.window.visibleTextEditors[0];
  if (!ed) throw new Error('No editor to type into - open and focus a file first.');
  lastEditor = ed;
  await vscode.window.showTextDocument(ed.document, {
    viewColumn: ed.viewColumn,
    preserveFocus: false
  });
}

function sendToTerminal(text, enter) {
  const term = vscode.window.activeTerminal || vscode.window.createTerminal('Input Automator');
  term.show(true);
  term.sendText(text, enter);
}

async function runCommand(commandId, argsRaw) {
  const id = String(commandId || '').trim();
  if (!id) throw new Error('Command id is empty.');
  let args = [];
  const raw = String(argsRaw || '').trim();
  if (raw) {
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      throw new Error('Args must be valid JSON: ' + e.message);
    }
    args = Array.isArray(parsed) ? parsed : [parsed];
  }
  await vscode.commands.executeCommand(id, ...args);
}

// ------------------------------------------------------------------ snippets

function getSnippets() {
  return extContext.globalState.get(SNIPPET_KEY, []);
}

async function saveSnippet(name, spec) {
  const trimmed = String(name || '').trim();
  if (!trimmed) throw new Error('Give the snippet a name first.');
  const list = getSnippets().filter((s) => s.name !== trimmed);
  list.push({ name: trimmed, spec });
  list.sort((a, b) => a.name.localeCompare(b.name));
  await extContext.globalState.update(SNIPPET_KEY, list);
  broadcastSnippets();
}

async function deleteSnippet(name) {
  await extContext.globalState.update(
    SNIPPET_KEY,
    getSnippets().filter((s) => s.name !== name)
  );
  broadcastSnippets();
}

function broadcastSnippets() {
  for (const w of webviews) sendSnippets(w);
}

function sendSnippets(webview) {
  post(webview, { type: 'snippets', snippets: getSnippets() });
}

// --------------------------------------------------------------------- utils

function status(webview, text, running) {
  post(webview, { type: 'status', text, running });
}

function post(webview, msg) {
  try {
    webview.postMessage(msg);
  } catch (_) {
    // webview was disposed mid-job; nothing to update
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function clampInt(v, min, max) {
  const n = parseInt(v, 10);
  if (isNaN(n)) return min;
  return Math.min(max, Math.max(min, n));
}

// ----------------------------------------------------------------------- ui

function getHtml() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    background: transparent;
    padding: 10px 12px;
    max-width: 720px;
  }
  label { display: block; margin: 8px 0 3px; opacity: 0.85; font-size: 0.92em; }
  select, input, textarea, button {
    font-family: inherit; font-size: inherit;
    color: var(--vscode-input-foreground);
    background: var(--vscode-input-background);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 3px; padding: 4px 6px; box-sizing: border-box;
  }
  select, input { width: 100%; }
  textarea { width: 100%; min-height: 90px; resize: vertical; }
  select:focus, input:focus, textarea:focus {
    outline: 1px solid var(--vscode-focusBorder); outline-offset: -1px;
  }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .row { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
  button {
    cursor: pointer;
    color: var(--vscode-button-foreground);
    background: var(--vscode-button-background);
    border: none; padding: 5px 14px;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.secondary {
    color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
    background: var(--vscode-button-secondaryBackground, rgba(128,128,128,0.25));
  }
  button:disabled { opacity: 0.45; cursor: default; }
  .check { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
  .check input { width: auto; }
  #status { margin-top: 10px; min-height: 1.2em; opacity: 0.85; }
  #status.running { color: var(--vscode-charts-yellow, #d7ba7d); }
  .hidden { display: none !important; }
  hr { border: none; border-top: 1px solid var(--vscode-widget-border, rgba(128,128,128,0.35)); margin: 14px 0 6px; }
  h3 { margin: 8px 0 4px; font-size: 1em; }
  .snip { display: flex; align-items: center; gap: 6px; padding: 3px 0; }
  .snip .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .snip button { padding: 2px 8px; font-size: 0.9em; }
  .muted { opacity: 0.6; font-size: 0.9em; }
</style>
</head>
<body>
  <label for="target">Target</label>
  <select id="target">
    <option value="editor-type">Editor &mdash; simulate typing</option>
    <option value="editor-insert">Editor &mdash; insert instantly</option>
    <option value="terminal">Terminal</option>
    <option value="command">VS Code command</option>
  </select>

  <div id="textWrap">
    <label for="text">Text to send</label>
    <textarea id="text" placeholder="Text to send... (newlines are typed too)"></textarea>
  </div>

  <div id="commandWrap" class="hidden">
    <label for="commandId">Command id</label>
    <input id="commandId" placeholder="e.g. workbench.action.files.save">
    <label for="commandArgs">Args (JSON, optional; array = multiple args)</label>
    <input id="commandArgs" placeholder='e.g. ["arg1", {"key": 1}]'>
  </div>

  <div class="grid">
    <div><label for="startDelay">Start delay ms</label><input id="startDelay" type="number" min="0" value="0"></div>
    <div><label for="charDelay">Per-char ms</label><input id="charDelay" type="number" min="0" value="0"></div>
    <div><label for="repeat">Repeat</label><input id="repeat" type="number" min="1" value="1"></div>
    <div><label for="interval">Interval ms</label><input id="interval" type="number" min="0" value="500"></div>
  </div>

  <div class="check" id="enterWrap">
    <input id="enter" type="checkbox" checked>
    <label for="enter" style="margin:0">Press Enter after sending (terminal)</label>
  </div>

  <div class="row">
    <button id="run">&#9654; Run</button>
    <button id="stop" class="secondary" disabled>&#9632; Stop</button>
    <span id="status"></span>
  </div>

  <hr>
  <h3>Snippets</h3>
  <div class="row" style="margin-top:4px">
    <input id="snipName" placeholder="Snippet name" style="flex:1">
    <button id="snipSave" class="secondary">Save current</button>
  </div>
  <div id="snipList"><span class="muted">No snippets saved yet.</span></div>

<script>
  const vscode = acquireVsCodeApi();
  const $ = (id) => document.getElementById(id);
  const fields = ['target', 'text', 'commandId', 'commandArgs', 'startDelay', 'charDelay', 'repeat', 'interval'];

  function spec() {
    const s = {};
    for (const f of fields) s[f] = $(f).value;
    s.enter = $('enter').checked;
    return s;
  }

  function applySpec(s) {
    for (const f of fields) {
      if (s[f] !== undefined) $(f).value = s[f];
    }
    if (s.enter !== undefined) $('enter').checked = !!s.enter;
    syncVisibility();
    persist();
  }

  function syncVisibility() {
    const t = $('target').value;
    $('commandWrap').classList.toggle('hidden', t !== 'command');
    $('textWrap').classList.toggle('hidden', t === 'command');
    $('enterWrap').classList.toggle('hidden', t !== 'terminal');
    $('charDelay').disabled = t !== 'editor-type';
  }

  function persist() { vscode.setState(spec()); }

  function setRunning(running) {
    $('run').disabled = running;
    $('stop').disabled = !running;
  }

  $('target').addEventListener('change', () => { syncVisibility(); persist(); });
  for (const f of fields.concat([])) {
    $(f).addEventListener('input', persist);
  }
  $('enter').addEventListener('change', persist);

  $('run').addEventListener('click', () => {
    setRunning(true);
    vscode.postMessage({ type: 'run', spec: spec() });
  });
  $('stop').addEventListener('click', () => vscode.postMessage({ type: 'stop' }));
  $('snipSave').addEventListener('click', () => {
    vscode.postMessage({ type: 'saveSnippet', name: $('snipName').value, spec: spec() });
  });

  function renderSnippets(snippets) {
    const list = $('snipList');
    list.innerHTML = '';
    if (!snippets.length) {
      list.innerHTML = '<span class="muted">No snippets saved yet.</span>';
      return;
    }
    for (const s of snippets) {
      const row = document.createElement('div');
      row.className = 'snip';

      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = s.name;
      name.title = s.name;

      const runBtn = document.createElement('button');
      runBtn.textContent = 'Run';
      runBtn.addEventListener('click', () => {
        applySpec(s.spec);
        setRunning(true);
        vscode.postMessage({ type: 'run', spec: s.spec });
      });

      const loadBtn = document.createElement('button');
      loadBtn.className = 'secondary';
      loadBtn.textContent = 'Load';
      loadBtn.addEventListener('click', () => { applySpec(s.spec); $('snipName').value = s.name; });

      const delBtn = document.createElement('button');
      delBtn.className = 'secondary';
      delBtn.textContent = '\\u2715';
      delBtn.title = 'Delete';
      delBtn.addEventListener('click', () => vscode.postMessage({ type: 'deleteSnippet', name: s.name }));

      row.appendChild(name);
      row.appendChild(runBtn);
      row.appendChild(loadBtn);
      row.appendChild(delBtn);
      list.appendChild(row);
    }
  }

  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg.type === 'status') {
      $('status').textContent = msg.text;
      $('status').className = msg.running ? 'running' : '';
      setRunning(!!msg.running);
    } else if (msg.type === 'snippets') {
      renderSnippets(msg.snippets);
    }
  });

  const saved = vscode.getState();
  if (saved) applySpec(saved);
  syncVisibility();
  vscode.postMessage({ type: 'ready' });
</script>
</body>
</html>`;
}

function deactivate() {}

module.exports = { activate, deactivate };

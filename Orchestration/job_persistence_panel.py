<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Monitor — Vera</title>
<style>
  :root {
    --bg0: #13110f; --bg1: #1a1816; --bg2: #221f1c; --bg3: #2a2724;
    --fg: #d4cfc8; --fg2: #a89f94; --dim: #7a7168; --dim2: #5a5248;
    --accent: #c4956a; --accent2: #d4a574; --accent-dim: #8a6a4a;
    --red: #c45a5a; --red-dim: #8a3a3a; --green: #6a9a5a; --green-dim: #4a6a3a;
    --yellow: #c4a84a; --yellow-dim: #8a7a2a; --blue: #5a8ac4; --blue-dim: #3a5a8a;
    --border: #2a2724; --border2: #3a3734;
    --mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
    --sans: 'Inter', -apple-system, sans-serif;
    --radius: 4px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg0); color: var(--fg); font-family: var(--mono);
    font-size: 11px; line-height: 1.5; overflow: hidden; height: 100vh;
  }
  .layout {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto auto 1fr;
    height: 100vh; gap: 0;
  }

  /* ── Header bar ── */
  .header {
    grid-column: 1 / -1;
    display: flex; align-items: center; gap: 12px;
    padding: 8px 14px; background: var(--bg1);
    border-bottom: 1px solid var(--border);
  }
  .header-title {
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    color: var(--accent); letter-spacing: 0.02em;
  }
  .header-boot {
    font-size: 9px; color: var(--dim); background: var(--bg2);
    padding: 2px 8px; border-radius: 10px;
  }
  .header-actions { margin-left: auto; display: flex; gap: 6px; }
  .btn {
    font-family: var(--mono); font-size: 9.5px; padding: 4px 10px;
    border: 1px solid var(--border2); border-radius: var(--radius);
    background: var(--bg2); color: var(--fg2); cursor: pointer;
    transition: all 0.15s;
  }
  .btn:hover { background: var(--bg3); color: var(--fg); border-color: var(--accent-dim); }
  .btn.primary { background: var(--accent-dim); color: var(--fg); border-color: var(--accent); }
  .btn.primary:hover { background: var(--accent); color: var(--bg0); }
  .btn.danger { border-color: var(--red-dim); color: var(--red); }
  .btn.danger:hover { background: var(--red-dim); }

  /* ── Stats strip ── */
  .stats-strip {
    grid-column: 1 / -1;
    display: flex; gap: 0; padding: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg1);
  }
  .stat-cell {
    flex: 1; padding: 8px 14px;
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 2px;
  }
  .stat-cell:last-child { border-right: none; }
  .stat-label { font-size: 8.5px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.08em; }
  .stat-value { font-size: 16px; font-weight: 600; color: var(--fg); }
  .stat-value.green { color: var(--green); }
  .stat-value.red { color: var(--red); }
  .stat-value.yellow { color: var(--yellow); }
  .stat-value.blue { color: var(--blue); }
  .stat-value.accent { color: var(--accent); }

  /* ── Panel sections ── */
  .panel {
    display: flex; flex-direction: column;
    border-right: 1px solid var(--border);
    overflow: hidden;
  }
  .panel:last-child { border-right: none; }
  .panel-head {
    padding: 8px 12px; background: var(--bg1);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px;
  }
  .panel-label {
    font-size: 10px; font-weight: 600; color: var(--accent);
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .panel-badge {
    font-size: 8.5px; padding: 1px 6px; border-radius: 8px;
    background: var(--bg3); color: var(--dim);
  }
  .panel-actions { margin-left: auto; display: flex; gap: 4px; }
  .filter-row {
    display: flex; gap: 4px; padding: 6px 12px;
    background: var(--bg0); border-bottom: 1px solid var(--border);
  }
  .filter-chip {
    font-family: var(--mono); font-size: 8.5px; padding: 2px 8px;
    border-radius: 10px; border: 1px solid var(--border2);
    background: var(--bg2); color: var(--dim); cursor: pointer;
    transition: all 0.12s;
  }
  .filter-chip:hover { color: var(--fg2); border-color: var(--accent-dim); }
  .filter-chip.active { background: var(--accent-dim); color: var(--fg); border-color: var(--accent); }
  .scroll { flex: 1; overflow-y: auto; overflow-x: hidden; }
  .scroll::-webkit-scrollbar { width: 5px; }
  .scroll::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

  /* ── Job rows ── */
  .job-row {
    display: grid;
    grid-template-columns: 18px 1fr auto auto;
    align-items: center; gap: 8px;
    padding: 5px 12px; border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  .job-row:hover { background: var(--bg2); }
  .job-dot {
    width: 7px; height: 7px; border-radius: 50%;
    justify-self: center;
  }
  .job-dot.running { background: var(--yellow); box-shadow: 0 0 6px var(--yellow-dim); animation: pulse 1.5s infinite; }
  .job-dot.done { background: var(--green); }
  .job-dot.failed { background: var(--red); }
  .job-dot.pending { background: var(--dim); }
  .job-dot.orphan_reclaimed { background: var(--blue); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .job-cap {
    font-size: 10.5px; color: var(--fg);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .job-cap .cap-name { color: var(--accent2); }
  .job-meta {
    font-size: 9px; color: var(--dim); white-space: nowrap;
  }
  .job-time { font-size: 9px; color: var(--dim2); white-space: nowrap; text-align: right; }
  .job-error {
    grid-column: 2 / -1; font-size: 9px; color: var(--red);
    padding: 2px 0 2px 0; word-break: break-all;
    max-height: 36px; overflow: hidden;
  }

  /* ── Ollama log ── */
  .ollama-row {
    display: grid;
    grid-template-columns: auto 1fr auto auto;
    align-items: center; gap: 8px;
    padding: 4px 12px; border-bottom: 1px solid var(--border);
    font-size: 10px;
  }
  .ollama-row:hover { background: var(--bg2); }
  .inst-tag {
    font-size: 8.5px; padding: 1px 6px; border-radius: 3px;
    font-weight: 600; letter-spacing: 0.02em;
  }
  .inst-tag.gpu { background: var(--yellow-dim); color: var(--yellow); }
  .inst-tag.cpu { background: var(--blue-dim); color: var(--blue); }

  /* ── Stream health ── */
  .stream-section {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }
  .stream-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 3px 0; font-size: 10px;
  }
  .stream-label { color: var(--dim); }
  .stream-val { color: var(--fg); font-weight: 500; }
  .consumer-row {
    display: grid; grid-template-columns: 1fr auto auto;
    gap: 8px; padding: 3px 8px; font-size: 9.5px;
    border-left: 2px solid var(--border2); margin-left: 4px;
  }
  .consumer-row .name { color: var(--fg2); }
  .consumer-row .pending { color: var(--yellow); }
  .consumer-row .idle { color: var(--dim); }
  .consumer-row.stale { border-left-color: var(--red-dim); }
  .consumer-row.stale .name { color: var(--red); }

  /* ── Empty state ── */
  .empty { padding: 30px; text-align: center; color: var(--dim); font-size: 10px; }

  /* ── Recovery log ── */
  .recovery-entry {
    padding: 6px 12px; border-bottom: 1px solid var(--border);
    font-size: 10px;
  }
  .recovery-entry .tag {
    display: inline-block; font-size: 8px; padding: 1px 5px;
    border-radius: 3px; margin-right: 6px;
    background: var(--blue-dim); color: var(--blue);
  }
</style>
</head>
<body>

<div class="layout">
  <!-- ═══ HEADER ═══ -->
  <div class="header">
    <span class="header-title">Job Monitor</span>
    <span class="header-boot" id="boot-id">—</span>
    <div class="header-actions">
      <button class="btn" onclick="refresh()" title="Refresh now">Refresh</button>
      <button class="btn primary" onclick="recoverNow()" title="Scan for orphaned tasks">Recover Orphans</button>
      <select id="auto-refresh" class="btn" onchange="setAutoRefresh(this.value)" title="Auto-refresh interval">
        <option value="0">Manual</option>
        <option value="5" selected>5s</option>
        <option value="10">10s</option>
        <option value="30">30s</option>
      </select>
    </div>
  </div>

  <!-- ═══ STATS STRIP ═══ -->
  <div class="stats-strip">
    <div class="stat-cell">
      <span class="stat-label">Stream Pending</span>
      <span class="stat-value yellow" id="st-pending">—</span>
    </div>
    <div class="stat-cell">
      <span class="stat-label">Running</span>
      <span class="stat-value accent" id="st-running">—</span>
    </div>
    <div class="stat-cell">
      <span class="stat-label">Done (all time)</span>
      <span class="stat-value green" id="st-done">—</span>
    </div>
    <div class="stat-cell">
      <span class="stat-label">Failed</span>
      <span class="stat-value red" id="st-failed">—</span>
    </div>
    <div class="stat-cell">
      <span class="stat-label">Reclaimed</span>
      <span class="stat-value blue" id="st-reclaimed">—</span>
    </div>
    <div class="stat-cell">
      <span class="stat-label">History (Redis)</span>
      <span class="stat-value" id="st-history">—</span>
    </div>
  </div>

  <!-- ═══ LEFT: Job History ═══ -->
  <div class="panel">
    <div class="panel-head">
      <span class="panel-label">Job History</span>
      <span class="panel-badge" id="job-count">0</span>
      <div class="panel-actions">
        <select id="job-limit" class="btn" onchange="refresh()" style="font-size:9px">
          <option value="50">50</option>
          <option value="100" selected>100</option>
          <option value="200">200</option>
        </select>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-chip active" data-status="" onclick="filterStatus(this)">All</span>
      <span class="filter-chip" data-status="running" onclick="filterStatus(this)">Running</span>
      <span class="filter-chip" data-status="done" onclick="filterStatus(this)">Done</span>
      <span class="filter-chip" data-status="failed" onclick="filterStatus(this)">Failed</span>
      <span class="filter-chip" data-status="orphan_reclaimed" onclick="filterStatus(this)">Reclaimed</span>
    </div>
    <div class="scroll" id="job-list-wrap">
      <div id="job-list"><div class="empty">loading…</div></div>
    </div>
  </div>

  <!-- ═══ RIGHT: Ollama Requests + Stream Health ═══ -->
  <div class="panel" style="border-right:none">
    <div class="panel-head">
      <span class="panel-label">Ollama Requests</span>
      <span class="panel-badge" id="ollama-count">0</span>
      <div class="panel-actions">
        <span class="panel-label" style="font-size:8.5px;color:var(--dim);font-weight:400">
          tracks GPU vs CPU routing
        </span>
      </div>
    </div>
    <!-- Stream health section -->
    <div class="stream-section" id="stream-health">
      <div style="font-size:9px;color:var(--accent);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;font-weight:600">
        Stream Health
      </div>
      <div id="stream-info"><span class="empty" style="padding:4px">loading…</span></div>
    </div>
    <!-- Consumer list -->
    <div style="padding:6px 12px;border-bottom:1px solid var(--border);background:var(--bg0)">
      <span style="font-size:9px;color:var(--accent);text-transform:uppercase;letter-spacing:0.06em;font-weight:600">
        Consumers
      </span>
    </div>
    <div id="consumer-list" style="border-bottom:1px solid var(--border);max-height:100px;overflow-y:auto">
      <div class="empty" style="padding:6px">—</div>
    </div>
    <!-- Ollama request log -->
    <div class="scroll" id="ollama-list-wrap">
      <div id="ollama-list"><div class="empty">loading…</div></div>
    </div>
  </div>
</div>

<script>
const BASE = (() => {
  try {
    const el = window.parent.document.getElementById('backendUrl');
    if (el && el.value) return el.value.replace(/\/$/, '');
  } catch (_) {}
  return window._veraBase || 'http://llm.int:8999';
})();

let _refreshTimer = null;
let _statusFilter = '';
let _stats = {};
let _jobs = [];
let _ollama = [];

function $(id) { return document.getElementById(id); }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function age(ts) {
  if (!ts) return '—';
  try {
    const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
    const s = Math.floor((Date.now() - d) / 1000);
    if (s < 0) return 'now';
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s/60) + 'm ago';
    if (s < 86400) return Math.floor(s/3600) + 'h ago';
    return Math.floor(s/86400) + 'd ago';
  } catch (_) { return '—'; }
}

function fmtElapsed(s) {
  const n = parseFloat(s);
  if (isNaN(n) || n <= 0) return '';
  if (n < 1) return Math.round(n * 1000) + 'ms';
  if (n < 60) return n.toFixed(1) + 's';
  return Math.floor(n/60) + 'm ' + Math.round(n%60) + 's';
}

async function api(path) {
  try {
    const r = await fetch(BASE + path);
    return await r.json();
  } catch (e) {
    console.error('api', path, e);
    return null;
  }
}

function filterStatus(el) {
  document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  _statusFilter = el.dataset.status || '';
  renderJobs();
}

function renderJobs() {
  const wrap = $('job-list');
  const filtered = _statusFilter
    ? _jobs.filter(j => j.status === _statusFilter)
    : _jobs;

  $('job-count').textContent = filtered.length;

  if (!filtered.length) {
    wrap.innerHTML = '<div class="empty">No jobs' + (_statusFilter ? ' with status "'+esc(_statusFilter)+'"' : '') + '</div>';
    return;
  }

  let html = '';
  for (const j of filtered) {
    const st = j.status || 'pending';
    const capShort = (j.capability || '?').split('.').pop();
    const ts = j.updated_at || j.created_at || '';
    const elapsed = fmtElapsed(j.elapsed_s);

    html += '<div class="job-row">';
    html += '  <div class="job-dot ' + esc(st) + '" title="' + esc(st) + '"></div>';
    html += '  <div class="job-cap"><span class="cap-name">' + esc(j.capability || '?') + '</span></div>';
    html += '  <div class="job-meta">' + (elapsed ? elapsed + ' ' : '') + (j.worker_id ? esc(j.worker_id.slice(0,12)) : '') + '</div>';
    html += '  <div class="job-time">' + age(ts) + '</div>';

    if (j.error) {
      html += '  <div class="job-error">' + esc(j.error) + '</div>';
    }
    if (st === 'orphan_reclaimed' && j.original_consumer) {
      html += '  <div class="job-error" style="color:var(--blue)">';
      html += '    reclaimed from ' + esc(j.original_consumer) + ' (idle ' + esc(j.idle_ms) + 'ms)';
      html += '  </div>';
    }
    html += '</div>';
  }
  wrap.innerHTML = html;
}

function renderOllama() {
  const wrap = $('ollama-list');
  $('ollama-count').textContent = _ollama.length;

  if (!_ollama.length) {
    wrap.innerHTML = '<div class="empty">No Ollama requests logged yet</div>';
    return;
  }

  let html = '';
  for (const r of _ollama) {
    const isGpu = (r.instance_id || '').includes('gpu') || (r.instance_id || '').includes('250');
    const tag = isGpu ? 'gpu' : 'cpu';
    const label = isGpu ? 'GPU' : 'CPU';

    html += '<div class="ollama-row">';
    html += '  <span class="inst-tag ' + tag + '">' + label + '</span>';
    html += '  <span style="color:var(--fg2)">';
    html +=      esc(r.caller_file || '?') + ':' + esc(r.caller_func || '?');
    if (r.cap_name) html += ' <span style="color:var(--accent-dim)">(' + esc(r.cap_name) + ')</span>';
    html += '  </span>';
    html += '  <span style="color:var(--dim)">' + esc(r.model || '') + '</span>';
    html += '  <span style="color:var(--dim2)">' + age(r.ts) + '</span>';
    html += '</div>';
  }
  wrap.innerHTML = html;
}

function renderStreamHealth(stats) {
  const s = stats.stream || {};
  const info = $('stream-info');
  const clist = $('consumer-list');

  if (s.error) {
    info.innerHTML = '<span style="color:var(--red);font-size:10px">' + esc(s.error) + '</span>';
    clist.innerHTML = '';
    return;
  }

  let html = '';
  html += '<div class="stream-row"><span class="stream-label">Stream length</span><span class="stream-val">' + (s.length ?? '—') + '</span></div>';
  html += '<div class="stream-row"><span class="stream-label">Pending (unacked)</span><span class="stream-val" style="color:' + ((s.pending_total||0)>0?'var(--yellow)':'var(--green)') + '">' + (s.pending_total ?? '—') + '</span></div>';
  info.innerHTML = html;

  // Consumers
  const consumers = s.consumers || [];
  if (!consumers.length) {
    clist.innerHTML = '<div class="empty" style="padding:6px;font-size:9px">No consumers</div>';
    return;
  }

  let chtml = '';
  for (const c of consumers) {
    const idle_s = Math.floor((c.idle || 0) / 1000);
    const isStale = idle_s > 300;
    chtml += '<div class="consumer-row' + (isStale ? ' stale' : '') + '">';
    chtml += '  <span class="name">' + esc(c.name) + '</span>';
    chtml += '  <span class="pending">' + (c.pending||0) + ' pending</span>';
    chtml += '  <span class="idle">' + (idle_s > 3600 ? Math.floor(idle_s/3600) + 'h' : idle_s > 60 ? Math.floor(idle_s/60) + 'm' : idle_s + 's') + ' idle</span>';
    chtml += '</div>';
  }
  clist.innerHTML = chtml;
}

async function refresh() {
  const limit = $('job-limit').value || 100;

  const [statsData, jobsData, ollamaData] = await Promise.all([
    api('/jobs/stats'),
    api('/jobs/history?limit=' + limit + (_statusFilter ? '&status=' + _statusFilter : '')),
    api('/jobs/ollama_log?limit=80'),
  ]);

  if (statsData) {
    _stats = statsData;
    $('boot-id').textContent = statsData.boot_id || '—';

    const st = statsData.stats || {};
    const stream = statsData.stream || {};
    $('st-pending').textContent = stream.pending_total ?? '—';
    $('st-running').textContent = statsData.running_local ?? '—';
    $('st-done').textContent = st.total_done ?? 0;
    $('st-failed').textContent = st.total_failed ?? 0;
    $('st-reclaimed').textContent = st.total_reclaimed ?? 0;
    $('st-history').textContent = statsData.history_redis ?? '—';

    renderStreamHealth(statsData);
  }

  if (jobsData && jobsData.jobs) {
    _jobs = jobsData.jobs;
    renderJobs();
  }

  if (ollamaData && ollamaData.requests) {
    _ollama = ollamaData.requests;
    renderOllama();
  }
}

async function recoverNow() {
  const r = await fetch(BASE + '/jobs/recover', { method: 'POST' });
  const d = await r.json();
  if (d && d.status === 'started') {
    // Flash the button
    const btn = document.querySelector('.btn.primary');
    btn.textContent = 'Scanning…';
    btn.disabled = true;
    setTimeout(() => { btn.textContent = 'Recover Orphans'; btn.disabled = false; refresh(); }, 3000);
  }
}

function setAutoRefresh(sec) {
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  const n = parseInt(sec);
  if (n > 0) {
    _refreshTimer = setInterval(refresh, n * 1000);
  }
}

// Listen for base URL relay from parent
window.addEventListener('message', e => {
  if (e.data && e.data.type === 'vera:base') {
    // Could update BASE but for simplicity we just refresh
    location.reload();
  }
});

// Initial load
refresh();
setAutoRefresh(5);
</script>
</body>
</html>
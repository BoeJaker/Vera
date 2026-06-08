/* fabric_discovery_panel.js — Vera "Discover+" tab logic.
 * Self-contained: resolves its own API base, defines its own api()/esc(),
 * lazy-loads /ui/vera-graph.js, and drives the markup in
 * fabric_discovery_panel.html. Robust live updates come from POLLING the
 * reconstructed graph (fabric.discover.graph) while a crawl runs server-side,
 * so it never depends on the harness event relay (though it listens for it
 * too, as a bonus, to refresh sooner).
 */
(function initDiscoverPlus(){
  var root = document.getElementById('fdsc-root');
  if (!root) { return; }              // markup not mounted yet
  if (root._fdscInit) { return; }     // guard against double-init
  root._fdscInit = true;

  // ── API base + helpers ────────────────────────────────────────────────────
  var API = (function(){
    try { if (window.parent && window.parent !== window && window.parent._veraBase) return window.parent._veraBase; } catch(e){}
    if (window._veraBase) return String(window._veraBase).replace(/\/$/, '');
    try { var u = document.getElementById('urlInput'); if (u && u.value) return u.value.replace(/\/$/, ''); } catch(e){}
    try { var s = localStorage.getItem('vera_base'); if (s) return s.replace(/\/$/, ''); } catch(e){}
    return '';
  })();

  function esc(s){ var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }
  function $(id){ return document.getElementById(id); }

  async function api(path, method, body, timeoutMs){
    var ctrl = new AbortController();
    var to = setTimeout(function(){ ctrl.abort(); }, timeoutMs || 30000);
    try {
      var opt = { method: method || 'GET', headers: { 'Content-Type': 'application/json' }, signal: ctrl.signal };
      if (body) opt.body = JSON.stringify(body);
      var r = await fetch(API + path, opt);
      var txt = await r.text();
      try { return JSON.parse(txt); } catch(e){ return { error: txt || ('HTTP ' + r.status) }; }
    } catch(e){
      return { error: (e && e.name === 'AbortError') ? 'timeout' : (e && e.message) || 'network error' };
    } finally { clearTimeout(to); }
  }

  // ── Logging — routes to graph's bottom Terminal drawer ─────────────────────
  // Falls back to a local buffer if the graph isn't mounted yet.
  var logLines = [];  // kept for re-play if the terminal drawer hasn't mounted yet
  function _graphBd(){
    try { return graph && graph.bottomDrawer || null; } catch(_){ return null; }
  }
  function log(msg, type){
    var bd = _graphBd();
    if (bd) {
      bd.log(msg, type);
    } else {
      // Buffer until the graph is ready
      logLines.push({ msg: msg, type: type });
      if (logLines.length > 400) logLines = logLines.slice(-300);
    }
  }
  // Flush buffered lines into the drawer once the graph is ready
  function _flushLog(){
    var bd = _graphBd();
    if (!bd || !logLines.length) return;
    logLines.forEach(function(l){ bd.log(l.msg, l.type); });
    logLines = [];
  }
  function overlay(line, counts, state){
    var ov = $('fdsc-overlay'); if (!ov) return;
    ov.style.display = 'block';
    if (line != null) $('fdsc-ov-line').textContent = line;
    if (counts != null) $('fdsc-ov-counts').textContent = counts;
    var dot = $('fdsc-ov-dot');
    if (dot && state) dot.className = 'fdsc-dot ' + state;
  }
  function overlayHide(delay){ setTimeout(function(){ var ov = $('fdsc-overlay'); if (ov) ov.style.display = 'none'; }, delay || 0); }

  // ── Graph host ─────────────────────────────────────────────────────────────
  var graph = null, seenNodes = {}, seenEdges = {};
  var lastGraph = { nodes: [], edges: [] };           // full payload, for filtering
  var nodeById = {};                                  // id -> last node spec
  var degree = {};                                    // id -> edge degree (spacing)
  var filters = { nodeOff: {}, edgeOff: {}, minRel: 0 };
  // relevance -> 2-hex alpha (faint when low). Honoured by patched vera-graph.js
  // via node._alpha; harmless if the graph build doesn't read it.
  function relAlpha(rel){
    var a = Math.round(60 + Math.max(0, Math.min(1, (rel == null ? 0.5 : rel))) * 195); // 60..255
    var h = a.toString(16); return (h.length < 2 ? '0' + h : h);
  }

  // ── Persistence across tab switches / panel re-injection ──────────────────
  // The harness may tear down and re-inject this panel when tabs change, which
  // would otherwise blank the graph. We stash the live graph on a window-level
  // store and restore it on init.
  // Prefer the harness (parent) window so the cache survives a full iframe
  // reload on panel switch; fall back to this window if cross-origin / no parent.
  function _storeRoot(){
    try { if (window.parent && window.parent !== window) { void window.parent._fdscProbe; return window.parent; } } catch(e){}
    return window;
  }
  (function(){ try { var r=_storeRoot(); r._fdscStore = r._fdscStore || {}; } catch(e){ window._fdscStore = window._fdscStore || {}; } })();
  function saveStore(){
    try {
      var s = _storeRoot()._fdscStore || (_storeRoot()._fdscStore = {});
      s.graph = lastGraph;
      s.filters = filters;
      s.active = active;
    } catch(e){}
  }
  function loadStore(){
    try {
      var s = _storeRoot()._fdscStore || {};
      if (s.filters) filters = s.filters;
      if (s.active) active = s.active;
      return s.graph && s.graph.nodes && s.graph.nodes.length ? s.graph : null;
    } catch(e){ return null; }
  }
  function ensureGraphLib(cb){
    if (window.veraUI && window.veraUI.Graph) { cb(); return; }
    var existing = document.getElementById('fdsc-veragraph-js');
    if (!existing){
      var s = document.createElement('script');
      s.id = 'fdsc-veragraph-js';
      s.src = API + '/ui/vera-graph.js';
      s.onload = function(){ cb(); };
      s.onerror = function(){ log('Could not load vera-graph.js — map disabled', 'err'); };
      document.body.appendChild(s);
    } else {
      // wait for it
      var tries = 0;
      var iv = setInterval(function(){
        if (window.veraUI && window.veraUI.Graph) { clearInterval(iv); cb(); }
        else if (++tries > 40) { clearInterval(iv); }
      }, 150);
    }
  }
  function extendPalette(){
    try {
      var C = window.veraUI && window.veraUI.Graph && window.veraUI.Graph.colors;
      if (!C) return;
      // node types unique to the discovery map
      C.Page      = C.Page      || '#6b9bd2';   // blue — fetched pages
      C.Surface   = C.Surface   || '#c9955a';   // amber — interaction surfaces
      C.Subtable  = C.Subtable  || '#5ec9a0';   // teal-green — extracted tables
      C.Search    = C.Search    || '#facc15';   // yellow — search/seed
      // entity subtype colours (so each kind is identifiable in the key)
      C.person       = C.person       || '#e8a87c';
      C.organisation = C.organisation || '#c98f5a';
      C.account      = C.account      || '#d98ec0';
      C.email        = C.email        || '#d98ec0';
      C.identity     = C.identity     || '#d98ec0';
      C.technology   = C.technology   || '#5a9e8f';
      C.product      = C.product      || '#7fb37f';
      C.location     = C.location     || '#9ec96b';
      C.event        = C.event        || '#c9b15a';
      C.domain       = C.domain       || '#8f9ed9';
      C['function']  = C['function']  || '#b08fd9';
      C['class']     = C['class']     || '#9b7fd4';
      C.module       = C.module       || '#a78bd0';
      C.year         = C.year         || '#7a8a99';
      C.money        = C.money        || '#6db87a';
      // edge colours for our relationships
      C._edge_HAS_PAGE       = C._edge_HAS_PAGE       || 'rgba(107,155,210,0.55)';
      C._edge_HAS_SURFACE    = C._edge_HAS_SURFACE    || 'rgba(201,149,90,0.7)';
      C._edge_HAS_SUBTABLE   = C._edge_HAS_SUBTABLE   || 'rgba(94,201,160,0.7)';
      C._edge_HAS_DATA_SUBSET= C._edge_HAS_DATA_SUBSET|| 'rgba(94,201,160,0.6)';
      C._edge_MENTIONS       = C._edge_MENTIONS       || 'rgba(201,122,90,0.6)';
      C._edge_RELATED        = C._edge_RELATED        || 'rgba(150,140,120,0.5)';
      C._edge_CO_OCCURS      = C._edge_CO_OCCURS      || 'rgba(160,150,130,0.55)';
    } catch(e){}
  }
  // DASHED = inferred entity↔entity links discovered POST extraction
  // (RELATED / SIMILAR, written by the global cross-source linker & loom).
  // SOLID  = direct relations: page→entity (MENTIONS), structure (HAS_*,
  //          LINKS_TO) and same-page CO_OCCURS.
  var SOFT_RE = /^(RELATED|SIMILAR)/i;
  function edgeStyle(e){
    var soft = SOFT_RE.test(e.rel || '');
    var df = degree[e.from] || 1, dt = degree[e.to] || 1;
    // push hubs apart: longer rest length when either endpoint is dense
    var spring = 80 + Math.min(180, (df + dt) * 7);
    if (soft){
      return { dash: [3, 4], width: 0.8, springLength: spring + 50,
               springStrength: 0.0035, color: 'rgba(150,140,120,0.5)' };
    }
    return { springLength: spring, springStrength: 0.013 };
  }
  // Private no-op bus so discovery NEVER shares the live event bus with the
  // main fabric graph (was causing discovery nodes to spill across UIs).
  var PRIVATE_BUS = { subscribe: function(){ return function(){}; },
                      emit: function(){}, on: function(){ return function(){}; } };
  function ensureGraph(){
    if (graph) return graph;
    if (!(window.veraUI && window.veraUI.Graph)) return null;
    var host = $('fdsc-graph-host'); if (!host) return null;
    extendPalette();
    graph = window.veraUI.Graph.create(host, {
      height: 'fill', showSearch: true, showLegend: false, showLayerToggle: false,
      filtersOnly: true, showRelevance: true,
      apiBase: API, actionsEnabled: true, edgeStyleFn: edgeStyle,
      subscribeLiveEvents: false, eventBus: PRIVATE_BUS,
      autoOpenTerminal: false,
      bottomDrawerHeight: 180,
      defaultPanel: 'discover',
      onNodeSelect: function(node, detailEl, inst){
        // Show long-form content in the content drawer when a scraped page / record
        // with meaningful body text is clicked.
        var p = node.props || {};
        var body = p.content || p.text || p.body || p.summary || p.description || '';
        if (body && body.length > 80) {
          var title = node.label || p.title || p.url || node.id;
          inst.bottomDrawer.showContent(title + ' — content', body);
          inst.bottomDrawer.open('content');
        }
        // Outbound links — show as table (preferred over generic records)
        var links = p.links;
        if (Array.isArray(links) && links.length) {
          var lRows = links.slice(0, 300).map(function(l){ return [l.url || '', l.anchor || '']; });
          inst.bottomDrawer.showTable(['url','anchor'], lRows,
            (p.title || node.label || node.id || 'Page') + ' — links (' + links.length + ')');
          inst.bottomDrawer.open('table');
          return;
        }
        // Fallback: tabular record rows (subtable nodes etc.)
        if (p.records && Array.isArray(p.records) && p.records.length) {
          var cols = Object.keys(p.records[0] || {}).slice(0, 12);
          var rows = p.records.slice(0, 200).map(function(r){ return cols.map(function(c){ return r[c]; }); });
          inst.bottomDrawer.showTable(cols, rows, node.label || node.id);
          inst.bottomDrawer.open('table');
        }
      },
      onAction: function(action, node){
        if (action === 'browse' || action === 'open_record'){
          var ds = (node && node.props && node.props.dataset_id) ||
                   (node && (node.type === 'Dataset' || node.type === 'Subtable') && node.id) || '';
          if (ds){ openBrowser(ds, node && node.label); return false; }
        }
        if (action === 'open_url'){
          var u = (node && node.props && node.props.url) || (node && node.id) || '';
          if (u && /^https?:/.test(u)) window.open(u, '_blank');
          return false;
        }
        // add_source / pull_expand / crawl_surface / expand_links / extract_entities /
        // auto_mine / forget → server node-action runner; delta merged in onActionDone.
      },
      onActionDone: function(action, node, result){
        var payload = result && result.result;   // run_node_action wraps as {ok,result}
        if (payload && (payload.nodes || payload.edges)){
          applyGraph(payload, true);
          log((payload.note || 'Expanded ' + (node && node.label || node && node.id)) +
              '  (+' + ((payload.nodes || []).length) + ' nodes)', 'ok');
        } else if (payload && payload.pulled !== undefined){
          log('Auto-mine: pulled ' + payload.pulled + ' surfaces over ' + (payload.rounds_run||0) + ' rounds', 'ok');
          if (active.crawlId) pollOnce();
        } else if (result && result.result && result.result.ok && result.result.source_id){
          log('Added as source: ' + result.result.source_id, 'ok');
        } else if (result && result.error){
          log('Action failed: ' + result.error, 'err');
        }
        refreshSideLists();
      }
    });
    if (host && window.ResizeObserver && !host._fdscRO){
      host._fdscRO = new ResizeObserver(function(){
        if (host.clientWidth > 0 && graph && graph.wake){ graph.wake(); }
      });
      host._fdscRO.observe(host);
    }
    // Flush any log lines that were buffered before the graph was ready
    try { _flushLog(); } catch(_){}
    // Open the terminal drawer if there's already content
    if (graph && graph.bottomDrawer && logLines.length === 0) {
      // already flushed - don't auto-open, let user pull it up
    }
    return graph;
  }
  function graphReset(){
    seenNodes = {}; seenEdges = {};
    lastGraph = { nodes: [], edges: [] }; nodeById = {}; degree = {};
    var g = ensureGraph(); if (g){ g.clear(); if (g.rebuildChips) g.rebuildChips(); }
  }

  function nodeRel(n){ var p = (n && n.props) || {}; return (typeof p.relevance === 'number') ? p.relevance : 0.5; }
  function nodeRadius(n){
    var p = (n && n.props) || {};
    if (n.type === 'Dataset' || (p && p.root)) return 15;
    // mostly constant — relevance is conveyed by OPACITY, not size
    var base = isEntity(n) ? 7 : 10;
    return Math.round(base + nodeRel(n) * 3);
  }

  var STRUCTURAL = { Dataset: 1, Page: 1, Surface: 1, Subtable: 1, Search: 1 };
  function isEntity(n){ return n && !STRUCTURAL[n.type]; }
  // record into lastGraph (deduped) so filtering always has the full picture
  function remember(payload){
    (payload.nodes || []).forEach(function(n){
      // entities arrive as type 'Entity' with props.type = subtype; promote the
      // subtype to the node type so colour + LHM chips key on it per-kind
      var t = n.type || 'Page';
      if (t === 'Entity' && n.props && n.props.type) t = n.props.type;
      if (nodeById[n.id]){
        var ex = nodeById[n.id];
        if (n.label) ex.label = n.label;
        if (t) ex.type = t;
        if (n.props) ex.props = Object.assign(ex.props || {}, n.props);
      } else { nodeById[n.id] = { id: n.id, label: n.label || n.id, type: t, props: n.props || {} }; lastGraph.nodes.push(nodeById[n.id]); }
    });
    (payload.edges || []).forEach(function(e){
      var k = e.from + '|' + e.to + '|' + e.rel;
      if (!seenEdges['L' + k]){
        seenEdges['L' + k] = true; lastGraph.edges.push({ from: e.from, to: e.to, rel: e.rel || 'LINKS_TO' });
        degree[e.from] = (degree[e.from] || 0) + 1; degree[e.to] = (degree[e.to] || 0) + 1;
      }
    });
  }

  function applyGraph(payload, incremental){
    var g = ensureGraph(); if (!g || !payload) return;
    if (!incremental){
      g.clear(); seenNodes = {}; seenEdges = {};
      lastGraph = { nodes: [], edges: [] }; nodeById = {}; degree = {};
    }
    remember(payload);
    saveStore();
    (payload.nodes || []).forEach(function(n){
      var spec = nodeById[n.id] || n;
      if (seenNodes[n.id]){
        var ex = g.getNode(n.id);
        if (ex){
          if (spec.label && ex.label !== spec.label) ex.label = spec.label;
          if (spec.props){ ex.props = Object.assign(ex.props || {}, spec.props); }
          if (spec.type && ex.type !== spec.type) ex.type = spec.type;
          ex.r = nodeRadius(spec);
        }
        return;
      }
      seenNodes[n.id] = true;
      var added = g.addNode({ id: spec.id, label: spec.label || spec.id, type: spec.type || 'Page',
                              props: spec.props || {}, r: nodeRadius(spec) });
      if (added && incremental && g.pulseNode) g.pulseNode(n.id);
    });
    (payload.edges || []).forEach(function(e){
      var k = e.from + '|' + e.to + '|' + e.rel;
      if (seenEdges[k]) return;
      seenEdges[k] = true;
      g.addEdge({ from: e.from, to: e.to, rel: e.rel || 'LINKS_TO' });
    });
    // vera-graph owns the filter UI + visibility/relevance now; refresh both.
    if (g.rebuildChips) g.rebuildChips();
    if (g.applyVis) g.applyVis();
    if (g.draw) try { g.draw(); } catch(_){}
  }


  // ── State ──────────────────────────────────────────────────────────────────
  var active = { crawlId: '', datasetId: '', running: false };
  var pollTimer = null;
  var lastCrawls = [];                 // most recent /history payload
  function newCrawlId(){ return 'disc_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  async function pollOnce(){
    if (!active.crawlId) return;
    var g = await api('/fabric/discover/graph?crawl_id=' + encodeURIComponent(active.crawlId) +
                      (active.datasetId ? '&dataset_id=' + encodeURIComponent(active.datasetId) : ''));
    if (g && !g.error){
      if (g.dataset_id) active.datasetId = g.dataset_id;   // always track current crawl's dataset
      applyGraph(g, true);
      var st = g.stats || {};
      overlay(null, (st.pages || 0) + ' pages \u00b7 ' + (st.surfaces || 0) + ' surfaces \u00b7 ' +
                    (st.subdatasets || 0) + ' sub-tables \u00b7 ' + (st.entities || 0) + ' entities',
              active.running ? 'running' : 'paused');
    }
    updateCurrentCrawl();
  }
  function startPolling(){
    stopPolling();
    pollTimer = setInterval(function(){ pollOnce(); refreshSideLists(); }, 2600);
  }
  function stopPolling(){ if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

  // ── Current-crawl strip (return-to-current) ───────────────────────────────
  function updateCurrentCrawl(){
    var bar = $('fdsc-current'); if (!bar) return;
    if (!active.crawlId){ bar.style.display = 'none'; return; }
    var row = (lastCrawls || []).filter(function(c){ return c.crawl_id === active.crawlId; })[0] || {};
    var label = row.topic || row.seed_url || active.datasetId || active.crawlId;
    var st = active.running ? 'running' : (row.status || 'paused');
    var lab = $('fdsc-current-label'); if (lab) lab.textContent = (active.running ? 'Crawling: ' : 'Current: ') + label;
    var dot = $('fdsc-current-dot'); if (dot) dot.className = 'fdsc-dot ' + st;
    bar.style.display = 'flex';
  }
  // Reload the active crawl's live graph without disturbing its run/poll state.
  async function returnToCurrent(){
    if (!active.crawlId) return;
    overlay('Loading current crawl\u2026', '', active.running ? 'running' : 'paused');
    var g = await api('/fabric/discover/graph?crawl_id=' + encodeURIComponent(active.crawlId) +
                      (active.datasetId ? '&dataset_id=' + encodeURIComponent(active.datasetId) : ''));
    if (g && !g.error){
      applyGraph(g, false);
      var st = g.stats || {};
      overlay('Current crawl', (st.pages || 0) + ' pages \u00b7 ' + (st.surfaces || 0) + ' surfaces \u00b7 ' +
              (st.subdatasets || 0) + ' sub-tables', active.running ? 'running' : 'paused');
    } else {
      log('Could not load current crawl: ' + ((g && g.error) || 'unknown'), 'err');
    }
    var hl = $('fdsc-hist-list');
    if (hl) Array.prototype.forEach.call(hl.querySelectorAll('.fdsc-item.crawl'), function(el){
      el.classList.toggle('sel', el.getAttribute('data-cid') === active.crawlId);
    });
    refreshSideLists();
  }
  // On start, when nothing was restored, show the active or most-recent crawl.
  function autoLoadInitial(){
    if (!lastCrawls || !lastCrawls.length) return;
    var target = null;
    if (active.crawlId) target = lastCrawls.filter(function(c){ return c.crawl_id === active.crawlId; })[0];
    if (!target) target = lastCrawls[0];   // history is newest-first
    if (target && target.crawl_id) selectCrawl(target.crawl_id, target.dataset_id);
  }

  // Poll history until this crawl reports done/paused (used when the HTTP
  // request itself times out but the crawl keeps running server-side).
  async function waitForCompletion(crawlId, maxMs){
    var deadline = Date.now() + (maxMs || 600000);
    while (Date.now() < deadline){
      await new Promise(function(r){ setTimeout(r, 3000); });
      await pollOnce();
      var h = await api('/fabric/discover/history');
      var row = h && h.crawls && h.crawls.filter(function(c){ return c.crawl_id === crawlId; })[0];
      if (row && (row.status === 'done' || row.status === 'paused' || row.status === 'error')){
        return row;
      }
    }
    return null;
  }

  // Generic "run a crawl request while polling its graph live"
  async function runCrawl(reqPromise, crawlId, label){
    active.crawlId = crawlId; active.datasetId = ''; active.running = true;
    graphReset(); updateCurrentCrawl();
    overlay(label || 'Crawling\u2026', '', 'running');
    log(label || ('Starting ' + crawlId), 'ok');
    setTimeout(pollOnce, 700);      // first quick poll
    startPolling();
    var res = await reqPromise;

    // The crawl can outlive the HTTP request (long topic runs / proxies). A
    // timeout is NOT a failure — the server keeps going, so we keep tracking.
    if (res && res.error === 'timeout'){
      log('Request timed out, but the crawl is still running \u2014 tracking it in the background\u2026', 'warn');
      overlay('Running in background\u2026', 'tracking via history', 'running');
      var row = await waitForCompletion(crawlId);
      active.running = false; stopPolling(); await pollOnce(); refreshSideLists(); loadHistory(true);
      if (row){
        log('Done (background) \u2014 ' + (row.pages || row.pages_fetched || 0) + ' pages, ' +
            (row.surfaces || 0) + ' surfaces, ' + (row.entities || 0) + ' entities', 'ok');
        overlay(row.status === 'paused' ? 'Paused \u2014 Continue to resume' : 'Complete',
                (row.pages || 0) + ' pages \u00b7 ' + (row.surfaces || 0) + ' surfaces \u00b7 ' +
                (row.entities || 0) + ' entities', row.status === 'paused' ? 'paused' : 'done');
        if (row.status !== 'paused') overlayHide(6000);
      } else {
        overlay('Still running', 'taking a while \u2014 the map keeps updating', 'running');
      }
      return row || res;
    }

    active.running = false;
    stopPolling();
    await pollOnce();               // final state
    refreshSideLists(); loadHistory(true);
    if (res && (res.ok || res.crawl_id)){
      if (res.crawl_id) active.crawlId = res.crawl_id;
      if (res.dataset_id) active.datasetId = res.dataset_id;
      log('Done \u2014 ' + (res.pages_fetched || 0) + ' pages, ' + (res.surfaces_found || 0) +
          ' surfaces, ' + (res.subtables_found || 0) + ' sub-tables, ' + (res.entities_found || 0) + ' entities' +
          (res.surfaces_promoted ? ', ' + res.surfaces_promoted + ' promoted' : '') +
          (res.status === 'paused' ? ' (paused, ' + (res.queue_remaining || 0) + ' queued \u2014 Continue to resume)' : ''), 'ok');
      overlay(res.status === 'paused' ? 'Paused \u2014 Continue to resume' : 'Complete',
              (res.pages_fetched || 0) + ' pages \u00b7 ' + (res.surfaces_found || 0) + ' surfaces \u00b7 ' +
              (res.subtables_found || 0) + ' sub-tables \u00b7 ' + (res.entities_found || 0) + ' entities',
              res.status === 'paused' ? 'paused' : 'done');
      if (res.status !== 'paused') overlayHide(6000);
    } else {
      log('Failed: ' + ((res && res.error) || 'unknown'), 'err');
      overlay('Failed', (res && res.error) || 'unknown', 'paused');
    }
    return res;
  }

  // ── Topic discovery ──────────────────────────────────────────────────────
  // Collect selected site groups (or "all" if the master toggle is on).
  function collectSites(){
    if ($('fdsc-site-all') && $('fdsc-site-all').checked) return 'all';
    var groups = ['reddit','x','youtube','news','github','stackoverflow',
                  'hackernews','blogs','wikipedia','academic','forums',
                  'podcasts','mastodon','linkedin','tiktok','docs'];
    return groups.filter(function(g){ var el=$('fdsc-site-'+g); return el && el.checked; })
                 .join(',');
  }

  // Rolling topic description box (fed by topic_description progress events).
  function setTopicDesc(desc, final){
    var el = $('fdsc-desc'); if (!el) return;
    if (!desc){ el.style.display = 'none'; return; }
    el.style.display = 'block';
    el.innerHTML = '<b style="color:var(--acc,#c98a3a)">Topic so far' +
      (final ? ' (final)' : '') + ':</b> ' + esc(desc);
  }

  // ── Map entire topic (comprehensive multi-site crawl) ──────────────────────
  async function mapTopic(){
    var topic = ($('fdsc-topic').value || '').trim();
    if (!topic){ status('fdsc-topic-status', 'Enter a topic', 'err'); return; }
    var depth = (($('fdsc-map-depth') || {}).value) || 'standard';
    status('fdsc-topic-status', 'Mapping topic (' + depth + ')\u2026', '');
    setTopicDesc('', false);
    var cid = newCrawlId();
    var body = {
      topic: topic, depth: depth, crawl_id: cid,
      dataset_id: ($('fdsc-topic-ds') ? $('fdsc-topic-ds').value : '').trim(),
      sites: (collectSites() || 'all'),
      use_llm_entities: !$('fdsc-topic-noent') || !$('fdsc-topic-noent').checked,
      llm_steering: !!($('fdsc-map-steer') && $('fdsc-map-steer').checked),
      focus: ($('fdsc-map-focus') ? $('fdsc-map-focus').value : '').trim(),
      max_concurrency: _intv('fdsc-topic-conc', 4, 1, 16),
      entity_workers: _intv('fdsc-topic-workers', 3, 1, 8),
      auto_synthesize: !!($('fdsc-topic-autosyn') && $('fdsc-topic-autosyn').checked),
      synth_neighbor_depth: _intv('fdsc-topic-ndepth', 1, 0, 3),
      synth_infer_edges: !($('fdsc-topic-inferedges') && !$('fdsc-topic-inferedges').checked),
      consolidate_entities: !($('fdsc-topic-consol') && !$('fdsc-topic-consol').checked),
      drift_guard: !($('fdsc-topic-drift') && !$('fdsc-topic-drift').checked),
      llm_drift_gate: !!($('fdsc-topic-llmdrift') && $('fdsc-topic-llmdrift').checked),
      swallow_domains: !($('fdsc-topic-swallow') && !$('fdsc-topic-swallow').checked),
      unlimited_depth: !!($('fdsc-topic-nodepth') && $('fdsc-topic-nodepth').checked),
      topic_brief: !!($('fdsc-topic-brief') && $('fdsc-topic-brief').checked)
    };
    log('\u25b6 Mapping "' + topic + '" (depth ' + depth + ', sites ' + body.sites + ')', 'ok');
    var to = (depth === 'exhaustive') ? 3600000 : (depth === 'deep' ? 1800000 : 600000);
    var res = await runCrawl(api('/fabric/discover/map_topic', 'POST', body, to), cid, 'Map: ' + topic);
    if (res && res.topic_description) setTopicDesc(res.topic_description, true);
    status('fdsc-topic-status', (res && res.error) ? res.error : 'Mapped', (res && res.error) ? 'err' : 'ok');
  }

  // ── NER / NLP backend status + control ─────────────────────────────────────
  function renderNer(r){
    var st = $('fdsc-ner-status'); if (!st) return;
    var det = $('fdsc-ner-detail');
    if (!r || r.error){
      st.textContent = 'Error: ' + ((r && r.error) || 'failed');
      st.style.color = 'var(--err,#c96b6b)';
      if (det){ det.style.display = 'none'; det.textContent = ''; }
      return;
    }
    var a = r.available || {};
    var hist = (r.self_test && r.self_test.type_histogram) || {};
    var ht = Object.keys(hist).map(function(k){ return k + ':' + hist[k]; }).join('  ');
    var backendOk = r.active_backend !== 'heuristic';
    st.style.color = backendOk ? 'var(--ok,#6db87a)' : 'var(--acc3,#c9955a)';
    st.innerHTML = '<b>' + esc(r.active_backend || 'unknown') + '</b>' +
      ' \u00b7 spaCy ' + (a.spacy ? '\u2713' : '\u2717') +
      ' GLiNER ' + (a.gliner ? '\u2713' : '\u2717') +
      ' heuristic \u2713';

    // Populate the detail box
    if (det){
      var lines = [];
      lines.push('active backend : ' + (r.active_backend || 'none'));
      lines.push('configured     : ' + (r.configured || 'auto'));
      lines.push('spaCy          : ' + (a.spacy ? 'available' + (r.spacy_model ? ' (' + r.spacy_model + ')' : '') : 'not found'));
      lines.push('GLiNER         : ' + (a.gliner ? 'available' + (r.gliner_model ? ' (' + r.gliner_model + ')' : '') : 'not found'));
      lines.push('heuristic      : always available');
      if (ht) lines.push('self-test      : ' + ht);
      if (r.self_test && r.self_test.sample_entities && r.self_test.sample_entities.length){
        lines.push('sample ents    : ' + r.self_test.sample_entities.slice(0,6).map(function(en){ return en.text + ' [' + en.type + ']'; }).join(', '));
      }
      if (!backendOk) lines.push('\u26a0 only heuristic available — install spaCy or GLiNER for better extraction');
      det.textContent = lines.join('\n');
      det.style.display = 'block';
    }

    var sel = $('fdsc-ner-backend'); if (sel && r.configured) sel.value = r.configured;
    log('NER: ' + r.active_backend + (backendOk ? '' : ' (heuristic fallback)') + (ht ? ' \u00b7 ' + ht : ''),
        backendOk ? 'ok' : 'warn');
  }
  async function nerRefresh(){
    var st = $('fdsc-ner-status'); if (st) st.textContent = 'checking\u2026';
    renderNer(await api('/fabric/entity_graph/ner', 'POST', {}));
  }
  async function nerApply(){
    var b = (($('fdsc-ner-backend') || {}).value) || 'auto';
    var st = $('fdsc-ner-status'); if (st) st.textContent = 'applying ' + b + '\u2026';
    renderNer(await api('/fabric/entity_graph/ner', 'POST', { backend: b }));
  }

  async function topicGo(){
    var topic = ($('fdsc-topic').value || '').trim();
    if (!topic){ status('fdsc-topic-status', 'Enter a topic', 'err'); return; }
    status('fdsc-topic-status', 'Discovering\u2026', '');
    var cid = newCrawlId();
    var body = {
      topic: topic,
      seed_urls: ($('fdsc-topic-seeds').value || '').trim(),
      max_sources: parseInt($('fdsc-topic-max').value || '10', 10),
      content_type: $('fdsc-topic-type').value || 'all',
      max_pages: parseInt($('fdsc-topic-pages').value || '120', 10),
      topic_dropoff: parseInt($('fdsc-topic-drop').value || '3', 10),
      search_angles: parseInt(($('fdsc-topic-angles') || {}).value || '6', 10),
      expansion_rounds: parseInt(($('fdsc-topic-rounds') || {}).value || '2', 10),
      same_domain: !!$('fdsc-topic-same').checked,
      auto_promote: !!$('fdsc-topic-promote').checked,
      loom: !$('fdsc-topic-noloom') || !$('fdsc-topic-noloom').checked,
      loom_cross: !$('fdsc-topic-nocross') || !$('fdsc-topic-nocross').checked,
      llm_search: !$('fdsc-topic-nollm') || !$('fdsc-topic-nollm').checked,
      llm_tagging: !!($('fdsc-topic-llmtag') && $('fdsc-topic-llmtag').checked),
      min_relevance: (parseInt(($('fdsc-topic-minrel') || {}).value || '6', 10) / 100),
      extract_entities: !$('fdsc-topic-noent') || !$('fdsc-topic-noent').checked,
      extract_entities_llm: !!($('fdsc-topic-llment') && $('fdsc-topic-llment').checked),
      sites: collectSites(),
      include_vera_sources: !$('fdsc-site-vera') || !!$('fdsc-site-vera').checked,
      negative_words: ($('fdsc-topic-neg') ? $('fdsc-topic-neg').value : '').trim(),
      negative_urls: ($('fdsc-topic-negurl') ? $('fdsc-topic-negurl').value : '').trim(),
      max_concurrency: _intv('fdsc-topic-conc', 4, 1, 16),
      entity_workers: _intv('fdsc-topic-workers', 3, 1, 8),
      auto_synthesize: !!($('fdsc-topic-autosyn') && $('fdsc-topic-autosyn').checked),
      synth_neighbor_depth: _intv('fdsc-topic-ndepth', 1, 0, 3),
      synth_infer_edges: !($('fdsc-topic-inferedges') && !$('fdsc-topic-inferedges').checked),
      consolidate_entities: !($('fdsc-topic-consol') && !$('fdsc-topic-consol').checked),
      drift_guard: !($('fdsc-topic-drift') && !$('fdsc-topic-drift').checked),
      llm_drift_gate: !!($('fdsc-topic-llmdrift') && $('fdsc-topic-llmdrift').checked),
      swallow_domains: !($('fdsc-topic-swallow') && !$('fdsc-topic-swallow').checked),
      unlimited_depth: !!($('fdsc-topic-nodepth') && $('fdsc-topic-nodepth').checked),
      topic_brief: !!($('fdsc-topic-brief') && $('fdsc-topic-brief').checked),
      crawl_id: cid
    };
    var to = Math.max(120000, body.max_pages * 5000);
    var res = await runCrawl(api('/fabric/discover/topic', 'POST', body, to), cid, 'Topic: ' + topic);
    if (res && res.queries) log('Searched ' + res.queries.length + ' angles' +
        (res.expansion_rounds_run ? ' + ' + res.expansion_rounds_run + ' concept rounds' : '') +
        '; ' + (res.seed_urls ? res.seed_urls.length : 0) + ' seeds', 'info');
    if (res && res.loom) log('Loom: ' + (res.loom.internal_links || 0) + ' internal, ' +
        (res.loom.cross_links || 0) + ' cross-dataset links', 'ok');
    status('fdsc-topic-status', (res && res.error) ? res.error : 'Done', (res && res.error) ? 'err' : 'ok');
  }
  async function topicContinue(){ await continueActive('fdsc-topic-status', parseInt($('fdsc-topic-pages').value || '80', 10)); }

  // ── URL crawl ──────────────────────────────────────────────────────────────
  async function urlGo(){
    var url = ($('fdsc-url').value || '').trim();
    if (!url){ status('fdsc-url-status', 'Enter a URL', 'err'); return; }
    status('fdsc-url-status', 'Crawling\u2026', '');
    var cid = newCrawlId();
    var body = {
      url: url, dataset_id: ($('fdsc-url-ds').value || '').trim(),
      topic: ($('fdsc-url-topic').value || '').trim(),
      max_pages: parseInt($('fdsc-url-pages').value || '60', 10),
      max_depth: parseInt($('fdsc-url-depth').value || '4', 10),
      max_concurrency: _intv('fdsc-url-conc', 4, 1, 16),
      entity_workers: _intv('fdsc-url-workers', 3, 1, 8),
      auto_synthesize: !!($('fdsc-url-autosyn') && $('fdsc-url-autosyn').checked),
      synth_neighbor_depth: _intv('fdsc-url-ndepth', 1, 0, 3),
      synth_infer_edges: !($('fdsc-url-inferedges') && !$('fdsc-url-inferedges').checked),
      consolidate_entities: !($('fdsc-url-consol') && !$('fdsc-url-consol').checked),
      drift_guard: !($('fdsc-url-drift') && !$('fdsc-url-drift').checked),
      llm_drift_gate: !!($('fdsc-url-llmdrift') && $('fdsc-url-llmdrift').checked),
      swallow_domains: !($('fdsc-url-swallow') && !$('fdsc-url-swallow').checked),
      unlimited_depth: !!($('fdsc-url-nodepth') && $('fdsc-url-nodepth').checked),
      topic_brief: !!($('fdsc-url-brief') && $('fdsc-url-brief').checked),
      same_domain: !!$('fdsc-url-same').checked,
      detect_surfaces: !!$('fdsc-url-surf').checked,
      extract_subtables: !!$('fdsc-url-sub').checked,
      extract_entities: !$('fdsc-url-noent') || !$('fdsc-url-noent').checked,
      auto_promote: !!$('fdsc-url-promote').checked,
      negative_words: ($('fdsc-url-neg') ? $('fdsc-url-neg').value : '').trim(),
      negative_urls: ($('fdsc-url-negurl') ? $('fdsc-url-negurl').value : '').trim(),
      crawl_id: cid
    };
    var to = Math.max(120000, body.max_pages * 5000);
    var res = await runCrawl(api('/fabric/discover/crawl', 'POST', body, to), cid, 'Crawl: ' + url);
    status('fdsc-url-status', (res && res.error) ? res.error : 'Done', (res && res.error) ? 'err' : 'ok');
  }
  async function urlContinue(){ await continueActive('fdsc-url-status', parseInt($('fdsc-url-pages').value || '60', 10)); }

  async function continueActive(statusId, addPages){
    if (!active.crawlId){ status(statusId, 'Select a crawl in History first', 'warn'); return; }
    status(statusId, 'Resuming\u2026', '');
    var to = Math.max(120000, (addPages || 60) * 5000);
    var res = await runCrawl(
      api('/fabric/discover/continue', 'POST', { crawl_id: active.crawlId, additional_pages: addPages || 60 }, to),
      active.crawlId, 'Resuming ' + active.crawlId);
    status(statusId, (res && res.error) ? res.error : (res && res.note) ? res.note : 'Resumed', (res && res.error) ? 'err' : 'ok');
  }

  function status(id, msg, type){ var e = $(id); if (!e) return; e.textContent = msg; e.className = 'fdsc-status' + (type ? ' ' + type : ''); }

  // ── History ──────────────────────────────────────────────────────────────
  async function loadHistory(silent){
    var listEl = $('fdsc-hist-list'); if (!listEl) return;
    if (!silent) status('fdsc-hist-status', 'Loading\u2026', '');
    var res = await api('/fabric/discover/history?limit=60');
    if (!res || res.error || !res.crawls || !res.crawls.length){
      listEl.innerHTML = '<span style="color:var(--dim,#8a8278);font-size:10px">' +
        (res && res.error ? esc(res.error) : 'No discovery crawls yet.') + '</span>';
      if (!silent) status('fdsc-hist-status', '', '');
      return;
    }
    lastCrawls = res.crawls;
    listEl.innerHTML = res.crawls.map(function(c){
      var sel = c.crawl_id === active.crawlId ? ' sel' : '';
      return '<div class="fdsc-item crawl' + sel + '" data-cid="' + esc(c.crawl_id) + '" data-ds="' + esc(c.dataset_id) + '">' +
        '<span class="fdsc-dot ' + esc(c.status) + '"></span>' +
        '<div class="mid">' +
          '<div class="t1">' + esc(c.topic || c.seed_url || c.dataset_id) + '</div>' +
          '<div class="t2">' + esc(c.dataset_id) + '</div>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:2px;align-items:flex-end">' +
          '<span class="fdsc-pill">' + (c.pages_fetched || 0) + 'p \u00b7 ' + (c.surfaces_found || 0) + 's \u00b7 ' + (c.subtables_found || 0) + 't</span>' +
          (c.queued ? '<span class="fdsc-pill" style="border-color:var(--acc3,#c9955a);color:var(--acc3,#c9955a)">' + c.queued + ' queued</span>' : '') +
        '</div>' +
        '<span class="fdsc-del" title="Delete this scan" data-del="' + esc(c.crawl_id) + '" data-ds="' + esc(c.dataset_id) + '" data-topic="' + esc(c.topic || c.dataset_id) + '" style="cursor:pointer;color:var(--dim,#8a8278);padding:0 4px;font-size:13px">\u2715</span>' +
      '</div>';
    }).join('');
    Array.prototype.forEach.call(listEl.querySelectorAll('.fdsc-item.crawl'), function(el){
      el.addEventListener('click', function(){ selectCrawl(el.getAttribute('data-cid'), el.getAttribute('data-ds')); });
    });
    Array.prototype.forEach.call(listEl.querySelectorAll('.fdsc-del'), function(b){
      b.addEventListener('click', function(ev){
        ev.stopPropagation();
        deleteScan(b.getAttribute('data-del'), b.getAttribute('data-ds'), b.getAttribute('data-topic'));
      });
    });
    if (!silent) status('fdsc-hist-status', res.crawls.length + ' crawls', '');
  }

  // Delete a scan; optionally purge its dataset (records + vectors + graph).
  async function deleteScan(cid, ds, topic){
    if (!cid) return;
    if (!window.confirm('Remove scan "' + (topic || cid) + '" from history?')) return;
    var withData = window.confirm(
      'Also DELETE the underlying dataset?\n\n' +
      'OK  = delete ALL fabric data for "' + (ds || '') + '" (records, vectors, entity graph)\n' +
      'Cancel = keep the data, only remove the scan record');
    status('fdsc-hist-status', 'Deleting\u2026', '');
    var r = await api('/fabric/discover/delete_scan', 'POST',
                      { crawl_id: cid, delete_dataset: withData });
    if (r && r.ok){
      log('\u2715 deleted scan ' + (topic || cid) + (withData ? ' + data' : ''), 'warn');
      if (active && active.crawlId === cid){ active = { crawlId:'', datasetId:'', running:false }; try{ saveStore(); }catch(_){} }
    } else {
      log('delete failed: ' + ((r && r.error) || '?'), 'err');
    }
    loadHistory(false);
  }

  // Clear all (or filtered) scan history.
  async function clearHistory(){
    if (!window.confirm('Clear ALL discovery scan history?')) return;
    var withData = window.confirm('Also DELETE all associated fabric datasets?\n\nOK = delete data too, Cancel = keep data');
    status('fdsc-hist-status', 'Clearing\u2026', '');
    var r = await api('/fabric/discover/clear_history', 'POST', { delete_data: withData });
    log('\u2715 cleared ' + ((r && r.scans_deleted) || 0) + ' scans' + (withData ? ' + data' : ''), 'warn');
    loadHistory(false);
  }

  async function selectCrawl(cid, ds){
    active.crawlId = cid; active.datasetId = ds || ''; active.running = false;
    Array.prototype.forEach.call($('fdsc-hist-list').querySelectorAll('.fdsc-item.crawl'), function(el){
      el.classList.toggle('sel', el.getAttribute('data-cid') === cid);
    });
    log('Loading map for ' + cid, 'info');
    overlay('Loading map\u2026', '', 'paused');
    var g = await api('/fabric/discover/graph?crawl_id=' + encodeURIComponent(cid) +
                      (ds ? '&dataset_id=' + encodeURIComponent(ds) : ''));
    if (g && !g.error){
      applyGraph(g, false);
      var st = g.stats || {};
      overlay('Loaded ' + cid, (st.pages || 0) + ' pages \u00b7 ' + (st.surfaces || 0) + ' surfaces \u00b7 ' +
              (st.subdatasets || 0) + ' sub-tables', 'paused');
      log('Map loaded: ' + (st.pages || 0) + ' pages, ' + (st.surfaces || 0) + ' surfaces, ' + (st.subdatasets || 0) + ' sub-tables', 'ok');
    } else {
      log('Map load failed: ' + ((g && g.error) || 'unknown'), 'err');
    }
    refreshSideLists();
    updateCurrentCrawl();
  }

  // ── Surfaces + sub-tables side lists ───────────────────────────────────────
  async function refreshSideLists(){
    var ds = active.datasetId;
    var surfEl = $('fdsc-surf-list'), subEl = $('fdsc-sub-list');
    if (ds){
      var sres = await api('/fabric/surfaces?parent_dataset=' + encodeURIComponent(ds) + '&limit=60');
      renderSurfaces(sres && sres.surfaces || []);
      var tres = await api('/fabric/subtables?parent_dataset=' + encodeURIComponent(ds) + '&limit=60');
      renderSubtables(tres && tres.subtables || []);
    }
  }
  function renderSurfaces(rows){
    var el = $('fdsc-surf-list'); var ct = $('fdsc-surf-ct'); if (!el) return;
    if (ct) ct.textContent = rows.length ? rows.length : '';
    if (!rows.length){ el.innerHTML = '<span style="color:var(--dim,#8a8278);font-size:10px">No surfaces yet.</span>'; return; }
    el.innerHTML = rows.map(function(s){
      var canPromote = s.source_type && !s.promoted && s.kind !== 'db';
      return '<div class="fdsc-item" data-sid="' + esc(s.id) + '">' +
        '<span class="gl">' + glyphFor(s.kind) + '</span>' +
        '<div class="mid"><div class="t1">' + esc(s.label || s.url) + '</div>' +
          '<div class="t2">' + esc(s.kind) + (s.source_type ? ' \u00b7 ' + esc(s.source_type) : '') +
            ' \u00b7 conf ' + (s.confidence != null ? Number(s.confidence).toFixed(2) : '?') +
            (s.topic_score ? ' \u00b7 topic ' + Number(s.topic_score).toFixed(2) : '') + '</div></div>' +
        (s.promoted ? '<span class="fdsc-pill" style="border-color:var(--ok,#6db87a);color:var(--ok,#6db87a)">promoted</span>'
          : canPromote ? '<button class="fdsc-btn sm" data-promote="' + esc(s.id) + '">promote</button>'
          : (s.kind === 'db' ? '<span class="fdsc-pill" title="needs credentials">db</span>' : '')) +
      '</div>';
    }).join('');
    Array.prototype.forEach.call(el.querySelectorAll('[data-promote]'), function(b){
      b.addEventListener('click', function(ev){ ev.stopPropagation(); promote(b.getAttribute('data-promote'), b); });
    });
  }
  function renderSubtables(rows){
    var el = $('fdsc-sub-list'); var ct = $('fdsc-sub-ct'); if (!el) return;
    if (ct) ct.textContent = rows.length ? rows.length : '';
    if (!rows.length){ el.innerHTML = '<span style="color:var(--dim,#8a8278);font-size:10px">No sub-tables yet.</span>'; return; }
    el.innerHTML = rows.map(function(t){
      var cols = Array.isArray(t.columns) ? t.columns : [];
      return '<div class="fdsc-item crawl" data-sub="' + esc(t.sub_dataset || '') + '" data-lbl="' + esc(t.title || t.kind) + '" title="click to browse rows">' +
        '<span class="gl">' + glyphFor(t.kind) + '</span>' +
        '<div class="mid"><div class="t1">' + esc(t.title || t.kind) + '</div>' +
          '<div class="t2">' + esc(t.kind) + ' \u00b7 ' + (t.row_count || 0) + ' rows' +
            (cols.length ? ' \u00b7 ' + esc(cols.slice(0, 4).join(', ')) : '') + '</div></div>' +
        '<button class="fdsc-btn sm" data-browse="' + esc(t.sub_dataset || '') + '">browse</button>' +
      '</div>';
    }).join('');
    Array.prototype.forEach.call(el.querySelectorAll('[data-browse]'), function(b){
      b.addEventListener('click', function(ev){ ev.stopPropagation();
        openBrowser(b.getAttribute('data-browse'), b.parentNode.getAttribute('data-lbl')); });
    });
    Array.prototype.forEach.call(el.querySelectorAll('.fdsc-item.crawl[data-sub]'), function(it){
      it.addEventListener('click', function(){ openBrowser(it.getAttribute('data-sub'), it.getAttribute('data-lbl')); });
    });
  }
  function glyphFor(kind){
    var m = { rss:'\u2934', sitemap:'\u25a6', github:'\u2325', gitlab:'\u2325', gitea:'\u2325',
              openapi:'\u2317', graphql:'\u25c8', json_api:'{ }', csv:'\u25a4', tsv:'\u25a4',
              json:'{ }', jsonl:'{ }', db:'\u25ad', table:'\u25a4', api_endpoints:'\u2317',
              cli_flags:'\u2014', definitions:'\u00b6' };
    return m[kind] || '\u25cb';
  }
  async function promote(sid, btn){
    if (btn){ btn.disabled = true; btn.textContent = '\u2026'; }
    var res = await api('/fabric/surfaces/promote', 'POST', { surface_id: sid, auto_pull: true }, 120000);
    if (res && (res.ok || res.source_id)){
      log('Promoted surface \u2192 source ' + (res.source_id || ''), 'ok');
    } else {
      log('Promote failed: ' + ((res && res.error) || 'unknown'), 'err');
      if (btn){ btn.disabled = false; btn.textContent = 'promote'; }
    }
    refreshSideLists();
  }

  // ── Tabs ───────────────────────────────────────────────────────────────────
  function _intv(id, def, lo, hi){
    var el = $(id); var v = parseInt((el && el.value) || def, 10);
    if (isNaN(v)) v = def;
    return Math.max(lo, Math.min(hi, v));
  }

  // ── 3rd-order topic models viewer ──────────────────────────────────────────
  async function loadModels(){
    var listEl = $('fdsc-models-list'); if (!listEl) return;
    status('fdsc-models-status', 'Loading\u2026', '');
    var r = await api('/fabric/synthesize/list', 'GET');
    var models = (r && r.models) || [];
    if (!models.length){
      listEl.innerHTML = '<div style="color:var(--dim,#8a8278);font-size:11px;padding:8px 2px">No topic models yet. Run a Dataset\u2019s \u201cSynthesize topic\u201d action to build one.</div>';
      status('fdsc-models-status', '0 models', ''); return;
    }
    listEl.innerHTML = models.map(function(m){
      return '<div class="fdsc-item model" data-mid="' + esc(m.id) + '" style="cursor:pointer;padding:7px 8px;border:1px solid var(--border,#3a3530);border-radius:3px;margin-bottom:5px">' +
        '<div style="display:flex;align-items:center;gap:6px">' +
        '<span style="flex:1;font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(m.topic || m.id) + '</span>' +
        '<span class="fdsc-del-model" title="Delete model" data-mid="' + esc(m.id) + '" style="cursor:pointer;color:var(--dim,#8a8278);padding:0 4px">\u2715</span></div>' +
        '<div style="font-size:10px;color:var(--dim,#8a8278);margin-top:2px">' +
        (m.entry_count || 0) + ' \u00d7 ' + esc(m.entry_type || 'entry') +
        ' \u00b7 ' + esc((m.created_at || '').slice(0, 10)) + '</div></div>';
    }).join('');
    Array.prototype.forEach.call(listEl.querySelectorAll('.fdsc-item.model'), function(el){
      el.addEventListener('click', function(){ openModel(el.getAttribute('data-mid')); });
    });
    Array.prototype.forEach.call(listEl.querySelectorAll('.fdsc-del-model'), function(b){
      b.addEventListener('click', function(ev){ ev.stopPropagation(); deleteModel(b.getAttribute('data-mid')); });
    });
    status('fdsc-models-status', models.length + ' models', '');
  }

  async function deleteModel(mid){
    if (!mid || !window.confirm('Delete this topic model?')) return;
    await api('/fabric/synthesize/delete', 'POST', { model_id: mid });
    var d = $('fdsc-models-detail'); if (d) d.style.display = 'none';
    loadModels();
  }

  function _kvHtml(obj){
    var keys = Object.keys(obj || {}); if (!keys.length) return '';
    return '<div style="margin-top:3px">' + keys.map(function(k){
      return '<span style="display:inline-block;font-size:10px;background:var(--bg2,#262320);border:1px solid var(--border,#3a3530);border-radius:3px;padding:1px 5px;margin:1px 3px 1px 0">' +
        esc(k) + ': ' + esc(String(obj[k])) + '</span>'; }).join('') + '</div>';
  }

  async function openModel(mid){
    var det = $('fdsc-models-detail'); if (!det) return;
    det.style.display = 'block';
    det.innerHTML = '<div style="color:var(--dim,#8a8278)">Loading model\u2026</div>';
    var r = await api('/fabric/synthesize/get', 'POST', { model_id: mid });
    if (!r || r.error){ det.innerHTML = '<div style="color:var(--err,#c96b6b)">' + esc((r && r.error) || 'error') + '</div>'; return; }
    var m = r.model || {}, entries = r.entries || [], rels = r.relations || [];
    var html = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<button class="fdsc-btn sm" id="fdsc-models-close">\u2190 Back</button>' +
      '<span style="font-size:15px;font-weight:700;flex:1">' + esc(m.topic || m.id) + '</span>' +
      '<span style="font-size:10px;color:var(--dim,#8a8278)">' + (m.entry_count || entries.length) + ' \u00d7 ' + esc(m.entry_type || 'entry') + '</span></div>';
    if (m.summary) html += '<div style="font-size:12px;line-height:1.5;color:var(--text,#ddd5c8);margin-bottom:8px;padding:7px 9px;background:var(--bg1,#201d1a);border-radius:4px">' + esc(m.summary) + '</div>';
    if (m.discovery_runs && m.discovery_runs.length) html += '<div style="font-size:10px;color:var(--dim,#8a8278);margin-bottom:8px">Gap-filling discovery: ' + m.discovery_runs.length + ' runs</div>';
    html += '<div style="font-size:11px;font-weight:600;margin:6px 0 4px;color:var(--acc,#5a9e8f)">Entries (' + entries.length + ')</div>';
    html += entries.map(function(e){
      var facts = (e.facts || []).map(function(f){ return '<li>' + esc(f) + '</li>'; }).join('');
      return '<div style="border:1px solid var(--border,#3a3530);border-radius:4px;padding:7px 9px;margin-bottom:5px">' +
        '<div><span style="font-weight:600;font-size:12px">' + esc(e.name) + '</span>' +
        ' <span style="font-size:9.5px;color:var(--acc2,#8fb87a)">[' + esc(e.type) + ']</span></div>' +
        _kvHtml(e.attributes) +
        (facts ? '<ul style="margin:4px 0 0;padding-left:16px;font-size:11px;color:var(--text,#ddd5c8)">' + facts + '</ul>' : '') +
        '</div>';
    }).join('');
    if (rels.length){
      html += '<div style="font-size:11px;font-weight:600;margin:10px 0 4px;color:var(--acc,#5a9e8f)">Relations (' + rels.length + ')</div>';
      html += '<div style="font-size:11px;color:var(--text,#ddd5c8)">' + rels.slice(0, 400).map(function(x){
        return esc(x.from) + ' \u2192 <b>' + esc(x.rel) + '</b> \u2192 ' + esc(x.to) +
          (x.why ? ' <span style="color:var(--dim,#8a8278)">(' + esc(x.why) + ')</span>' : ''); }).join('<br>') + '</div>';
    }
    det.innerHTML = html;
    var cb = $('fdsc-models-close'); if (cb) cb.addEventListener('click', function(){ det.style.display = 'none'; });
  }

  Array.prototype.forEach.call(root.querySelectorAll('.fdsc-tab'), function(tab){
    tab.addEventListener('click', function(){
      var pane = tab.getAttribute('data-pane');
      Array.prototype.forEach.call(root.querySelectorAll('.fdsc-tab'), function(t){ t.classList.toggle('active', t === tab); });
      Array.prototype.forEach.call(root.querySelectorAll('.fdsc-pane'), function(p){ p.classList.toggle('active', p.getAttribute('data-pane') === pane); });
      if (pane === 'history')  loadHistory(false);
      if (pane === 'models')   loadModels();
      if (pane === 'surfaces') refreshSideLists();
      if (pane === 'settings') { try { nerRefresh(); } catch(_){} }
    });
  });

  // ── Wire buttons ─────────────────────────────────────────────────────────
  (function(){ var cg = $('fdsc-current-go'); if (cg) cg.addEventListener('click', returnToCurrent); })();
  $('fdsc-topic-go').addEventListener('click', topicGo);
  $('fdsc-topic-cont').addEventListener('click', topicContinue);
  $('fdsc-url-go').addEventListener('click', urlGo);
  $('fdsc-url-cont').addEventListener('click', urlContinue);
  $('fdsc-hist-refresh').addEventListener('click', function(){ loadHistory(false); });
  (function(){ var b;
    if ((b=$('fdsc-models-refresh'))) b.addEventListener('click', loadModels);
    if ((b=$('fdsc-topic-map')))      b.addEventListener('click', mapTopic);
    if ((b=$('fdsc-hist-clear')))     b.addEventListener('click', clearHistory);
    if ((b=$('fdsc-ner-apply')))      b.addEventListener('click', nerApply);
    if ((b=$('fdsc-ner-refresh')))    b.addEventListener('click', nerRefresh);
    if ((b=$('fdsc-surf-refresh')))   b.addEventListener('click', refreshSideLists);
    // Terminal drawer controls (in the Settings tab)
    if ((b=$('fdsc-log-open')))  b.addEventListener('click', function(){
      var bd = _graphBd(); if (bd) bd.open('terminal');
    });
    if ((b=$('fdsc-log-clear'))) b.addEventListener('click', function(){
      var bd = _graphBd(); if (bd) bd.clearLog();
    });
    // Entity consolidation
    if ((b=$('fdsc-ner-consolidate'))) b.addEventListener('click', async function(){
      var st = $('fdsc-ner-consol-status');
      if (st){ st.textContent = 'Running\u2026'; st.className = 'fdsc-status'; }
      var ds = active && active.datasetId;
      var r = await api('/fabric/entity_graph/consolidate', 'POST', ds ? { dataset_id: ds } : {}, 120000);
      if (r && !r.error){
        var msg = 'Consolidated: merged ' + (r.merged || 0) + ', linked ' + (r.linked || 0);
        if (st){ st.textContent = msg; st.className = 'fdsc-status ok'; }
        log('\u29d6 ' + msg, 'ok');
      } else {
        if (st){ st.textContent = r && r.error || 'failed'; st.className = 'fdsc-status err'; }
      }
    });
  })();
  $('fdsc-topic').addEventListener('keydown', function(e){ if (e.key === 'Enter') topicGo(); });
  $('fdsc-url').addEventListener('keydown', function(e){ if (e.key === 'Enter') urlGo(); });

  // ── Table/record browser — uses graph bottom drawer ───────────────────────
  // Falls back to the floating modal if the graph drawer isn't available.
  async function openBrowser(ds, label){
    if (!ds) return;
    var bd = _graphBd();
    if (bd){
      // Show a loading state and fetch
      bd.showTable(['loading\u2026'], [['Fetching records for ' + (label || ds) + '\u2026']], label || ds);
      bd.open('table');
      await loadBrowserInDrawer(ds, label, '');
      return;
    }
    // Fallback: floating modal
    _openBrowserModal(ds, label);
  }

  async function loadBrowserInDrawer(ds, label, search){
    var bd = _graphBd(); if (!bd) return;
    var res = await api('/fabric/browse', 'POST', { dataset_id: ds, limit: 200, offset: 0, search: search || '' }, 30000);
    if (!res || res.error || !res.records || !res.records.length){
      bd.showTable(['status'], [[res && res.error ? res.error : 'No records found.']], label || ds);
      return;
    }
    // Collect columns (skip bulky text fields but keep short ones)
    var cols = []; var seen = {};
    res.records.forEach(function(r){
      var d = r.data || r;
      if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(err){ d = {}; } }
      Object.keys(d).forEach(function(k){
        if (!seen[k] && !k.startsWith('_') && k !== 'embedding'){
          seen[k] = 1; cols.push(k);
        }
      });
    });
    // Separate structural columns (short) from content columns (long)
    var shortCols = [], longCols = [];
    res.records.slice(0, 5).forEach(function(r){
      var d = r.data || r;
      if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(err){ d = {}; } }
      cols.forEach(function(k){
        var v = String(d[k] == null ? '' : d[k]);
        if (v.length > 200) longCols.push(k);
      });
    });
    longCols = Array.from(new Set(longCols));
    shortCols = cols.filter(function(c){ return longCols.indexOf(c) < 0; }).slice(0, 12);

    var rows = res.records.slice(0, 200).map(function(r){
      var d = r.data || r;
      if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(err){ d = {}; } }
      return shortCols.map(function(c){
        var v = d[c];
        if (v == null) return '';
        if (typeof v === 'object') return JSON.stringify(v).slice(0, 120);
        return String(v);
      });
    });
    bd.showTable(shortCols, rows, (label || ds) + ' \u2014 ' + (res.total || res.records.length) + ' records');

    // If there are long-form content fields, offer them in the content drawer too
    if (longCols.length && res.records.length){
      var firstRec = res.records[0];
      var d = firstRec.data || firstRec;
      if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(err){ d = {}; } }
      var contentField = longCols[0];
      var contentVal = String(d[contentField] || '');
      if (contentVal.length > 80){
        bd.showContent((label || ds) + ' \u2014 ' + contentField, contentVal);
        // Don't auto-switch to content — user can click that tab
      }
    }
  }

  async function _openBrowserModal(ds, label){
    var m = $('fdsc-modal');
    if (!m){
      m = document.createElement('div'); m.id = 'fdsc-modal'; m.className = 'fdsc-modal';
      m.innerHTML = '<div class="fdsc-modal-box"><div class="fdsc-modal-hd">' +
        '<span id="fdsc-modal-title"></span>' +
        '<input id="fdsc-modal-search" placeholder="filter\u2026" style="margin-left:auto;width:140px">' +
        '<button class="fdsc-btn sm" id="fdsc-modal-x">close</button></div>' +
        '<div class="fdsc-modal-body" id="fdsc-modal-body"></div></div>';
      root.appendChild(m);
      m.addEventListener('click', function(ev){ if (ev.target === m) m.style.display = 'none'; });
      $('fdsc-modal-x').addEventListener('click', function(){ m.style.display = 'none'; });
      var si = $('fdsc-modal-search');
      si.addEventListener('keydown', function(ev){ if (ev.key === 'Enter') _loadBrowserModal(m._ds, si.value); });
    }
    m._ds = ds;
    $('fdsc-modal-title').textContent = label || ds;
    $('fdsc-modal-search').value = '';
    m.style.display = 'flex';
    _loadBrowserModal(ds, '');
  }
  async function _loadBrowserModal(ds, search){
    var body = $('fdsc-modal-body'); if (!body) return;
    body.innerHTML = '<span style="color:var(--dim,#8a8278)">Loading\u2026</span>';
    var res = await api('/fabric/browse', 'POST', { dataset_id: ds, limit: 100, offset: 0, search: search || '' }, 30000);
    if (!res || res.error || !res.records || !res.records.length){
      body.innerHTML = '<span style="color:var(--dim,#8a8278)">' + (res && res.error ? esc(res.error) : 'No records.') + '</span>'; return;
    }
    var cols = []; var seen = {};
    res.records.forEach(function(r){
      var d = r.data || r; if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(e){ d = {}; } }
      Object.keys(d).forEach(function(k){ if (!seen[k] && k !== 'text' && !k.startsWith('_')){ seen[k] = 1; cols.push(k); } });
    });
    cols = cols.slice(0, 8);
    var html = '<div style="font-size:9.5px;color:var(--dim,#8a8278);margin-bottom:6px">' + (res.total || res.records.length) + ' records</div>';
    html += '<table class="fdsc-tbl"><thead><tr>' + cols.map(function(c){ return '<th>' + esc(c) + '</th>'; }).join('') + '</tr></thead><tbody>';
    res.records.forEach(function(r){
      var d = r.data || r; if (typeof d === 'string'){ try { d = JSON.parse(d); } catch(e){ d = {}; } }
      html += '<tr>' + cols.map(function(c){
        var v = d[c]; if (v && typeof v === 'object') v = JSON.stringify(v);
        return '<td>' + esc(String(v == null ? '' : v)).slice(0, 160) + '</td>';
      }).join('') + '</tr>';
    });
    html += '</tbody></table>';
    body.innerHTML = html;
  }
  window._fdscBrowse = openBrowser;   // also callable from side-list

  // ── React to harness-relayed live events ──────────────────────────────────
  window.addEventListener('message', function(ev){
    try {
      if (!ev.data || ev.data.type !== 'vera_fabric_event') return;
      var e = ev.data.event; if (!e) return;
      var t = e.type || '';
      if (t === 'fabric.discover.progress'){
        var s = e.stage || '';
        var rel  = (e.relevance != null) ? (' rel=' + (+e.relevance).toFixed(2)) : '';
        var dep  = (e.depth != null) ? (' d=' + e.depth) : '';
        var auth = (e.authority != null) ? (' auth=' + (+e.authority).toFixed(2)) : '';
        if (s === 'starting')          log('\u25b6 crawl start: ' + (e.url||'') + ' max=' + (e.max_pages||'?') + ' depth=' + (e.max_depth||'?') + (e.resumed?' (resumed)':''), 'ok');
        else if (s === 'map_start')    log('\u25b6 ' + (e.message || 'mapping topic'), 'ok');
        else if (s === 'seeding')      log('\u2197 seeding' + (e.queries ? ' \u00b7 ' + e.queries.length + ' angles' : '') + (e.message ? ': ' + e.message : ''), 'info');
        else if (s === 'expanding')    log('\u21bb concept expansion round ' + (e.round||'?') + (e.concepts && e.concepts.length ? ' \u00b7 ' + e.concepts.slice(0,4).join(', ') : ''), 'info');
        else if (s === 'page_fetching') log('\u2026 fetch ' + ((e.url||'').slice(0,100)) + dep, 'dim');
        else if (s === 'content_extracted') log('\u2261 ' + (e.chars||0) + ' chars \u00b7 ' + (e.links||0) + ' links \u2014 ' + ((e.url||'').slice(0,70)), 'dim');
        else if (s === 'llm_action')   log('\u2699 LLM ' + (e.action||'') + (e.url ? ' @ ' + e.url.slice(0,60) : '') + (e.message ? ' \u2014 ' + e.message : ''), 'acc');
        else if (s === 'page_added')   log('\u2795 ' + ((e.title || e.url || '').slice(0,80)) + rel + dep + auth + (e.usefulness!=null?' use='+(+e.usefulness).toFixed(2):'') + (e.source_type?' ['+e.source_type+']':'') + (e.entities_queued?' \u00b7 '+e.entities_queued+' ents queued':''), 'info');
        else if (s === 'page_skipped') log('\u2296 skip ' + ((e.url||'').slice(0,80)) + ' \u2014 ' + (e.reason||'') + rel, 'dim');
        else if (s === 'entity_found') log('\u2299 ' + (e.count||0) + ' entities' + (e.backend ? ' [' + e.backend + ']' : '') + (e.names ? ': ' + e.names.slice(0,8).join(', ') : '') + (e.url ? ' @ ' + (e.url||'').slice(0,50) : ''), 'info');
        else if (s === 'surface_detected') log('\u25c6 surface [' + (e.kind||'') + '] ' + ((e.label||e.surface_url||'').slice(0,80)) + (e.confidence?' conf='+(+e.confidence).toFixed(2):''), 'ok');
        else if (s === 'subtable_added' || s === 'data_detected') log('\u25a6 data [' + (e.kind||'') + '] \u2192 ' + (e.dataset_id||e.sub_dataset||'') + (e.rows?' \u00b7 '+e.rows+' rows':''), 'ok');
        else if (s === 'repetition_dropoff') log('\u25a0 stopped: repetition/saturation at ' + (e.pages||0) + ' pages', 'warn');
        else if (s === 'topic_description'){ log('\u2263 topic: ' + (e.description||'').slice(0,120), 'ok'); setTopicDesc(e.description||'', !!e.final); }
        else if (s === 'progress')     log('\u2026 ' + (e.pages||0) + 'p \u00b7 ' + (e.queued||0) + ' queued \u00b7 ' + (e.surfaces||0) + ' surfaces' + (e.concurrency ? ' \u00b7 ' + e.concurrency + '\u00d7' : '') + (e.entities_found ? ' \u00b7 ' + e.entities_found + ' ents' : ''), 'dim');
        else if (s === 'scanning')     log('\u21c9 parallel scan: in-flight=' + (e.in_flight||0) + ' queued=' + (e.queued||0), 'acc');
        else if (s === 'consolidated') log('\u29d6 consolidated: merged ' + (e.merged||0) + ' duplicate entities', 'ok');
        else if (s === 'table_entities_queued') log('\u25a6 table entities queued: ' + (e.count||0), 'dim');
        else if (s === 'brief')        log('\u270e research brief built' + (e.topic ? ': ' + e.topic.slice(0,80) : ''), 'acc');
        else if (s === 'brief_refined') log('\u270e brief refined (round ' + (e.round||'?') + ')', 'acc');
        else if (s === 'git_paths')    log('\u2387 git: ' + (e.message || 'enumerating repos for ' + (e.domain||'')), 'info');
        else if (s === 'domain_rich')  log('\u25c9 rich source: ' + (e.domain||'') + (e.score?' score='+(+e.score).toFixed(2):''), 'ok');
        else if (s === 'domain_dropoff') log('\u2198 drop-off: ' + (e.domain||'') + (e.pages_from_domain?' after '+e.pages_from_domain+'p':''), 'dim');
        else if (s === 'consolidating') log('\u29d6 consolidating entities\u2026', 'acc');
        else if (s === 'synthesizing') log('\u25c8 auto-synthesizing 3rd-order model\u2026', 'acc');
        else if (s === 'synthesized')  log('\u2713 3rd-order model ready' + (e.entries ? ' \u00b7 ' + e.entries + ' entries' : ''), 'ok');
        else if (s === 'synthesize_error') log('\u26a0 synthesis failed: ' + (e.error||e.message||'unknown'), 'err');
        else if (s === 'loom')         log('\u29d6 loom: ' + (e.message||'stitching relations'), 'acc');
        else if (s === 'seeded')       log('\u2713 seeded: ' + (e.message||''), 'ok');
        else if (s === 'done')         log('\u2713 done \u2014 ' + (e.pages||0) + 'p \u00b7 ' + (e.surfaces||0) + ' surfaces \u00b7 ' + (e.subtables||0) + ' subtables \u00b7 ' + (e.entities||0) + ' entities', 'ok');
        else if (e.message)            log(e.message, 'info');
        if (s === 'page_added' || s === 'surface_detected' || s === 'subtable_added' || s === 'done') pollOnce();
        if (s === 'synthesized') { try { loadModels(); } catch(e2){} }
      } else if (t === 'fabric.discover.surface' || t === 'fabric.discover.subtable'){
        pollOnce();
      } else if (t === 'fabric.entity_graph.progress'){
        var es = e.stage || '';
        var bk = e.backend ? ' [' + e.backend + ']' : '';
        if (es === 'extracting')      log('\u2299 extracting from ' + (e.count||0) + ' records' + bk + (e.use_llm ? ' + LLM' : ''), 'info');
        else if (es === 'extracted')  log('\u2713 extracted: ' + (e.total||0) + ' entities (' + (e.new_entities||0) + ' new)' + bk, 'ok');
        else if (es === 'ner_batch')  log('\u2299 NER batch ' + (e.batch||0) + '/' + (e.total_batches||'?') + bk + (e.entities_found?' \u00b7 '+e.entities_found+' found':''), 'dim');
        else if (es === 'aliased')    log('\u29c9 alias merge: ' + (e.merged||0) + ' (' + (e.before||0) + '\u2192' + (e.after||0) + ')', 'ok');
        else if (es === 'steering')   log('\u2699 LLM steering round ' + (e.round||'') + '\u2026', 'acc');
        else if (es === 'steered')    log('\u2699 steered: +' + (e.added||0) + ' \u2212' + (e.dropped||0) + ' \u2715' + (e.merged||0) + ' \u21c6' + (e.retyped||0), 'ok');
        else if (es === 'profiling')  log('\u2699 profiling ' + (e.count||0) + ' entities (LLM)\u2026', 'acc');
        else if (es === 'profiled')   log('\u2713 profiled ' + (e.count||0) + ' entities', 'ok');
        else if (es === 'relating')   log('\u2699 inferring relationships (' + (e.pairs||0) + ' pairs)\u2026', 'acc');
        else if (es === 'related')    log('\u2713 +' + (e.added||0) + ' relationships', 'ok');
        else if (es === 'describing') log('\u2699 describing weak edges (' + (e.count||0) + ')\u2026', 'acc');
        else if (es === 'described')  log('\u2713 described ' + (e.count||0) + ' weak edges', 'ok');
        else if (es === 'gliner_load') log('\u2699 loading GLiNER model\u2026 (first run may take a moment)', 'warn');
        else if (es === 'gliner_ready') log('\u2713 GLiNER ready' + (e.model?' \u00b7 '+e.model:''), 'ok');
        else if (es === 'spacy_load') log('\u2699 loading spaCy model\u2026', 'warn');
        else if (es === 'spacy_ready') log('\u2713 spaCy ready' + (e.model?' \u00b7 '+e.model:''), 'ok');
        else if (es === 'consolidating') log('\u29d6 consolidating entity graph\u2026', 'acc');
        else if (es === 'consolidated') log('\u29d6 consolidated: merged ' + (e.merged||0) + (e.linked?', +'+e.linked+' relations':''), 'ok');
        else if (es === 'persisting') log('\u25bc persisting ' + (e.count||0) + ' entities\u2026', 'dim');
        else if (es === 'done')       log('\u2713 entity extraction done \u2014 ' + (e.entities||0) + ' entities, ' + (e.relations||0) + ' relations (persisted ' + (e.persisted||0) + ')' + bk, 'ok');
        else if (e.message)           log('\u2699 entity: ' + e.message, 'acc');
      } else if (t === 'fabric.synthesize.progress'){
        var ss = e.stage || '';
        if (ss === 'start')            log('\u25c8 synthesising: ' + (e.topic||e.message||''), 'ok');
        else if (ss === 'loaded')      log('\u25c8 scope: ' + (e.entities||0) + ' entities, ' + (e.relations||0) + ' relations', 'info');
        else if (ss === 'planning')    log('\u2699 planning structure (LLM)\u2026', 'acc');
        else if (ss === 'planned')     log('\u25c8 planned: ' + (e.entry_type||'') + ' \u00b7 ' + (e.expected||0) + ' expected entries', 'ok');
        else if (ss === 'discovering') log('\u2197 gap-filling discovery' + (e.queries?' \u00b7 '+e.queries.length+' queries':'') + (e.message?': '+e.message:''), 'acc');
        else if (ss === 'coverage')    log('\u25d0 coverage: ' + (e.message||'updating'), 'info');
        else if (ss === 'synthesising') log('\u2699 distilling entries (' + (e.done||0) + '/' + (e.total||0) + ')\u2026', 'acc');
        else if (ss === 'auto')        log('\u25c8 auto-synthesis: ' + (e.message||''), 'ok');
        else if (ss === 'inferring_edges') log('\u2699 inferring complex relations\u2026', 'acc');
        else if (ss === 'inferred_edges') log('\u2713 +' + (e.count||0) + ' complex edges', 'ok');
        else if (ss === 'summarised')  log('\u25c8 overview written \u00b7 ' + (e.entries||0) + ' entries', 'info');
        else if (ss === 'done')        log('\u2713 3rd-order model \u2014 ' + (e.entries||0) + ' entries, ' + (e.relations||0) + ' relations (' + (e.inferred||0) + ' inferred), ' + (e.discovery_runs||0) + ' discovery runs', 'ok');
        else if (e.message)            log('\u25c8 ' + e.message, 'acc');
        if (ss === 'done') pollOnce();
      } else if (t === 'fabric.loom.progress'){
        var ls = e.stage || '';
        if (ls === 'linking')     log('\u29d6 loom linking: ' + (e.message||'batch ' + (e.batch||0)), 'acc');
        else if (ls === 'done')   log('\u2713 loom: +' + (e.internal||0) + ' internal, +' + (e.cross||0) + ' cross-dataset links', 'ok');
        else if (e.message)       log('\u29d6 loom: ' + e.message, 'acc');
      }
    } catch(_){}
  });
  window.addEventListener('message', function(ev){
    try {
      if (!ev.data || ev.data.type !== 'vera_fabric_event') return;
      var e = ev.data.event; if (!e) return;
      var t = e.type || '';
      if (t === 'fabric.discover.progress'){
        var s = e.stage || '';
        var rel = (e.relevance != null) ? (' rel ' + (+e.relevance).toFixed(2)) : '';
        var dep = (e.depth != null) ? (' d' + e.depth) : '';
        if (s === 'map_start')            log('\u25b6 ' + (e.message || 'mapping topic'), 'ok');
        else if (s === 'seeding')         log('\u2197 ' + (e.message || 'seeding') + (e.queries ? ' (' + e.queries.length + ' queries)' : ''), 'info');
        else if (s === 'expanding')       log('\u21bb ' + (e.message || 'expanding search'), 'info');
        else if (s === 'page_fetching')   log('\u2026 ' + (e.message || ('fetch ' + ((e.url||'').slice(0,90)))), 'dim');
        else if (s === 'content_extracted') log('\u2261 ' + (e.message || ('extracted ' + (e.chars||0) + ' chars')), 'dim');
        else if (s === 'llm_action')      log('\u2699 ' + (e.message || ('LLM ' + (e.action||''))), 'acc');
        else if (s === 'page_added')      log('+ ' + ((e.title || e.url || '').slice(0,90)) + rel + dep + (e.usefulness!=null?(' u'+(+e.usefulness).toFixed(2)):'') + (e.authority!=null?(' a'+(+e.authority).toFixed(2)):'') + (e.source_type?(' ['+e.source_type+']'):''), 'info');
        else if (s === 'page_skipped')    log('\u2298 skip ' + ((e.url||'').slice(0,70)) + ' (' + (e.reason||'') + ')', 'dim');
        else if (s === 'entity_found')    log('\u2299 ' + (e.count||0) + ' entities' + (e.names ? ': ' + e.names.slice(0,6).join(', ') : '') + ((e.url)?(' @ '+(e.url||'').slice(0,50)):''), 'info');
        else if (s === 'surface_detected')log('\u25c6 surface [' + (e.kind||'') + '] ' + ((e.label||e.surface_url||'').slice(0,80)), 'ok');
        else if (s === 'subtable_added' || s === 'data_detected') log('\u25a6 data [' + (e.kind||'') + '] \u2192 ' + (e.dataset_id||e.sub_dataset||''), 'ok');
        else if (s === 'repetition_dropoff') log('\u25a0 ' + (e.message || 'stopped: repetition'), 'warn');
        else if (s === 'topic_description'){ log('\u2263 topic: ' + (e.description||''), 'ok'); setTopicDesc(e.description||'', !!e.final); }
        else if (s === 'progress')        log('\u2026 ' + (e.pages||0) + ' pages, ' + (e.queued||0) + ' queued, ' + (e.surfaces||0) + ' surfaces' + (e.concurrency ? ' \u00b7 ' + e.concurrency + '\u00d7 parallel' : ''), 'dim');
        else if (s === 'scanning')        log('\u21c9 ' + (e.message || 'scanning in parallel'), 'acc');
        else if (s === 'consolidated')    log('\u29d6 ' + (e.message || ('merged ' + (e.merged||0) + ' duplicate entities')), 'ok');
        else if (s === 'table_entities_queued') log('\u25a6 ' + (e.message || 'table entities queued'), 'dim');
        else if (s === 'brief')           log('\u270e ' + (e.message || 'research brief built'), 'acc');
        else if (s === 'brief_refined')   log('\u270e ' + (e.message || 'brief refined'), 'acc');
        else if (s === 'git_paths')       log('\u2387 ' + (e.message || 'enumerating git repos'), 'info');
        else if (s === 'domain_rich')     log('\u25c9 ' + (e.message || ('rich source: '+(e.domain||''))), 'ok');
        else if (s === 'domain_dropoff')  log('\u2198 ' + (e.message || ('drop-off: '+(e.domain||''))), 'dim');
        else if (s === 'consolidating')   log('\u29d6 ' + (e.message || 'consolidating\u2026'), 'acc');
        else if (s === 'synthesizing')    log('\u25c8 ' + (e.message || 'auto-synthesizing 3rd-order\u2026'), 'acc');
        else if (s === 'synthesized')     log('\u2713 ' + (e.message || '3rd-order model ready'), 'ok');
        else if (s === 'synthesize_error') log('\u26a0 ' + (e.message || 'synthesis failed'), 'err');
        else if (s === 'done')            log('\u2713 done: ' + (e.pages||0) + ' pages, ' + (e.surfaces||0) + ' surfaces, ' + (e.subtables||0) + ' subtables, ' + (e.entities||0) + ' entities', 'ok');
        else if (e.message)               log(e.message, 'info');
        if (s === 'page_added' || s === 'surface_detected' || s === 'subtable_added' || s === 'done') pollOnce();
        if (s === 'synthesized') { try { loadModels(); } catch(e){} }
      } else if (t === 'fabric.discover.surface' || t === 'fabric.discover.subtable'){
        pollOnce();
      } else if (t === 'fabric.entity_graph.progress'){
        var es = e.stage || '';
        if (es === 'extracting')      log('\u2299 extracting entities from ' + (e.count||0) + ' records' + (e.use_llm ? ' (+LLM)' : ''), 'info');
        else if (es === 'aliased')    log('\u29c9 merged ' + (e.merged||0) + ' duplicate/alias entities (' + (e.before||0) + '\u2192' + (e.after||0) + ')', 'ok');
        else if (es === 'steering')   log('\u2699 LLM steering round ' + (e.round||'') + '\u2026', 'acc');
        else if (es === 'steered')    log('\u2699 steered: +' + (e.added||0) + ' \u2212' + (e.dropped||0) + ' merged ' + (e.merged||0) + ' retyped ' + (e.retyped||0), 'ok');
        else if (es === 'profiling')  log('\u2699 LLM building entity profiles\u2026', 'acc');
        else if (es === 'profiled')   log('\u2713 profiled ' + (e.count||0) + ' entities', 'ok');
        else if (es === 'relating')   log('\u2699 LLM inferring inter-entity relationships\u2026', 'acc');
        else if (es === 'related')    log('\u2713 +' + (e.added||0) + ' secondary relationships', 'ok');
        else if (es === 'describing') log('\u2699 LLM describing weak edges\u2026', 'acc');
        else if (es === 'described')  log('\u2713 described ' + (e.count||0) + ' weak edges', 'ok');
        else if (es === 'consolidating') log('\u29d6 ' + (e.message || 'consolidating entity graph\u2026'), 'acc');
        else if (es === 'consolidated')  log('\u29d6 ' + (e.message || ('merged ' + (e.merged||0) + ' duplicates')) + ((e.linked) ? (', +' + e.linked + ' relations') : ''), 'ok');
        else if (es === 'done')       log('\u2713 entities: ' + (e.entities||0) + ', relations: ' + (e.relations||0) + ' (persisted ' + (e.persisted||0) + ')', 'ok');
        else if (e.message)           log('\u2699 ' + e.message, 'acc');
      } else if (t === 'fabric.synthesize.progress'){
        var ss = e.stage || '';
        if (ss === 'start')            log('\u25c8 ' + (e.message || 'synthesising topic'), 'ok');
        else if (ss === 'loaded')      log('\u25c8 ' + (e.message || 'loaded scope'), 'info');
        else if (ss === 'planning')    log('\u2699 ' + (e.message || 'planning structure'), 'acc');
        else if (ss === 'planned')     log('\u25c8 ' + (e.message || 'planned'), 'ok');
        else if (ss === 'discovering') log('\u2197 ' + (e.message || 'gap-filling discovery') + (e.queries ? ' (' + e.queries.length + ')' : ''), 'acc');
        else if (ss === 'coverage')    log('\u25d0 ' + (e.message || 'coverage update'), 'info');
        else if (ss === 'synthesising')log('\u2699 ' + (e.message || 'distilling entries'), 'acc');
        else if (ss === 'auto')        log('\u25c8 ' + (e.message || 'auto-synthesis starting'), 'ok');
        else if (ss === 'inferring_edges') log('\u2699 ' + (e.message || 'mapping complex/distant relations'), 'acc');
        else if (ss === 'inferred_edges')  log('\u2713 +' + (e.count||0) + ' complex/distant edges', 'ok');
        else if (ss === 'summarised')  log('\u25c8 overview written (' + (e.entries||0) + ' entries)', 'info');
        else if (ss === 'done')        log('\u2713 3rd-order model: ' + (e.entries||0) + ' entries, ' + (e.relations||0) + ' relations (' + (e.inferred||0) + ' inferred), ' + (e.discovery_runs||0) + ' discovery runs', 'ok');
        else if (e.message)            log('\u25c8 ' + e.message, 'acc');
        if (ss === 'done') pollOnce();
      }
    } catch(_){}
  });

  // ── Blank-on-tab fix ──────────────────────────────────────────────────────
  // When the panel is hidden then re-shown (tab change), the canvas can be
  // cleared and the layout tick parked. Watch visibility and, on return, wake
  // the graph, re-fit, and re-apply the stored graph if the canvas lost it.
  function wakeAndRedraw(){
    var g = graph; if (!g) return;
    try { if (g.wake) g.wake(); } catch(e){}
    var live = (g.state && g.state.nodes) ? g.state.nodes.length : 0;
    if (!live && lastGraph.nodes.length){ applyGraph(lastGraph, false); }
    else if (g.draw){ try { g.draw(); } catch(e){} }
  }
  function watchVisibility(){
    var host = $('fdsc-graph-host'); if (!host) return;
    if (window.IntersectionObserver && !host._fdscIO){
      host._fdscIO = new IntersectionObserver(function(entries){
        entries.forEach(function(en){ if (en.isIntersecting) setTimeout(wakeAndRedraw, 60); });
      }, { threshold: 0.05 });
      host._fdscIO.observe(host);
    }
    document.addEventListener('visibilitychange', function(){
      if (!document.hidden) setTimeout(wakeAndRedraw, 80);
    });
  }

  // ── Boot ─────────────────────────────────────────────────────────────────
  // Populate the history card immediately (DOM only — not gated on the graph lib).
  loadHistory(true).then(updateCurrentCrawl);
  ensureGraphLib(function(){
    extendPalette(); ensureGraph(); watchVisibility();
    var restored = loadStore();
    if (restored){ applyGraph(restored, false); log('Restored graph (' + restored.nodes.length + ' nodes)', 'info'); }
    if (active && active.crawlId && active.running) startPolling();
    // Once the graph is ready, if nothing is showing, load the active or most
    // recent crawl so the map + history are populated on a fresh start.
    if (!restored && !(active && active.running)){
      loadHistory(true).then(function(){ updateCurrentCrawl(); autoLoadInitial(); });
    }
  });
  log('Discover+ ready', 'ok');
  try { nerRefresh(); } catch(_){}
})();
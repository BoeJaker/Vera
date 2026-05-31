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

  // ── Logging + overlay ──────────────────────────────────────────────────────
  var logEl = $('fdsc-log');
  var logLines = [];
  function log(msg, type){
    var c = type === 'ok' ? 'var(--ok,#6db87a)' : type === 'err' ? 'var(--err,#c96b6b)'
          : type === 'warn' ? 'var(--acc3,#c9955a)' : 'var(--dim,#8a8278)';
    var t = new Date().toLocaleTimeString();
    logLines.push('<span style="color:' + c + '">[' + t + '] ' + esc(msg) + '</span>');
    if (logLines.length > 240) logLines = logLines.slice(-140);
    if (logEl) { logEl.innerHTML = logLines.join('<br>'); logEl.scrollTop = logEl.scrollHeight; }
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
      // edge colours for our relationships
      C._edge_HAS_PAGE       = C._edge_HAS_PAGE       || 'rgba(107,155,210,0.55)';
      C._edge_HAS_SURFACE    = C._edge_HAS_SURFACE    || 'rgba(201,149,90,0.7)';
      C._edge_HAS_SUBTABLE   = C._edge_HAS_SUBTABLE   || 'rgba(94,201,160,0.7)';
      C._edge_HAS_DATA_SUBSET= C._edge_HAS_DATA_SUBSET|| 'rgba(94,201,160,0.6)';
      C._edge_MENTIONS       = C._edge_MENTIONS       || 'rgba(201,122,90,0.6)';
    } catch(e){}
  }
  function ensureGraph(){
    if (graph) return graph;
    if (!(window.veraUI && window.veraUI.Graph)) return null;
    var host = $('fdsc-graph-host'); if (!host) return null;
    extendPalette();
    graph = window.veraUI.Graph.create(host, {
      height: 'fill', showSearch: true, showLegend: true, showLayerToggle: false,
      apiBase: API, actionsEnabled: true,
      onAction: function(action, node){
        if ((action === 'open_record' || action === 'browse' || action === 'browse-dataset')){
          var ds = (node && node.props && node.props.dataset_id) || (node && node.type === 'Dataset' && node.id) || '';
          try { if (ds && window.parent && window.parent.fabSelectDs) window.parent.fabSelectDs(ds); } catch(e){}
          return false;
        }
        if (action === 'open_url'){
          var u = (node && node.props && node.props.url) || (node && node.id) || '';
          if (u && /^https?:/.test(u)) window.open(u, '_blank');
          return false;
        }
        // everything else (add_source / pull_expand / expand_links / extract_entities /
        // forget) goes to the server-side node-action runner; we merge any
        // returned {nodes,edges} delta in onActionDone below.
      },
      onActionDone: function(action, node, result){
        var payload = result && result.result;   // run_node_action wraps as {ok,result}
        if (payload && (payload.nodes || payload.edges)){
          applyGraph(payload, true);
          log((payload.note || 'Expanded ' + (node && node.label || node && node.id)) +
              '  (+' + ((payload.nodes || []).length) + ' nodes)', 'ok');
        } else if (result && result.result && result.result.ok && result.result.source_id){
          log('Added as source: ' + result.result.source_id, 'ok');
        } else if (result && result.error){
          log('Action failed: ' + result.error, 'err');
        }
        refreshSideLists();
      }
    });
    return graph;
  }
  function graphReset(){
    seenNodes = {}; seenEdges = {};
    var g = ensureGraph(); if (g) g.clear();
  }
  function applyGraph(payload, incremental){
    var g = ensureGraph(); if (!g || !payload) return;
    if (!incremental){ g.clear(); seenNodes = {}; seenEdges = {}; }
    (payload.nodes || []).forEach(function(n){
      if (seenNodes[n.id]){
        var ex = g.getNode(n.id);
        if (ex){
          // a node first seen as a bare edge endpoint may have had only its id
          // as a label; refresh once richer data arrives
          if (n.label && ex.label !== n.label) ex.label = n.label;
          if (n.props){ ex.props = Object.assign(ex.props || {}, n.props); }
          if (n.type && ex.type !== n.type) ex.type = n.type;
        }
        return;
      }
      seenNodes[n.id] = true;
      var added = g.addNode({ id: n.id, label: n.label || n.id, type: n.type || 'Page', props: n.props || {} });
      if (added && incremental && g.pulseNode) g.pulseNode(n.id);
    });
    (payload.edges || []).forEach(function(e){
      var k = e.from + '|' + e.to + '|' + e.rel;
      if (seenEdges[k]) return;
      seenEdges[k] = true;
      g.addEdge({ from: e.from, to: e.to, rel: e.rel || 'LINKS_TO' });
    });
    if (g.draw) try { g.draw(); } catch(_){}
  }

  // ── State ──────────────────────────────────────────────────────────────────
  var active = { crawlId: '', datasetId: '', running: false };
  var pollTimer = null;
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
  }
  function startPolling(){
    stopPolling();
    pollTimer = setInterval(function(){ pollOnce(); refreshSideLists(); }, 2600);
  }
  function stopPolling(){ if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

  // Generic "run a crawl request while polling its graph live"
  async function runCrawl(reqPromise, crawlId, label){
    active.crawlId = crawlId; active.datasetId = ''; active.running = true;
    graphReset();
    overlay(label || 'Crawling\u2026', '', 'running');
    log(label || ('Starting ' + crawlId), 'ok');
    setTimeout(pollOnce, 700);      // first quick poll
    startPolling();
    var res = await reqPromise;
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
      extract_entities: !$('fdsc-topic-noent') || !$('fdsc-topic-noent').checked,
      negative_words: ($('fdsc-topic-neg') ? $('fdsc-topic-neg').value : '').trim(),
      negative_urls: ($('fdsc-topic-negurl') ? $('fdsc-topic-negurl').value : '').trim(),
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
      '</div>';
    }).join('');
    Array.prototype.forEach.call(listEl.querySelectorAll('.fdsc-item.crawl'), function(el){
      el.addEventListener('click', function(){ selectCrawl(el.getAttribute('data-cid'), el.getAttribute('data-ds')); });
    });
    if (!silent) status('fdsc-hist-status', res.crawls.length + ' crawls', '');
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
      return '<div class="fdsc-item">' +
        '<span class="gl">' + glyphFor(t.kind) + '</span>' +
        '<div class="mid"><div class="t1">' + esc(t.title || t.kind) + '</div>' +
          '<div class="t2">' + esc(t.kind) + ' \u00b7 ' + (t.row_count || 0) + ' rows' +
            (cols.length ? ' \u00b7 ' + esc(cols.slice(0, 4).join(', ')) : '') + '</div></div>' +
      '</div>';
    }).join('');
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
  Array.prototype.forEach.call(root.querySelectorAll('.fdsc-tab'), function(tab){
    tab.addEventListener('click', function(){
      var pane = tab.getAttribute('data-pane');
      Array.prototype.forEach.call(root.querySelectorAll('.fdsc-tab'), function(t){ t.classList.toggle('active', t === tab); });
      Array.prototype.forEach.call(root.querySelectorAll('.fdsc-pane'), function(p){ p.classList.toggle('active', p.getAttribute('data-pane') === pane); });
      if (pane === 'history') loadHistory(false);
    });
  });

  // ── Wire buttons ─────────────────────────────────────────────────────────
  $('fdsc-topic-go').addEventListener('click', topicGo);
  $('fdsc-topic-cont').addEventListener('click', topicContinue);
  $('fdsc-url-go').addEventListener('click', urlGo);
  $('fdsc-url-cont').addEventListener('click', urlContinue);
  $('fdsc-hist-refresh').addEventListener('click', function(){ loadHistory(false); });
  $('fdsc-topic').addEventListener('keydown', function(e){ if (e.key === 'Enter') topicGo(); });
  $('fdsc-url').addEventListener('keydown', function(e){ if (e.key === 'Enter') urlGo(); });

  // ── Bonus: react to harness-relayed live events for snappier updates ───────
  window.addEventListener('message', function(ev){
    try {
      if (!ev.data || ev.data.type !== 'vera_fabric_event') return;
      var e = ev.data.event; if (!e) return;
      var t = e.type || '';
      if (t === 'fabric.web.acquire.progress' && e.engine === 'discovery' && active.running){
        if (e.stage === 'page_added' && e.title) log('+ ' + e.title, 'info');
        else if (e.stage === 'data_detected') log('data: ' + (e.kind || '') + ' \u2192 ' + (e.dataset_id || ''), 'ok');
        else if (e.stage === 'seeding') log(e.message || 'seeding\u2026', 'info');
        if (e.stage === 'page_added' || e.stage === 'surface_detected' || e.stage === 'subtable_added') pollOnce();
      } else if ((t === 'fabric.discover.surface' || t === 'fabric.discover.subtable') && active.running){
        pollOnce();
      }
    } catch(_){}
  });

  // ── Boot ─────────────────────────────────────────────────────────────────
  ensureGraphLib(function(){ extendPalette(); ensureGraph(); });
  loadHistory(true);
  log('Discover+ ready', 'ok');
})();
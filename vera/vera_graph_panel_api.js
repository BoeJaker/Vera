/**
 * vera_graph_panel_api.js — "API" sidebar panel / API browser for vera_graph.js
 * ============================================================================
 * A dedicated Data-Fabric API browser. Load after vera_graph.js:
 *
 *   <script src="/ui/vera-graph.js"></script>
 *   <script src="/ui/vera-graph-panel-api.js"></script>
 *
 * It lets you:
 *   • Browse every API the discovery system found (OpenAPI / json_api / json /
 *     graphql surfaces), across all datasets.
 *   • Enumerate an API into a full api_endpoints sub-table.
 *   • Map an API's data — sample a response, infer a flat field schema, and get a
 *     suggested jq_path to the record array.
 *   • Set it up as a recurring pull source (it then appears in the Sources
 *     section) and pull it on demand.
 *   • Browse the endpoint sub-tables / records inline.
 *
 * Backend endpoints used:
 *   GET  /fabric/api/list
 *   POST /fabric/api/map
 *   POST /fabric/surfaces/enumerate
 *   POST /fabric/surfaces/promote
 *   POST /fabric/sources/add
 *   POST /fabric/sources/pull
 *   GET  /fabric/sources
 *   POST /fabric/browse
 * ----------------------------------------------------------------------------
 */
(function(){
  'use strict';
  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerPanel) {
    if (typeof console !== 'undefined') {
      console.warn('vera_graph_panel_api: veraUI.Graph.registerPanel not found — ' +
                   'load vera_graph.js before this file.');
    }
    return;
  }

  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  var INTERVALS = [
    { v: 0,       l: 'Manual' },
    { v: 3600,    l: 'Hourly' },
    { v: 21600,   l: 'Every 6h' },
    { v: 86400,   l: 'Daily' },
    { v: 604800,  l: 'Weekly' },
  ];

  function _injectCSS(){
    if (document.getElementById('vg-api-panel-css')) return;
    var s = document.createElement('style');
    s.id = 'vg-api-panel-css';
    s.textContent = [
      '.apip{font-size:10px;color:var(--text,#ddd5c8)}',
      // NOTE: the host fabric_panel defines a global `.sec{display:flex;...}` that
      // would otherwise lay each <details> summary + body side-by-side. Reset it
      // hard so our sections stack (header ABOVE body), like the Discover panel.
      '.apip .sec{display:block!important;text-transform:none!important;letter-spacing:normal!important;gap:0!important;align-items:initial!important;margin-bottom:6px!important;border:1px solid var(--border,#3a3530)!important;border-radius:4px!important;background:var(--bg0,#181614)!important;overflow:hidden!important}',
      '.apip .sec::after{display:none!important}',
      '.apip .sec-hd{display:flex!important;align-items:center;gap:6px;padding:6px 8px;cursor:pointer;user-select:none;list-style:none;background:var(--bg1,#1f1d1a);font-size:10.5px;font-weight:600;text-transform:none!important;letter-spacing:normal!important;color:var(--text,#ddd5c8)}',
      '.apip .sec-hd::-webkit-details-marker{display:none}',
      '.apip .sec-hd .sub{margin-left:auto;font-size:8.5px;color:var(--dim,#6a6058);font-weight:500;font-family:var(--mono,monospace)}',
      '.apip .sec[open] .sec-hd{border-bottom:1px solid var(--border,#3a3530)}',
      '.apip .sec-body{padding:7px 8px}',
      '.apip .row{display:flex;align-items:center;gap:6px;margin-bottom:4px}',
      '.apip .row label{font-size:9px;color:var(--dim2,#8a7e70);min-width:62px;flex-shrink:0}',
      '.apip .row input,.apip .row select{font-size:10px;padding:3px 5px;flex:1;min-width:0;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;font-family:var(--mono,monospace)}',
      '.apip .btn{font-size:9px;padding:3px 8px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace);transition:.12s;white-space:nowrap}',
      '.apip .btn:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.apip .btn.primary{background:rgba(90,158,143,.12);border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.apip .btn.teal{background:rgba(143,184,122,.12);border-color:var(--acc2,#8fb87a);color:var(--acc2,#8fb87a)}',
      '.apip .btn[disabled]{opacity:.5;cursor:default}',
      '.apip .btn-row{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}',
      '.apip .item{padding:5px 7px;border-bottom:1px solid var(--border,#3a3530);cursor:pointer;display:flex;align-items:center;gap:6px;transition:.08s}',
      '.apip .item:hover{background:var(--bg2,#272421)}',
      '.apip .item.sel{border-left:2px solid var(--acc,#5a9e8f);background:var(--bg2,#272421)}',
      '.apip .item .col{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}',
      '.apip .item .nm{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px;color:var(--text,#ddd5c8)}',
      '.apip .item .sub2{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace)}',
      '.apip .item .badges{display:flex;gap:3px;align-items:center;flex-shrink:0}',
      '.apip .pill{font-size:7.5px;padding:1px 5px;border-radius:2px;border:1px solid var(--border,#3a3530);color:var(--dim,#6a6058);flex-shrink:0}',
      '.apip .pill.api{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.apip .pill.src{border-color:var(--ok,#8fb87a);color:var(--ok,#8fb87a)}',
      '.apip .status{font-size:9px;min-height:12px;color:var(--dim,#6a6058);margin-top:4px}',
      '.apip .status.ok{color:var(--ok,#8fb87a)} .apip .status.err{color:var(--err,#c96b6b)} .apip .status.warn{color:var(--acc3,#c9955a)}',
      '.apip .list{max-height:200px;overflow-y:auto;border:1px solid var(--border,#3a3530);border-radius:3px;background:var(--bg0,#181614)}',
      '.apip .empty{text-align:center;padding:16px;color:var(--dim,#6a6058);font-size:10px}',
      '.apip .schema{width:100%;border-collapse:collapse;font-size:9px}',
      '.apip .schema td{padding:2px 5px;border-bottom:1px solid var(--border,#3a3530);vertical-align:top}',
      '.apip .schema .k{color:var(--acc,#5a9e8f);font-family:var(--mono,monospace)}',
      '.apip .schema .t{color:var(--acc3,#c9955a);font-size:8px;text-transform:uppercase}',
      '.apip .schema .ex{color:var(--dim2,#8a7e70);font-family:var(--mono,monospace);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.apip pre.json{font-size:8.5px;font-family:var(--mono,monospace);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:6px;max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:var(--dim2,#8a7e70)}',
      '.apip .hint{font-size:8.5px;color:var(--dim2,#8a7e70);line-height:1.4;margin-bottom:5px}',
    ].join('\n');
    document.head.appendChild(s);
  }

  window.veraUI.Graph.registerPanel({
    id:    'api',
    title: 'API',
    icon:  '⧉',           // ⧉ — joined panes
    order: 16,                 // just after Discover+ (15)
    mount: function(bodyEl, graph, papi){
      _injectCSS();
      var apiBase = (papi && papi.apiBase) || (window._veraBase || '');

      var st = { apis: [], selected: null, lastMap: null };

      async function api(path, method, payload, ms){
        var ctrl = new AbortController();
        var to = setTimeout(function(){ ctrl.abort(); }, ms || 30000);
        try {
          var opts = { method: method || 'GET', signal: ctrl.signal,
                       headers: { 'Content-Type': 'application/json' } };
          if (payload !== undefined && method && method !== 'GET') opts.body = JSON.stringify(payload);
          var res = await fetch(apiBase + path, opts);
          var txt = await res.text();
          try { return JSON.parse(txt); } catch(e){ return { error: txt || ('HTTP ' + res.status) }; }
        } catch (e) {
          return { error: (e && e.name === 'AbortError') ? 'timeout' : ((e && e.message) || 'net err') };
        } finally { clearTimeout(to); }
      }
      function $(sel){ return bodyEl.querySelector(sel); }
      function setStatus(sel, msg, type){
        var el = (typeof sel === 'string') ? $(sel) : sel;
        if (!el) return; el.textContent = msg || '';
        el.className = 'status' + (type ? ' ' + type : '');
      }

      bodyEl.className = (bodyEl.className || '') + ' apip';
      bodyEl.innerHTML =
        // DISCOVERED APIS
        '<details class="sec" open><summary class="sec-hd"><span>⧉ Discovered APIs</span><span class="sub ap-count"></span></summary><div class="sec-body" style="padding:0">' +
          '<div style="display:flex;gap:4px;padding:5px 6px;border-bottom:1px solid var(--border,#3a3530)">' +
            '<button class="btn teal ap-refresh" style="flex:0 0 auto">↻ Refresh</button>' +
            '<input class="ap-search" placeholder="Filter APIs…" style="flex:1;min-width:0;font-size:9px;padding:3px 6px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px">' +
          '</div>' +
          '<div class="list ap-list" style="max-height:220px;border:none;border-radius:0"><div class="empty">Press ↻ Refresh.</div></div>' +
        '</div></details>' +
        // SELECTED API
        '<details class="sec" open><summary class="sec-hd"><span>◈ Selected API</span><span class="sub ap-sel-kind"></span></summary><div class="sec-body">' +
          '<div class="ap-sel-url hint" style="word-break:break-all">No API selected.</div>' +
          '<div class="btn-row">' +
            '<button class="btn ap-act-enum">☰ Enumerate</button>' +
            '<button class="btn ap-act-map">◫ Map data</button>' +
            '<button class="btn ap-act-promote">⊕ Add as source</button>' +
          '</div>' +
          '<div class="status ap-sel-status"></div>' +
          '<div class="ap-endpoints" style="margin-top:6px"></div>' +
        '</div></details>' +
        // MAP / SCHEMA
        '<details class="sec ap-map-sec" style="display:none" open><summary class="sec-hd"><span>◫ Data map</span><span class="sub ap-map-meta"></span></summary><div class="sec-body">' +
          '<div class="ap-schema-wrap"></div>' +
          '<div style="font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin:7px 0 3px">Sample</div>' +
          '<pre class="json ap-sample"></pre>' +
        '</div></details>' +
        // SET UP PULL SOURCE
        '<details class="sec ap-setup-sec" style="display:none" open><summary class="sec-hd"><span>⤵ Set up pull source</span></summary><div class="sec-body">' +
          '<div class="hint">Register this API as a recurring source. It will appear in the Sources section and can be pulled on demand or on a schedule.</div>' +
          '<div class="row"><label>jq path</label><input class="ap-jq" placeholder="e.g. results (array key)"></div>' +
          '<div class="row"><label>Interval</label><select class="ap-interval"></select></div>' +
          '<div class="row"><label>Dataset</label><input class="ap-ds" placeholder="auto"></div>' +
          '<div class="row"><label>Label</label><input class="ap-label" placeholder="auto"></div>' +
          '<div class="row"><label>Limit</label><input class="ap-limit" type="number" value="50" min="1" max="5000"></div>' +
          '<div class="btn-row">' +
            '<button class="btn primary ap-create-src" style="flex:1">Create source</button>' +
            '<button class="btn teal ap-create-pull" style="flex:1">Create + pull now</button>' +
          '</div>' +
          '<div class="status ap-setup-status"></div>' +
        '</div></details>' +
        // API SOURCES
        '<details class="sec"><summary class="sec-hd"><span>⤵ API sources</span><span class="sub ap-src-count"></span></summary><div class="sec-body" style="padding:0">' +
          '<div style="padding:5px 6px;border-bottom:1px solid var(--border,#3a3530)">' +
            '<button class="btn ap-src-refresh">↻ Refresh sources</button>' +
          '</div>' +
          '<div class="list ap-src-list" style="max-height:180px;border:none;border-radius:0"><div class="empty">Press refresh.</div></div>' +
        '</div></details>';

      // Interval options
      (function(){
        var sel = $('.ap-interval'); if (!sel) return;
        sel.innerHTML = INTERVALS.map(function(o){ return '<option value="'+o.v+'">'+o.l+'</option>'; }).join('');
        sel.value = '0';
      })();

      // ── Discovered APIs ───────────────────────────────────────────────────
      async function loadApis(){
        setStatus($('.ap-sel-status'), '');
        var listEl = $('.ap-list');
        if (listEl) listEl.innerHTML = '<div class="empty">Loading…</div>';
        var res = await api('/fabric/api/list?include_endpoints=true&limit=300');
        st.apis = (res && res.apis) || [];
        if ($('.ap-count')) $('.ap-count').textContent = st.apis.length || '';
        renderApiList();
      }
      // Build a readable name from a url: "host · last-path-segment".
      function _readableName(url, fallback){
        try {
          var u = new URL(url);
          var segs = u.pathname.split('/').filter(Boolean);
          var tail = segs.length ? segs[segs.length - 1] : '';
          return u.hostname.replace(/^www\./, '') + (tail ? ' · ' + decodeURIComponent(tail) : '');
        } catch(e) { return fallback || url || ''; }
      }
      function _apiPrimary(a){
        // Prefer a meaningful label; if the label is just the url, derive one.
        if (a.label && a.label !== a.url) return a.label;
        return _readableName(a.url, a.id);
      }
      function renderApiList(){
        var listEl = $('.ap-list'); if (!listEl) return;
        var filter = ($('.ap-search') && $('.ap-search').value || '').toLowerCase();
        var rows = st.apis.filter(function(a){
          return !filter || ((a.label||'')+' '+(a.url||'')+' '+(a.kind||'')).toLowerCase().indexOf(filter) >= 0;
        });
        if (!rows.length) { listEl.innerHTML = '<div class="empty">'+(st.apis.length?'No matches.':'No APIs discovered yet.')+'</div>'; return; }
        listEl.innerHTML = rows.map(function(a){
          var idx = st.apis.indexOf(a);
          return '<div class="item' + (st.selected === a ? ' sel' : '') + '" data-idx="' + idx + '" title="' + esc(a.url||'') + '">' +
            '<span class="col"><span class="nm">' + esc(_apiPrimary(a)) + '</span>' +
              '<span class="sub2">' + esc(a.url || a.id) + '</span></span>' +
            '<span class="badges">' +
              '<span class="pill api">' + esc(a.kind || 'api') + '</span>' +
              (a.endpoint_count ? '<span class="pill">' + a.endpoint_count + ' ep</span>' : '') +
              (a.promoted ? '<span class="pill src">src</span>' : '') +
            '</span>' +
          '</div>';
        }).join('');
        listEl.querySelectorAll('.item[data-idx]').forEach(function(el){
          el.onclick = function(){ selectApi(st.apis[parseInt(el.getAttribute('data-idx'))]); };
        });
      }

      function selectApi(a){
        st.selected = a; st.lastMap = null;
        renderApiList();
        $('.ap-sel-kind').textContent = a ? (a.kind || 'api') : '';
        $('.ap-sel-url').textContent = a ? (a.url || a.id) : 'No API selected.';
        $('.ap-map-sec').style.display = 'none';
        $('.ap-setup-sec').style.display = 'none';
        setStatus($('.ap-sel-status'), '');
        renderEndpoints();
        if (a && graph && graph.focusNode) { try { graph.focusNode(a.id); } catch(e){} }
      }

      function renderEndpoints(){
        var wrap = $('.ap-endpoints'); if (!wrap) return;
        var a = st.selected;
        var eps = (a && a.endpoint_tables) || [];
        if (!a) { wrap.innerHTML = ''; return; }
        if (!eps.length) { wrap.innerHTML = '<div class="hint">No enumerated endpoints yet — press Enumerate.</div>'; return; }
        wrap.innerHTML = '<div style="font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Endpoint tables</div>' +
          '<div class="list" style="max-height:130px">' + eps.map(function(e, i){
            return '<div class="item" data-ep="' + i + '" title="' + esc(e.sub_dataset||'') + '">' +
              '<span class="nm" style="flex:1">' + esc(e.title || e.sub_dataset) + '</span>' +
              '<span class="pill">' + (e.rows||0) + '</span></div>';
          }).join('') + '</div>' +
          '<div class="ap-ep-records" style="margin-top:5px"></div>';
        wrap.querySelectorAll('.item[data-ep]').forEach(function(el){
          el.onclick = function(){ browseEndpoint(eps[parseInt(el.getAttribute('data-ep'))]); };
        });
      }
      async function browseEndpoint(ep){
        var out = $('.ap-ep-records'); if (!out || !ep) return;
        out.innerHTML = '<div class="hint">Loading records…</div>';
        var res = await api('/fabric/browse', 'POST',
          { dataset_id: ep.sub_dataset, limit: 100, offset: 0, search: '', lite: false }, 20000);
        var recs = (res && res.records) || [];
        if (!recs.length) { out.innerHTML = '<div class="hint">'+((res&&res.error)||'No records.')+'</div>'; return; }
        var rows = recs.map(function(r){ var d = r.data || r; if (typeof d === 'string') { try { d = JSON.parse(d); } catch(e){ d = { value: d }; } } return d; });
        // Collect columns (skip internal/text-heavy keys), cap to keep it tidy.
        var cols = [], seen = {};
        rows.forEach(function(d){ if (d && typeof d === 'object') Object.keys(d).forEach(function(k){
          if (!seen[k] && k !== 'text' && k[0] !== '_') { seen[k] = 1; cols.push(k); }
        }); });
        cols = cols.slice(0, 6);
        if (!cols.length) { out.innerHTML = '<pre class="json">' + esc(JSON.stringify(rows.slice(0,30), null, 1)) + '</pre>'; return; }
        out.innerHTML = '<div style="font-size:8px;color:var(--dim,#6a6058);margin:2px 0 3px">' + esc(ep.title || ep.sub_dataset) + ' — ' + (res.total || rows.length) + ' rows</div>' +
          '<div style="max-height:200px;overflow:auto"><table class="schema"><thead><tr>' +
          cols.map(function(c){ return '<th style="text-align:left;color:var(--dim2,#8a7e70);font-size:8px;padding:2px 5px;text-transform:uppercase">' + esc(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          rows.slice(0, 60).map(function(d){
            return '<tr>' + cols.map(function(c){
              var v = d ? d[c] : ''; if (v && typeof v === 'object') v = JSON.stringify(v);
              var s = String(v == null ? '' : v);
              var isU = /^https?:\/\//.test(s);
              return '<td class="ex" style="max-width:140px">' + (isU ? '<a href="' + esc(s) + '" target="_blank" style="color:var(--acc,#5a9e8f)">' + esc(s.slice(0,50)) + '</a>' : esc(s.slice(0,120))) + '</td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>';
      }

      // ── Actions ───────────────────────────────────────────────────────────
      async function enumerateSel(){
        var a = st.selected; if (!a) return;
        setStatus($('.ap-sel-status'), 'Enumerating…');
        var res = await api('/fabric/surfaces/enumerate', 'POST', { surface_id: a.id }, 120000);
        if (res && !res.error) {
          setStatus($('.ap-sel-status'), 'Enumerated ' + (res.endpoints||0) + ' endpoints' + (res.note ? ' ('+res.note+')' : ''), res.endpoints ? 'ok' : 'warn');
          await loadApis();
          var again = st.apis.find(function(x){ return x.id === a.id; });
          if (again) selectApi(again);
        } else setStatus($('.ap-sel-status'), (res && res.error) || 'Failed', 'err');
      }

      async function mapSel(){
        var a = st.selected; if (!a) return;
        setStatus($('.ap-sel-status'), 'Sampling API…');
        var res = await api('/fabric/api/map', 'POST', { surface_id: a.id, sample: 8 }, 60000);
        if (!res || res.error) { setStatus($('.ap-sel-status'), (res && res.error) || 'Map failed', 'err'); return; }
        st.lastMap = res;
        setStatus($('.ap-sel-status'), 'Mapped: ' + (res.count||0) + ' rows, ' + (res.schema||[]).length + ' fields', 'ok');
        // Render schema + sample
        $('.ap-map-sec').style.display = '';
        $('.ap-map-meta').textContent = (res.root_type||'') + ' · ' + (res.count||0) + ' rows';
        var sw = $('.ap-schema-wrap');
        sw.innerHTML = (res.schema && res.schema.length)
          ? '<table class="schema">' + res.schema.map(function(f){
              return '<tr><td class="k">' + esc(f.field) + '</td><td class="t">' + esc(f.type) + '</td><td class="ex">' + esc(f.example) + '</td></tr>';
            }).join('') + '</table>'
          : '<div class="hint">No object fields detected.</div>';
        $('.ap-sample').textContent = JSON.stringify(res.records || [], null, 1).slice(0, 4000);
        // Prefill the setup form with the mapping
        $('.ap-setup-sec').style.display = '';
        if ($('.ap-jq'))    $('.ap-jq').value = res.suggested_jq || '';
        if ($('.ap-ds'))    $('.ap-ds').value = a.parent_dataset || '';
        if ($('.ap-label')) $('.ap-label').value = a.label || a.url || '';
      }

      async function promoteSel(){
        var a = st.selected; if (!a) return;
        setStatus($('.ap-sel-status'), 'Promoting…');
        var res = await api('/fabric/surfaces/promote', 'POST', { surface_id: a.id, auto_pull: false }, 60000);
        if (res && !res.error) {
          setStatus($('.ap-sel-status'), 'Added as source: ' + (res.source_id||'') + (res.dataset_id ? ' → ' + res.dataset_id : ''), 'ok');
          await loadApis(); loadSources();
        } else setStatus($('.ap-sel-status'), (res && res.error) || 'Failed', 'err');
      }

      async function createSource(thenPull){
        var a = st.selected; if (!a) return;
        var payload = {
          url: a.url, source_type: 'api',
          jq_path: ($('.ap-jq') && $('.ap-jq').value.trim()) || '',
          interval: parseInt(($('.ap-interval') && $('.ap-interval').value) || '0', 10),
          dataset_id: ($('.ap-ds') && $('.ap-ds').value.trim()) || '',
          label: ($('.ap-label') && $('.ap-label').value.trim()) || (a.label || a.url),
          limit: parseInt(($('.ap-limit') && $('.ap-limit').value) || '50', 10),
          tags: 'api,discovered',
        };
        setStatus($('.ap-setup-status'), 'Creating source…');
        var res = await api('/fabric/sources/add', 'POST', payload, 30000);
        if (!res || res.error || !res.id) { setStatus($('.ap-setup-status'), (res && res.error) || 'Failed', 'err'); return; }
        if (!thenPull) {
          setStatus($('.ap-setup-status'), 'Source created: ' + res.id, 'ok');
          loadSources(); return;
        }
        setStatus($('.ap-setup-status'), 'Created — pulling…');
        var pr = await api('/fabric/sources/pull', 'POST', { source_id: res.id }, 120000);
        if (pr && !pr.error) setStatus($('.ap-setup-status'), 'Pulled ' + (pr.ingested||0) + ' records → ' + (pr.dataset_id||''), 'ok');
        else setStatus($('.ap-setup-status'), 'Created but pull failed: ' + ((pr && pr.error)||'?'), 'warn');
        loadSources();
      }

      // ── API sources ───────────────────────────────────────────────────────
      async function loadSources(){
        var listEl = $('.ap-src-list'); if (listEl) listEl.innerHTML = '<div class="empty">Loading…</div>';
        var res = await api('/fabric/sources');
        var srcs = ((res && res.sources) || []).filter(function(s){
          return s.source_type === 'api' || s.source_type === 'http';
        });
        if ($('.ap-src-count')) $('.ap-src-count').textContent = srcs.length || '';
        if (!listEl) return;
        if (!srcs.length) { listEl.innerHTML = '<div class="empty">No API sources yet.</div>'; return; }
        listEl.innerHTML = srcs.map(function(s, i){
          var iv = INTERVALS.find(function(o){ return o.v === s.interval; });
          var sub = (s.url || '') + (s.last_pulled ? '  ·  last ' + String(s.last_pulled).slice(0,16).replace('T',' ') : '');
          return '<div class="item" data-src="' + esc(s.id) + '" title="' + esc(s.url||'') + '">' +
            '<span class="col"><span class="nm">' + esc(s.label || _readableName(s.url, s.id)) + '</span>' +
              '<span class="sub2">' + esc(sub) + '</span></span>' +
            '<span class="badges">' +
              '<span class="pill">' + esc(iv ? iv.l : (s.interval ? s.interval + 's' : 'manual')) + '</span>' +
              (s.pull_count ? '<span class="pill">×' + s.pull_count + '</span>' : '') +
              '<button class="btn ap-pull" data-src="' + esc(s.id) + '" style="padding:1px 6px;font-size:8px">pull</button>' +
            '</span>' +
          '</div>';
        }).join('');
        listEl.querySelectorAll('.ap-pull').forEach(function(b){
          b.onclick = async function(ev){
            ev.stopPropagation();
            b.disabled = true; b.textContent = '…';
            var pr = await api('/fabric/sources/pull', 'POST', { source_id: b.getAttribute('data-src') }, 120000);
            b.disabled = false;
            b.textContent = (pr && !pr.error) ? ('✓' + (pr.ingested||0)) : 'err';
          };
        });
      }

      // ── Wire events ─────────────────────────────────────────────────────
      $('.ap-refresh').onclick = loadApis;
      if ($('.ap-search')) $('.ap-search').oninput = renderApiList;
      $('.ap-act-enum').onclick = enumerateSel;
      $('.ap-act-map').onclick = mapSel;
      $('.ap-act-promote').onclick = promoteSel;
      $('.ap-create-src').onclick = function(){ createSource(false); };
      $('.ap-create-pull').onclick = function(){ createSource(true); };
      $('.ap-src-refresh').onclick = loadSources;

      // Initial: load the API list (read-only — does not touch the graph).
      loadApis();
      loadSources();
    },
  });
})();

/* VeraDiff - THE diff viewer (looplab-control §9 Phase K6, 18 Oct 2026). Browser (window.VeraDiff + <vera-diff>) and
   Node (module.exports) - the pure half is the same in both. Used by Compare, Branches, Runs (a step's diff), Routing
   (a rule edit) and a gate's test run.

   Owner, 18 Oct: "need to be able to easily read diffs from the loop lab fully linted and nice qol for the code ui
   (like the canvas or ide)".

     parse(diff)                  -> [{path, old_path, new, deleted, binary, adds, dels, hunks:[{header, lines:[{t, text, old, new}]}]}]
     wordDiff(a, b)               -> [{t: '=' | '-' | '+', s}]   the words that changed between a removed and an added line
     tokens(text, path)           -> html: the line's syntax (comments, strings, numbers, keywords) by file type
     fold(lines, keep)            -> lines with long unchanged runs folded to {t: 'fold', n, from}
     render(files, opts)          -> html; opts = {mode: 'unified'|'split', lint: [{path, line, code, message, source,
                                     severity}], open: Set of fold ids, closed: Set of paths}
   <vera-diff pipeline="id">        reads evolve.pipeline.diff + evolve.pipeline.lint itself; or .setDiff(text, findings).
     Keys (focus the viewer): j / k next / previous hunk, n / p next / previous file, u unified <-> split, e unfold all.
     Emits diff:select {path, line} when a line number is clicked. */
(function (root) {
  'use strict';
  if (root.VeraDiff && typeof root.VeraDiff.parse === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraDiff;
    return;
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  var HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

  function parse(diff) {
    var files = [], cur = null, hunk = null, o = 0, n = 0;
    String(diff || '').split('\n').forEach(function (raw) {
      if (raw.indexOf('diff --git ') === 0) {
        var m = /^diff --git a\/(.*?) b\/(.*)$/.exec(raw);
        cur = { path: m ? m[2] : raw.slice(11), old_path: m ? m[1] : '', new: false, deleted: false, binary: false, adds: 0, dels: 0, hunks: [] };
        files.push(cur); hunk = null; return;
      }
      if (!cur) return;
      if (!hunk || raw.indexOf('@@') === 0) {
        if (raw.indexOf('new file mode') === 0) cur.new = true;
        else if (raw.indexOf('deleted file mode') === 0) cur.deleted = true;
        else if (raw.indexOf('Binary files') === 0) cur.binary = true;
        else if (raw.indexOf('+++ ') === 0 && raw.slice(4) !== '/dev/null') cur.path = raw.slice(4).replace(/^b\//, '');
        var h = HUNK.exec(raw);
        if (h) { o = +h[1]; n = +h[3]; hunk = { header: raw, ctx: (h[5] || '').trim(), lines: [] }; cur.hunks.push(hunk); }
        return;
      }
      if (raw.charAt(0) === '\\') return;
      var t = raw.charAt(0) || ' ', text = raw.slice(1);
      if (t === '+') { hunk.lines.push({ t: '+', text: text, old: null, new: n++ }); cur.adds++; }
      else if (t === '-') { hunk.lines.push({ t: '-', text: text, old: o++, new: null }); cur.dels++; }
      else hunk.lines.push({ t: ' ', text: text, old: o++, new: n++ });
    });
    return files;
  }

  /* the words that changed: LCS over word / space / punctuation tokens; a very long line falls back to the whole line */
  function wordDiff(a, b) {
    var A = String(a).match(/\w+|\s+|[^\w\s]/g) || [], B = String(b).match(/\w+|\s+|[^\w\s]/g) || [];
    if (A.length * B.length > 160000) return [{ t: '-', s: a }, { t: '+', s: b }];
    var m = A.length, k = B.length, L = [], i, j;
    for (i = 0; i <= m; i++) { L.push(new Array(k + 1).fill(0)); }
    for (i = m - 1; i >= 0; i--) for (j = k - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    var out = [];
    function put(t, s) { var last = out[out.length - 1]; if (last && last.t === t) last.s += s; else out.push({ t: t, s: s }); }
    i = 0; j = 0;
    while (i < m && j < k) {
      if (A[i] === B[j]) { put('=', A[i]); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) put('-', A[i++]);
      else put('+', B[j++]);
    }
    while (i < m) put('-', A[i++]);
    while (j < k) put('+', B[j++]);
    return out;
  }

  /* syntax by file type - a small offline tokenizer (comments, strings, numbers, keywords), tolerant of partial lines */
  var KW = {
    py: 'and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self',
    js: 'async await break case catch class const continue default delete do else export extends false finally for function if import in instanceof let new null return super switch this throw true try typeof undefined var void while yield',
    sh: 'if then else elif fi for while do done case esac in function return local export echo',
  };
  var KWSET = {}; Object.keys(KW).forEach(function (k) { KWSET[k] = {}; KW[k].split(' ').forEach(function (w) { KWSET[k][w] = 1; }); });
  function langOf(path) {
    var p = String(path || '').toLowerCase();
    if (/\.py$/.test(p)) return 'py';
    if (/\.(js|cjs|mjs|ts|html|json)$/.test(p)) return 'js';
    if (/\.(sh|bash)$/.test(p)) return 'sh';
    if (/\.css$/.test(p)) return 'css';
    return '';
  }
  function tokens(text, path) {
    var lang = langOf(path), s = String(text == null ? '' : text);
    if (!lang) return esc(s);
    var re = lang === 'py' || lang === 'sh' ? /(#.*$)|("(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)/g
      : /(\/\/.*$|\/\*.*?(?:\*\/|$))|("(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_$][\w$]*)/g;
    var out = '', last = 0, m;
    while ((m = re.exec(s))) {
      out += esc(s.slice(last, m.index));
      if (m[1]) out += '<span class="vd-c">' + esc(m[1]) + '</span>';
      else if (m[2]) out += '<span class="vd-s">' + esc(m[2]) + '</span>';
      else if (m[3]) out += '<span class="vd-n">' + esc(m[3]) + '</span>';
      else if (m[4]) out += KWSET[lang] && KWSET[lang][m[4]] ? '<span class="vd-k">' + esc(m[4]) + '</span>' : esc(m[4]);
      last = re.lastIndex;
      if (m[0] === '') re.lastIndex++;
    }
    return out + esc(s.slice(last));
  }

  /* long unchanged runs fold down to `keep` lines either side */
  function fold(lines, keep) {
    keep = keep == null ? 3 : keep;
    var out = [], run = [];
    function flush(atEnd, atStart) {
      if (run.length > 2 * keep + 2) {
        var head = atStart ? 0 : keep, tail = atEnd ? 0 : keep;
        out.push.apply(out, run.slice(0, head));
        out.push({ t: 'fold', n: run.length - head - tail, from: run[head].new, lines: run.slice(head, run.length - tail) });
        out.push.apply(out, run.slice(run.length - tail));
      } else out.push.apply(out, run);
      run = [];
    }
    var seenChange = false;
    lines.forEach(function (l) { if (l.t === ' ') run.push(l); else { flush(false, !seenChange); seenChange = true; out.push(l); } });
    flush(true, !seenChange);
    return out;
  }

  /* removed lines followed by added lines are paired, so their changed words show */
  function pairs(lines) {
    var map = {}, i = 0;
    while (i < lines.length) {
      if (lines[i].t !== '-') { i++; continue; }
      var d = i; while (i < lines.length && lines[i].t === '-') i++;
      var a = i; while (i < lines.length && lines[i].t === '+') i++;
      for (var k = 0; k < Math.min(a - d, i - a); k++) { map[d + k] = a + k; map[a + k] = d + k; }
    }
    return map;
  }
  function wordHtml(parts, side) {
    return parts.map(function (p) {
      if (p.t === '=') return esc(p.s);
      if (p.t === side) return '<span class="vd-w' + side + '">' + esc(p.s) + '</span>';
      return '';
    }).join('');
  }
  function lintIndex(list) {
    var by = {};
    (list || []).forEach(function (f) { var k = f.path + '\u0000' + (f.line == null ? 'file' : f.line); (by[k] = by[k] || []).push(f); });
    return by;
  }
  var SEV = { error: '#f87171', warning: '#e0a458', info: '#7aa2f7' };
  function lintRow(fs, cols) {
    return '<tr class="vd-lint"><td colspan="' + cols + '">' + fs.map(function (f) {
      return '<span class="vd-lf" style="border-color:' + (SEV[f.severity] || '#e0a458') + '"><b>' + esc(f.code) + '</b> ' + esc(f.message) + ' <i>' + esc(f.source) + '</i></span>'; }).join('') + '</td></tr>';
  }

  function render(files, opts) {
    opts = opts || {};
    var split = opts.mode === 'split', lint = lintIndex(opts.lint), open = opts.open || {}, closed = opts.closed || {};
    if (!files.length) return '<div class="vd-empty">no changes</div>';
    var tree = '<div class="vd-tree">' + files.map(function (f, i) {
      var nl = (opts.lint || []).filter(function (x) { return x.path === f.path; }), ne = nl.filter(function (x) { return x.severity === 'error'; }).length;
      return '<a class="vd-tf" data-file="' + i + '" title="' + esc(f.path) + '"><span class="vd-tp">' + esc(f.path.split('/').pop()) + '</span><span class="vd-td">' + esc(f.path.split('/').slice(0, -1).join('/')) + '</span>' +
        '<span class="vd-ta">+' + f.adds + '</span><span class="vd-tr">−' + f.dels + '</span>' + (nl.length ? '<span class="vd-tl' + (ne ? ' err' : '') + '">' + nl.length + '</span>' : '') + '</a>'; }).join('') + '</div>';
    var body = files.map(function (f, fi) {
      var head = '<div class="vd-fh" data-file="' + fi + '"><span class="vd-chev">' + (closed[f.path] ? '▸' : '▾') + '</span><b>' + esc(f.path) + '</b>' +
        (f.new ? ' <span class="vd-tag">new</span>' : '') + (f.deleted ? ' <span class="vd-tag">deleted</span>' : '') + (f.old_path && f.old_path !== f.path ? ' <span class="vd-tag">from ' + esc(f.old_path) + '</span>' : '') +
        '<span class="vd-ta">+' + f.adds + '</span><span class="vd-tr">−' + f.dels + '</span></div>';
      var fileLint = lint[f.path + '\u0000file'];
      if (closed[f.path]) return '<div class="vd-file" data-file="' + fi + '">' + head + '</div>';
      if (f.binary) return '<div class="vd-file" data-file="' + fi + '">' + head + '<div class="vd-empty">binary file</div></div>';
      var cols = split ? 4 : 3;
      var rows = (fileLint ? lintRow(fileLint, cols) : '') + f.hunks.map(function (h, hi) {
        var pm = pairs(h.lines), folded = fold(h.lines.map(function (l, i) { return Object.assign({ i: i }, l); }));
        var hr = '<tr class="vd-hunk" data-hunk="' + fi + ':' + hi + '"><td colspan="' + cols + '">' + esc(h.header.replace(/@@(.*?)@@.*/, '@@$1@@')) + (h.ctx ? ' <span class="vd-hc">' + esc(h.ctx) + '</span>' : '') + '</td></tr>';
        var lines = [], done = {};
        folded.forEach(function (l) {
          var fid = fi + ':' + hi + ':' + (l.from || 0);
          if (l.t === 'fold' && !open[fid]) { lines.push('<tr class="vd-fold" data-fold="' + fid + '"><td colspan="' + cols + '">⋯ ' + l.n + ' unchanged line' + (l.n === 1 ? '' : 's') + ' (click to show)</td></tr>'); return; }
          (l.t === 'fold' ? l.lines.map(function (x, k) { return Object.assign({ i: -1 - k }, x); }) : [l]).forEach(function (x) {
            var other = pm[x.i] != null ? h.lines[pm[x.i]] : null;
            var code = other ? wordHtml(wordDiff(x.t === '-' ? x.text : other.text, x.t === '-' ? other.text : x.text), x.t) : tokens(x.text, f.path);
            var cls = x.t === '+' ? 'vd-add' : x.t === '-' ? 'vd-del' : 'vd-ctx', sign = x.t === ' ' ? ' ' : x.t;
            var lf = x.t === '+' ? lint[f.path + '\u0000' + x.new] : null;
            if (split && x.t === '+' && done[x.i]) return;               // drawn beside the line it replaces
            if (split && x.t === '-' && other) {
              done[pm[x.i]] = 1;
              var lfo = lint[f.path + '\u0000' + other.new];
              lines.push('<tr' + (lfo ? ' class="vd-has-lint"' : '') + '><td class="vd-ln" data-line="' + x.old + '">' + x.old + '</td><td class="vd-code vd-del">' + code + '</td>' +
                '<td class="vd-ln" data-line="' + other.new + '">' + other.new + '</td><td class="vd-code vd-add">' + wordHtml(wordDiff(x.text, other.text), '+') + '</td></tr>');
              if (lfo) lines.push(lintRow(lfo, cols));
              return;
            }
            if (split) {
              var left = x.t !== '+' ? '<td class="vd-ln" data-line="' + x.old + '">' + x.old + '</td><td class="vd-code ' + (x.t === '-' ? 'vd-del' : 'vd-ctx') + '">' + code + '</td>' : '<td class="vd-ln"></td><td class="vd-code vd-blank"></td>';
              var right = x.t !== '-' ? '<td class="vd-ln" data-line="' + x.new + '">' + x.new + '</td><td class="vd-code ' + (x.t === '+' ? 'vd-add' : 'vd-ctx') + '">' + (x.t === ' ' ? code : code) + '</td>' : '<td class="vd-ln"></td><td class="vd-code vd-blank"></td>';
              lines.push('<tr' + (lf ? ' class="vd-has-lint"' : '') + '>' + left + right + '</tr>');
            } else {
              lines.push('<tr class="' + cls + (lf ? ' vd-has-lint' : '') + '"><td class="vd-ln" data-line="' + (x.new == null ? '' : x.new) + '">' + (x.old == null ? '' : x.old) + '</td><td class="vd-ln" data-line="' + (x.new == null ? '' : x.new) + '">' + (x.new == null ? '' : x.new) + '</td><td class="vd-code"><span class="vd-sign">' + sign + '</span>' + code + '</td></tr>');
            }
            if (lf) lines.push(lintRow(lf, cols));
          });
        });
        return hr + lines.join('');
      }).join('');
      // fixed layout takes its widths from the first row - a colspan hunk header - so the columns are set here
      var cg = split ? '<colgroup><col style="width:46px"><col><col style="width:46px"><col></colgroup>' : '<colgroup><col style="width:46px"><col style="width:46px"><col></colgroup>';
      return '<div class="vd-file" data-file="' + fi + '" data-path="' + esc(f.path) + '">' + head + '<table class="vd-t' + (split ? ' vd-split' : '') + '">' + cg + rows + '</table></div>';
    }).join('');
    return '<div class="vd-wrap">' + tree + '<div class="vd-body">' + body + '</div></div>';
  }
  function lintSummary(res) {
    if (!res) return '';
    var s = res.summary || {}, src = res.sources || {};
    return (s.errors || 0) + ' error(s) · ' + (s.warnings || 0) + ' warning(s) · ' + (s.info || 0) + ' note(s) on changed lines' +
      ' · checks: ' + Object.keys(src).map(function (k) { return k + (src[k] === 'ok' ? '' : ' (' + src[k] + ')'); }).join(', ');
  }

  var api = { parse: parse, wordDiff: wordDiff, tokens: tokens, fold: fold, pairs: pairs, render: render, lintSummary: lintSummary, langOf: langOf };
  root.VeraDiff = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  /* registered in vera/ui/vera_item.js's element registry (DO-0.2), not here: a bare registerBody under a block's name replaced that block's body */
  if (!root.HTMLElement || !root.customElements) return;

  var CSS = 'vera-diff{display:block;outline:none;font-size:12px}.vd-bar{display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap}' +
    '.vd-bar .vd-sum{color:var(--muted,#8b93a1);font-size:11px}.vd-seg{display:inline-flex;border:1px solid var(--border,#2a313c);border-radius:6px;overflow:hidden}.vd-seg button{background:none;border:0;color:var(--muted,#8b93a1);font:inherit;font-size:11px;padding:2px 8px;cursor:pointer}.vd-seg button.on{background:var(--accent,#7aa2f7);color:#0b0e13}' +
    '.vd-wrap{display:grid;grid-template-columns:minmax(150px,220px) 1fr;gap:10px;align-items:start}.vd-tree{position:sticky;top:0;max-height:70vh;overflow:auto;border-right:1px solid var(--border,#2a313c);padding-right:6px}' +
    '.vd-tf{display:grid;grid-template-columns:1fr auto auto auto;gap:4px;padding:2px 4px;border-radius:4px;cursor:pointer;font-size:11.5px;align-items:baseline}.vd-tf:hover{background:var(--bg2,#1b222c)}' +
    '.vd-tp{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vd-td{grid-column:1/-1;color:var(--muted,#8b93a1);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;order:5}' +
    '.vd-ta{color:#6db87a;font-size:10.5px}.vd-tr{color:#c96b6b;font-size:10.5px}.vd-tl{background:#e0a458;color:#0b0e13;border-radius:7px;padding:0 5px;font-size:10px}.vd-tl.err{background:#f87171}' +
    '.vd-body{min-width:0;overflow:auto;max-height:75vh}.vd-file{border:1px solid var(--border,#2a313c);border-radius:6px;margin-bottom:10px;overflow:hidden}' +
    '.vd-fh{display:flex;gap:8px;align-items:center;padding:5px 8px;background:var(--bg2,#1b222c);cursor:pointer;position:sticky;top:0;z-index:1}.vd-fh .vd-ta{margin-left:auto}.vd-tag{font-size:10px;border:1px solid var(--border,#2a313c);border-radius:6px;padding:0 5px;color:var(--muted,#8b93a1)}' +
    '.vd-t{border-collapse:collapse;width:100%;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;table-layout:fixed}.vd-t td{padding:0 6px;vertical-align:top}' +
    '.vd-t .vd-ln{width:42px;text-align:right;color:var(--muted,#8b93a1);user-select:none;cursor:pointer}.vd-code{white-space:pre-wrap;word-break:break-word}.vd-sign{color:var(--muted,#8b93a1);display:inline-block;width:12px}' +
    '.vd-add,.vd-code.vd-add{background:rgba(109,184,122,.12)}.vd-del,.vd-code.vd-del{background:rgba(201,107,107,.13)}.vd-blank{background:rgba(127,127,127,.05)}' +
    '.vd-w\\+{background:rgba(109,184,122,.38);border-radius:2px}.vd-w-{background:rgba(201,107,107,.40);border-radius:2px;text-decoration:line-through;text-decoration-color:rgba(255,255,255,.3)}' +
    '.vd-hunk td{background:rgba(122,162,247,.08);color:#7aa2f7;padding:2px 8px}.vd-hc{color:var(--muted,#8b93a1)}.vd-hunk.cur td{background:rgba(122,162,247,.22)}' +
    '.vd-fold td{color:var(--muted,#8b93a1);text-align:center;cursor:pointer;background:rgba(127,127,127,.06)}.vd-fold:hover td{color:inherit}' +
    '.vd-lint td{padding:2px 8px 4px 60px}.vd-lf{display:inline-block;border-left:3px solid;padding:1px 6px;margin:1px 6px 1px 0;background:rgba(224,164,88,.08);font-family:inherit;font-size:11px}.vd-lf i{color:var(--muted,#8b93a1)}' +
    '.vd-has-lint .vd-ln{box-shadow:inset 3px 0 #e0a458}.vd-c{color:#7f8a99;font-style:italic}.vd-s{color:#a5d6a7}.vd-n{color:#f2c879}.vd-k{color:#c792ea}.vd-empty{color:var(--muted,#8b93a1);padding:10px}';
  function ensureCss() { if (root.document.getElementById('vera-diff-css')) return; var st = root.document.createElement('style'); st.id = 'vera-diff-css'; st.textContent = CSS; root.document.head.appendChild(st); }
  function call(cap, args) {
    if (root.VeraDrill && root.VeraDrill.call) return root.VeraDrill.call(cap, args);
    return fetch((root._veraBase || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': 'diff; trigger=auto' }, body: JSON.stringify({ name: cap, arguments: args || {} }) })
      .then(function (r) { return r.json(); }).then(function (j) { if (j && j.content === undefined && j.detail) throw new Error(j.detail); return j && j.content !== undefined ? j.content : j; });
  }

  class VeraDiffEl extends root.HTMLElement {
    constructor() { super(); this._files = []; this._lint = null; this._mode = 'unified'; this._open = {}; this._closed = {}; this._hunk = -1; }
    connectedCallback() {
      ensureCss();
      if (!this.hasAttribute('tabindex')) this.setAttribute('tabindex', '0');
      if (!this._wired) { this._wired = true; this._wire(); }
      var pid = this.getAttribute('pipeline');
      if (pid && this._pid !== pid) { this._pid = pid; this._load(pid); } else this._draw();
    }
    setDiff(text, lint) { this._files = parse(text); this._lint = lint || null; this._draw(); }
    _load(pid) {
      var S = root.VeraStates;
      this.innerHTML = S ? S.html('loading', 'reading the change…') : 'loading…';
      call('evolve.pipeline.diff', { id: pid }).then((d) => {
        if (!d || d.error) throw new Error((d && d.error) || 'no diff');
        this._files = parse(d.diff || ''); this._trunc = !!d.truncated; this._draw();
        return call('evolve.pipeline.lint', { id: pid }).then((l) => { this._lint = l && !l.error ? l : { error: (l && l.error) || 'lint failed' }; this._draw(); },
                                                              (e) => { this._lint = { error: e.message }; this._draw(); });
      }).catch((e) => { this.innerHTML = S ? S.html('error', 'could not read the change: ' + e.message) : esc(e.message); });
    }
    _draw() {
      if (!this.isConnected) return;
      var l = this._lint, lintList = l && !l.error ? l.findings || [] : [];
      var bar = '<div class="vd-bar"><div class="vd-seg"><button data-vd="mode" data-v="unified" class="' + (this._mode === 'unified' ? 'on' : '') + '">unified</button><button data-vd="mode" data-v="split" class="' + (this._mode === 'split' ? 'on' : '') + '">side by side</button></div>' +
        '<span class="vd-sum">' + this._files.length + ' file(s) · +' + this._files.reduce(function (s, f) { return s + f.adds; }, 0) + ' −' + this._files.reduce(function (s, f) { return s + f.dels; }, 0) +
        (this._trunc ? ' · the diff was cut short (too large)' : '') + ' · ' + (l ? (l.error ? 'lint: ' + esc(l.error) : esc(lintSummary(l))) : 'linting…') + ' · keys j/k hunk, n/p file, u view, e unfold</span></div>';
      this.innerHTML = bar + render(this._files, { mode: this._mode, lint: lintList, open: this._open, closed: this._closed });
    }
    _hunks() { return Array.prototype.slice.call(this.querySelectorAll('.vd-hunk')); }
    _go(list, i) { if (!list.length) return; i = Math.max(0, Math.min(list.length - 1, i)); list.forEach(function (h) { h.classList.remove('cur'); }); list[i].classList.add('cur'); list[i].scrollIntoView({ block: 'center', behavior: 'smooth' }); return i; }
    _wire() {
      this.addEventListener('click', (ev) => {
        var b = ev.target.closest('[data-vd="mode"]'); if (b) { this._mode = b.getAttribute('data-v'); this._draw(); return; }
        var fd = ev.target.closest('.vd-fold'); if (fd) { this._open[fd.getAttribute('data-fold')] = 1; this._draw(); return; }
        var fh = ev.target.closest('.vd-fh'); if (fh) { var p = this._files[+fh.getAttribute('data-file')].path; this._closed[p] = !this._closed[p]; this._draw(); return; }
        var tf = ev.target.closest('.vd-tf'); if (tf) { var el = this.querySelector('.vd-file[data-file="' + tf.getAttribute('data-file') + '"]'); if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' }); return; }
        var ln = ev.target.closest('.vd-ln[data-line]'); if (ln && ln.getAttribute('data-line')) {
          var file = ln.closest('.vd-file');
          this.dispatchEvent(new CustomEvent('diff:select', { bubbles: true, composed: true, detail: { path: file && file.getAttribute('data-path'), line: +ln.getAttribute('data-line') } }));
        }
      });
      this.addEventListener('keydown', (ev) => {
        if (ev.target !== this || ev.ctrlKey || ev.metaKey || ev.altKey) return;
        var k = ev.key, hs = this._hunks();
        if (k === 'j' || k === 'k') { this._hunk = this._go(hs, this._hunk + (k === 'j' ? 1 : -1)); }
        else if (k === 'n' || k === 'p') {
          var fs = Array.prototype.slice.call(this.querySelectorAll('.vd-file')), cur = this._fileAt || 0;
          this._fileAt = Math.max(0, Math.min(fs.length - 1, cur + (k === 'n' ? 1 : -1))); if (fs[this._fileAt]) fs[this._fileAt].scrollIntoView({ block: 'start', behavior: 'smooth' });
        } else if (k === 'u') { this._mode = this._mode === 'split' ? 'unified' : 'split'; this._draw(); }
        else if (k === 'e') { this.querySelectorAll('.vd-fold').forEach((f) => { this._open[f.getAttribute('data-fold')] = 1; }); this._draw(); }
        else return;
        ev.preventDefault();
      });
    }
  }
  if (!root.customElements.get('vera-diff')) root.customElements.define('vera-diff', VeraDiffEl);
})(typeof window !== 'undefined' ? window : globalThis);

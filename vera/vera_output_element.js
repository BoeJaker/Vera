/* vera_output_element.js — window.VeraOut: ONE way output is drawn, wherever it is drawn
   =======================================================================================
   Owner, 2026-10-03: "all rendering of output types needs to be consistent and use the same element so its easy to
   update, the chat, the agentic loop, the canvas" · "markdown in the previews for the agentic loop final output dont
   render markdown, code isnt linted and is missing the buttons other code rendering areas have like explode" ·
   "need the ability to edit code that has been output".

   Markdown is <vera-markdown>'s (VeraMD.render - one block grammar everywhere). What this adds is everything a
   reply's OUTPUT carries beyond prose, in one place:

     VeraOut.render(md, opts)        markdown → HTML, every fence drawn as below (opts.fence may draw one itself)
     VeraOut.card(lang, code, o)     a code card: Copy · Edit · Explode · Preview/Source · Pop out · Run · Save ·
                                     Canvas, the language and file on its bar - o.incomplete = still streaming
     VeraOut.highlight(code, lang)   the offline, streaming-tolerant highlighter (memoised)
     VeraOut.lintCode(lang, code)    conservative, definite-errors-only lint
     VeraOut.hydrate(root)           after HTML lands: lint the finished cards, draw diagrams, open whole pages
     VeraOut.wire(root)              ONE delegated click handler for a document or a shadow root
     VeraOut.adopt(root)             the cards' style into a shadow root (the document gets it on load)
     VeraOut.blocks                  the registry: id → {lang, code, filename}
     VeraOut.hooks                   what a host draws its own way: {explode, save, pop, pane, canvas, session,
                                     edited} - each (blk, id, btn, card) → true when handled; the defaults below
                                     work in any page, so every surface has every action

   The markup is the chat's own (.code-card[data-cb], .code-bar, .cb-act, code.language-x, .code-exec-out,
   .cb-prev, .code-lint): everything that already reads a reply's cards - the canvas harvest, Explode, auto-save,
   the linter - keeps reading them. Served at /ui/elements/vera_output.js. */
(function () {
  'use strict';
  if (window.VeraOut && window.VeraOut.version >= 1) return;

  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const BASE = () => String(window.__VERA_BASE__ || (window.veraUI && window.veraUI.BASE) || '').replace(/\/$/, '');

  // ── the registry ─────────────────────────────────────────────────────────────────────────────────────
  let seq = 0;
  const blocks = {};
  function register(lang, code, filename) {
    const id = 'cb' + (++seq);
    blocks[id] = { lang: String(lang || ''), code: String(code || ''), filename: String(filename || '') };
    return id;
  }
  // where cards live: the document and every shadow root a host wired - a card is found by id in any of them
  const roots = new Set([document]);
  function cardOf(id) {
    for (const r of roots) { const c = r.querySelector && r.querySelector('.code-card[data-cb="' + id + '"]'); if (c) return c; }
    return null;
  }

  // ── languages ──────────────────────────────────────────────────────────────────────────────────────────
  const PREVIEWABLE = ['html', 'js', 'javascript', 'css', 'jsx', 'svg'];
  const previewable = (lang) => PREVIEWABLE.indexOf(String(lang || '').toLowerCase()) >= 0;
  // a WHOLE PAGE draws itself without being asked; a fragment, a sheet or a script waits to be asked
  const wholePage = (lang, code) => ['html', 'svg'].indexOf(String(lang || '').toLowerCase()) >= 0
    && /<\s*(!doctype\s+html|html[\s>]|body[\s>]|svg[\s>])/i.test(String(code || ''));
  // kept in step with _LANG_SPECS / _LANG_ALIASES in exec_capabilities.py (what /exec/code/run executes)
  const SERVER_RUN = new Set(['python', 'py', 'python3', 'node', 'js', 'javascript', 'nodejs', 'mjs', 'ruby', 'rb', 'php',
    'perl', 'pl', 'go', 'golang', 'lua', 'deno', 'ts', 'typescript', 'bash', 'sh', 'shell', 'zsh', 'powershell', 'ps', 'ps1',
    'pwsh', 'posh']);
  const runnable = (lang) => SERVER_RUN.has(String(lang || '').toLowerCase());
  const EXT = { python: 'py', py: 'py', javascript: 'js', js: 'js', typescript: 'ts', ts: 'ts', html: 'html', css: 'css',
    json: 'json', bash: 'sh', sh: 'sh', shell: 'sh', sql: 'sql', go: 'go', rust: 'rs', ruby: 'rb', php: 'php',
    yaml: 'yaml', yml: 'yaml', markdown: 'md', md: 'md', svg: 'svg', jsx: 'jsx', tsx: 'tsx', java: 'java', c: 'c', cpp: 'cpp' };
  const langClass = (lang) => { const L = String(lang || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''); return L ? ' class="language-' + L + '"' : ''; };
  // a filename out of a fence's info line: `app.py`, `file=app.py`, `path=src/app.py`, `title=app.py`
  function fenceFilename(info) {
    const s = String(info || '').trim(); if (!s) return '';
    let m = s.match(/(?:file|filename|name|title|path)\s*[=:]\s*["']?([\w./\-]+)["']?/i); if (m) return m[1];
    m = s.match(/(?:^|\s)([\w./\-]+\.[A-Za-z0-9]+)(?:\s|$)/); return m ? m[1] : '';
  }
  // the page a block draws as: a page as itself; a sheet dressing sample content; a script run in a blank page
  function previewDoc(lang, code) {
    const L = String(lang || '').toLowerCase(), src = String(code || '');
    if (L === 'html' || L === 'svg') return src;
    const head = '<!doctype html><meta charset="utf-8"><style>body{margin:0;padding:10px;background:#101012;color:#d8dce4;font:12px system-ui,sans-serif}</style>';
    if (L === 'css') return head + '<style>' + src + '</style><h1>Heading</h1><p>A paragraph, a <a href="#">link</a> and a <button>button</button>, dressed by the sheet.</p>';
    return head + '<body><script>try{' + src + '}catch(e){document.body.innerHTML=\'<pre style="color:#c96b6b">\'+String(e&&e.message||e)+\'</pre>\';}<\/script>';
  }

  // ── highlighting (self-contained, offline, tolerant of a block still being typed) ──────────────────────
  const hlEsc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const FAMILY = {
    js: 'clike', javascript: 'clike', jsx: 'clike', ts: 'clike', typescript: 'clike', tsx: 'clike', mjs: 'clike', node: 'clike',
    nodejs: 'clike', deno: 'clike', c: 'clike', cpp: 'clike', 'c++': 'clike', h: 'clike', hpp: 'clike', cs: 'clike', csharp: 'clike',
    java: 'clike', go: 'clike', golang: 'clike', rust: 'clike', rs: 'clike', swift: 'clike', kotlin: 'clike', kt: 'clike',
    scala: 'clike', php: 'clike', dart: 'clike',
    python: 'hash', py: 'hash', python3: 'hash', ruby: 'hash', rb: 'hash', bash: 'hash', sh: 'hash', shell: 'hash', zsh: 'hash',
    ps1: 'hash', powershell: 'hash', pwsh: 'hash', perl: 'hash', pl: 'hash', r: 'hash', yaml: 'hash', yml: 'hash', toml: 'hash', ini: 'hash',
    sql: 'sql', css: 'css', scss: 'css', less: 'css', json: 'json',
    html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html', diff: 'diff', patch: 'diff' };
  const KW = {
    clike: new Set(('var let const function return if else for while do switch case break continue new class extends super this '
      + 'typeof instanceof in of try catch finally throw async await yield import export from default null undefined true false '
      + 'delete void static public private protected interface enum struct type int float double char bool boolean string long '
      + 'short namespace using package func map range chan go defer select fn impl pub mut use mod match loop where trait as').split(' ')),
    hash: new Set(('def class return if elif else for while break continue import from as pass raise try except finally with lambda '
      + 'yield global nonlocal in is not and or None True False async await self del assert print echo then fi do done function local '
      + 'export source set unset case esac elif').split(' ')),
    sql: new Set(('select from where insert into update delete create table drop alter add column values set join inner left right '
      + 'outer full on group by order having limit offset union all distinct as and or not null is in like between count sum avg '
      + 'min max primary key foreign references default index view exists').split(' ')),
    json: new Set(['true', 'false', 'null']) };
  function hlGeneric(code, family) {
    const kw = KW[family] || KW.clike, ci = family === 'sql', parts = [];
    if (family === 'hash') parts.push({ re: "'''[\\s\\S]*?'''", cat: 'str' }, { re: '"""[\\s\\S]*?"""', cat: 'str' });
    if (family === 'clike' || family === 'css' || family === 'sql') parts.push({ re: '/\\*[\\s\\S]*?\\*/', cat: 'com' });
    const lineC = family === 'sql' ? '--' : family === 'hash' ? '#' : family === 'css' ? null : '//';
    if (lineC) parts.push({ re: escRe(lineC) + '[^\\n]*', cat: 'com' });
    parts.push({ re: '"(?:\\\\.|[^"\\\\\\n])*"', cat: 'str' }, { re: "'(?:\\\\.|[^'\\\\\\n])*'", cat: 'str' });
    if (family === 'clike') parts.push({ re: '`(?:\\\\.|[^`\\\\])*`', cat: 'str' });
    parts.push({ re: '\\b0[xX][0-9a-fA-F]+\\b', cat: 'num' }, { re: '\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b', cat: 'num' },
      { re: '[A-Za-z_$][A-Za-z0-9_$]*', cat: 'id' });
    const re = new RegExp(parts.map((p) => '(' + p.re + ')').join('|'), 'g');
    let out = '', last = 0, m;
    while ((m = re.exec(code))) {
      if (re.lastIndex === m.index) { re.lastIndex++; continue; }
      if (m.index > last) out += hlEsc(code.slice(last, m.index));
      let cat = null; for (let i = 0; i < parts.length; i++) { if (m[i + 1] !== undefined) { cat = parts[i].cat; break; } }
      const tok = m[0];
      if (cat === 'id') cat = kw.has(ci ? tok.toLowerCase() : tok) ? 'kw' : /^[A-Z]/.test(tok) ? 'type' : /^\s*\(/.test(code.slice(re.lastIndex)) ? 'fn' : 'pln';
      out += cat === 'pln' ? hlEsc(tok) : '<span class="hl-' + cat + '">' + hlEsc(tok) + '</span>';
      last = re.lastIndex;
    }
    return last < code.length ? out + hlEsc(code.slice(last)) : out;
  }
  function hlDiff(code) {
    return code.split('\n').map((l) => { const e = hlEsc(l);
      if (/^@@/.test(l)) return '<span class="hl-hunk">' + e + '</span>';
      if (/^(\+\+\+|---|diff |index |rename |similarity )/.test(l)) return '<span class="hl-com">' + e + '</span>';
      if (l[0] === '+') return '<span class="hl-add">' + e + '</span>';
      if (l[0] === '-') return '<span class="hl-del">' + e + '</span>';
      if (l[0] === '#') return '<span class="hl-com">' + e + '</span>';
      return e; }).join('\n');
  }
  function hlTag(seg) {
    return hlEsc(seg).replace(/^(&lt;\/?)([A-Za-z][\w:.-]*)|([\w:.-]+)(=)(&quot;[^&]*?&quot;|'[^']*?')|(&quot;[^&]*?&quot;|'[^']*?')/g,
      (m, open, name, attr, eq, val, bare) => open !== undefined ? open + '<span class="hl-tag">' + name + '</span>'
        : attr !== undefined ? '<span class="hl-attr">' + attr + '</span>' + eq + '<span class="hl-str">' + val + '</span>'
        : bare !== undefined ? '<span class="hl-str">' + bare + '</span>' : m);
  }
  function hlHtml(code) {
    const re = /<!--[\s\S]*?-->|<\/?[A-Za-z][\w:.-]*(?:[^<>"']|"[^"]*"|'[^']*')*?\/?>/g; let out = '', last = 0, m;
    while ((m = re.exec(code))) { if (m.index > last) out += hlEsc(code.slice(last, m.index));
      out += m[0].slice(0, 4) === '<!--' ? '<span class="hl-com">' + hlEsc(m[0]) + '</span>' : hlTag(m[0]); last = re.lastIndex; }
    return last < code.length ? out + hlEsc(code.slice(last)) : out;
  }
  const hlCache = new Map();
  function highlight(code, lang) {
    code = String(code == null ? '' : code);
    const fam = FAMILY[String(lang || '').toLowerCase()] || null, key = (fam || '_') + '\u0000' + code;
    const hit = hlCache.get(key); if (hit !== undefined) return hit;
    let html;
    try { html = !fam ? hlEsc(code) : fam === 'html' ? hlHtml(code) : fam === 'diff' ? hlDiff(code) : hlGeneric(code, fam); }
    catch (_) { html = hlEsc(code); }
    if (hlCache.size > 400) hlCache.clear();
    hlCache.set(key, html); return html;
  }

  // ── lint (definite errors only; a block still being typed is never linted) ──────────────────────────────
  function skipStr(code, i, q) { const n = code.length; i++;
    while (i < n) { const c = code[i]; if (c === '\\') { i += 2; continue; } if (c === q) return i + 1; if (c === '\n') return i; i++; } return n; }
  function bracketBalance(code, fam) {
    const pairs = { ')': '(', ']': '[', '}': '{' }, stack = [], n = code.length; let i = 0;
    const lineC = fam === 'sql' ? '--' : fam === 'hash' ? '#' : fam === 'css' ? null : '//', block = fam === 'clike' || fam === 'css' || fam === 'sql';
    while (i < n) { const c = code[i];
      if (block && c === '/' && code[i + 1] === '*') { const e = code.indexOf('*/', i + 2); i = e < 0 ? n : e + 2; continue; }
      if (lineC && code.startsWith(lineC, i)) { const e = code.indexOf('\n', i); i = e < 0 ? n : e; continue; }
      if (c === '"' || c === "'") { i = skipStr(code, i, c); continue; }
      if (c === '(' || c === '[' || c === '{') { stack.push(c); i++; continue; }
      if (c === ')' || c === ']' || c === '}') { if (!stack.length || stack[stack.length - 1] !== pairs[c]) return { sev: 'err', msg: 'Unbalanced "' + c + '"' }; stack.pop(); i++; continue; }
      i++; }
    return stack.length ? { sev: 'err', msg: 'Unclosed "' + stack[stack.length - 1] + '"' } : null;
  }
  function lintCode(lang, code) {
    const l = String(lang || '').toLowerCase(), fam = FAMILY[l], out = [];
    if (!code || !String(code).trim()) return out;
    if (l === 'json') { try { JSON.parse(code); } catch (e) { out.push({ sev: 'err', msg: 'Invalid JSON — ' + String(e.message || e).replace(/^JSON\.parse:?\s*/i, '') }); } return out; }
    if (l === 'py' || l === 'python' || l === 'python3') { let tab = false, sp = false;
      for (const ln of code.split('\n')) { const ind = (ln.match(/^[ \t]+/) || [''])[0]; if (ind.indexOf('\t') >= 0) tab = true; if (ind.indexOf(' ') >= 0) sp = true; }
      if (tab && sp) out.push({ sev: 'warn', msg: 'Mixed tabs and spaces in indentation' }); }
    if ((fam === 'clike' || fam === 'hash' || fam === 'sql' || fam === 'css') && !/[`<]/.test(code)) { const b = bracketBalance(code, fam); if (b) out.push(b); }
    return out;
  }
  const lintCache = new Map();
  function lintCard(card) {
    if (!card || card.getAttribute('data-incomplete') === '1') return;
    const old = card.querySelector(':scope > .code-lint'); const blk = blocks[card.getAttribute('data-cb')]; if (!blk) return;
    const key = (blk.lang || '') + '\u0000' + (blk.code || '');
    if (old && old.dataset.key === key) return;
    let d = lintCache.get(key);
    if (d === undefined) { try { d = lintCode(blk.lang, blk.code); } catch (_) { d = []; } if (lintCache.size > 300) lintCache.clear(); lintCache.set(key, d); }
    if (old) old.remove(); if (!d.length) return;
    const strip = document.createElement('div'); strip.className = 'code-lint'; strip.dataset.key = key;
    strip.innerHTML = d.slice(0, 6).map((x) => '<div class="cl-' + (x.sev === 'err' ? 'err' : 'warn') + '"><span class="cl-ico">' + (x.sev === 'err' ? '&#9888;' : '&#9679;') + '</span>' + esc((x.line ? 'L' + x.line + ': ' : '') + x.msg) + '</div>').join('');
    const out = card.querySelector(':scope > .code-exec-out'); card.insertBefore(strip, out || null);
  }

  // ── the card ────────────────────────────────────────────────────────────────────────────────────────────
  const ICO = (d) => '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
  const I = {
    copy: ICO('<rect x="5.5" y="5.5" width="8" height="8" rx="1.6"/><path d="M10.5 5.5V3.6A1.1 1.1 0 0 0 9.4 2.5H3.6A1.1 1.1 0 0 0 2.5 3.6v5.8a1.1 1.1 0 0 0 1.1 1.1h1.9"/>'),
    edit: ICO('<path d="M10.8 2.7 13.3 5.2 6 12.5l-3.2.7.7-3.2z"/>'),
    explode: ICO('<circle cx="8" cy="8" r="1.8"/><circle cx="3" cy="3.5" r="1.2"/><circle cx="13" cy="3.5" r="1.2"/><circle cx="3.5" cy="12.8" r="1.2"/><circle cx="12.8" cy="12.5" r="1.2"/><path d="M6.6 6.7 4 4.4M9.4 6.7 12 4.4M6.7 9.4l-2.3 2.5M9.4 9.3l2.4 2.3"/>'),
    prev: ICO('<path d="M1.8 8s2.3-4.3 6.2-4.3S14.2 8 14.2 8s-2.3 4.3-6.2 4.3S1.8 8 1.8 8z"/><circle cx="8" cy="8" r="1.9"/>'),
    pop: ICO('<path d="M9.5 2.5h4v4M13.5 2.5 8 8M6.5 3.5h-3v9h9v-3"/>'),
    run: ICO('<path d="M5 3.2v9.6L12.6 8z"/>'),
    save: ICO('<path d="M8 2.5v7.5M4.8 7 8 10.2 11.2 7M3 13.5h10"/>'),
    canvas: ICO('<rect x="2" y="2.5" width="12" height="11" rx="1.6"/><path d="M8.5 2.5v11M8.5 7h5.5"/>') };
  /* the kit's buttons say data-vo, never data-act: a host that runs its own delegated data-act handler (the canvas, the
     agent loop) must not take a kit click for one of its own actions - both have an "edit", a "save", an "explode" */
  const btn = (act, ico, label, tip, cls) => '<button class="cb-act' + (cls ? ' ' + cls : '') + '" data-vo="' + act + '" title="' + esc(tip) + '">' + (I[ico] || '') + '<span>' + label + '</span></button>';
  // the card's head for a host that draws its own body (the canvas's line-addressable code item): the same bar and
  // buttons, the actions a host lists (default: all that apply), and the host's own buttons after them
  function bar(id, lang, filename, o) {
    o = o || {}; const L = String(lang || '').toLowerCase(); const want = (a) => !o.actions || o.actions.indexOf(a) >= 0;
    const lbl = esc(L || 'plain') + (filename ? ' · <span class="cb-file">' + esc(filename) + '</span>' : '');
    return '<div class="code-bar"><span class="code-lang">' + lbl + '</span><div class="cb-sp"></div>'
      + (want('copy') ? btn('copy', 'copy', 'Copy', 'Copy to the clipboard') : '')
      + (want('edit') ? btn('edit', 'edit', 'Edit', 'Edit this code in place') : '')
      + (want('explode') ? btn('explode', 'explode', 'Explode', 'A structured diagram of this code') : '')
      + (previewable(lang) && want('pop') ? btn('pop', 'pop', 'Pop out', 'Draw it in a window of its own') : '')
      + (runnable(lang) && want('run') ? btn('run', 'run', 'Run', 'Execute it on the Vera host and show the output here (the exec sandbox applies)', 'run') : '')
      + (want('save') ? btn('save', 'save', 'Save', "Save it to the session's artifacts") : '')
      + (o.extra || '') + '</div>';
  }
  // a host's own button in the card's look (its own attribute, its own handler)
  const hostBtn = (attrs, ico, label, tip, on) => '<button class="cb-act' + (on ? ' on' : '') + '" ' + attrs + ' title="' + esc(tip) + '">' + (I[ico] || '') + '<span>' + label + '</span></button>';
  function card(lang, code, o) {
    o = o || {}; lang = String(lang || ''); code = String(code || '');
    const L = lang.toLowerCase(), fname = String(o.filename || ''), id = o.id || register(lang, code, fname);
    if (o.id) blocks[id] = { lang, code, filename: fname };
    const lbl = esc(L || 'plain') + (fname ? ' · <span class="cb-file">' + esc(fname) + '</span>' : '');
    if (o.incomplete) return '<div class="code-card" data-cb="' + id + '" data-incomplete="1"><div class="code-bar"><span class="code-lang">' + lbl
      + '</span><div class="cb-sp"></div><span class="cb-stream">streaming</span></div><pre><code' + langClass(lang) + '>' + highlight(code, lang) + '</code></pre></div>';
    const page = wholePage(lang, code);
    return '<div class="code-card" data-cb="' + id + '"' + (page ? ' data-auto-prev="1"' : '') + '><div class="code-bar"><span class="code-lang">' + lbl + '</span><div class="cb-sp"></div>'
      + btn('copy', 'copy', 'Copy', 'Copy to the clipboard')
      + btn('edit', 'edit', 'Edit', 'Edit this code in place - Run, Preview and Save use your version')
      + btn('explode', 'explode', 'Explode', 'A structured diagram of this code - symbols, calls, imports, lint')
      + (previewable(lang) ? btn('preview', 'prev', page ? 'Source' : 'Preview', 'Draw it here - a sandboxed frame, no network', 'cb-prevbtn')
          + btn('pop', 'pop', 'Pop out', 'Draw it in a window of its own')
          + (o.pane ? btn('pane', 'prev', 'Pane', 'Open it in the preview pane, larger') : '') : '')   // only where the host has a pane (the chat says so)
      + (runnable(lang) ? btn('run', 'run', 'Run', 'Execute it on the Vera host and show the output here (the exec sandbox applies)', 'run') : '')
      + btn('save', 'save', 'Save', "Save it to the session's artifacts")
      + (o.canvas === false ? '' : btn('canvas', 'canvas', 'Canvas', 'Put this code on the session canvas'))
      + '</div><pre><code' + langClass(lang) + '>' + highlight(code, lang) + '</code></pre>'
      + '<div class="code-exec-out" data-exec-out="' + id + '" style="display:none"></div></div>';
  }
  // a whole fence: diagrams and widgets draw as themselves, everything else is a code card
  function fence(lang, info, code, st, opts) {
    st = st || {}; opts = opts || {}; const L = String(lang || '').toLowerCase();
    if (L === 'mermaid' && !st.incomplete) { const id = register('mermaid', code, ''); return '<div class="vo-mm" data-vo-mm="' + id + '"></div>'; }
    return card(lang, code, { filename: fenceFilename(info) || String(opts.title || ''), incomplete: !!st.incomplete, canvas: opts.canvas });
  }
  function render(md, opts) {
    opts = opts || {}; const t = String(md == null ? '' : md);
    const f = (lang, info, code, st) => { if (opts.fence) { try { const h = opts.fence(lang, info, code, st || {}); if (h) return h; } catch (_) {} }
      return fence(lang, info, code, st, opts); };
    if (window.VeraMD && typeof VeraMD.render === 'function') { try { return VeraMD.render(t, { fence: f }); } catch (e) { /* fall through */ } }
    // without the markdown element: the fences still become cards, the prose stays readable
    return esc(t).replace(/```(\w*)([^\n]*)\n?([\s\S]*?)```/g, (_, l, i, c) => f(l, i.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'),
      c.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'), { incomplete: false }))
      .replace(/\n/g, '<br>');
  }

  // ── the actions ─────────────────────────────────────────────────────────────────────────────────────────
  const hooks = {};
  const session = () => { try { return (hooks.session && hooks.session()) || ''; } catch (_) { return ''; } };
  async function cap(name, args) {
    const r = await fetch(BASE() + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, session_id: session(), arguments: args || {} }) });
    const j = await r.json(); return (j && j.content !== undefined) ? j.content : j;
  }
  function flash(b, ok, txt) { if (!b) return; const s = b.querySelector('span'); const was = s ? s.textContent : '';
    b.classList.remove('ok', 'err'); b.classList.add(ok ? 'ok' : 'err'); if (s && txt) s.textContent = txt;
    setTimeout(() => { b.classList.remove('ok', 'err'); if (s && txt) s.textContent = was; }, 1400); }
  function preview(id, on) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk) return;
    const want = on == null ? !c.classList.contains('prev') : !!on;
    c.classList.toggle('prev', want);
    const b = c.querySelector('.cb-prevbtn span'); if (b) b.textContent = want ? 'Source' : 'Preview';
    if (!want) return;
    let host = c.querySelector(':scope > .cb-prev');
    if (!host) { host = document.createElement('div'); host.className = 'cb-prev'; c.insertBefore(host, c.querySelector(':scope > .code-lint') || c.querySelector(':scope > .code-exec-out')); }
    const doc = previewDoc(blk.lang, blk.code);
    let f = host.querySelector('iframe');
    if (!f) { f = document.createElement('iframe'); f.setAttribute('sandbox', 'allow-scripts'); f.setAttribute('referrerpolicy', 'no-referrer'); host.appendChild(f); }
    if (f._doc !== doc) { f._doc = doc; f.srcdoc = doc; }
  }
  async function run(id, b) {
    const blk = blocks[id], c = cardOf(id); if (!blk || !c) return;
    const out = c.querySelector(':scope > .code-exec-out'); if (!out) return;
    if (b) b.disabled = true; out.style.display = 'block'; out.innerHTML = '<div class="ce-meta">running…</div>';
    try {
      const r = await fetch(BASE() + '/exec/code/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: blk.lang || '', code: blk.code || '', timeout: 120, session_id: session() }) });
      const j = await r.json(), p = [];
      if (j.error || j.blocked) p.push('<div class="ce-err">' + (j.blocked ? '⛔ ' : '✗ ') + esc(j.error || 'error') + '</div>');
      if (j.stdout) p.push('<pre class="ce-stdout">' + esc(j.stdout) + '</pre>');
      if (j.stderr) p.push('<pre class="ce-stderr">' + esc(j.stderr) + '</pre>');
      p.push('<div class="ce-foot"><span class="' + (j.ok ? 'ce-ok' : 'ce-bad') + '">[rc=' + esc(j.rc != null ? j.rc : '?') + ']</span> <span class="ce-meta">'
        + esc(j.language || blk.lang || '') + (j.bin ? ' · ' + esc(j.bin) : '') + (j.elapsed_ms != null ? ' · ' + esc(j.elapsed_ms) + 'ms' : '') + '</span></div>');
      out.innerHTML = p.join('') || '<div class="ce-meta">(no output)</div>';
    } catch (e) { out.innerHTML = '<div class="ce-err">✗ ' + esc(String(e && e.message || e)) + '</div>'; }
    finally { if (b) b.disabled = false; }
  }
  // the defaults - a host's hook runs first and returns true when it handled the action its own way
  const DEFAULT = {
    async explode(blk, id, b, c) {
      const old = c.querySelector(':scope > .xp-inline'); if (old) { old.remove(); return; }
      const box = document.createElement('div'); box.className = 'xp-inline';
      box.innerHTML = '<div class="xp-h"><span class="xp-t">explode</span><span class="xp-l"></span><span class="cb-sp"></span><button class="cb-act" data-vo="xp-close">close</button></div><div class="xp-body"></div>';
      box.querySelector('.xp-l').textContent = (blk.filename || blk.lang || 'code') + ' · ' + blk.code.split('\n').length + ' lines';
      c.appendChild(box);
      try { await ensureLib('/ui/vera-graph-embed.js', 'vera-graph-embed');
        const el = document.createElement('vera-graph-embed'); el.setAttribute('renderer', 'struct'); el.setAttribute('code', blk.code);
        el.setAttribute('lang', blk.lang || ''); if (blk.filename) el.setAttribute('path', blk.filename); el.setAttribute('height', '330');
        el.style.cssText = 'display:block;width:100%'; box.querySelector('.xp-body').appendChild(el); }
      catch (e) { box.querySelector('.xp-body').textContent = String(e && e.message || e); }
    },
    async save(blk, id, b) {
      const name = window.prompt('Save to artifacts as (filename, may include sub/dir):', blk.filename || ('snippet.' + (EXT[String(blk.lang).toLowerCase()] || 'txt')));
      if (!name) return;
      try { const r = await fetch(BASE() + '/exec/sandbox/write_artifact', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: name, content: blk.code || '', session_id: session() }) });
        const j = await r.json(); flash(b, r.ok && j.ok !== false, r.ok && j.ok !== false ? 'Saved' : 'Failed'); }
      catch (e) { flash(b, false, 'Failed'); }
    },
    pop(blk) { try { const u = URL.createObjectURL(new Blob([previewDoc(blk.lang, blk.code)], { type: 'text/html' })); window.open(u, '_blank', 'noopener'); } catch (_) {} },
    async canvas(blk, id, b) {
      const sid = session(); if (!sid) { flash(b, false, 'No session'); return; }
      try { const r = await cap('canvas.add', { session_id: sid, kind: 'code', key: 'code:' + hash(blk.lang + '\u0000' + blk.code),
          content: { code: blk.code, lang: blk.lang, filename: blk.filename || '', title: blk.filename || (blk.lang || 'code') }, at: 'now' });
        flash(b, !!(r && r.ok), r && r.ok ? 'On canvas' : 'Failed');
        if (r && r.ok) document.dispatchEvent(new CustomEvent('vera:output:canvas', { detail: { id, key: r.key || '' } })); }
      catch (e) { flash(b, false, 'Failed'); }
    } };
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); }
  async function act(name, id, b, c) {
    const blk = blocks[id]; if (!blk) return;
    if (name === 'copy') { try { await navigator.clipboard.writeText(blk.code); flash(b, true, 'Copied'); } catch (_) { flash(b, false, 'Failed'); } return; }
    if (name === 'edit') return edit(id);
    if (name === 'preview') return preview(id);
    if (name === 'run') return run(id, b);
    // the hooks of the root the card is in: a shadow root's own (wire(root, hooks)), the document's VeraOut.hooks - a
    // card in the loop's or the canvas's shadow root is never handed to the chat's explode or save, which look for it
    // in the document and would quietly find nothing
    const rt = c && c.getRootNode ? c.getRootNode() : document;
    const H = (rt && rt.__voHooks) || (rt === document ? hooks : null);
    if (H && H[name]) { try { if (await H[name](blk, id, b, c)) return; } catch (e) { console.warn('VeraOut hook ' + name, e); } }
    if (DEFAULT[name]) return DEFAULT[name](blk, id, b, c);
  }

  // ── editing ─────────────────────────────────────────────────────────────────────────────────────────────
  /* THE CODE IS YOURS TO CHANGE (owner, 2026-10-03: "need the ability to edit code that has been output"). Edit turns
     the block into an editor of the same size; Apply keeps your version - the card is redrawn and relinted, Run,
     Preview, Save, Canvas and Explode all use it from then on - and the host hears about it (hooks.edited, and a
     composed 'vera:code:edited' event) so the chat or the canvas can keep it with the message or the item. */
  function edit(id) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk || c.classList.contains('editing')) return;
    const pre = c.querySelector(':scope > pre'); if (!pre) return;
    c.classList.add('editing'); if (c.classList.contains('prev')) preview(id, false);
    const ta = document.createElement('textarea'); ta.className = 'cb-editor'; ta.spellcheck = false; ta.value = blk.code;
    ta.style.height = Math.max(120, Math.min(560, pre.offsetHeight + 24)) + 'px';
    const bar = document.createElement('div'); bar.className = 'cb-editbar';
    bar.innerHTML = '<span class="cb-edithint">editing · Ctrl+Enter applies · Esc cancels</span><span class="cb-sp"></span>'
      + '<button class="cb-act" data-vo="edit-cancel">Cancel</button><button class="cb-act run" data-vo="edit-apply">Apply</button>';
    pre.after(ta); ta.after(bar);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') { e.preventDefault(); const s = ta.selectionStart, en = ta.selectionEnd; ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(en); ta.selectionStart = ta.selectionEnd = s + 2; }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); editDone(id, true); }
      else if (e.key === 'Escape') { e.preventDefault(); editDone(id, false); } });
    try { ta.focus(); } catch (_) {}
  }
  function editDone(id, apply) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk) return;
    const ta = c.querySelector(':scope > .cb-editor'), bar = c.querySelector(':scope > .cb-editbar');
    const next = ta ? ta.value : blk.code; if (ta) ta.remove(); if (bar) bar.remove(); c.classList.remove('editing');
    if (!apply || next === blk.code) return;
    const before = blk.code; blk.code = next;
    const code = c.querySelector(':scope > pre > code'); if (code) code.innerHTML = highlight(next, blk.lang);
    c.dataset.edited = '1'; lintCard(c);
    const out = c.querySelector(':scope > .code-exec-out'); if (out) { out.style.display = 'none'; out.innerHTML = ''; }
    const detail = { id, lang: blk.lang, code: next, before, filename: blk.filename };
    try { const rt = c.getRootNode ? c.getRootNode() : document; const H = (rt && rt.__voHooks) || (rt === document ? hooks : null); if (H && H.edited) H.edited(blk, id, detail, c); } catch (_) {}
    c.dispatchEvent(new CustomEvent('vera:code:edited', { bubbles: true, composed: true, detail }));
  }

  // ── wiring ──────────────────────────────────────────────────────────────────────────────────────────────
  function wire(root, rootHooks) {
    root = root || document; roots.add(root);
    if (rootHooks && root !== document) root.__voHooks = Object.assign(root.__voHooks || {}, rootHooks);
    if (root.__voWired) return root; root.__voWired = true;
    root.addEventListener('click', (e) => {
      const b = e.target && e.target.closest && e.target.closest('.code-card [data-vo]'); if (!b) return;
      const c = b.closest('.code-card'); const id = c && c.getAttribute('data-cb'); if (!id) return;
      const a = b.getAttribute('data-vo'); e.preventDefault(); e.stopPropagation();
      if (a === 'edit-apply') return editDone(id, true);
      if (a === 'edit-cancel') return editDone(id, false);
      if (a === 'xp-close') { const x = b.closest('.xp-inline'); if (x) x.remove(); return; }
      act(a, id, b, c);
    });
    if (root !== document) adopt(root);
    return root;
  }
  const libs = {};
  function ensureLib(src, tag) {
    if (tag && customElements.get(tag)) return Promise.resolve(true);
    if (libs[src]) return libs[src];
    libs[src] = new Promise((res) => { const s = document.createElement('script'); s.src = BASE() + src; s.onload = () => res(true); s.onerror = () => res(false); document.head.appendChild(s); });
    return libs[src];
  }
  // after HTML lands: lint the finished cards, open whole pages, draw the diagrams
  function hydrate(root) {
    root = root || document;
    if (root.getRootNode && root.getRootNode() !== document) { const r = root.getRootNode(); if (r && r.host) wire(r); }
    let lintOn = true; try { const t = document.getElementById('cfgLintCode'); if (t && t.checked === false) lintOn = false; } catch (_) {}
    root.querySelectorAll('.code-card[data-cb]').forEach((c) => {
      if (lintOn) lintCard(c);
      if (c.getAttribute('data-auto-prev') === '1' && !c.dataset.autoDone) { c.dataset.autoDone = '1'; preview(c.getAttribute('data-cb'), true); }
    });
    const mm = root.querySelectorAll('.vo-mm[data-vo-mm]:not([data-drawn])');
    if (mm.length) ensureLib('/ui/elements/vera_mermaid.js', 'vera-mermaid').then(() => mm.forEach((slot) => {
      const blk = blocks[slot.getAttribute('data-vo-mm')]; if (!blk || slot.dataset.drawn) return; slot.dataset.drawn = '1';
      const el = document.createElement('vera-mermaid'); el.setAttribute('popout', ''); slot.appendChild(el);
      try { el.render(blk.code); } catch (_) { slot.textContent = blk.code; } }));
  }

  // ── the look (one copy: the document gets it on load, a shadow root through wire/adopt) ────────────────
  const CSS = `
.code-card{margin:6px 0;border:1px solid var(--border,rgba(255,255,255,.09));border-radius:var(--ui-radius,6px);overflow:hidden;background:var(--bg2,#1a1d24)}
.code-card pre{margin:0;padding:10px 12px;overflow:auto;background:var(--bg0,#0e0f12);font-family:var(--mono,ui-monospace,monospace);font-size:11.5px;line-height:1.5;color:var(--text,#d8dce4);max-height:420px}
.code-card pre code{font-family:inherit;background:transparent;padding:0;color:inherit;white-space:pre}
.code-card .code-bar{display:flex;align-items:center;flex-wrap:wrap;gap:2px;padding:3px 6px 3px 10px;background:var(--bg1,#14161b);border-bottom:1px solid var(--border,rgba(255,255,255,.09));font-family:var(--mono,ui-monospace,monospace);font-size:9px}
.code-card .code-lang{color:var(--acc,#5a9e8f);text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.code-card .cb-file{color:var(--acc2,#8fb87a);text-transform:none;letter-spacing:0}
.code-card .cb-sp{flex:1}
.code-card .cb-act{display:inline-flex;align-items:center;gap:4px;padding:3px 7px;font:500 10px/1 var(--f-ui,var(--sans,system-ui,sans-serif));border:none;background:transparent;color:var(--dim2,#8a92a0);border-radius:var(--r-sm,5px);cursor:pointer;transition:background .12s,color .12s}
.code-card .cb-act svg{width:12px;height:12px;flex:0 0 auto}
.code-card .cb-act:hover,.code-card .cb-act:focus-visible{background:var(--surf2,rgba(255,255,255,.06));color:var(--text,#d8dce4);outline:none}
.code-card .cb-act.run{color:var(--acc2,#8fb87a)}
.code-card .cb-act.ok{color:var(--ok,#7bc47f)}.code-card .cb-act.err{color:var(--err,#e06c6c)}
.code-card .cb-act:disabled{opacity:.5;cursor:default}
.code-card .cb-stream{color:var(--dim2,#8a92a0);font-size:9px;font-style:italic}
.code-card .cb-stream::after{content:'';display:inline-block;width:5px;height:5px;margin-left:5px;border-radius:50%;background:var(--acc,#5a9e8f);animation:vo-pulse 1s ease-in-out infinite;vertical-align:middle}
@keyframes vo-pulse{0%,100%{opacity:.25}50%{opacity:1}}
.code-card .code-exec-out{border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg0,#0e0f12);font-family:var(--mono,ui-monospace,monospace);font-size:11px;line-height:1.5;max-height:340px;overflow:auto}
.code-card .code-exec-out pre{margin:0;padding:6px 12px;white-space:pre-wrap;word-break:break-word;background:transparent;max-height:none}
.code-card .ce-stdout{color:var(--text,#d8dce4)}.code-card .ce-stderr{color:#e0a060}.code-card .ce-err{padding:6px 12px;color:#e06060}
.code-card .ce-meta{padding:6px 12px;color:var(--dim2,#8a92a0);font-size:9.5px}
.code-card .ce-foot{padding:5px 12px;border-top:1px solid var(--border,rgba(255,255,255,.09));display:flex;gap:8px;align-items:center}
.code-card .ce-foot .ce-meta{padding:0}.code-card .ce-ok{color:var(--acc2,#8fb87a);font-weight:600}.code-card .ce-bad{color:#e06060;font-weight:600}
.code-card .code-lint{border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg0,#0e0f12);font-family:var(--mono,ui-monospace,monospace);font-size:10px;line-height:1.55;padding:4px 10px}
.code-card .code-lint .cl-err{color:var(--err,#e06c6c)}.code-card .code-lint .cl-warn{color:var(--warn,#d9a54a)}.code-card .code-lint .cl-ico{margin-right:5px}
.code-card .cb-prev{display:none}
.code-card.prev .cb-prev{display:block;border-top:1px solid var(--border,rgba(255,255,255,.09))}
.code-card.prev .cb-prev iframe{display:block;width:100%;height:320px;border:0;background:#fff}
.code-card.prev > pre{display:none}
.code-card.editing > pre{display:none}
.code-card .cb-editor{display:block;width:100%;box-sizing:border-box;margin:0;padding:10px 12px;border:none;outline:none;resize:vertical;background:var(--bg0,#0e0f12);color:var(--text,#d8dce4);font-family:var(--mono,ui-monospace,monospace);font-size:11.5px;line-height:1.5;tab-size:2;box-shadow:inset 2px 0 0 var(--acc,#5a9e8f)}
.code-card .cb-editbar{display:flex;align-items:center;gap:4px;padding:3px 6px 3px 10px;border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg1,#14161b)}
.code-card .cb-edithint{font:9px var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0)}
.code-card[data-edited] .code-lang::after{content:' · edited';color:var(--warn,#d9a54a);text-transform:none;letter-spacing:0}
.code-card .xp-inline{border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg1,#14161b)}
.code-card .xp-h{display:flex;align-items:center;gap:8px;padding:3px 6px 3px 10px;font:9px var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0)}
.code-card .xp-t{color:var(--acc,#5a9e8f)}.code-card .xp-body{height:340px}
.vo-mm{margin:8px 0}
.hl-kw{color:var(--acc,#5a9e8f);font-weight:600}.hl-str{color:var(--acc2,#8fb87a)}.hl-com{color:var(--dim,#6b7280);font-style:italic}
.hl-add{background:rgba(95,207,154,.14);color:var(--acc2,#8fb87a);display:inline-block;width:100%}
.hl-del{background:rgba(232,112,107,.14);color:var(--err,#e06c6c);display:inline-block;width:100%}
.hl-hunk{color:var(--acc,#5a9e8f);display:inline-block;width:100%}
.hl-num{color:var(--acc3,#c9a96e)}.hl-fn{color:var(--acc4,#8fa7d9)}.hl-type{color:var(--acc4,#8fa7d9)}.hl-tag{color:var(--acc,#5a9e8f)}.hl-attr{color:var(--acc2,#8fb87a)}`;
  function adopt(root) {
    root = root || document; const host = root === document ? document.head : root;
    if (!host || (host.querySelector && host.querySelector(':scope > style[data-vera-output]'))) return;
    const st = document.createElement('style'); st.setAttribute('data-vera-output', ''); st.textContent = CSS;
    if (root === document) host.insertBefore(st, host.firstChild); else host.appendChild(st);   // the document's own sheets still win there
  }

  window.VeraOut = { version: 1, render, card, bar, hostBtn, icons: I, fence, highlight, lintCode, lintCard, hydrate, wire, adopt, preview, run, edit,
    register, blocks, hooks, previewable, runnable, wholePage, previewDoc, fenceFilename, langClass, ensureLib, css: CSS };
  const boot = () => { adopt(document); wire(document); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();

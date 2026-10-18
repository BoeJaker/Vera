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
    yaml: 'yaml', yml: 'yaml', markdown: 'md', md: 'md', svg: 'svg', jsx: 'jsx', tsx: 'tsx', java: 'java', c: 'c', cpp: 'cpp', mermaid: 'mmd' };
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
    canvas: ICO('<rect x="2" y="2.5" width="12" height="11" rx="1.6"/><path d="M8.5 2.5v11M8.5 7h5.5"/>'),
    outline: ICO('<path d="M3 4h10M5 8h8M5 12h8"/><circle cx="2.5" cy="8" r=".6"/><circle cx="2.5" cy="12" r=".6"/>'),
    full: ICO('<path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/>'),
    vscode: ICO('<path d="M11.5 2.5 6 7.6 3.2 5.4 2 6.1v3.8l1.2.7L6 8.4l5.5 5.1 2.5-1.2V3.7z"/>'),
    send: ICO('<path d="M2.5 8h10M9 4.5 12.5 8 9 11.5"/>'),
    html: ICO('<path d="M4 2.5h5.5L12.5 5.5v8H4z"/><path d="M9.5 2.5v3h3M6 9.5h4.5M6 11.5h3"/>'),
    podcast: ICO('<rect x="6" y="2" width="4" height="7.5" rx="2"/><path d="M3.8 7.8a4.2 4.2 0 0 0 8.4 0M8 12v2"/>'),
    notebook: ICO('<path d="M4 2.5h8.5v11H4a1.5 1.5 0 0 1-1.5-1.5v-8A1.5 1.5 0 0 1 4 2.5z"/><path d="M5.5 2.5v11M7.5 6h3.5"/>'),
    ide: ICO('<path d="m5.5 5-3 3 3 3M10.5 5l3 3-3 3"/>'),
    project: ICO('<path d="M2.5 4.5h4l1.5 1.5h5.5v7h-11z"/>'),
    think: ICO('<path d="M5.2 11.5h5.6M6 13.5h4M8 2.5a4 4 0 0 0-2.4 7.2c.4.3.6.8.6 1.3h3.6c0-.5.2-1 .6-1.3A4 4 0 0 0 8 2.5z"/>'),
    channel: ICO('<path d="M2.5 5.5h9M9 3l2.5 2.5L9 8M13.5 10.5h-9M7 8l-2.5 2.5L7 13"/>') };

  /* ── the code body: one numbered line per line (owner, 2026-10-03: line numbers, the place a definition is jumped to,
     and the canvas's explode binding - which lights a span of .vc-line rows - all read the same markup now). The number
     is in a gutter selection never reaches, so a copy is still the code. */
  function splitHl(html) {
    const out = []; const open = []; let cur = '';
    const re = /(<span\b[^>]*>)|(<\/span>)|(\n)|([^<\n]+)|(<)/g; let m;
    while ((m = re.exec(String(html || '')))) {
      if (m[1]) { open.push(m[1]); cur += m[1]; }
      else if (m[2]) { open.pop(); cur += m[2]; }
      else if (m[3]) { cur += '</span>'.repeat(open.length); out.push(cur); cur = open.join(''); }
      else cur += m[0];
    }
    out.push(cur); return out;
  }
  /* ── WHAT THE CODE DEFINES (owner, 2026-10-03: "could the code rendered in vera display details of a function on
     mouseover on a call site of the function, and have other useful helpers like this"). A light reading of the source -
     functions, methods and classes with the line they start on, their signature and the comment or docstring that goes
     with them - for every language family the highlighter knows. Not a parser: enough to name what a call points at. */
  const symCache = new Map();
  function symbols(code, lang) {
    const L = String(lang || '').toLowerCase(), fam = FAMILY[L] || '', key = fam + '\u0000' + code;
    if (symCache.has(key)) return symCache.get(key);
    const out = {}, lines = String(code || '').split('\n');
    const docFor = (i) => {
      const d = [];
      for (let j = i - 1; j >= 0 && j >= i - 8; j--) { const t = lines[j].trim(); const m = t.match(/^(?:#+|\/\/+|\/\*\*?|\*+\/?)\s?(.*?)\s*(?:\*\/)?$/); if (t && m && !/^#!/.test(t)) d.unshift(m[1]); else break; }
      if (fam === 'hash') { const n = (lines[i + 1] || '').trim(); const m = n.match(/^(?:[rbu]?)("""|''')(.*)$/);
        if (m) { let s = m[2]; const end = s.indexOf(m[1]); if (end >= 0) s = s.slice(0, end); else for (let j = i + 2; j < lines.length && j < i + 10; j++) { const t = lines[j].trim(); const k = t.indexOf(m[1]); if (k >= 0) { s += ' ' + t.slice(0, k); break; } s += ' ' + t; } d.push(s); } }
      return d.join(' ').replace(/\s+/g, ' ').trim().slice(0, 320);
    };
    const add = (name, kind, i, sig) => { if (!name || out[name]) return; out[name] = { name, kind, line: i + 1, sig: String(sig).trim().replace(/[{:]\s*$/, '').slice(0, 180), doc: docFor(i) }; };
    lines.forEach((ln, i) => { let m;
      if (fam === 'hash') {
        if ((m = ln.match(/^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/))) add(m[1], 'function', i, ln);
        else if ((m = ln.match(/^\s*class\s+([A-Za-z_]\w*)/))) add(m[1], 'class', i, ln);
        else if ((m = ln.match(/^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{/))) add(m[1], 'function', i, ln);
      } else {
        if ((m = ln.match(/\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(/))) add(m[1], 'function', i, ln);
        else if ((m = ln.match(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/))) add(m[1], 'function', i, ln);
        else if ((m = ln.match(/\bclass\s+([A-Za-z_$][\w$]*)/))) add(m[1], 'class', i, ln);
        else if ((m = ln.match(/\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(/))) add(m[1], 'function', i, ln);
        else if ((m = ln.match(/\bfn\s+([A-Za-z_]\w*)/))) add(m[1], 'function', i, ln);
        else if ((m = ln.match(/^\s*(?:(?:public|private|protected|static|async|override|virtual|final|export|default)\s+)*(?:[\w<>\[\],.*&]+\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$/)) && !/^(if|for|while|switch|catch|return|else|function)$/.test(m[1])) add(m[1], 'method', i, ln);
      } });
    if (symCache.size > 300) symCache.clear();
    symCache.set(key, out); return out;
  }
  // the names every reply leans on: a call to one of these is explained without a definition in sight
  const BUILTIN = {
    print: 'print(*values, sep=" ", end="\\n") - write the values to standard output', len: 'len(obj) - the number of items in a container',
    range: 'range(start, stop[, step]) - a sequence of integers', open: 'open(file, mode="r") - open a file and return a file object',
    enumerate: 'enumerate(iterable, start=0) - pairs of (index, item)', zip: 'zip(*iterables) - tuples taking one item from each',
    sorted: 'sorted(iterable, key=None, reverse=False) - a new sorted list', isinstance: 'isinstance(obj, cls) - is obj an instance of cls',
    map: 'map(fn, iterable) - fn applied to each item', filter: 'filter(fn, iterable) - the items fn keeps', sum: 'sum(iterable, start=0) - the total',
    min: 'min(iterable) - the smallest item', max: 'max(iterable) - the largest item', str: 'str(obj) - the text form', int: 'int(x) - an integer',
    dict: 'dict(**kw) - a mapping', list: 'list(iterable) - a list', set: 'set(iterable) - a set', input: 'input(prompt) - a line from standard input',
    require: 'require(id) - load a CommonJS module', fetch: 'fetch(url, init) - a Promise of the HTTP Response', setTimeout: 'setTimeout(fn, ms) - run fn once after ms',
    parseInt: 'parseInt(text, radix) - an integer from text', Promise: 'Promise - a value that arrives later', JSON: 'JSON.parse / JSON.stringify - text <-> values' };
  function codeBody(code, lang, lines) {
    const src = String(code || '').replace(/\n$/, ''); const hl = highlight(src, lang);
    if (lines === false) return '<pre><code' + langClass(lang) + '>' + hl + '</code></pre>';
    return '<pre class="vo-lines vc-pre vc-code"><code' + langClass(lang) + '>' + splitHl(hl).map((l, i) =>
      '<span class="vc-line" data-line="' + (i + 1) + '"><i class="vc-lno">' + (i + 1) + '</i>' + (l || ' ') + '</span>').join('') + '</code></pre>';
  }
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
  /* A HEADLESS CARD (o.bar === false): for a host that draws one head of its own around the card (the canvas's item
     container, DEEP-29 11A.4). The bar is still made - hidden - so the card's actions keep finding it (a label that
     toggles, the outline under it, a run's flash); the host lists them with barActions(card) and runs one with
     actOn(card, name), and the bar's dropdowns (Send to, Download) are handed over whole, to be drawn in its menu. */
  const barOpen = (o) => '<div class="code-bar"' + (o && o.bar === false ? ' hidden' : '') + '>';
  function barActions(c) {
    const bar = c && c.querySelector && c.querySelector(':scope > .code-bar'); if (!bar) return { acts: [], menus: [] };
    const acts = [...bar.querySelectorAll(':scope > .cb-act[data-vo]')].filter((b) => !b.disabled).map((b) => {
      const s = b.querySelector('span'); return { vo: b.getAttribute('data-vo'), label: String((s ? s.textContent : b.textContent) || '').trim(), title: b.title || '', on: b.classList.contains('on') }; });
    const menus = [...bar.querySelectorAll(':scope > .vo-send')].map((x) => x.outerHTML);
    return { acts, menus };
  }
  function actOn(c, name, b) {
    const id = c && c.getAttribute && c.getAttribute('data-cb'); if (!id || !name) return;
    return act(String(name), id, b || c.querySelector(':scope > .code-bar [data-vo="' + String(name).replace(/"/g, '') + '"]'), c);
  }
  // a host's own button in the card's look (its own attribute, its own handler)
  const hostBtn = (attrs, ico, label, tip, on) => '<button class="cb-act' + (on ? ' on' : '') + '" ' + attrs + ' title="' + esc(tip) + '">' + (I[ico] || '') + '<span>' + label + '</span></button>';
  /* THE ONE CODE CARD (owner, 2026-10-03: "the code renderer in the canvas does not have the same options as the one in
     the chat - it should - and it should be using the same underlying code"). Every surface calls this. A host may:
       o.id        keep a stable id (the canvas: one per item key)      o.filename   the file it is
       o.lines     numbered lines (default on)                           o.incomplete still streaming: no actions yet
       o.canvas    false where it already IS on the canvas               o.pane       the host has a preview pane
       o.body      draw its own body in place of the source (the canvas's live-layer preview, which survives a redraw)
       o.prevOn    that body is showing the drawing (the Preview button says Source)
       o.extra     its own buttons after the kit's, in the same look     o.cls        classes on the card
       o.autoPrev  false where the host decides what is drawn (the canvas: a page it switched to source stays source) */
  // the code a card holds, as a fence: what "Send to" hands on (an IDE file gets the bare code instead)
  const fenceOf = (id) => { const b = blocks[id] || {}; return '```' + (b.lang || '') + (b.filename ? ' file=' + b.filename : '') + '\n' + String(b.code || '').replace(/\n$/, '') + '\n```'; };
  const sendFor = (id, title, skip) => sendMenu('cb:' + id, { getMd: () => fenceOf(id), raw: () => String((blocks[id] || {}).code || ''), title, skip: (skip || []).concat(['files']) });
  function card(lang, code, o) {
    o = o || {}; lang = String(lang || ''); code = String(code || '');
    const L = lang.toLowerCase(), fname = String(o.filename || ''), id = o.id || register(lang, code, fname);
    if (o.id) blocks[id] = Object.assign(blocks[id] || {}, { lang, code, filename: fname });
    const lbl = esc(L || 'plain') + (fname ? ' · <span class="cb-file">' + esc(fname) + '</span>' : '');
    const cls = 'code-card' + (o.cls ? ' ' + o.cls : '');
    if (o.incomplete) return '<div class="' + cls + '" data-cb="' + id + '" data-incomplete="1">' + barOpen(o) + '<span class="code-lang">' + lbl
      + '</span><div class="cb-sp"></div><span class="cb-stream">streaming</span></div>' + (o.body || codeBody(code, lang, o.lines)) + '</div>';
    const page = wholePage(lang, code) && !o.body && o.autoPrev !== false;
    const nSym = Object.keys(symbols(code, lang)).length;
    return '<div class="' + cls + '" data-cb="' + id + '"' + (page ? ' data-auto-prev="1"' : '') + '>' + barOpen(o) + '<span class="code-lang">' + lbl + '</span><div class="cb-sp"></div>'
      + btn('copy', 'copy', 'Copy', 'Copy to the clipboard')
      + btn('edit', 'edit', 'Edit', 'Edit this code - in place, or full screen; Run, Preview and Save use your version')
      + btn('explode', 'explode', 'Explode', 'A structured diagram of this code - symbols, calls, imports, lint')
      + (nSym > 1 ? btn('outline', 'outline', 'Outline', 'Its functions and classes - pick one to go to it') : '')
      + (previewable(lang) ? btn('preview', 'prev', (page || o.prevOn) ? 'Source' : 'Preview', 'Draw it here - a sandboxed frame, no network', 'cb-prevbtn')
          + btn('pop', 'pop', 'Pop out', 'Draw it in a window of its own')
          + (o.pane ? btn('pane', 'prev', 'Pane', 'Open it in the preview pane, larger') : '') : '')   // only where the host has a pane (the chat says so)
      + (runnable(lang) ? btn('run', 'run', 'Run', 'Execute it on the Vera host and show the output here (the exec sandbox applies)', 'run') : '')
      + btn('save', 'save', 'Save', "Save it to the session's artifacts")
      + (o.canvas === false ? '' : btn('canvas', 'canvas', 'Canvas', 'Put this code on the session canvas'))
      + (o.extra || '')
      + sendFor(id, fname || (L || 'code'), ['podcast'])
      + '</div>' + (o.body || codeBody(code, lang, o.lines))
      + '<div class="code-exec-out" data-exec-out="' + id + '" style="display:none"></div></div>';
  }
  /* A DIAGRAM IS A CARD TOO (owner, 2026-10-03: "mermaid diagrams rendered in the chat and canvas need the unified menu
     style and the relevant options for along the top same as the rendered code output and elsewhere"). The same bar
     in the same look over the drawing: Copy (its source), Edit (the diagram is redrawn from your version), Source (the
     mermaid under the drawing), Pop out, Save, Canvas, Send to. The drawing is the host's: the kit's own slot by
     default, the chat's live slot or the canvas's live layer where they hand one in (o.body).
       VeraOut.diagram(code, {id, title, body, canvas, extra, cls}) */
  function diagram(code, o) {
    o = o || {}; code = String(code || ''); const title = String(o.title || '');
    const id = o.id || register('mermaid', code, '');
    if (o.id) blocks[id] = Object.assign(blocks[id] || {}, { lang: 'mermaid', code, filename: '' });
    blocks[id].title = title;
    const lbl = 'mermaid' + (title ? ' · <span class="cb-file">' + esc(title) + '</span>' : '');
    return '<div class="code-card vo-diagram' + (o.cls ? ' ' + o.cls : '') + '" data-cb="' + id + '">' + barOpen(o) + '<span class="code-lang">' + lbl + '</span><div class="cb-sp"></div>'
      + btn('copy', 'copy', 'Copy', 'Copy the mermaid source')
      + btn('edit', 'edit', 'Edit', 'Edit the source - the diagram is redrawn from your version')
      + btn('dsrc', 'outline', 'Source', 'Show the mermaid source under the diagram', 'cb-srcbtn')
      + btn('dpop', 'pop', 'Pop out', 'Open the diagram in a window over the page')
      + btn('dsvg', 'save', 'SVG', 'Download the drawing as SVG') + btn('dpng', 'save', 'PNG', 'Download the drawing as PNG')
      + btn('save', 'save', 'Save', "Save the source to the session's artifacts")
      + (o.canvas === false ? '' : btn('canvas', 'canvas', 'Canvas', 'Put this diagram on the session canvas'))
      + (o.extra || '')
      + sendFor(id, title || 'Diagram', ['podcast'])
      + '</div>' + (o.body || '<div class="vo-mm" data-vo-mm="' + id + '"></div>') + codeBody(code, 'mermaid') + '</div>';
  }
  /* ── FILES: documents, presentations, spreadsheets (owner, 2026-10-04: "add options for documents, presentations and
     spreadsheets and rendering options for these documents. in the canvas and chat and they should stream in like
     existing renders like code and mermaid. id like as many types as you can implement") ────────────────────────────
     A ```slides fence is a deck you page through (and present), a ```sheet / ```csv / ```tsv fence a grid that sorts and
     works out its formulas, a ```document fence a page. Each is a card like the code card - Copy, Edit, Source, Canvas,
     Send to - with a Download menu of every file it can be (render.export makes them: pptx, odp, pdf, a web slideshow;
     xlsx, ods, csv, tsv, json; docx, pdf, odt, rtf, epub, html, markdown, text). While the reply streams, the card draws
     what has arrived: the slides so far (the one being written shown), the rows so far, the page so far. */
  const FILE_KIND = { slides: 'slides', deck: 'slides', presentation: 'slides', slide: 'slides',
    sheet: 'sheet', spreadsheet: 'sheet', csv: 'sheet', tsv: 'sheet', xlsx: 'sheet',
    document: 'document', doc: 'document', docx: 'document' };
  const FILE_FORMATS = {
    slides: [['pptx', 'PowerPoint (.pptx)'], ['odp', 'OpenDocument slides (.odp)'], ['slides_pdf', 'PDF'], ['deck', 'Web slideshow (.html)'], ['md', 'Markdown (.md)']],
    sheet: [['xlsx', 'Excel (.xlsx)'], ['ods', 'OpenDocument spreadsheet (.ods)'], ['csv', 'CSV (.csv)'], ['tsv', 'Tab-separated (.tsv)'], ['json', 'JSON rows (.json)']],
    document: [['docx', 'Word (.docx)'], ['pdf', 'PDF'], ['odt', 'OpenDocument text (.odt)'], ['rtf', 'Rich text (.rtf)'], ['epub', 'E-book (.epub)'], ['html', 'Web page (.html)'], ['md', 'Markdown (.md)'], ['txt', 'Plain text (.txt)']] };
  const fileState = {};   // id -> {i: the slide shown, sort: [col, dir], cell: 'B3'}
  // the slides: cut at --- rules (outside code), else at #/## headings; 'Note:' / '???' starts a slide's notes
  function slidesOf(src) {
    const lines = String(src || '').replace(/\r/g, '').split('\n'); const rule = (l) => /^\s*-{3,}\s*$/.test(l);
    let inCode = false; const hasRule = lines.some((l) => { if (/^\s*(```|~~~)/.test(l)) inCode = !inCode; return !inCode && rule(l); }); inCode = false;
    const parts = [[]];
    lines.forEach((l) => { if (/^\s*(```|~~~)/.test(l)) inCode = !inCode;
      if (!inCode && hasRule && rule(l)) { parts.push([]); return; }
      if (!inCode && !hasRule && /^#{1,2}\s+/.test(l) && parts[parts.length - 1].some((x) => x.trim())) parts.push([]);
      parts[parts.length - 1].push(l); });
    const out = parts.filter((p) => p.some((x) => x.trim())).map((p) => { let title = '', inN = false; const body = [], notes = [];
      p.forEach((l) => { if (!title && /^#{1,6}\s+/.test(l)) { title = l.replace(/^#{1,6}\s+/, '').trim(); return; }
        if (/^\s*(Notes?:|\?\?\?)/.test(l)) { inN = true; l = l.replace(/^\s*(Notes?:|\?\?\?)\s*/, ''); } (inN ? notes : body).push(l); });
      return { title, body: body.join('\n').trim(), notes: notes.join('\n').trim() }; });
    return out.length ? out : [{ title: '', body: '', notes: '' }];
  }
  // a table's rows from CSV / TSV / pipes (quotes honoured), the header first
  /* a model writes markdown inside a sheet fence (a real turn, 2026-10-04: a '# title', a pipe table with **bold** cells
     and formulas, a '> tip' under it): a pipe table in it is THE table; otherwise the separator is read from the data
     lines - never from a heading or a note - and a cell's emphasis marks are not its value */
  const cellClean = (c) => String(c == null ? '' : c).trim().replace(/^(\*\*|__)(.*)\1$/, '$2').replace(/^`(.*)`$/, '$1').trim();
  function rowsOf(src, lang) {
    const all = String(src || '').replace(/\r/g, '').split('\n');
    const sep = (l) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(l);
    const si = all.findIndex((l, i) => sep(l) && i > 0 && all[i - 1].indexOf('|') >= 0);
    if (si > 0) { const out = [all[si - 1]]; for (let i = si + 1; i < all.length && all[i].indexOf('|') >= 0 && all[i].trim(); i++) out.push(all[i]);
      return out.map((l) => l.trim().replace(/^\||\|$/g, '').split('|').map(cellClean)); }
    const text = all.filter((l) => l.trim() && !/^\s*(#{1,6}\s|>)/.test(l)).join('\n'); if (!text.trim()) return [];
    const first = text.split('\n')[0];
    const d = lang === 'tsv' ? '\t' : lang === 'csv' ? ',' : (first.split('\t').length > first.split(',').length ? '\t' : first.indexOf('|') >= 0 && first.indexOf(',') < 0 ? '|' : ',');
    if (d === '|') return text.split('\n').filter((l) => l.trim() && !/^\s*\|?\s*:?-{3,}/.test(l)).map((l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim()));
    const rows = []; let row = [], cell = '', q = false;
    for (let i = 0; i < text.length; i++) { const ch = text[i];
      if (q) { if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') q = false; else cell += ch; continue; }
      if (ch === '"' && !cell) { q = true; continue; }
      if (ch === d) { row.push(cell.trim()); cell = ''; continue; }
      if (ch === '\n') { row.push(cell.trim()); rows.push(row); row = []; cell = ''; continue; }
      cell += ch; }
    row.push(cell.trim()); rows.push(row);
    return rows.filter((r) => r.some((c) => c !== '')).map((r) => r.map(cellClean));
  }
  const NUMRE = /^\s*[-+(]?\s*[$£€¥]?\s*\d[\d,]*(\.\d+)?\s*%?\s*\)?\s*$/;
  const numOf = (s) => { const t = String(s == null ? '' : s).trim(); if (!NUMRE.test(t)) return null;
    let v = parseFloat(t.replace(/[^\d.]/g, '')); if (isNaN(v)) return null; if (/^\s*[-(]/.test(t)) v = -v; if (/%\s*\)?\s*$/.test(t)) v = v / 100; return v; };
  const colN = (L) => { let n = 0; for (const ch of L.toUpperCase()) n = n * 26 + ch.charCodeAt(0) - 64; return n - 1; };
  const colL = (c) => { let s = ''; c += 1; while (c) { const r = (c - 1) % 26; s = String.fromCharCode(65 + r) + s; c = Math.floor((c - 1) / 26); } return s; };
  // what each cell comes to: numbers read, formulas (SUM AVERAGE MIN MAX COUNT, cells, + - * /) worked out
  function sheetValues(rows) {
    const memo = {};
    const cell = (ref, depth) => { const m = /^([A-Za-z]+)(\d+)$/.exec(ref); if (!m) return null; const r = +m[2] - 1, c = colN(m[1]);
      if (!rows[r] || c < 0 || c >= rows[r].length) return null; return val(r, c, depth); };
    const range = (a, b, depth) => { const ma = /^([A-Za-z]+)(\d+)$/.exec(a), mb = /^([A-Za-z]+)(\d+)$/.exec(b); if (!ma || !mb) return []; const xs = [];
      for (let r = Math.min(+ma[2], +mb[2]); r <= Math.max(+ma[2], +mb[2]); r++) for (let c = Math.min(colN(ma[1]), colN(mb[1])); c <= Math.max(colN(ma[1]), colN(mb[1])); c++) {
        const v = cell(colL(c) + r, depth); if (typeof v === 'number') xs.push(v); } return xs; };
    const FN = { SUM: (x) => x.reduce((a, b) => a + b, 0), AVERAGE: (x) => x.length ? x.reduce((a, b) => a + b, 0) / x.length : 0,
      AVG: (x) => x.length ? x.reduce((a, b) => a + b, 0) / x.length : 0, MIN: (x) => x.length ? Math.min(...x) : 0, MAX: (x) => x.length ? Math.max(...x) : 0, COUNT: (x) => x.length };
    function calc(f, depth) { if (depth > 20) return f; let e = f.replace(/^=/, '');
      try { e = e.replace(/\b(SUM|AVERAGE|AVG|MIN|MAX|COUNT)\(([^()]*)\)/gi, (_, n, args) => { let xs = [];
          args.split(',').map((a) => a.trim()).forEach((a) => { if (a.indexOf(':') > 0) xs = xs.concat(range(a.split(':')[0], a.split(':')[1], depth + 1));
            else { const v = /^[A-Za-z]+\d+$/.test(a) ? cell(a, depth + 1) : parseFloat(a); if (typeof v === 'number' && !isNaN(v)) xs.push(v); } });
          return String(FN[n.toUpperCase()](xs)); });
        e = e.replace(/\b([A-Za-z]{1,3}\d+)\b/g, (_, ref) => { const v = cell(ref, depth + 1); return typeof v === 'number' ? String(v) : '0'; });
        if (!/^[\d\s.+\-*/()e]+$/.test(e)) return f;
        const v = Function('"use strict";return (' + e + ')')();   // digits and + - * / ( ) only, checked above
        return typeof v === 'number' && isFinite(v) ? v : f; } catch (_) { return f; } }
    function val(r, c, depth) { const k = r + ',' + c; if (k in memo) return memo[k]; memo[k] = null; const raw = String(rows[r][c] == null ? '' : rows[r][c]);
      const v = raw.trim().charAt(0) === '=' ? calc(raw.trim(), depth || 0) : (numOf(raw) != null && r > 0 ? numOf(raw) : raw); memo[k] = v; return v; }
    return rows.map((row, r) => row.map((_, c) => val(r, c, 0)));
  }
  const fmtNum = (v, raw) => { if (typeof v !== 'number') return String(v); if (/%\s*\)?\s*$/.test(String(raw || ''))) return String(raw).trim();
    if (raw && String(raw).trim().charAt(0) !== '=') return String(raw).trim(); return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(Math.round(v * 10000) / 10000); };
  const mdHtml = (md) => render(md, {});
  function deckView(id, slides, incomplete) {
    const st = fileState[id] || (fileState[id] = { i: 0 }); if (incomplete) st.i = slides.length - 1; st.i = Math.max(0, Math.min(slides.length - 1, st.i | 0));
    const s = slides[st.i] || {}; const cover = st.i === 0 && !s.body;
    return '<div class="vo-deck"><div class="vo-stage"><div class="vo-slide' + (cover ? ' cover' : '') + '">' + (s.title ? '<h1>' + esc(s.title) + '</h1>' : '')
      + '<div class="vo-sb">' + mdHtml(s.body || '') + '</div>' + (incomplete && st.i === slides.length - 1 ? '<span class="vo-writing">writing…</span>' : '') + '</div></div>'
      + '<div class="vo-deck-nav"><button class="cb-act" data-vo="fprev" title="The slide before (←)"' + (st.i ? '' : ' disabled') + '>‹</button><span class="vo-deck-n">' + (st.i + 1) + ' / ' + slides.length + '</span>'
      + '<button class="cb-act" data-vo="fnext" title="The next slide (→)"' + (st.i < slides.length - 1 ? '' : ' disabled') + '>›</button>'
      + '<div class="vo-thumbs">' + slides.map((x, k) => '<button class="vo-thumb' + (k === st.i ? ' on' : '') + '" data-vo="fgo" data-i="' + k + '" title="' + esc(x.title || 'Slide ' + (k + 1)) + '"><b>' + (k + 1) + '</b>' + esc((x.title || '').slice(0, 26)) + '</button>').join('') + '</div></div>'
      + (s.notes ? '<div class="vo-notes"><b>notes</b> ' + esc(s.notes) + '</div>' : '') + '</div>';
  }
  function sheetView(id, rows) {
    const st = fileState[id] || (fileState[id] = {}); if (!rows.length) return '<div class="vo-sheet"><div class="vo-dim">no rows yet</div></div>';
    const vals = sheetValues(rows); const ncol = Math.max(...rows.map((r) => r.length));
    let order = rows.slice(1).map((_, k) => k + 1);
    if (st.sort) { const [c, dir] = st.sort; order.sort((a, b) => { const x = vals[a][c], y = vals[b][c];
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * dir; }); }
    const sel = st.cell || ''; const m = /^([A-Z]+)(\d+)$/.exec(sel); const sr = m ? +m[2] - 1 : -1, sc = m ? colN(m[1]) : -1;
    const fx = sr >= 0 && rows[sr] ? String(rows[sr][sc] == null ? '' : rows[sr][sc]) : '';
    const cellHtml = (r, c) => { const raw = rows[r][c] == null ? '' : rows[r][c]; const v = vals[r][c]; const f = String(raw).trim().charAt(0) === '=';
      return '<td data-vo="fcell" data-cell="' + colL(c) + (r + 1) + '" class="' + (typeof v === 'number' ? 'n' : '') + (f ? ' f' : '') + (r === sr && c === sc ? ' on' : '') + '"' + (f ? ' title="' + esc(raw) + '"' : '') + '>' + esc(fmtNum(v, raw)) + '</td>'; };
    return '<div class="vo-sheet"><div class="vo-fx"><b>' + esc(sel || '—') + '</b><span>' + esc(fx) + (fx.charAt(0) === '=' ? ' → ' + esc(fmtNum(vals[sr][sc], fx)) : '') + '</span></div>'
      + '<div class="vo-grid-w"><table class="vo-grid"><thead><tr><th class="rn"></th>' + Array.from({ length: ncol }, (_, c) => '<th data-vo="fsort" data-col="' + c + '" title="Sort by this column">' + colL(c)
        + (st.sort && st.sort[0] === c ? (st.sort[1] > 0 ? ' ▴' : ' ▾') : '') + '</th>').join('') + '</tr></thead><tbody>'
      + '<tr class="hd"><th class="rn">1</th>' + Array.from({ length: ncol }, (_, c) => '<td data-vo="fcell" data-cell="' + colL(c) + '1" class="h' + (sr === 0 && sc === c ? ' on' : '') + '">' + esc(rows[0][c] == null ? '' : rows[0][c]) + '</td>').join('') + '</tr>'
      + order.map((r) => '<tr><th class="rn">' + (r + 1) + '</th>' + Array.from({ length: ncol }, (_, c) => cellHtml(r, c)).join('') + '</tr>').join('')
      + '</tbody></table></div></div>';
  }
  function docView(md) {
    const heads = []; String(md || '').replace(/^(#{1,3})\s+(.+)$/gm, (_, h, t) => { heads.push([h.length, t.trim()]); return _; });
    return '<div class="vo-doc">' + (heads.length >= 3 ? '<div class="vo-toc">' + heads.slice(0, 14).map((h) => '<span class="l' + h[0] + '">' + esc(h[1]) + '</span>').join('') + '</div>' : '')
      + '<div class="vo-paper">' + mdHtml(md) + '</div></div>';
  }
  function fileView(kind, id, code, lang, incomplete) {
    if (kind === 'slides') return deckView(id, slidesOf(code), incomplete);
    if (kind === 'sheet') return sheetView(id, rowsOf(code, lang));
    return docView(code);
  }
  function fileLabel(kind, code, lang) {
    if (kind === 'slides') { const n = slidesOf(code).length; return 'slides · ' + n + (n === 1 ? ' slide' : ' slides'); }
    if (kind === 'sheet') { const r = rowsOf(code, lang); return 'sheet · ' + Math.max(0, r.length - 1) + ' rows × ' + (r[0] ? r[0].length : 0) + ' cols'; }
    const w = String(code || '').split(/\s+/).filter(Boolean).length; return 'document · ' + w + ' words';
  }
  // the Download menu: every file this card can be, made by render.export
  function dlMenu(id, kind) {
    prose['dl:' + id] = { md: '', title: '', getMd: () => fenceOf(id), kind };
    return '<div class="vo-acts vo-send vo-dl" data-vo-prose="dl:' + esc(id) + '"><div class="ma-dd">' + mact('menu', 'save', 'Download', 'Download it as a file - every type it can be', 'ma-ddb')
      + '<div class="ma-menu" role="menu">' + FILE_FORMATS[kind].map((f) => mitem('dl:' + f[0], 'save', f[1], '')).join('') + '</div></div></div>';
  }
  function fileCard(kind, lang, code, o) {
    o = o || {}; const id = o.id || register(lang, code, ''); if (o.id) blocks[id] = Object.assign(blocks[id] || {}, { lang, code, filename: '' });
    blocks[id].fileKind = kind; blocks[id].title = String(o.title || '');
    const lbl = esc(fileLabel(kind, code, String(lang).toLowerCase()));
    const head = '<div class="code-card vo-file vo-' + kind + (o.cls ? ' ' + o.cls : '') + '" data-cb="' + id + '" data-kind="' + kind + '"' + (o.incomplete ? ' data-incomplete="1"' : '') + '>' + barOpen(o) + '<span class="code-lang">' + lbl + '</span><div class="cb-sp"></div>';
    if (o.incomplete) return head + '<span class="cb-stream">streaming</span></div><div class="vo-fview">' + fileView(kind, id, code, String(lang).toLowerCase(), true) + '</div></div>';
    return head + btn('copy', 'copy', 'Copy', 'Copy its source')
      + btn('edit', 'edit', 'Edit', 'Edit the source - it is redrawn from your version')
      + btn('dsrc', 'outline', 'Source', 'Show the source under it', 'cb-srcbtn')
      + (kind === 'slides' ? btn('present', 'pop', 'Present', 'Full screen, one slide at a time (arrows, Esc)') : '')
      + (kind === 'document' ? btn('fpop', 'pop', 'Pop out', 'Open the page in a window of its own') : '')
      + dlMenu(id, kind)
      + (o.canvas === false ? '' : btn('canvas', 'canvas', 'Canvas', 'Put this on the session canvas'))
      + sendFor(id, kind, ['podcast'])
      + '</div><div class="vo-fview">' + fileView(kind, id, code, String(lang).toLowerCase(), false) + '</div>' + codeBody(code, lang) + '</div>';
  }
  function fileRedraw(c, id) { const blk = blocks[id]; const v = c && c.querySelector(':scope > .vo-fview'); if (!blk || !v) return;
    v.innerHTML = fileView(blk.fileKind, id, blk.code, String(blk.lang).toLowerCase(), false);
    const l = c.querySelector(':scope > .code-bar .code-lang'); if (l) l.textContent = fileLabel(blk.fileKind, blk.code, String(blk.lang).toLowerCase()); }
  function present(id) {
    const blk = blocks[id]; if (!blk) return; const slides = slidesOf(blk.code); let i = (fileState[id] || {}).i | 0;
    const m = document.createElement('div'); m.className = 'vo-present';
    const draw = () => { const s = slides[i] || {}; m.innerHTML = '<div class="vo-slide big' + (i === 0 && !s.body ? ' cover' : '') + '">' + (s.title ? '<h1>' + esc(s.title) + '</h1>' : '') + '<div class="vo-sb">' + mdHtml(s.body || '') + '</div></div>'
      + '<div class="vo-present-n">' + (i + 1) + ' / ' + slides.length + ' · ← → · Esc</div>'; };
    const close = () => { m.remove(); document.removeEventListener('keydown', key, true); try { if (document.fullscreenElement) document.exitFullscreen(); } catch (_) {} };
    const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].indexOf(e.key) >= 0) { e.preventDefault(); i = Math.min(slides.length - 1, i + 1); draw(); }
      else if (['ArrowLeft', 'ArrowUp', 'PageUp'].indexOf(e.key) >= 0) { e.preventDefault(); i = Math.max(0, i - 1); draw(); } };
    m.addEventListener('click', (e) => { i = e.clientX < window.innerWidth / 3 ? Math.max(0, i - 1) : Math.min(slides.length - 1, i + 1); draw(); });
    document.addEventListener('keydown', key, true); adopt(document); document.body.appendChild(m); draw();
    try { m.requestFullscreen && m.requestFullscreen().catch(() => {}); } catch (_) {}
  }
  // a file: render.export makes it, the browser downloads it
  async function download(md, fmt, title, b) {
    const r = await cap('render.export', { content: md, format: fmt, title: title || '' });
    if (!(r && r.ok && r.url)) { flash(b, false, 'Failed'); return r; }
    const a = document.createElement('a'); a.href = BASE() + r.url; a.download = r.filename || ''; a.rel = 'noopener'; document.body.appendChild(a); a.click(); a.remove();
    flash(b, true, 'Downloaded'); return r;
  }
  // a whole fence: diagrams and widgets draw as themselves, everything else is a code card
  function fence(lang, info, code, st, opts) {
    st = st || {}; opts = opts || {}; const L = String(lang || '').toLowerCase();
    if (L === 'mermaid' && !st.incomplete) return diagram(code, { canvas: opts.canvas });
    if (FILE_KIND[L]) return fileCard(FILE_KIND[L], L, code, { incomplete: !!st.incomplete, canvas: opts.canvas, title: opts.title });
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
    // a diagram over the page: the chat's own diagram window hears vm:popout (from any root - the event is composed)
    dpop(blk, id, b, c) { try { (c || document).dispatchEvent(new CustomEvent('vm:popout', { bubbles: true, composed: true, detail: { code: blk.code, title: blk.title || 'Diagram' } })); } catch (_) {} },
    /* PANE, WHERE THERE IS NO PANE (owner, 2026-10-03: "the pane button on code doe nothing"): the chat's preview pane
       lives in its side menu, which the chat-only page does not show - the page loaded into a frame nobody could see.
       A host whose pane is on screen draws it there (its hook says so); otherwise it opens here, large, over the page. */
    pane(blk) {
      const m = document.createElement('div'); m.className = 'vo-modal';
      m.innerHTML = '<div class="vo-modal-box" role="dialog" aria-label="Preview"><div class="vo-modal-h"><span class="code-lang"></span><span class="cb-sp"></span>'
        + '<button class="cb-act" data-vom="pop">Pop out</button><button class="cb-act" data-vom="close">Close</button></div>'
        + '<div class="vo-modal-b"><iframe class="vo-modal-fr" sandbox="allow-scripts" title="Preview"></iframe></div></div>';
      m.querySelector('.code-lang').textContent = (blk.lang || 'plain') + (blk.filename ? ' · ' + blk.filename : '') + ' · preview';
      m.querySelector('iframe').srcdoc = previewDoc(blk.lang, blk.code);
      const close = () => { m.remove(); document.removeEventListener('keydown', key, true); };
      const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } };
      m.addEventListener('click', (e) => { const a = e.target.closest && e.target.closest('[data-vom]');
        if (a && a.getAttribute('data-vom') === 'pop') { DEFAULT.pop(blk); return; } if (a || e.target === m) close(); });
      document.addEventListener('keydown', key, true);
      adopt(document); document.body.appendChild(m);
    },
    async canvas(blk, id, b) {
      const sid = session(); if (!sid) { flash(b, false, 'No session'); return; }
      const dg = String(blk.lang).toLowerCase() === 'mermaid';
      if (blk.fileKind) { try { const r = await cap('canvas.add', { session_id: sid, kind: 'markdown', key: blk.fileKind + ':' + hash(blk.code), size: 'l',
          content: { md: fenceOf(id), title: blk.title || (blk.fileKind === 'sheet' ? 'Sheet' : blk.fileKind === 'slides' ? 'Slides' : 'Document') }, at: 'now' });
        flash(b, !!(r && r.ok), r && r.ok ? 'On canvas' : 'Failed'); } catch (e) { flash(b, false, 'Failed'); } return; }
      try { const r = await cap('canvas.add', dg
          ? { session_id: sid, kind: 'diagram', key: 'diagram:' + hash(blk.code), content: { mermaid: blk.code, title: blk.title || 'Diagram' }, at: 'now' }
          : { session_id: sid, kind: 'code', key: 'code:' + hash(blk.lang + '\u0000' + blk.code),
          content: { code: blk.code, lang: blk.lang, filename: blk.filename || '', title: blk.filename || (blk.lang || 'code') }, at: 'now' });
        flash(b, !!(r && r.ok), r && r.ok ? 'On canvas' : 'Failed');
        if (r && r.ok) document.dispatchEvent(new CustomEvent('vera:output:canvas', { detail: { id, key: r.key || '' } })); }
      catch (e) { flash(b, false, 'Failed'); }
    } };
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); }
  /* the hooks for a card in this root: a shadow root's own (wire(root, hooks)) first, the page's (VeraOut.hooks) for
     the document - and for a few things that belong to the PAGE wherever the card is drawn (its session, its notices,
     its VS Code, its save dialogs), the page's from any root. A card in the loop's or the canvas's shadow root is never
     handed the chat's explode or save, which look for it in the document and would quietly find nothing. */
  const HOSTWIDE = { session: 1, notify: 1, vscode: 1, ide: 1, project: 1, think: 1, notebook: 1, html: 1, podcast: 1, reveal: 1 };
  function hookFor(el, name) {
    const rt = el && el.getRootNode ? el.getRootNode() : document;
    const own = rt && rt.__voHooks; if (own && own[name]) return own[name];
    if (rt === document || HOSTWIDE[name]) return hooks[name] || null;
    return null;
  }
  async function act(name, id, b, c) {
    const blk = blocks[id]; if (!blk) return;
    if (name === 'copy') { try { await navigator.clipboard.writeText(blk.code); flash(b, true, 'Copied'); } catch (_) { flash(b, false, 'Failed'); } return; }
    if (name === 'edit') return edit(id);
    if (name === 'run') return run(id, b);
    if (name === 'outline') return outline(id, b, c);
    if (name === 'dsrc') { const on = c.classList.toggle('src'); const s = b && b.querySelector('span'); if (s) s.textContent = on ? 'Hide source' : 'Source'; return; }
    // the drawing's own downloads (the drawing is bare inside the card): where the host keeps it elsewhere it finds it
    if (name === 'dsvg' || name === 'dpng') { const fmt = name === 'dsvg' ? 'svg' : 'png'; const hk = hookFor(c, 'dimg');
      if (hk) { try { if (hk(blk, id, b, c, fmt)) return; } catch (_) {} }
      const vm = c.querySelector('vera-mermaid'); if (vm && typeof vm._download === 'function') { try { vm._download(fmt); return; } catch (_) {} }
      flash(b, false, 'Not drawn yet'); return; }
    if (blk.fileKind) {   // a file card's own: page the slides, present them, sort and read the sheet, pop the page out
      const st = fileState[id] || (fileState[id] = { i: 0 });
      const redraw = () => fileRedraw(c, id);
      if (name === 'fprev' || name === 'fnext') { st.i = (st.i | 0) + (name === 'fnext' ? 1 : -1); return redraw(); }
      if (name === 'fgo') { st.i = +(b.getAttribute('data-i') || 0); return redraw(); }
      if (name === 'fsort') { const col = +(b.getAttribute('data-col') || 0); st.sort = st.sort && st.sort[0] === col ? (st.sort[1] > 0 ? [col, -1] : null) : [col, 1]; return redraw(); }
      if (name === 'fcell') { st.cell = b.getAttribute('data-cell') || ''; return redraw(); }
      if (name === 'present') return present(id);
      if (name === 'fpop') { try { const doc = '<!doctype html><html><head><meta charset="utf-8"><title>' + esc(blk.title || 'Document') + '</title><style>body{font:16px/1.6 Georgia,serif;max-width:46em;margin:2em auto;padding:0 1em;color:#1d1f23}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em}pre{background:#f4f5f7;padding:.8em;overflow:auto}</style></head><body>' + mdHtml(blk.code) + '</body></html>';
        const u = URL.createObjectURL(new Blob([doc], { type: 'text/html' })); window.open(u, '_blank', 'noopener'); } catch (_) {} return; }
    }
    const h = hookFor(c, name);
    if (h) { try { if (await h(blk, id, b, c)) return; } catch (e) { console.warn('VeraOut hook ' + name, e); } }
    if (name === 'preview') return preview(id);
    if (DEFAULT[name]) return DEFAULT[name](blk, id, b, c);
  }

  // ── editing ─────────────────────────────────────────────────────────────────────────────────────────────
  /* THE CODE IS YOURS TO CHANGE (owner, 2026-10-03: "need the ability to edit code that has been output" · "the visible
     lines in the editor are not enough - about 3 or 4 ... possibly open a full editor ... or even let you open it in a
     vscode instance"). Edit opens an editor as tall as the code needs (up to most of the window, never less than a
     screenful's worth of rows); its bar opens the same code full screen, or - where the page has one - in the session's
     VS Code. Apply keeps your version: the card is redrawn and relinted, Run, Preview, Save, Canvas and Explode all use
     it, and the host hears about it (an `edited` hook, and a composed 'vera:code:edited' event) to keep it. */
  /* THE EDITOR READS LIKE THE CARD (owner, 2026-10-03: "in the code editor there is not line numbers, lintiing or the
     hover helper that displays function definitions - it needs to have these"). The text is typed into a textarea
     laid exactly over the highlighted code - the same highlighter, the same font and rows - with the line numbers in a
     gutter that stays put when the code scrolls sideways; the lint under it reruns as you type; and a pause over a call
     shows what it is, as it does on the card (the definitions read from what you are typing). The inline editor and
     the full-screen one are this one editor. */
  function mountEditor(c, text, lang) {
    const box = document.createElement('div'); box.className = 'vo-ed';
    box.innerHTML = '<div class="vo-ed-sc"><pre class="vo-ed-g" aria-hidden="true"></pre><div class="vo-ed-c"><pre class="vo-ed-h" aria-hidden="true"><code' + langClass(lang) + '></code></pre>'
      + '<textarea class="cb-editor vo-ed-ta" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="off"></textarea></div></div><div class="vo-ed-lint code-lint" style="display:none"></div>';
    const sc = box.querySelector('.vo-ed-sc'), gut = box.querySelector('.vo-ed-g'), hl = box.querySelector('.vo-ed-h code'), ta = box.querySelector('textarea'), lint = box.querySelector('.vo-ed-lint');
    ta.value = String(text || '');
    let raf = 0, lt = 0, rows = 0;
    const lintNow = () => { let d = []; try { d = lintCode(lang, ta.value); } catch (_) {}
      lint.innerHTML = d.slice(0, 6).map((x) => '<div class="cl-' + (x.sev === 'err' ? 'err' : 'warn') + '"><span class="cl-ico">' + (x.sev === 'err' ? '&#9888;' : '&#9679;') + '</span>' + esc((x.line ? 'L' + x.line + ': ' : '') + x.msg) + '</div>').join('');
      lint.style.display = d.length ? '' : 'none'; };
    const paint = () => { raf = 0; const v = ta.value;
      hl.innerHTML = highlight(v, lang) + '\n';      // the trailing row keeps a last empty line its height
      const k = v.split('\n').length; if (k !== rows) { rows = k; let s = ''; for (let i = 1; i <= k; i++) s += i + '\n'; gut.textContent = s; }
      clearTimeout(lt); lt = setTimeout(lintNow, 250); };
    const refresh = () => { if (!raf) raf = requestAnimationFrame(paint); };
    ta.addEventListener('input', refresh);
    // what a name is, under the pointer: the textarea is on top, so the token under it is found by looking through it
    let ht = 0;
    ta.addEventListener('mousemove', (e) => { clearTimeout(ht); const x = e.clientX, y = e.clientY; ht = setTimeout(() => {
      const rt = ta.getRootNode ? ta.getRootNode() : document; const look = rt && rt.elementFromPoint ? rt : document;
      let el = null; ta.style.pointerEvents = 'none'; try { el = look.elementFromPoint(x, y); } catch (_) {} ta.style.pointerEvents = '';
      const tok = el && el.closest && el.closest('.vo-ed-h span.hl-fn, .vo-ed-h span.hl-type');
      if (tok) { try { tipShow(tok, c, ta.value); } catch (_) {} } else tipHide(); }, 220); });
    ta.addEventListener('mouseleave', () => { clearTimeout(ht); tipHide(); });
    ta.addEventListener('keydown', () => tipHide());
    paint(); lintNow();
    return { box, ta, sc, refresh };
  }
  // the editor's keys: Tab indents, Ctrl+Enter applies, Esc cancels
  function edKeys(ed, done) {
    ed.ta.addEventListener('keydown', (e) => { const ta = ed.ta;
      if (e.key === 'Tab') { e.preventDefault(); const s = ta.selectionStart, en = ta.selectionEnd; ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(en); ta.selectionStart = ta.selectionEnd = s + 2; ed.refresh(); }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); done(true); }
      else if (e.key === 'Escape') { e.preventDefault(); done(false); } });
  }
  function edit(id) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk || c.classList.contains('editing')) return;
    const pre = c.querySelector(':scope > pre') || c.querySelector(':scope > .vc-live, :scope > .cb-prev'); if (!pre) return;
    c.classList.add('editing'); if (c.classList.contains('prev')) preview(id, false);
    const ed = mountEditor(c, blk.code, blk.lang);
    /* THE EDITOR IS AS TALL AS WHAT IT HOLDS (owner, WSU-59: "a few lines -> just enough to edit; grows on paste"): it
       opened at 320 px whatever the code, and kept that height as lines were typed or pasted. It fits its lines now (a
       floor of about three lines, a ceiling of 70% of the window), refits as the text changes, and says so to the page
       (vera:code:editstart / vera:code:editsize) so a host can give its card the room. */
    const fitEd = () => { const rows = ed.ta.value.split('\n').length; ed.sc.style.height = Math.round(Math.min(window.innerHeight * 0.7, Math.max(96, rows * 17.5 + 40))) + 'px'; return rows; };
    const rows = fitEd();
    ed.ta.addEventListener('input', () => { const h0 = ed.sc.style.height; const n = fitEd();
      if (ed.sc.style.height !== h0) { try { c.dispatchEvent(new CustomEvent('vera:code:editsize', { bubbles: true, composed: true, detail: { id, rows: n } })); } catch (_) {} } });
    const bar = document.createElement('div'); bar.className = 'cb-editbar';
    bar.innerHTML = '<span class="cb-edithint">editing · Ctrl+Enter applies · Esc cancels</span><span class="cb-sp"></span>'
      + btn('edit-full', 'full', 'Full screen', 'Edit it full screen')
      + (hookFor(c, 'vscode') ? btn('edit-vscode', 'vscode', 'VS Code', "Open it in the session's VS Code") : '')
      + '<button class="cb-act" data-vo="edit-cancel">Cancel</button><button class="cb-act run" data-vo="edit-apply">Apply</button>';
    /* the editor's bar ABOVE the code, and it stays in view (owner, 2026-10-04: "having to scroll to the bottom of code
       edit panes to stop editing is annoying") - Apply and Cancel are where you start reading, not after the last line */
    pre.after(bar); bar.after(ed.box);
    edKeys(ed, (apply) => editDone(id, apply));
    try { c.dispatchEvent(new CustomEvent('vera:code:editstart', { bubbles: true, composed: true, detail: { id, rows } })); } catch (_) {}
    try { ed.ta.focus({ preventScroll: true }); } catch (_) {}
  }
  const edText = (c) => { const ta = c && c.querySelector(':scope > .vo-ed .cb-editor'); return ta ? ta : null; };
  // the code a card holds, set - redrawn with its numbered lines, relinted, its old output cleared, the host told
  function applyCode(id, next) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk || next === blk.code) return;
    const before = blk.code; blk.code = next;
    const pre = c.querySelector(':scope > pre');
    if (pre) { const t = document.createElement('div'); t.innerHTML = codeBody(next, blk.lang, !pre.classList.contains('vo-lines') ? false : undefined); pre.replaceWith(t.firstChild); }
    c.dataset.edited = '1'; lintCard(c);
    if (blk.fileKind) fileRedraw(c, id);   // a file card is redrawn from the edited source
    // a diagram is redrawn from the new source where its drawing is in the card (the canvas redraws its own on update)
    if (c.classList.contains('vo-diagram')) { const vm = c.querySelector('vera-mermaid'); if (vm && typeof vm.render === 'function') { try { vm.render(next); } catch (_) {} } }
    const out = c.querySelector(':scope > .code-exec-out'); if (out) { out.style.display = 'none'; out.innerHTML = ''; }
    if (c.classList.contains('prev')) preview(id, true);
    const detail = { id, lang: blk.lang, code: next, before, filename: blk.filename };
    try { const h = hookFor(c, 'edited'); if (h) h(blk, id, detail, c); } catch (_) {}
    c.dispatchEvent(new CustomEvent('vera:code:edited', { bubbles: true, composed: true, detail }));
  }
  function editDone(id, apply) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk) return;
    const box = c.querySelector(':scope > .vo-ed'), ta = edText(c), bar = c.querySelector(':scope > .cb-editbar');
    const next = ta ? ta.value : blk.code; if (box) box.remove(); if (bar) bar.remove(); c.classList.remove('editing'); tipHide();
    if (apply) applyCode(id, next);
    try { c.dispatchEvent(new CustomEvent('vera:code:editend', { bubbles: true, composed: true, detail: { id } })); } catch (_) {}
  }
  // full screen: a page-wide editor with a line gutter that follows its scroll; Apply writes back through the card
  function editFull(id) {
    const c = cardOf(id), blk = blocks[id]; if (!c || !blk) return;
    const inline = edText(c); const text = inline ? inline.value : blk.code;
    const m = document.createElement('div'); m.className = 'vo-modal';
    m.innerHTML = '<div class="vo-modal-box" role="dialog" aria-label="Edit code"><div class="vo-modal-h"><span class="code-lang"></span><span class="cb-sp"></span>'
      + '<span class="cb-edithint">Ctrl+Enter applies · Esc cancels</span><button class="cb-act" data-vom="cancel">Cancel</button><button class="cb-act run" data-vom="apply">Apply</button></div>'
      + '<div class="vo-modal-b"></div></div>';
    m.querySelector('.code-lang').textContent = (blk.lang || 'plain') + (blk.filename ? ' · ' + blk.filename : '');
    const ed = mountEditor(c, text, blk.lang); m.querySelector('.vo-modal-b').appendChild(ed.box);
    const close = (apply) => { const v = ed.ta.value; m.remove(); tipHide();
      if (inline) { inline.value = v; inline.dispatchEvent(new Event('input')); if (apply) editDone(id, true); } else if (apply) applyCode(id, v); };
    m.addEventListener('click', (e) => { const a = e.target.closest && e.target.closest('[data-vom]'); if (a) close(a.getAttribute('data-vom') === 'apply'); else if (e.target === m) close(false); });
    edKeys(ed, close);
    adopt(document); document.body.appendChild(m); try { ed.ta.focus(); } catch (_) {}
  }

  // ── the reader's helpers: what a name is, where it is defined, where else it is used ────────────────────
  // every definition in the scope a card is read in: its message, its canvas item, its loop answer - so a call in one
  // block finds the function another block of the same reply defined
  function scopeOf(c) { return (c.closest && c.closest('.msg-body, .it[data-key], [data-part], .alo-final-val, .vo-md')) || c.parentNode || c; }
  // (text: what an open editor holds for card c - its definitions are read from what is being typed)
  function definitions(c, text) {
    const out = {}; const own = c.getAttribute('data-cb');
    scopeOf(c).querySelectorAll('.code-card[data-cb]').forEach((k) => { const id = k.getAttribute('data-cb'), blk = blocks[id]; if (!blk) return;
      const sy = symbols(id === own && text != null ? text : blk.code, blk.lang); Object.keys(sy).forEach((n) => { if (!out[n] || id === own) out[n] = Object.assign({ id, file: blk.filename || blk.lang || '' }, sy[n]); }); });
    return out;
  }
  let tip = null, tipT = 0;
  function tipShow(tok, card, text) {
    const c = card || tok.closest('.code-card'); if (!c) return;
    const inEd = text != null;
    const name = String(tok.textContent || '').trim(); if (!/^[A-Za-z_$][\w$]*$/.test(name)) return;
    const d = definitions(c, text)[name];
    const line = tok.closest('.vc-line'); const atDef = d && line && d.id === c.getAttribute('data-cb') && +line.dataset.line === d.line;
    let html;
    if (d && !atDef) html = '<div class="vo-tip-k">' + esc(d.kind) + (d.id !== c.getAttribute('data-cb') ? ' · ' + esc(d.file) : '') + ' · line ' + d.line + '</div><code>' + esc(d.sig) + '</code>'
      + (d.doc ? '<div class="vo-tip-d">' + esc(d.doc) + '</div>' : '') + (inEd ? '' : '<div class="vo-tip-a">click to go to it</div>');
    else if (atDef) html = '<div class="vo-tip-k">' + esc(d.kind) + ' · defined here</div>' + (d.doc ? '<div class="vo-tip-d">' + esc(d.doc) + '</div>' : '')
      + '<div class="vo-tip-a">' + usesOf(c, name) + ' use' + (usesOf(c, name) === 1 ? '' : 's') + ' in this block</div>';
    else if (BUILTIN[name]) html = '<div class="vo-tip-k">built-in</div><code>' + esc(BUILTIN[name]) + '</code>';
    else return;
    const doc = tok.ownerDocument || document;
    if (!tip) { tip = doc.createElement('div'); tip.className = 'vo-tip'; }
    adopt(document); document.body.appendChild(tip);
    tip.innerHTML = html; tip.style.display = 'block';
    const r = tok.getBoundingClientRect(); const w = Math.min(420, window.innerWidth - 16);
    tip.style.maxWidth = w + 'px'; tip.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
    const below = r.bottom + 6, h = tip.offsetHeight; tip.style.top = (below + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 6) : below) + 'px';
  }
  function tipHide() { if (tip) tip.style.display = 'none'; }
  function usesOf(c, name) { let n = 0; c.querySelectorAll(':scope > pre code span.hl-fn, :scope > pre code span.hl-type').forEach((s) => { if (s.textContent === name) n++; }); return n; }
  function occurrences(c, name, on) { c.querySelectorAll(':scope > pre code span.hl-fn, :scope > pre code span.hl-type, :scope > pre code span.vo-occ').forEach((s) => s.classList.toggle('vo-occ', !!on && s.textContent === name)); }
  function jumpTo(c, name) {
    const d = definitions(c)[name]; if (!d) return false;
    const target = cardOf(d.id); if (!target) return false;
    const line = target.querySelector('.vc-line[data-line="' + d.line + '"]'); if (!line) return false;
    try { line.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { line.scrollIntoView(); }
    line.classList.remove('vo-jump'); void line.offsetWidth; line.classList.add('vo-jump'); setTimeout(() => line.classList.remove('vo-jump'), 1600);
    return true;
  }
  // Outline: the block's functions and classes as a short menu under the button; a pick goes to it
  function outline(id, b, c) {
    const old = c.querySelector(':scope > .vo-outline'); if (old) { old.remove(); return; }
    const sy = symbols(blocks[id].code, blocks[id].lang); const names = Object.keys(sy).sort((a, z) => sy[a].line - sy[z].line);
    if (!names.length) return;
    const box = document.createElement('div'); box.className = 'vo-outline';
    box.innerHTML = names.map((n) => '<button class="vo-ol-it" data-vo-go="' + esc(n) + '"><span class="vo-ol-k">' + esc(sy[n].kind) + '</span><b>' + esc(n) + '</b><i>line ' + sy[n].line + '</i></button>').join('');
    const bar = c.querySelector(':scope > .code-bar'); bar.after(box);
  }

  // ── wiring ──────────────────────────────────────────────────────────────────────────────────────────────
  function wire(root, rootHooks) {
    root = root || document; roots.add(root);
    if (rootHooks && root !== document) root.__voHooks = Object.assign(root.__voHooks || {}, rootHooks);
    if (root.__voWired) return root; root.__voWired = true;
    root.addEventListener('click', (e) => {
      const go = e.target && e.target.closest && e.target.closest('.code-card [data-vo-go]');
      if (go) { const c = go.closest('.code-card'); e.preventDefault(); e.stopPropagation(); jumpTo(c, go.getAttribute('data-vo-go')); const ol = go.closest('.vo-outline'); if (ol) ol.remove(); return; }
      // a call (or a class) defined in reach: a click on it goes to the definition
      const tok = e.target && e.target.closest && e.target.closest('.code-card pre code span.hl-fn, .code-card pre code span.hl-type');
      if (tok && !e.target.closest('.cb-editor')) { const c = tok.closest('.code-card'); if (jumpTo(c, String(tok.textContent || '').trim())) { e.preventDefault(); return; } }
      const pv = e.target && e.target.closest && e.target.closest('.vo-acts [data-vp]');
      if (pv) { e.preventDefault(); e.stopPropagation(); proseAct(pv); return; }
      const b = e.target && e.target.closest && e.target.closest('.code-card [data-vo]'); if (!b) return;
      const c = b.closest('.code-card'); const id = c && c.getAttribute('data-cb'); if (!id) return;
      const a = b.getAttribute('data-vo'); e.preventDefault(); e.stopPropagation();
      if (a === 'edit-apply') return editDone(id, true);
      if (a === 'edit-cancel') return editDone(id, false);
      if (a === 'edit-full') return editFull(id);
      if (a === 'edit-vscode') { const h = hookFor(c, 'vscode'); const ta = edText(c); if (h) h(Object.assign({}, blocks[id], { code: ta ? ta.value : blocks[id].code }), id, b, c); return; }
      if (a === 'xp-close') { const x = b.closest('.xp-inline'); if (x) x.remove(); return; }
      act(a, id, b, c);
    });
    // what a name is: hovering a call or a class shows its definition; every use of it in the block lights
    root.addEventListener('mouseover', (e) => {
      const tok = e.target && e.target.closest && e.target.closest('.code-card pre code span.hl-fn, .code-card pre code span.hl-type'); if (!tok) return;
      const c = tok.closest('.code-card'); const name = String(tok.textContent || '').trim();
      clearTimeout(tipT); tipT = setTimeout(() => { try { tipShow(tok); } catch (_) {} }, 220);
      occurrences(c, name, true);
      const d = definitions(c)[name]; tok.classList.toggle('vo-def', !!d);
    });
    root.addEventListener('mouseout', (e) => {
      const tok = e.target && e.target.closest && e.target.closest('.code-card pre code span.hl-fn, .code-card pre code span.hl-type'); if (!tok) return;
      clearTimeout(tipT); tipHide(); const c = tok.closest('.code-card'); if (c) occurrences(c, '', false);
    });
    // the prose menu opens, walks with the keyboard and closes on Escape or a click elsewhere
    root.addEventListener('keydown', (e) => {
      const m = root.querySelector && root.querySelector('.vo-acts .ma-dd.open .ma-menu'); if (!m) return;
      if (e.key === 'Escape') { e.preventDefault(); menuClose(root); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const its = [...m.querySelectorAll('.ma-it')]; const i = its.indexOf((root.activeElement || document.activeElement));
        const n = its[(i + (e.key === 'ArrowDown' ? 1 : -1) + its.length) % its.length]; if (n) n.focus(); }
    });
    root.addEventListener('mousedown', (e) => { if (!(e.target && e.target.closest && e.target.closest('.vo-acts .ma-dd'))) menuClose(root); }, true);
    // a card's menu is placed over the page: it does not follow a scroll, so a scroll closes it
    root.addEventListener('scroll', (e) => { if (!(e.target && e.target.closest && e.target.closest('.ma-menu'))) root.querySelectorAll('.vo-send .ma-dd.open').forEach((x) => x.classList.remove('open')); }, true);
    if (root !== document) adopt(root);
    return root;
  }

  // ── prose: the same actions under every piece of writing ────────────────────────────────────────────────
  /* WRITING GETS THE SAME MENU WHEREVER IT IS (owner, 2026-10-03: "prose sent to the canvas also needs the same options
     as code (like the menu under responses) so it can be converted into other formats sent to other areas or saved or
     edited"). The chat's reply row and a canvas reading item draw this one row: Copy · Explode · Canvas, then one
     "Send to" menu - an HTML report, a podcast, a notebook, a file in the IDE, a project, a thought for later, any
     other channel with a style. Every action works from the text it is given; a page that has its own way of doing one
     (the chat's save dialogs, its HTML-opens setting) plugs it in, and anything it does not is done through the
     capability that does it.
       VeraOut.proseMenu(id, {md, title, canvas, explode, extra, lead})   lead/extra: the host's own buttons */
  const prose = {};
  const mact = (act, ico, label, tip, cls) => '<button class="mact' + (cls ? ' ' + cls : '') + '" data-vp="' + act + '" title="' + esc(tip) + '">' + (I[ico] || '') + '<span>' + label + '</span></button>';
  const mitem = (act, ico, label, hint) => '<button class="ma-it" role="menuitem" data-vp="' + act + '">' + (I[ico] || '') + '<span class="ma-l">' + label + '<small>' + hint + '</small></span></button>';
  function proseMenu(id, o) {
    o = o || {}; prose[id] = { md: String(o.md || ''), title: String(o.title || ''), getMd: o.getMd || null };
    const has = (n) => !!hooks[n];
    return '<div class="msg-actions vo-acts" data-vo-prose="' + esc(id) + '">' + (o.lead || '')
      + mact('copy', 'copy', 'Copy', 'Copy the text')
      + (o.explode === false ? '' : mact('explode', 'explode', 'Explode', 'A structured diagram of this - its code, or its prose'))
      + (o.canvas === false ? '' : mact('canvas', 'canvas', 'Canvas', 'Put this on the session canvas, beside the chat'))
      + (o.extra || '')
      + '<span class="ma-sep"></span>' + sendDd(o) + '</div>';
  }
  // the "Send to" dropdown: the same deliveries wherever it is drawn (o.skip: the ones that make no sense there)
  function sendDd(o) {
    o = o || {}; const has = (n) => !!hooks[n]; const no = (n) => (o.skip || []).indexOf(n) >= 0;
    return '<div class="ma-dd">'
      +   mact('menu', 'send', 'Send to', 'Deliver this somewhere else - the original stays where it is', 'ma-ddb')
      +   '<div class="ma-menu" role="menu">'
      +     mitem('html', 'html', 'HTML report', 'a rich document - on the canvas or a tab (Settings › Delivery)')
      +     (no('podcast') ? '' : mitem('podcast', 'podcast', 'Podcast', 'a spoken episode, multi-voice'))
      +     mitem('notebook', 'notebook', 'Notebook', 'a cell in a notebook')
      +     mitem('storage', 'save', 'Estate storage', 'the report kept as a file in the estate\'s storage, with a link')
      +     (has('ide') ? mitem('ide', 'ide', 'A file in the IDE', 'saved through the IDE') : '')
      +     (has('project') ? mitem('project', 'project', 'A project', 'added to a dream-system project') : '')
      +     (has('think') ? mitem('think', 'think', 'Think about it later', 'Vera revisits it when idle') : '')
      +     '<hr>' + mitem('channel', 'channel', 'Another channel…', 'email, telegram, memory - with a style')
      +     (no('files') ? '' : '<hr>' + mitem('dl:docx', 'save', 'Word document', 'download it as .docx')
        + mitem('dl:pdf', 'save', 'PDF', 'download it as a PDF document')
        + mitem('dl:pptx', 'save', 'PowerPoint', 'its sections as slides (.pptx)')
        + mitem('dl:xlsx', 'save', 'Excel', 'its tables as sheets (.xlsx)')
        + mitem('dlpick', 'save', 'Other file types…', 'odt · rtf · epub · html · markdown · odp · slideshow · ods · csv · tsv · json'))
      +   '</div></div>';
  }
  /* SEND TO, ON EVERY CARD (owner, 2026-10-03: "id also like the 'send to' menu to be integrated into the code and
     markdown and html rendering so it can be sent to other outputs"): a code card, a page, a diagram carry the same
     menu as the prose row, in their own bar. It hands on the code as a fence (a file in the IDE gets the bare code).
       VeraOut.sendMenu(id, {getMd, raw, title, skip}) */
  function sendMenu(id, o) {
    o = o || {}; prose[id] = { md: String(o.md || ''), title: String(o.title || ''), getMd: o.getMd || null, raw: o.raw || null };
    return '<div class="vo-acts vo-send" data-vo-prose="' + esc(id) + '">' + sendDd(o) + '</div>';
  }
  function menuClose(root) { (root || document).querySelectorAll('.vo-acts .ma-dd.open').forEach((x) => x.classList.remove('open')); }
  function notify(el, html) { const h = hookFor(el, 'notify'); if (h) { try { h(html); } catch (_) {} } }
  async function proseAct(b) {
    const row = b.closest('.vo-acts'); const id = row && row.getAttribute('data-vo-prose'); const p = prose[id]; if (!p) return;
    const a = b.getAttribute('data-vp'); const md = (a === 'ide' && p.raw) ? String(p.raw() || '') : p.getMd ? String(p.getMd() || '') : p.md;
    const title = p.title || (md.trim().split('\n').find((l) => l.trim()) || 'Output').replace(/^#+\s*/, '').slice(0, 120);
    const ctx = { id, md, title, btn: b, row };
    if (a === 'menu') { const dd = b.closest('.ma-dd'); const open = !dd.classList.contains('open'); menuClose(row.getRootNode ? row.getRootNode() : document);
      if (!open) return; dd.classList.add('open'); const m = dd.querySelector('.ma-menu'); m.classList.remove('up');
      if (row.classList.contains('vo-send')) {
        // in a card's bar: over the page, not inside the card (a card clips what overflows it)
        m.style.position = 'fixed'; m.style.zIndex = '10060'; m.style.left = '0px'; m.style.top = '0px';   // measured as it will be drawn
        const r = b.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
        const wantL = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
        const wantT = r.bottom + 4 + h > window.innerHeight - 8 && r.top - h - 4 > 8 ? r.top - h - 4 : r.bottom + 4;
        m.style.left = wantL + 'px'; m.style.top = wantT + 'px';
        // an ancestor that makes a containing block (a transform, a filter, contain) moves "fixed" off the viewport: put it right
        const got = m.getBoundingClientRect();
        if (Math.abs(got.left - wantL) > 0.5 || Math.abs(got.top - wantT) > 0.5) { m.style.left = (2 * wantL - got.left) + 'px'; m.style.top = (2 * wantT - got.top) + 'px'; }
      } else { const r = m.getBoundingClientRect(); if (r.bottom > window.innerHeight - 8 && r.height < r.top) m.classList.add('up'); }
      try { m.querySelector('.ma-it').focus({ preventScroll: true }); } catch (_) {} return; }
    menuClose(row.getRootNode ? row.getRootNode() : document);
    const h = hookFor(row, 'prose_' + a) || (HOSTWIDE[a] ? hookFor(row, a) : null);
    if (h) { try { if (await h(ctx)) return; } catch (e) { console.warn('VeraOut prose ' + a, e); } }
    const done = (ok, msg) => { flash(a === 'copy' || a === 'canvas' || a === 'explode' ? b : row.querySelector('.ma-ddb'), ok, ok ? (msg || 'Done') : 'Failed'); };
    try {
      if (a === 'copy') { await navigator.clipboard.writeText(md); return done(true, 'Copied'); }
      if (a === 'canvas') { const r = await cap('output.redeliver', { channel: 'canvas', content: md, title, session_id: session() }); return done(!!(r && r.ok), 'On canvas'); }
      if (a === 'html') { const r = await cap('report.canvas', { content: md, title, mode: 'auto', session_id: session() }); if (r && r.ok) notify(row, 'HTML report on the canvas'); return done(!!(r && r.ok), 'Sent'); }
      if (a === 'podcast') { const r = await cap('podcast.generate', { topic: title, sources: [{ type: 'text', text: md, label: title }], wait: false }); return done(!!(r && (r.job_id || r.episode_id || r.ok)), 'Started'); }
      if (a === 'notebook') { const r = await cap('output.redeliver', { channel: 'notebook', content: md, title, session_id: session() }); return done(!!(r && r.ok), 'Saved'); }
      // the estate's object store: the HTML report and its source, kept (report.store), with a link that fetches it
      if (a === 'storage') { flash(row.querySelector('.ma-ddb'), true, 'Saving…');
        const r = await cap('output.redeliver', { channel: 'storage', content: md, title, session_id: session() });
        if (r && r.ok) notify(row, 'Saved to the estate storage' + (r.key ? ' · ' + esc(r.key) : '') + (r.store_url ? ' · <a href="' + esc(BASE() + r.store_url) + '" target="_blank" rel="noopener">open it</a>' : ''));
        else notify(row, '<span style="color:var(--err,#e06c6c)">Could not save to the estate storage' + (r && r.error ? ': ' + esc(r.error) : '') + '</span>');
        return done(!!(r && r.ok), 'Saved'); }
      if (a === 'channel') return channelPick(row, ctx);
      if (a === 'dlpick') return filePick(row, ctx);
      if (a.indexOf('dl:') === 0) { const box = row.querySelector('.vo-fps'); if (box) box.remove();
        const r = await download(md, a.slice(3), title, row.querySelector('.ma-ddb') || b);
        if (r && r.ok) notify(row, 'Downloaded <a href="' + esc(BASE() + r.url) + '" download>' + esc(r.filename || 'the file') + '</a>');
        else notify(row, '<span style="color:var(--err,#e06c6c)">Could not make the file' + (r && r.error ? ': ' + esc(r.error) : '') + '</span>');
        return; }
      if (a === 'explode') return done(false);
      if (a.indexOf('ch:') === 0) { const sel = row.querySelector('.vo-chs select'); const r = await cap('output.redeliver', { channel: a.slice(3), content: md, title, format: sel ? sel.value : '', session_id: session() });
        const box = row.querySelector('.vo-chs'); if (box) box.remove();
        if (r && r.ok) { const rv = hookFor(row, 'reveal'); if (rv) try { rv(a.slice(3), r.url || ''); } catch (_) {} notify(row, 'Delivered as ' + esc(a.slice(3))); }
        return done(!!(r && r.ok), 'Sent'); }
    } catch (e) { done(false); }
  }
  // every other channel, with a style for it - listed under the row, the same picker wherever the row is
  let chans = null;
  async function channelPick(row, ctx) {
    const old = row.querySelector('.vo-chs'); if (old) { old.remove(); return; }
    if (!chans) { try { const r = await cap('output.channels', {}); chans = ((r && r.channels) || []).filter((c) => c && c.available !== false && c.id !== 'fabric'); } catch (_) { chans = []; } }
    const box = document.createElement('div'); box.className = 'vo-chs';
    let styles = ''; try { const r = await cap('output.delivery_menu', {}); styles = ((r && r.styles) || []).map((s) => '<option value="' + esc(s.id) + '">' + esc(s.label || s.id) + '</option>').join(''); } catch (_) {}
    box.innerHTML = '<span class="vo-chs-l">send as</span>' + chans.map((c) => '<button class="mact" data-vp="ch:' + esc(c.id) + '">' + esc(c.label || c.id) + '</button>').join('')
      + '<span class="vo-chs-l">in</span><select title="The style it is shaped into"><option value="">the channel\'s own style</option>' + styles + '</select>';
    row.appendChild(box);
  }
  let fileFormats = null;
  async function filePick(row, ctx) {
    const old = row.querySelector('.vo-fps'); if (old) { old.remove(); return; }
    if (!fileFormats) { try { const r = await cap('render.formats', {}); fileFormats = ((r && r.formats) || []).filter((f) => f && f.family); } catch (_) { fileFormats = []; } }
    const box = document.createElement('div'); box.className = 'vo-chs vo-fps';
    const fam = { document: 'document', presentation: 'slides', spreadsheet: 'tables' };
    box.innerHTML = Object.keys(fam).map((k) => '<span class="vo-chs-l">' + fam[k] + '</span>' + fileFormats.filter((f) => f.family === k)
      .map((f) => '<button class="mact" data-vp="dl:' + esc(f.id) + '"' + (f.live ? '' : ' disabled title="not available on this server"') + '>' + esc(f.label) + ' <small>.' + esc(f.ext) + '</small></button>').join('')).join('');
    row.appendChild(box);
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
      // inside a diagram card the card's bar is the chrome: the drawing is bare
      const el = document.createElement('vera-mermaid'); el.setAttribute(slot.closest('.vo-diagram') ? 'bare' : 'popout', ''); slot.appendChild(el);
      try { el.render(blk.code); } catch (_) { slot.textContent = blk.code; } }));
  }

  // ── the look (one copy: the document gets it on load, a shadow root through wire/adopt) ────────────────
  const CSS = `
.vo-file .vo-fview{background:var(--bg0,#0e0f12)}.vo-file:not(.src) > pre{display:none}
.vo-deck{padding:10px}.vo-stage{aspect-ratio:16/9;max-height:440px;margin:0 auto;border-radius:6px;overflow:hidden;background:linear-gradient(160deg,var(--bg2,#1c2029),var(--bg0,#12141a));border:1px solid var(--border,rgba(255,255,255,.09));container-type:inline-size}
.vo-slide{position:relative;height:100%;box-sizing:border-box;padding:5cqw 6cqw;overflow:hidden;color:var(--text,#e9ebef);display:flex;flex-direction:column}
.vo-slide.cover{justify-content:center}.vo-slide h1{font-size:max(15px,4.2cqw);margin:0 0 2.4cqw;line-height:1.15;font-weight:650}.vo-slide.cover h1{font-size:max(19px,5.6cqw)}
.vo-slide .vo-sb{font-size:max(11px,2.3cqw);line-height:1.5;overflow:hidden}.vo-slide .vo-sb li{margin:.5cqw 0}.vo-slide .vo-sb .code-card{font-size:11px}
.vo-slide table{border-collapse:collapse;margin:.4em 0;font-size:.9em}.vo-slide th,.vo-slide td{border:1px solid rgba(255,255,255,.2)!important;padding:.3em .9em!important;text-align:left;white-space:nowrap}
.vo-slide th{font-size:1em!important;text-transform:none!important;letter-spacing:0!important;font-weight:600;color:inherit!important;background:rgba(255,255,255,.07)!important}
.vo-writing{position:absolute;right:2cqw;bottom:1.6cqw;font:9px var(--mono,monospace);color:var(--acc,#5a9e8f)}
.vo-deck-nav{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap}.vo-deck-n{font:10px var(--mono,monospace);color:var(--dim,#6b7280);min-width:44px;text-align:center}
.vo-thumbs{display:flex;gap:4px;overflow-x:auto;flex:1;min-width:0;scrollbar-width:thin}.vo-thumb{flex:0 0 auto;max-width:150px;border:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg1,#14161b);color:var(--dim,#9aa1ad);border-radius:4px;padding:2px 7px;font:9.5px var(--ui,inherit);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.vo-thumb b{margin-right:4px;color:var(--text,#d8dce4)}.vo-thumb.on{border-color:var(--acc,#5a9e8f);color:var(--text,#d8dce4)}
.vo-notes{margin-top:6px;font-size:10.5px;color:var(--dim,#9aa1ad)}.vo-notes b{font:9px var(--mono,monospace);text-transform:uppercase;letter-spacing:.06em;margin-right:4px}
.vo-present{position:fixed;inset:0;z-index:10080;background:#0b0c10;display:flex;align-items:center;justify-content:center;container-type:inline-size;cursor:pointer}
.vo-present .vo-slide.big{width:min(100vw,177.8vh);height:min(56.25vw,100vh);padding:6% 7%;background:linear-gradient(160deg,#1c2029,#12141a);color:#e9ebef}
.vo-present .vo-slide.big h1{font-size:min(4.6vw,8vh)}.vo-present .vo-slide.big .vo-sb{font-size:min(2.6vw,4.6vh)}.vo-present-n{position:fixed;right:18px;bottom:12px;font:12px var(--mono,monospace);color:#8b93a1}
.vo-sheet{padding:0}.vo-fx{display:flex;gap:10px;align-items:center;padding:4px 10px;border-bottom:1px solid var(--border,rgba(255,255,255,.09));font:10.5px var(--mono,monospace);color:var(--dim,#9aa1ad);min-height:18px}
.vo-fx b{color:var(--acc,#5a9e8f);min-width:30px}.vo-grid-w{overflow:auto;max-height:420px;scrollbar-width:thin}
.vo-grid{border-collapse:collapse;font-size:11.5px;min-width:100%}.vo-grid th,.vo-grid td{border:1px solid var(--border,rgba(255,255,255,.09));padding:3px 8px;white-space:nowrap}
.vo-grid thead th{position:sticky;top:0;z-index:1;background:var(--bg1,#14161b);color:var(--dim,#9aa1ad);font:600 9.5px var(--mono,monospace);cursor:pointer;user-select:none}
.vo-grid th.rn{background:var(--bg1,#14161b);color:var(--dim,#6b7280);font:9.5px var(--mono,monospace);text-align:right;position:sticky;left:0}
.vo-grid td{color:var(--text,#d8dce4);cursor:cell}.vo-grid td.n{text-align:right;font-variant-numeric:tabular-nums}.vo-grid td.f{color:var(--acc,#5a9e8f)}
.vo-grid tr.hd td{font-weight:600;background:var(--bg2,#1a1d24)}.vo-grid td.on{outline:2px solid var(--acc,#5a9e8f);outline-offset:-2px}
.vo-doc{padding:14px;background:var(--bg0,#0e0f12)}.vo-toc{display:flex;flex-wrap:wrap;gap:4px 10px;margin:0 auto 10px;max-width:760px;font-size:10px;color:var(--dim,#9aa1ad)}.vo-toc .l2{padding-left:6px}.vo-toc .l3{padding-left:12px;opacity:.8}
.vo-paper{max-width:760px;margin:0 auto;padding:36px 44px;background:var(--paper,#fbfaf7);color:#1d1f23;border-radius:3px;box-shadow:0 1px 3px rgba(0,0,0,.35),0 8px 24px rgba(0,0,0,.22);font:13.5px/1.65 Georgia,'Times New Roman',serif}
.vo-paper h1,.vo-paper h2,.vo-paper h3{font-family:var(--ui,'Helvetica Neue',Arial,sans-serif);color:#111;line-height:1.25}.vo-paper a{color:#2a5db0}
.vo-paper table{border-collapse:collapse}.vo-paper td,.vo-paper th{border:1px solid #cfd2d8;padding:.3em .6em}.vo-paper code{background:#eef0f3;color:#1d1f23;padding:0 3px;border-radius:3px}
.vo-dl{display:inline-flex}.vo-fps small{opacity:.6;margin-left:2px}
.code-card{margin:6px 0;border:1px solid var(--border,rgba(255,255,255,.09));border-radius:var(--ui-radius,6px);overflow:hidden;background:var(--bg2,#1a1d24)}
.code-card pre{margin:0;padding:10px 12px;overflow:auto;background:var(--bg0,#0e0f12);font-family:var(--mono,ui-monospace,monospace);font-size:11.5px;line-height:1.5;color:var(--text,#d8dce4);max-height:420px}
.code-card pre code{font-family:inherit;background:transparent;padding:0;color:inherit;white-space:pre}
.code-card > .code-bar[hidden]{display:none}
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
.code-card .cb-editbar{display:flex;align-items:center;gap:4px;padding:3px 6px 3px 10px;border-top:1px solid var(--border,rgba(255,255,255,.09));border-bottom:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg1,#14161b)}
.code-card .cb-edithint{font:9px var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0)}
.code-card[data-edited] .code-lang::after{content:' · edited';color:var(--warn,#d9a54a);text-transform:none;letter-spacing:0}
.code-card .xp-inline{border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg1,#14161b)}
.code-card .xp-h{display:flex;align-items:center;gap:8px;padding:3px 6px 3px 10px;font:9px var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0)}
.code-card .xp-t{color:var(--acc,#5a9e8f)}.code-card .xp-body{min-height:0}
.vo-mm{margin:8px 0}
.hl-kw{color:var(--acc,#5a9e8f);font-weight:600}.hl-str{color:var(--acc2,#8fb87a)}.hl-com{color:var(--dim,#6b7280);font-style:italic}
.hl-add{background:rgba(95,207,154,.14);color:var(--acc2,#8fb87a);display:inline-block;width:100%}
.hl-del{background:rgba(232,112,107,.14);color:var(--err,#e06c6c);display:inline-block;width:100%}
.hl-hunk{color:var(--acc,#5a9e8f);display:inline-block;width:100%}
.hl-num{color:var(--acc3,#c9a96e)}.hl-fn{color:var(--acc4,#8fa7d9)}.hl-type{color:var(--acc4,#8fa7d9)}.hl-tag{color:var(--acc,#5a9e8f)}.hl-attr{color:var(--acc2,#8fb87a)}
/* numbered lines: the number in a gutter a selection never takes */
.code-card pre.vo-lines{padding-left:4px}
.code-card pre.vo-lines code{display:block}
.code-card .vc-line{display:block;padding-left:2px;border-left:2px solid transparent;transition:background .15s}
.code-card .vc-lno{display:inline-block;width:2.4em;margin-right:.75em;text-align:right;font-style:normal;color:var(--dim,#6b7280);opacity:.55;user-select:none;-webkit-user-select:none}
.code-card .vc-line.vo-jump{background:color-mix(in srgb,var(--acc,#5a9e8f) 22%,transparent);border-left-color:var(--acc,#5a9e8f)}
.code-card .vc-line.vo-jump .vc-lno{opacity:1;color:var(--acc,#5a9e8f)}
/* the reader's helpers: every use of the name in view, a name with a definition in reach, the outline */
.code-card code span.vo-occ{background:color-mix(in srgb,var(--acc,#5a9e8f) 20%,transparent);border-radius:2px;box-shadow:0 0 0 1px color-mix(in srgb,var(--acc,#5a9e8f) 35%,transparent)}
.code-card code span.vo-def{text-decoration:underline dotted;text-underline-offset:2px;cursor:pointer}
.code-card .vo-outline{display:flex;flex-direction:column;padding:4px;gap:1px;background:var(--bg1,#14161b);border-bottom:1px solid var(--border,rgba(255,255,255,.09));max-height:220px;overflow:auto}
.code-card .vo-ol-it{display:flex;align-items:baseline;gap:8px;border:none;background:none;text-align:left;padding:4px 8px;border-radius:var(--r-sm,5px);color:var(--text,#d8dce4);font:11px var(--mono,ui-monospace,monospace);cursor:pointer}
.code-card .vo-ol-it:hover{background:var(--surf2,rgba(255,255,255,.06))}
.code-card .vo-ol-k{flex:0 0 auto;min-width:64px;white-space:nowrap;color:var(--dim2,#8a92a0);font-size:9.5px;text-transform:uppercase;letter-spacing:.06em}.code-card .vo-ol-it b{white-space:nowrap}.code-card .vo-ol-it i{margin-left:auto;padding-left:12px;white-space:nowrap;font-style:normal;color:var(--dim,#6b7280);font-size:9.5px}
.vo-tip{position:fixed;z-index:10050;display:none;padding:8px 10px;border-radius:var(--ui-radius,8px);background:var(--surf,var(--bg1,#1a1d24));color:var(--text,#d8dce4);box-shadow:var(--elev,0 12px 34px -16px rgba(0,0,0,.7)),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.12)));font:11.5px/1.45 var(--f-ui,var(--sans,system-ui,sans-serif));pointer-events:none}
.vo-tip code{display:block;font:11px/1.5 var(--mono,ui-monospace,monospace);color:var(--acc4,#8fa7d9);white-space:pre-wrap;word-break:break-word}
.vo-tip .vo-tip-k{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim2,#8a92a0);margin-bottom:3px}
.vo-tip .vo-tip-d{margin-top:5px;color:var(--text,#d8dce4)}.vo-tip .vo-tip-a{margin-top:5px;font-size:10px;color:var(--acc,#5a9e8f)}
/* the full-screen editor */
.vo-modal{position:fixed;inset:0;z-index:10040;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
.vo-modal-box{width:min(1200px,94vw);height:88vh;display:flex;flex-direction:column;border-radius:var(--ui-radius,10px);overflow:hidden;background:var(--bg0,#0e0f12);box-shadow:0 30px 80px -20px rgba(0,0,0,.8),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.12)))}
.vo-modal-h{display:flex;align-items:center;gap:6px;padding:6px 8px 6px 14px;background:var(--bg1,#14161b);border-bottom:1px solid var(--border,rgba(255,255,255,.09));font:10px var(--mono,ui-monospace,monospace)}
.vo-modal-h .code-lang{color:var(--acc,#5a9e8f);text-transform:uppercase;letter-spacing:.7px;font-weight:600}.vo-modal-h .cb-sp{flex:1}
.vo-modal-h .cb-act{display:inline-flex;align-items:center;gap:4px;padding:5px 10px;font:500 11px/1 var(--f-ui,var(--sans,system-ui,sans-serif));border:none;background:transparent;color:var(--dim2,#8a92a0);border-radius:var(--r-sm,5px);cursor:pointer}
.vo-modal-h .cb-act:hover{background:var(--surf2,rgba(255,255,255,.06));color:var(--text,#d8dce4)}.vo-modal-h .cb-act.run{color:var(--acc2,#8fb87a)}
.vo-modal-h .cb-edithint{color:var(--dim2,#8a92a0);margin-right:6px}
.vo-modal-b{flex:1;display:flex;min-height:0}
.vo-modal-fr{flex:1;width:100%;height:100%;border:0;background:#fff}
/* the editor: the highlighted code under a transparent textarea, the same metrics to the pixel (a page's own pre and
   textarea rules must not move one off the other), the line numbers in a gutter that stays when the code scrolls across */
.vo-ed{border-top:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg0,#0e0f12)}
.vo-ed-sc{display:flex;align-items:flex-start;overflow:auto;box-shadow:inset 2px 0 0 var(--acc,#5a9e8f)}
.vo-ed .vo-ed-g{position:sticky;left:0;z-index:1;flex:0 0 auto;margin:0!important;padding:10px 8px 10px 6px!important;min-width:2.6em;max-height:none!important;overflow:visible!important;text-align:right;color:var(--dim,#6b7280);background:var(--bg1,#14161b)!important;border:0!important;user-select:none;-webkit-user-select:none;font:11.5px/1.5 var(--mono,ui-monospace,monospace)!important}
.vo-ed-c{position:relative;display:grid;flex:1 0 auto}
.vo-ed .vo-ed-h,.vo-ed .vo-ed-ta{grid-area:1/1;margin:0!important;padding:10px 12px!important;border:0!important;border-radius:0!important;box-shadow:none!important;font:11.5px/1.5 var(--mono,ui-monospace,monospace)!important;letter-spacing:normal!important;word-spacing:normal!important;tab-size:2;-moz-tab-size:2;white-space:pre!important;overflow-wrap:normal!important;word-break:normal!important;overflow:hidden!important;box-sizing:border-box!important;text-align:left}
.vo-ed .vo-ed-h{color:var(--text,#d8dce4);background:transparent!important;max-height:none!important}
.vo-ed .vo-ed-h code{font:inherit!important;white-space:pre!important;background:transparent;padding:0!important;color:inherit}
.vo-ed .vo-ed-ta{width:100%;height:100%;min-width:0;min-height:0;resize:none;outline:none;background:transparent!important;color:transparent!important;-webkit-text-fill-color:transparent;caret-color:var(--text,#d8dce4)}
.vo-ed .vo-ed-ta::selection{background:color-mix(in srgb,var(--acc,#5a9e8f) 32%,transparent)}
.vo-ed .vo-ed-lint{border-top:1px solid var(--border,rgba(255,255,255,.09))}
.vo-modal .vo-ed{flex:1;display:flex;flex-direction:column;min-height:0;border-top:0}
.vo-modal .vo-ed-sc{flex:1;min-height:0}
.vo-modal .vo-ed .vo-ed-g{font-size:12.5px!important;line-height:1.55!important;padding:12px 8px 12px 10px!important}
.vo-modal .vo-ed .vo-ed-h,.vo-modal .vo-ed .vo-ed-ta{font-size:12.5px!important;line-height:1.55!important;padding:12px 14px!important}
.vo-ed .code-lint .cl-err{color:var(--err,#e06c6c)}.vo-ed .code-lint .cl-warn{color:var(--warn,#d9a54a)}
.vo-ed .code-lint{background:var(--bg0,#0e0f12);font-family:var(--mono,ui-monospace,monospace);font-size:10px;line-height:1.55;padding:4px 10px}
/* a diagram card: the drawing, its source under it on request */
.code-card.vo-diagram > pre{display:none}
.code-card.vo-diagram.src > pre{display:block;border-top:1px solid var(--border,rgba(255,255,255,.09))}
.code-card.vo-diagram.editing > pre{display:none}
.code-card.vo-diagram > .vo-mm,.code-card.vo-diagram > .mm-slot{margin:0!important;padding:8px;background:var(--bg0,#0e0f12)}
/* "Send to" in a card's bar: the bar's own look; the menu opens over the page */
.code-bar .vo-send,.vc-codehead .vo-send{display:inline-flex}
.code-bar .vo-send .mact,.vc-codehead .vo-send .mact{padding:3px 7px;font:500 10px/1 var(--f-ui,var(--sans,system-ui,sans-serif));color:var(--dim2,#8a92a0)}
.code-bar .vo-send svg,.vc-codehead .vo-send svg{width:12px;height:12px}
.code-bar .vo-send:has(.vo-chs),.vc-codehead .vo-send:has(.vo-chs){flex-basis:100%;flex-wrap:wrap}
/* the prose row (the same as the chat's reply row, for writing drawn anywhere) */
.vo-acts{display:flex;align-items:center;gap:2px;flex-wrap:wrap;position:relative}
.vo-acts .mact{display:inline-flex;align-items:center;gap:5px;border:none;border-radius:var(--r-sm,6px);background:transparent;color:var(--t3,var(--dim,#6b7280));font:500 10.5px/1 var(--f-ui,var(--sans,system-ui,sans-serif));padding:5px 8px;cursor:pointer}
.vo-acts .mact:hover,.vo-acts .mact:focus-visible,.vo-acts .ma-dd.open .ma-ddb{background:var(--surf2,rgba(255,255,255,.06));color:var(--t1,var(--text,#d8dce4));outline:none}
.vo-acts .mact.ok{color:var(--ok,#7bc47f)}.vo-acts .mact.err{color:var(--err,#e06c6c)}
.vo-acts svg{width:13px;height:13px;flex:0 0 auto}
.vo-acts .ma-sep{width:1px;height:14px;background:var(--bd,var(--border,rgba(255,255,255,.09)));margin:0 4px}
.vo-acts .ma-dd{position:relative}
.vo-acts .ma-menu{display:none;position:absolute;left:0;top:calc(100% + 4px);z-index:60;min-width:250px;padding:5px;border-radius:var(--ui-radius,10px);background:var(--surf,var(--bg1,#1a1d24));box-shadow:var(--elev,0 12px 34px -16px rgba(0,0,0,.7)),0 0 0 1px var(--bd,var(--border,rgba(255,255,255,.09)))}
.vo-acts .ma-menu.up{top:auto;bottom:calc(100% + 4px)}.vo-acts .ma-dd.open .ma-menu{display:block}
.vo-acts .ma-it{display:flex;align-items:flex-start;gap:10px;width:100%;border:none;background:none;text-align:left;padding:7px 9px;border-radius:var(--r-sm,6px);color:var(--t1,var(--text,#d8dce4));font:12px/1.3 var(--f-ui,var(--sans,system-ui,sans-serif));cursor:pointer}
.vo-acts .ma-it svg{margin-top:1px;color:var(--t3,var(--dim,#6b7280))}
.vo-acts .ma-it:hover,.vo-acts .ma-it:focus{background:var(--surf2,rgba(255,255,255,.06));outline:none}
.vo-acts .ma-it small{display:block;margin-top:2px;color:var(--t3,var(--dim,#6b7280));font-size:10.5px}
.vo-acts .ma-menu hr{border:none;height:1px;background:var(--bd,var(--border,rgba(255,255,255,.09)));margin:4px 2px}
.vo-acts .vo-chs{flex-basis:100%;display:flex;align-items:center;flex-wrap:wrap;gap:2px;margin-top:4px;padding:4px;border-radius:var(--r-sm,6px);background:var(--surf2,rgba(255,255,255,.04))}
.vo-acts .vo-chs-l{font:9.5px var(--mono,ui-monospace,monospace);color:var(--t3,var(--dim,#6b7280));margin:0 4px}
.vo-acts .vo-chs select{font-size:10.5px;max-width:170px}`;
  function adopt(root) {
    root = root || document; const host = root === document ? document.head : root;
    if (!host || (host.querySelector && host.querySelector(':scope > style[data-vera-output]'))) return;
    const st = document.createElement('style'); st.setAttribute('data-vera-output', ''); st.textContent = CSS;
    if (root === document) host.insertBefore(st, host.firstChild); else host.appendChild(st);   // the document's own sheets still win there
  }

  window.VeraOut = { version: 2, fileKind: (l) => FILE_KIND[String(l || '').toLowerCase()] || '', slidesOf, rowsOf, sheetValues, fileCard, render, card, diagram, sendMenu, bar, hostBtn, icons: I, fence, highlight, lintCode, lintCard, hydrate, wire, adopt, preview, run, edit, editFull, applyCode, symbols, codeBody, proseMenu, prose,
    register, blocks, hooks, previewable, runnable, wholePage, previewDoc, fenceFilename, langClass, ensureLib, css: CSS, barActions, actOn };
  const boot = () => { adopt(document); wire(document); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();

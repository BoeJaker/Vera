// The ESTATE INDEX — the UI Vera has today, as data. Sweeps the repo once and writes estate.json: every panel
// (a *_panel.html, chat_panel.html, the harness, and every register_ui(...) registration), with its handlers
// (CH.x() / onclick names), the top-level containers (ids / classes that shape it), the capabilities it calls,
// the LHM sections it registers (registerNav / rpane ids / .sec headings), the sub-tabs it draws, and its size.
// A future canvas is mapped against THIS, so a redesign starts from what exists rather than from memory.
//   node estate.mjs <vera repo> [out.json]
import fs from 'node:fs';
import path from 'node:path';
const [R, OUT = 'estate.json'] = process.argv.slice(2);
if (!R) { console.error('usage: node estate.mjs <vera repo> [out.json]'); process.exit(2); }
const rel = (p) => path.relative(R, p).replace(/\\/g, '/');
const CAP = /\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3})\b/g;   // dotted lowercase: a capability name
const CAP_STOP = new Set(['e.g', 'i.e', 'vs.', 'window.location', 'document.body', 'this.state', 'this.props', 'console.log', 'console.warn', 'console.error', 'json.stringify', 'json.parse', 'object.assign', 'object.keys', 'array.from', 'math.max', 'math.min', 'math.round', 'math.floor', 'math.abs', 'date.now', 'promise.all', 'style.display', 'e.target', 'e.data', 'el.style', 'el.dataset', 'r.ok', 'r.error', 'self.files', 'os.path', 'sys.argv', 'np.array']);
const isCapName = (s) => !CAP_STOP.has(s) && !/^(el|e|r|s|t|x|y|d|m|n|o|p|q|v|w|k|i|j|a|b|c|f|g|h|u|z|this|self|window|document|console|json|object|array|math|date|promise|navigator|localstorage|style|classlist|dataset|target|parent|node|item|data|res|req|resp|ctx|cfg|opts|args|kw|event|evt|err|error)\./.test(s) && s.split('.').length >= 2 && s.length <= 48;

// the capability catalog: every @capability("name") in the Python sources — the only dotted names that count
const CATALOG = new Set();
const walkPy = (dir, depth) => { if (depth > 6) return; let ents = []; try { ents = fs.readdirSync(dir, { withFileTypes:true }); } catch { return; }
  for (const e of ents){ if (e.name.startsWith('.') || e.name === '__pycache__' || e.name === 'node_modules') continue; const p = path.join(dir, e.name);
    if (e.isDirectory()) walkPy(p, depth + 1); else if (e.name.endsWith('.py')){ let t; try { t = fs.readFileSync(p, 'utf8'); } catch { continue; }
      for (const m of t.matchAll(/@capability\(\s*["']([a-z][a-z0-9_.]+)["']/g)) CATALOG.add(m[1]); } } };
walkPy(path.join(R, 'vera'), 0);
const panels = {};
const at = (file, key, label) => { const id = key || file; if (!panels[id]) panels[id] = { id, file, label:label || '', registered:[], handlers:new Set(), containers:new Set(), caps:new Set(), sections:new Set(), subtabs:new Set(), bytes:0, kind:'' }; return panels[id]; };

const walk = (dir, depth) => { if (depth > 6) return; let ents = []; try { ents = fs.readdirSync(dir, { withFileTypes:true }); } catch { return; }
  for (const e of ents){
    if (e.name.startsWith('.') || e.name === 'node_modules' || e.name === '__pycache__' || e.name === 'static' && depth === 0) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()){ walk(p, depth + 1); continue; }
    if (!/\.(html|js|py)$/.test(e.name)) continue;
    // the UI runtimes: vera_graph*.js, vera-*.js, *_element.js, the chat's / elements' / render's scripts — never a panel's extracted blocks, tests or vendor code
    const isRuntime = /\.js$/.test(e.name) && !/\.html\.blk\d+\.js$|^test_|\.min\.js$/.test(e.name) && (/^vera[_-]|_element\.js$|_overlay\.js$/.test(e.name) || /^vera\/(chat|elements|render|ui)\//.test(rel(p)));
    if (/\.js$/.test(e.name) && !isRuntime) continue;
    let st; try { st = fs.statSync(p); } catch { continue; }
    if (st.size > 3e6) continue;
    const t = fs.readFileSync(p, 'utf8');
    // registrations: register_ui("id", "Label", "icon", html, js, ...)
    for (const m of t.matchAll(/register_ui\(\s*["']([^"']+)["']\s*,\s*["']([^"']*)["']/g)){
      const pn = at(rel(p) + '#' + m[1], rel(p), m[2]); pn.registered.push({ id:m[1], label:m[2], in:rel(p) }); pn.kind = pn.kind || 'registered';
    }
    if (isRuntime){
      const pn = at(rel(p), rel(p), (t.match(/customElements\.define\(\s*['"]([^'"]+)['"]/) || [])[1] || e.name.replace(/\.js$/, ''));
      pn.kind = 'runtime'; pn.bytes = st.size;
      for (const m of t.matchAll(/customElements\.define\(\s*['"]([^'"]+)['"]/g)) pn.containers.add('<' + m[1] + '>');
      for (const m of t.matchAll(/^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)) if (pn.handlers.size < 400) pn.handlers.add(m[1]);
      for (const m of t.matchAll(/^\s{2,6}(?:async\s+)?([a-zA-Z_$][\w$]*)\s*\([^)]*\)\s*\{/gm)) if (!/^(if|for|while|switch|catch|function|return)$/.test(m[1]) && pn.handlers.size < 400) pn.handlers.add(m[1]);
      for (const m of t.matchAll(/\bid="([a-zA-Z][\w-]{1,40})"/g)) if (pn.containers.size < 300) pn.containers.add('#' + m[1]);
      for (const m of t.matchAll(/\bclass="([a-zA-Z][\w-]{1,40})/g)) if (pn.containers.size < 300) pn.containers.add('.' + m[1]);
      for (const m of t.matchAll(CAP)) if (CATALOG.has(m[1])) pn.caps.add(m[1]);
      for (const m of t.matchAll(/window\.([A-Z][A-Za-z]+)\s*=/g)) pn.sections.add('window.' + m[1]);
    }
    if (/\.html$/.test(e.name)){
      const key = e.name.replace(/\.html$/, '');
      if (!/_panel$|^chat_panel$|^capability_orchestration$|_studio$|_hub$|_dashboard$|^registry_panel$/.test(key) && !/panel/.test(key)) continue;
      const pn = at(rel(p), rel(p), (t.match(/<title>([^<]{1,80})</) || [])[1] || (t.match(/<h1[^>]*>([^<]{1,80})</) || [])[1] || key);
      pn.kind = pn.kind || (key === 'capability_orchestration' ? 'harness' : key === 'chat_panel' ? 'chat' : 'panel'); pn.bytes = st.size;
      for (const m of t.matchAll(/onclick="\s*(?:CH\.)?([A-Za-z_$][\w$]*)\s*\(/g)) pn.handlers.add(m[1]);
      for (const m of t.matchAll(/<(?:div|section|aside|nav|main|header|footer)\s+[^>]*\bid="([^"]{2,40})"/g)) pn.containers.add('#' + m[1]);
      for (const m of t.matchAll(/^\s*(#[a-zA-Z][\w-]{1,40}|\.[a-zA-Z][\w-]{1,40})\s*\{/gm)) if (pn.containers.size < 400) pn.containers.add(m[1]);
      for (const m of t.matchAll(CAP)) if (CATALOG.has(m[1])) pn.caps.add(m[1]);
      for (const m of t.matchAll(/registerNav\(\s*\[([^\]]{0,600})\]/g)) for (const x of m[1].matchAll(/label\s*:\s*['"]([^'"]+)['"]/g)) pn.sections.add(x[1]);
      for (const m of t.matchAll(/class="rpane"\s+id="([^"]+)"/g)) pn.sections.add(m[1]);
      for (const m of t.matchAll(/<div class="sec">([^<]{1,60})</g)) pn.sections.add(m[1].trim());
      for (const m of t.matchAll(/class="(?:tab|subtab|st|ctx-tab)[^"]*"[^>]*>([^<]{1,40})</g)) pn.subtabs.add(m[1].trim());
    }
  } };
walk(path.join(R, 'vera'), 0);

const list = Object.values(panels).map((p) => ({ id:p.id, file:p.file, label:p.label, kind:p.kind, bytes:p.bytes, registered:p.registered,
  handlers:[...p.handlers].sort(), containers:[...p.containers].slice(0, 200), caps:[...p.caps].sort(), sections:[...p.sections], subtabs:[...p.subtabs] }));
const summary = { catalog:CATALOG.size, panels:list.length, files:new Set(list.map((p) => p.file)).size, handlers:list.reduce((n, p) => n + p.handlers.length, 0),
  caps:new Set(list.flatMap((p) => p.caps)).size, sections:list.reduce((n, p) => n + p.sections.length, 0) };
fs.writeFileSync(OUT, JSON.stringify({ generated:'by vera-design-adopt/estate.mjs', at:new Date().toISOString(), repo:R, summary, catalog:[...CATALOG].sort(), panels:list }, null, 1));
console.log('estate: ' + summary.panels + ' panels in ' + summary.files + ' files · ' + summary.handlers + ' handlers · ' + summary.caps + ' of ' + summary.catalog + ' catalogued capabilities named · ' + summary.sections + ' sections → ' + OUT);

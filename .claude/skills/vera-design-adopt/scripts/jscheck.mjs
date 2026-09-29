// Syntax-check what a slice is about to land: every .js file as a script, and every inline <script> of every .html
// page (node --check on each, in a temp dir). A page that dies in its own script mounts nothing and looks like a
// half-built UI for as long as it takes someone to notice — run this before the stand-in smoke, never instead of it.
//   node scripts/jscheck.mjs <file.js|page.html> [...]        exit 1 on the first failure, listing every file
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path'; import { spawnSync } from 'node:child_process';
const files = process.argv.slice(2);
if (!files.length) { console.log('usage: node scripts/jscheck.mjs <file.js|page.html> [...]'); process.exit(2); }
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jscheck-'));
let n = 0, bad = 0;
const chk = (label, code) => {
  const f = path.join(tmp, 'chk_' + (++n) + '.js'); fs.writeFileSync(f, code);
  const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' });
  const ok = r.status === 0; if (!ok) bad++;
  console.log((ok ? 'ok   ' : 'FAIL ') + label + ' (' + code.length + ' chars)' + (ok ? '' : '\n' + r.stderr.split('\n').slice(0, 8).join('\n')));
};
for (const f of files) {
  const src = fs.readFileSync(f, 'utf8');
  if (/\.html?$/i.test(f)) {
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g; let m, i = 0;
    while ((m = re.exec(src))) { i++; if (m[1].trim().length > 40) chk(path.basename(f) + ' · inline script ' + i, m[1]); }
  } else chk(path.basename(f), src);
}
try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
console.log(bad ? bad + ' FAILED' : 'all ' + n + ' scripts parse');
process.exit(bad ? 1 : 0);

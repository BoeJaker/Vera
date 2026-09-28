// Scope a board's stylesheet to its own root, so the board can be imported into another board
// (dc-import) without its rules colliding with the host's. Idempotent: marks the sheet when done.
import fs from 'node:fs';
const F = process.argv[2], TAG = process.argv[3] || 'canvas';
let t = fs.readFileSync(F, 'utf8');
const MARK = '/*@scoped ' + TAG + '*/';
if (t.includes(MARK)) { console.log('already scoped'); process.exit(0); }
const a = t.indexOf('<style>') + 7, b = t.indexOf('</style>');
const css = t.slice(a, b);
const ROOT = '.rt[data-board="' + TAG + '"]';
const rewriteSel = (sel) => {
  const s = sel.trim(); if (!s) return sel;
  if (/^(\*|body|html|:root|from|to|\d+%|@)/.test(s)) return s;
  if (s.startsWith('.rt')) return ROOT + s.slice(3);
  if (/^\[data-(style|theme|den|blocks|embed)/.test(s)) return ROOT + s;
  return ROOT + ' ' + s;
};
let out = '', i = 0, depth = 0, inKeyframes = false, kfDepth = 0;
let selStart = 0;
while (i < css.length){
  const ch = css[i];
  if (ch === '/' && css[i + 1] === '*'){ const e = css.indexOf('*/', i + 2); const j = e < 0 ? css.length : e + 2; out += css.slice(i, j); i = j; selStart = out.length; continue; }
  if (ch === '{'){
    const head = out.slice(selStart);
    const hs = head.trim();
    if (hs.startsWith('@keyframes')){ inKeyframes = true; kfDepth = depth; }
    if (!inKeyframes && !hs.startsWith('@')){
      const rewritten = hs.split(',').map(rewriteSel).join(',');
      out = out.slice(0, selStart) + head.replace(hs, rewritten);
    }
    out += ch; depth++; i++; selStart = out.length; continue;
  }
  if (ch === '}'){ depth--; out += ch; i++; selStart = out.length; if (inKeyframes && depth === kfDepth) inKeyframes = false; continue; }
  if (ch === ';' && depth === 0){ out += ch; i++; selStart = out.length; continue; }
  out += ch; i++;
}
t = t.slice(0, a) + '\n  ' + MARK + '\n' + out + t.slice(b);
// the root carries the scope
t = t.replace(/<div class="rt"([^>]*)>/, (m, attrs) => attrs.includes('data-board=') ? m : `<div class="rt"${attrs} data-board="${TAG}">`);
fs.writeFileSync(F, t);
console.log('scoped ' + TAG + ': ' + (out.match(/\{/g) || []).length + ' blocks');

// The canvas puts a removed item back (owner 2026-10-11: "removed canvas element should be restorable"): a remove shows
// Undo for a few seconds; the "removed" shelf (the document's last 30 removals) restores any of them later.
// The server side is tests/test_canvas_restore.py.   node tests/test_canvas_restore_ui.cjs
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const CE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
t('a remove shows Undo, which calls canvas.restore with the item\'s key', /if \(act === 'remove'\) \{[\s\S]{0,600}this\._undoToast\('Removed ' \+ name, \(\) => this\.call\('canvas\.restore', \{ key: String\(b0\.key\) \}\)\)/.test(CE));
t('the toast leaves after 8 s or when acted on, and lives outside the redrawn layers (the shadow root)', CE.includes('t._t = setTimeout(done, 8000); sr.appendChild(t);') && CE.includes("t.className = 'vc-toast'"));
t('the removed shelf lists doc.removed (newest first, not what is back on the canvas), up to 12', CE.includes("((doc && doc.removed) || []).filter((r) => r && r.key && !keyed.some((b) => String(b.key) === String(r.key))).slice().reverse().slice(0, 12)"));
t('a shelf chip restores its item', CE.includes('data-act="restore" data-rkey="') && CE.includes("if (act === 'restore') { const rk = btn.dataset.rkey; if (rk) return this.call('canvas.restore', { key: rk }); return; }"));
t('the shelf opens like the hidden and parked shelves', CE.includes("act === 'hid' || act === 'parkpop' || act === 'rmvpop'"));
t('toast and shelf colours come from theme names', /\.vc-toast\{[^}]*var\(--bg2,[^}]*\}/.test(CE) && /\.vc-toast button\{[^}]*var\(--accent,/.test(CE));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);

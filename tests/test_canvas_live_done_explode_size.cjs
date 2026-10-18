// 2026-10-03: a canvas code item streamed from the chat stops being the streaming card when the stream ends (or goes
// quiet); an exploded diagram can be made taller or opened over the window, and its caption is never clipped
//   node tests/test_canvas_live_done_explode_size.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const G = fs.readFileSync(path.join(__dirname, '..', 'vera', 'graph_embed_element.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const K = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera_output_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const d = C.indexOf('    liveDone(key) {'), done = C.slice(d, C.indexOf('    _liveRedraw()', d));
t('the end of a stream redraws the item, so the card gets its bar back', done.includes('this._liveRedraw();'));
t('every growth is timed', C.includes('(this._liveAt = this._liveAt || {})[String(key)] = Date.now();') && C.includes('this._liveRedraw(); this._liveExpire();'));
t('an item nothing has grown for 8 s lets go by itself (a stopped or failed stream)', /now - \(\(this\._liveAt && this\._liveAt\[k\]\) \|\| 0\) > 8000\) \{ delete this\._liveOv\[k\];/.test(C) && C.includes('if (gone) this._liveRedraw();'));
t('the canvas sizes its own explode item: no grip there', C.includes("inner = document.createElement('vera-graph-embed'); h.textContent = ''; inner.setAttribute('resize', 'off');"));

t('the explode caption wraps rather than clips', G.includes("left.style.cssText = 'min-width:0;overflow-wrap:anywhere';"));
t('a struct diagram gets a drag grip unless the host says resize="off"', G.includes("=== 'struct' && this.getAttribute('resize') !== 'off') this._mkGrip();") && G.includes("g.style.cssText = 'height:9px;cursor:ns-resize;"));
t('...bounded between 160 px and most of the window', G.includes("if (!(mn >= 40)) mn = 160;") && G.includes('h = Math.max(mn, Math.min(Math.round(h), Math.round((window.innerHeight || 900) * 0.92)));')   /* a host may ask for less (min-h, a desk card) */);
t('"enlarge" opens it over the window, with a placeholder holding its place', G.includes("big.textContent = this._ov ? 'back to size ✕' : 'enlarge ⤢';") && G.includes('this.parentNode.insertBefore(this._ph, this);') && G.includes("ov.style.cssText = 'position:fixed;inset:3vh 3vw;"));
t('...Esc and the backdrop put it back', G.includes("if (ev.key === 'Escape') { ev.stopPropagation(); self._enlarge(false); }") && G.includes('back.onclick = function () { self._enlarge(false); };'));
t('...and the move up and back does not tear the graph down', G.includes('if (this._ov) return;   // only moving up to (or back from) the enlarged view'));
t('the chat panel no longer fixes the body under the diagram (it clipped the caption)', !H.includes('<div class="xp-body" style="height:340px"></div>') && H.includes("'<div class=\"xp-body\"></div>'")   /* full screen fills instead: flex, never a fixed height */);
t('...nor does the kit card', !K.includes('.code-card .xp-body{height:340px}'));

// the embed parses
try { new Function(G); t('graph_embed_element.js parses', true); } catch (e) { t('graph_embed_element.js parses: ' + e.message, false); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

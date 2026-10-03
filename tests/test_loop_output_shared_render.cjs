// 2026-10-03: the agent loop draws its output with the shared kit (markdown, code cards with every action) and what it
// makes reaches the session canvas like a chat reply
//   node tests/test_loop_output_shared_render.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const L = fs.readFileSync(path.join(__dirname, '..', 'vera', 'agent_loop_ouput.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('the loop loads the markdown element and the output kit itself', L.includes("['/ui/elements/vera_markdown.js','data-vera-md',() => window.VeraMD]") && L.includes("['/ui/elements/vera_output.js','data-vera-out',() => window.VeraOut]"));
t('its markdown is the kit\'s (fences become the shared cards)', /function _renderMarkdown\(md\)\{\s*if\(!md\) return '';\s*if\(window\.VeraOut && typeof VeraOut\.render === 'function'\)/.test(L));
t('the final answer renders - not raw text', L.includes('<div class="alo-final-val">${_smartRender(summary)}${proseRow(summary)}</div>') && !L.includes('<div class="alo-final-val summary">${_esc(summary)}</div>'));
t('the fast-path preview renders too', L.includes('${_smartRender(String(ev.preview))}'));
t('its shadow root takes the kit (actions + style) and hydrates what lands', L.includes('VeraOut.wire(this._sr); VeraOut.hydrate(this._sr);') && L.includes('this._outMO.observe(this._sr, {childList:true, subtree:true})') && /this\._render\(\);\s*this\._outKit\(\);/.test(L));
t('the chat lands the loop\'s final answer on the session canvas', H.includes("aloEl.shadowRoot.querySelector('[data-part=\"final-body\"]')") && H.includes('_cvLandFrom(fb, uMsg.mid, statusCard.mid||uMsg.mid)'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

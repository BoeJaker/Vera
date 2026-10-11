// The session canvas's controls live in the chat's LHM (owner 2026-10-11: "id like the session canvas controls along the
// top to be moved to the chat lhm to give the session canvas full height and a more minimalist look").
// Harness proofs: tools/snip_canvas_lhm.js (one page), tools/snip_canvas_lhm_split.js (?only=menu beside ?only=chat).
//   node tests/test_canvas_controls_in_lhm.cjs
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const CE = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const CH = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
t('the canvas draws no add bar with chrome="none"', CE.includes(':host([chrome="none"]) .addbar{display:none!important}'));
t('the canvas exposes its add bar\'s actions: kinds, addKind, shelves, bringBack, restore, clearAll', ['kinds() {', 'addKind(n, anchor) {', 'shelves() {', 'bringBack(key) {', 'restore(key) {', 'clearAll(keepPinned) {'].every((s) => CE.includes(s)));
t('the column mounts the banner controls into the Canvas pane (ids kept) and sets chrome="none"; vera:canvas:chrome=bar keeps the old banner',
  CH.includes('try{ _cvChromeMount(el); }catch(_){} host.appendChild(el') && CH.includes("el.setAttribute('chrome','none');") && CH.includes("localStorage.getItem('vera:canvas:chrome')==='bar'") && !CH.includes("if(tpl&&tpl.content) el.appendChild(tpl.content.cloneNode(true)); }catch(_){} host.appendChild(el"));
t('the driven ribbon floats over the column (it must be seen while Vera drives the canvas)', CH.includes("rib.classList.add('cv-ovl'); col.appendChild(rib);") && CH.includes('#canvasColumn > .drv-ribbon.cv-ovl{position:absolute'));
t('a Canvas tab and pane in the rail, and a Canvas menu in the LHM with its action', CH.includes(`<div class="ctab rtab" onclick="CH.railTab('Canvas',this)">Canvas</div>`) && CH.includes('<div class="rpane" id="rpCanvas"') && /\{id:'canvas',\s+icon:'▤', label:'Canvas'/.test(CH) && CH.includes("cta:{label:'+ Add to the canvas'"));
t('the split: the chat page publishes cvstate and runs cvact; the menu page draws from cvstate and sends cvact',
  CH.includes("_lhmPost('cvstate', st)") && CH.includes("if(m.act==='cvact'){ try{ _cvMenuAct(a)") && CH.includes("else if(m.act==='cvstate'){ _cvRemote=a;") && CH.includes("_lhmPost('cvact', a)"));
t('the pane lists the kinds and the three shelves (parked, hidden, recently removed) and clears', ["'Parked'", "'Hidden'", "'Recently removed'", 'data-cvm="clear"', 'data-cvm="add"'].every((s) => CH.includes(s)));
console.log(fails ? fails + ' failed' : 'all passed'); process.exit(fails ? 1 : 0);

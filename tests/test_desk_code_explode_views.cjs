// WSU-54: the CODE explode on the desk (not prose, not the chat's Explode scene) fills the desk and shows the code, the
// structure, or both side by side with the structure the larger.   node tests/test_desk_code_explode_views.cjs
const path = require('node:path'), fs = require('node:fs');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
// WSU-58 S4: the three views are the item's ORDINARY views (viewsOf · setview · viewSet → _itemSet); see test_code_explode_views.cjs
t('only a code item whose explode is open has the views', C.includes("if (b.type === 'code' && el && key && codeXploded(el, key) && canExplode(b, el.explodeOffer ? el.explodeOffer() : 'both') === 'code')"));
t('both is the default, the pick is per item', C.includes("const v = ['code', 'structure', 'both'], cur = viewOf(el, key, 'both');"));
t('the switch is the item\'s view control: the code, both, the structure (DEEP-29 11A.4 - one view control)', C.includes("code: 'The code alone', structure: 'The structure alone', both: 'The code and its structure'") && !C.includes('data-act="xview"'));
t('both: the structure takes three parts to the code\'s two', C.includes('.it.xv-both > .it-bd.fu-beside > :not(.fu-p){flex:2 1 0!important}') && C.includes('.it.xv-both > .it-bd.fu-beside > .fu-p{flex:3 1 0!important}'));
t('one view hides the other pane', C.includes('.it.xv-code > .it-bd.fu > .fu-p{display:none!important}') && C.includes('.it.xv-structure > .it-bd.fu > :not(.fu-p){display:none!important}'));
t('explode still fills the desk and back still returns it (unchanged)', C.includes("if (this.deskAlign() && this._max !== key) this._toggleMax(key);") && C.includes("if (bound) { if (this.deskAlign() && this._max === key) this._toggleMax(key); return this.call('canvas.remove'"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// WSU-76 (owner 2026-10-12): the Canvas LHM menu shows every control the old top bar had, adding from it works, and the
// arrangement is in it.   node tests/test_canvas_lhm_menu_complete.cjs
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the Canvas pane is moved into the unified rail with the other menus (it was left in the hidden #leftRail)',
  H.includes("['rpHist','rpCfg','rpCtx','rpCaps','rpPanels','rpLoop','rpCanvas'].forEach(id=>{") && H.includes("{id:'rpCanvas',w:'canvas controls · menu'}"));
t('the menu action brings the add list into view and points it out', H.includes("find(x=>/Add to the canvas/.test(x.textContent)); if(!sec) return;") && H.includes('#rpCanvas .cvm-sec.cvm-flash{'));
t('the arrangement is in the menu, from the same Settings field', H.includes("arrange:(({orbit:'auto',stack:'auto',kind:'map',timeline:'flow'})[_cvCfg('cfgCvArrange')]") && H.includes("if(a.a==='arrange'){ const e=document.getElementById('cfgCvArrange');") && H.includes('<h5>Arrange the desk</h5>'));
t('an add with no top bar opens its popup floating beside the button (it returned nothing since WSU-72)',
  C.includes("const bar = (btn && btn.closest && btn.closest('.addbar')) || this._addFloat(btn); if (!bar) return null;") && C.includes(".vc-addfloat{position:fixed;z-index:420;width:0;height:0}"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// 2026-10-03 (owner), the LHM audit: point 1 ("each panel use a common lhm styling that matches the lhm of the harness")
// and points 3/4 ("sub/internal LHMs editable, with widgets like the chat UI"; "chat UI is the template"); 2026-10-04:
// "continue on the outstanding items other than 8". A panel's menu carries what a chat menu carries - its items, the
// widgets named for each, one primary action - docked or on its own; a menu that is a strip across the page is drawn as
// the menu column when the panel stands alone.
//   node tests/test_lhm_chat_anatomy.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const VP = R('vera', 'vera-panel.js'), BR = R('vera', 'chat', 'vera-panel-bridge.js'), LHM = R('vera', 'chat', 'vera-lhm.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// point 1: a strip drawn as the menu column, standalone
t('strip: a menu that is not a column gets the menu column (vp-menu), its items pressing the panel\'s own tabs', /function initStripMenu\(host, btns, items\) \{/.test(VP) && /col\.className = 'vp-menu vp-strip-menu';/.test(VP) && /b\.addEventListener\('click', function \(\) \{ try \{ btns\[i\]\.click\(\); \} catch \(e\) \{\} setTimeout\(lit, 30\); \}\);/.test(VP));
t('strip: the lit item follows the panel', /var mo = new MutationObserver\(lit\);/.test(VP) && /attributeFilter: \['class', 'aria-selected'\]/.test(VP));
t('strip: no letters - an unmarked item takes the glyph its name maps to (VeraLHM.glyphFor)', /m\.gl\.textContent = VeraLHM\.glyphFor\(m\.it\.label\) \|\| '';/.test(VP) && /s\.src = '\/ui\/vera-lhm\.js';/.test(VP));
t('strip: docked, the column steps aside and the strip is back', /html\.vpb-nav-hosted \.vp-strip-menu\{display:none!important\}/.test(VP) && /html\.vpb-nav-hosted body\.vp-has-strip-menu \.vp-strip-tab\{display:revert!important\}/.test(VP));
t('strip: decided once the menu has a size (a hidden harness tab measures 0x0)', /if \(hr0 && \(hr0\.width \|\| hr0\.height\)\) placeMenus\(\);/.test(VP) && /new ResizeObserver\(function \(\) \{ var r = host\.getBoundingClientRect\(\); if \(!r\.width && !r\.height\) return; ro0\.disconnect\(\); placeMenus\(\); \}\)/.test(VP));
t('strip: a panel can keep its strip (data-vp-strip-keep)', /host\.hasAttribute\('data-vp-strip-keep'\)/.test(VP));
t('strip: the menu\'s widgets go into the column', /initMenuWidgets\(strip \|\| host, btns, idOf, groupOf, items\);/.test(VP));

// points 3/4: the chat's menu anatomy
t('bridge: a menu carries its items\' widgets (data-lhm-widgets on an item)', /if\(it\.widgets && it\.widgets\.length\) o\.widgets = /.test(BR) && /if\(own\.length\) m\.widgets = own\.slice\(0, 8\);/.test(BR));
t('bridge: a menu carries its primary action (data-lhm-cta, for one item\'s menu or every menu)', /function _navCtas\(\)\{/.test(BR) && /if\(c\) m\.cta = \{ id: 'cta:' \+ c\.i, label: c\.label, desc: c\.desc \};/.test(BR));
t('bridge: pressing it presses the panel\'s own, the menu stays', /if\(id\.indexOf\('cta:'\) === 0\)\{ var ce = _navCtaEls\(\)\[\+id\.slice\(4\)\]; if\(!ce\) return \{ok: false, error: 'no primary action ' \+ id\}; ce\.click\(\); return \{ok: true\}; \}/.test(BR));
t('bridge: a group\'s descriptions are set (they sat behind a // and never ran)', /^\s*if\(it\.group_desc\) o\.group_desc = cl\(it\.group_desc, 140\);/m.test(BR) && !/SVG cleaner if\(it\.group_desc\)/.test(BR));
t('docked: the menu\'s own widgets before the panel\'s', /var dflt = \(cur\.widgets && cur\.widgets\.length\) \? \{ widgets: cur\.widgets \} : spec;/.test(LHM) && /_absDefaults\(dflt\)/.test(LHM));
t('docked: the primary action ends the menu, as the chat\'s menus end', /if\(cur && cur\.cta && cur\.cta\.id\)\{ var cta = _el\('button', 'lhm-cta lhm-a-cta'/.test(LHM) && /pickFn\(cur\.cta\.id\);/.test(LHM));
t('standalone: the open item\'s widgets, and its primary action under them', /it\.widgets = wl\.split\(\/\[\\s,\]\+\/\);/.test(VP) && /function ctaFor\(\) \{/.test(VP) && /cta\.onclick = function \(\) \{ e\.click\(\); \};/.test(VP));

// the panels that name theirs
for (const [name, file, re] of [['Dream', ['vera', 'dream', 'dream_panel.html'], /id="btn-run" data-lhm-cta="" data-label="Run a dream"/],
  ['Calendar', ['vera', 'calendar', 'calendar_panel.html'], /data-lhm-cta="" data-label="New event"/],
  ['Intelligence (topics)', ['vera', 'intel', 'intel_panel.html'], /data-lhm-cta="topics" data-label="New topic"/],
  ['Intelligence (reports)', ['vera', 'intel', 'intel_panel.html'], /data-lhm-cta="reports" data-label="Build a brief"/]])
  t(name + ': names its primary action', re.test(R(...file)));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

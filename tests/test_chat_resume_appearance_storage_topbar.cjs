// 2026-10-03: the chat you left is the chat you come back to; the chat's Aa opens the appearance menu (not Settings);
// Full/Hover/Zen live in that menu, not the bar; "Ops glance" is Capabilities; a folded menu frame shows its rail alone;
// the harness keeps its activity dropdown and draws the agent as the chat's chip; the estate's storage is a delivery
//   node tests/test_chat_resume_appearance_storage_topbar.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), U = R('vera/vera-ui.js'), HAR = R('vera/capability_orchestration.html');
const K = R('vera/vera_output_element.js'), D = R('vera/delivery.py'), RC = R('vera/render/render_capabilities.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const i = H.indexOf('function initSid(id){'), sid = H.slice(i, i + 400);
t('the stored session is resumed whatever its age (no four-hour cut-off)', sid.includes("try{ resolved = localStorage.getItem('vera_session_id') || ''; }catch(_){}") && !H.includes('SESSION_RESUME_HOURS'));

t("the chat's Aa asks for the appearance menu, and the page has one (vera-ui.js)", H.includes("function _appearanceSheet(ev){ try{ if(window.veraUI&&typeof veraUI.openAppearance==='function'){ veraUI.openAppearance(ev); return; } }")
  && U.includes('function openAppearance(ev){') && U.includes('openAppearance: openAppearance,'));
t('...with the rows the harness Aa has: style, detail (Full/Hover/Zen), blocks, zoom, text, themes', U.includes("rows.appendChild(row('Style',") && U.includes("rows.appendChild(row('Detail', [['full', 'Full'") && U.includes("rows.appendChild(row('Blocks',")
  && U.includes('menu.appendChild(_makeScaleControl());') && U.includes('menu.appendChild(_makeTextControl());') && U.includes("fetch(BASE + '/ui/themes')"));
t('...and a second press closes it (not closed then reopened by the same press)', U.includes('if(Date.now() - (openAppearance._shut || 0) < 350) return;') && U.includes('if(btn && btn.contains(e.target)) openAppearance._shut = Date.now();'));
t('Full / Hover / Zen are not buttons in the bar', !H.includes('id="denGroup"') && !/<button class="tb-btn" data-den=/.test(H));
t('"Ops glance" is called Capabilities', H.includes("{id:'ops',      icon:'✦', label:'Capabilities',") && !H.includes("label:'Ops glance'"));

t('folded by the harness, the chat menu frame shows its rail alone', H.includes('html[data-only="menu"].lhm-folded #settingsPage,html[data-only="menu"].lhm-folded #rightRail.lhm-host > .lhm-det{visibility:hidden!important}')
  && H.includes("d.type!=='vera:lhm:folded'") && HAR.includes("fr.contentWindow.postMessage({ type: 'vera:lhm:folded', on: f }, '*')"));
t('the harness keeps its activity dropdown in the bar while the chat holds it', /const _HDR_DRAWER_IDS = \['hdrPills', 'hdrMetrics', 'hdrMetricCfgWrap', 'hdrStyleSeg', 'hdrSwatches'\];/.test(HAR));
t('the agent is offered with its model and caps, and drawn as one chip', H.includes("meta:String(((document.getElementById('agentMeta')||{}).textContent)||'').trim().slice(0,48), caps:!!(cb&&!cb.classList.contains('hidden'))")
  && HAR.includes("if(g.grp === 'agent'){ G.classList.add('hp-agent');") && HAR.includes('header .hp-grp.hp-agent{gap:8px;height:30px;'));

t('the estate storage is a delivery channel over report.store', /"storage": \{\s*"label": "Estate storage", "cap": "report\.store"/.test(D) && D.includes('"report.store":     _build_store,'));
t('...report.store keeps the report and its source in the object store, with a link', RC.includes('"report.store", memory="off",') && RC.includes('rep, put = _cap("report.html"), _cap("fabric.objects.put")')
  && RC.includes('pre = (prefix or "").strip().strip("/") or ("reports/" + time.strftime("%Y-%m"))') && RC.includes('link = "/render/stored?key=" + _q(key, safe="/")'));
t('...the link goes through Vera (the store\'s presigned links name its internal endpoint), and serves only reports/', RC.includes('@APP.get("/render/stored", include_in_schema=False)')
  && RC.includes('if not k.startswith("reports/") or ".." in k.split("/"):'));
t('...re-delivery hands back where it went', RC.includes('"episode_id", "store_url", "key", "bucket"):'));
t('...and Send to offers it everywhere', K.includes("mitem('storage', 'save', 'Estate storage',") && K.includes("const r = await cap('output.redeliver', { channel: 'storage', content: md, title, session_id: session() });"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

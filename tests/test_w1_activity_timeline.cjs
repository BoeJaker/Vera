// Wave 1 (panel-derived): an activity.timeline answer opens as the Activity panel's own <vera-activity-timeline> (the widget element
// form); its scope comes from the answer ({scope}: an element attribute may name a field of the answer it was opened from).
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const D = JSON.parse(R('tests/fixtures/presentation_declared.json'))['activity.timeline'] || {};
t('activity.timeline declares the element form with the Activity timeline, its scope from the answer', D.form === 'element' && D.draw && D.draw.tag === 'vera-activity-timeline' && D.draw.attrs && D.draw.attrs.scope === '{scope}');
const WE = R('vera/widgets/widget_element.js');
t('the element form knows where the timeline script is', WE.includes("'vera-activity-timeline': 'activity_timeline'"));
// the attribute filling, run alone
const i = WE.indexOf('    const fillAttr = '), j = WE.indexOf('\n', i); const src = WE.slice(i, j).trim();
const mk = (data) => new Function('rec', src + '; return fillAttr;')({ data });
t('{field} is the answer field; a plain value stays; a missing field leaves the attribute out', mk({ scope: 'project:x' })('{scope}') === 'project:x' && mk({ scope: 'all' })('15') === '15' && mk({})('{scope}') === '' && mk([1])('{scope}') === '');
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

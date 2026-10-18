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
const END = "String(a[m[1]]) : ''; };";
const i = WE.indexOf('    const plainVal = '), j = WE.indexOf(END, i) + END.length; const src = WE.slice(i, j).trim();
const mk = (data, args) => new Function('rec', src + '; return fillAttr;')({ data, read: { args: args || {} } });
t('{field} is the answer field; a plain value stays; a missing field leaves the attribute out', mk({ scope: 'project:x' })('{scope}') === 'project:x' && mk({ scope: 'all' })('15') === '15' && mk({})('{scope}') === '' && mk([1])('{scope}') === '');
t("...else the call's argument of that name (intel.timeline.get's topic_id); the answer's field wins", mk({ days: [] }, { topic_id: 't7' })('{topic_id}') === 't7' && mk({ scope: 'a' }, { scope: 'b' })('{scope}') === 'a' && mk({}, {})('{days}') === '');
t('an answer field that is a list or a record is not an attribute: the argument is (intel.timeline.get days)', mk({ days: [{ date: 'x' }] }, { days: 30 })('{days}') === '30' && mk({ days: { a: 1 } }, {})('{days}') === '');
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

// WSU-77 (owner 2026-10-15: "the first part of llm output is the plan for the research, the outline - it streams in to the
// main panel but never reaches the formatted right hand panel"): what streams before the run writes is kept as its outline
// and drawn in the method pane; the main pane streams only the report.   node tests/test_research_outline.cjs
const path = require('node:path'), fs = require('node:fs');
const root = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(root, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const C = fs.readFileSync(path.join(root, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* the chat's run content, as the page has it, over a staged research card */
const a = H.indexOf('  function _cvRunContent('), b = H.indexOf('  function _cvRunFeed(');
t('the chat page has _cvRunContent', a > 0 && b > a);
const fn = new Function('return (' + H.slice(a, b).trim().replace(/^function _cvRunContent/, 'function') + ')')();
let snap = {};
const r = { rc: { snapshot: () => snap }, q: 'probe', err: '' };
const outline = '## Research plan\n\n1. What changed in the EU AI Act\n2. Who it applies to\n3. Deadlines';
snap = { status: 'running', step: 'planning', writing: false, live: outline, pages: [], cites: [], report: '' };
let c = fn('job1', r, true);
t('planning: the stream is kept as the outline', c.outline === outline, JSON.stringify(c.outline));
t('planning: the main pane gets nothing of it', c.live === '');
snap = Object.assign({}, snap, { step: 'writing the report', writing: true, live: '# The report\n\nIt begins.' });
c = fn('job1', r, true);
t('writing: the outline stays', c.outline === outline);
t('writing: the main pane streams the report', c.live === '# The report\n\nIt begins.');
snap = Object.assign({}, snap, { status: 'done', writing: true, live: '', report: '# The report\n\nDone.' });
c = fn('job1', r, false);
t('done: the outline is kept with the item (saved to the server)', c.outline === outline && c.report.startsWith('# The report'));

/* the desk draws it */
t('the research item draws the outline in the method pane', C.includes("const outlineHtml = outline ? `<h5>Outline</h5><div class=\"vc-rr-doc vc-rr-outline\">${draw(outline)}</div>` : '';")
  && C.includes("if (!plan) return outlineHtml ||") && C.includes('return h + outlineHtml;'));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

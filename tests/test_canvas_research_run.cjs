// A research run as one canvas item (vera/canvas/canvas_element.js BLOCK.research), written by the chat from the run's
// own card (vera/chat/chat_panel.html _cvRun*, vera/research_card_element.js snapshot).
const fs = require('node:fs');
const path = require('node:path');

let fails = 0;
const t = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) fails++; };

const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
t('the research block ships', !!(mod && mod.BLOCK && typeof mod.BLOCK.research === 'function'));
if (!mod || !mod.BLOCK || !mod.BLOCK.research) { console.log(fails + ' FAILED'); process.exit(1); }

const el = {};
const plan = { depth: 'deep', style: 'report', needs_recency: true, sub_questions: ['What changed in October?', 'Who released what?'],
  writer_instructions: 'Lead with the dated releases.', source_priority: ['news', 'papers'] };
const pages = [
  { id: 'https://a.example/x', url: 'https://a.example/x', domain: 'a.example', title: 'A release note', meta: { read: '4.2k chars', cited: 'cited in the report' }, cited: true },
  { id: 'https://b.example/y', url: 'https://b.example/y', domain: 'b.example', title: 'b.example', meta: { read: '900 chars' } },
  { id: 'https://c.example/z', url: 'https://c.example/z', domain: 'c.example', title: 'c.example', meta: { status: 'did not load' } },
];
const running = { title: 'Research · latest ai', query: 'latest ai', job: 'j1', status: 'running', step: 'crawling - 3 of 12', started: Date.now() - 42000,
  plan, items: pages, cites: [], live: 'Gathered so far: **two** releases.' };
const done = Object.assign({}, running, { status: 'done', secs: 95, live: '', step: '',
  cites: [{ url: 'https://a.example/x', title: 'A release note' }], report: '# October\n\n| model | date |\n|---|---|\n| X | 2026-10-02 |\n\nA paragraph.' });

const r = mod.BLOCK.research(running, 'l', 'research-run:j1', el);
t('running: says it is researching, with its step', /Researching/.test(r) && /crawling - 3 of 12/.test(r));
t('running: a progress bar and a pulsing state', /vc-rr-bar/.test(r) && /st-running/.test(r));
t('running: pages read and failed are counted', /2 pages read · 1 failed/.test(r));
t('the four panes are there, each behind its tab', ['report', 'method', 'pages', 'sources'].every((p) => r.includes(`data-p="${p}"`) && r.includes(`data-rr-tab="${p}"`)));
t('the method: depth, recency, its questions and its brief', /deep/.test(r) && /required/.test(r) && /What changed in October\?/.test(r) && /Lead with the dated releases/.test(r));
t('the report pane shows the prose so far while it runs', /Gathered so far/.test(r));
t('the pages are the records browser on the same key (reader mode, facets)', /vc-rbx/.test(r) && /data-rec-key="research-run:j1"/.test(r));
t('wide: the method sits beside the report while it runs', /class="vc-rr-p side" data-p="method"/.test(r) || /vc-rr-p[^"]*side[^"]*" data-p="method"/.test(r));

const d = mod.BLOCK.research(done, 'l', 'research-run:j1', {});
t('done: complete, with its time, no bar', /Research complete/.test(d) && /1 min 35 s/.test(d) && !/vc-rr-bar/.test(d));
t('done: the report is drawn (a table, not pipes)', /<table/.test(d) && !/\| model \|/.test(d));
t('done: the cited source is listed with its site', /A release note/.test(d) && /a\.example/.test(d) && /1 cited/.test(d));
t('done: the pages sit beside the report', /vc-rr-p[^"]*side[^"]*" data-p="pages"/.test(d));

const e = mod.BLOCK.research(Object.assign({}, running, { status: 'error', error: 'Cancelled.', live: '' }), 'm', 'k', {});
t('failed: says so, and why', /Research failed/.test(e) && /Cancelled\./.test(e));

const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
t('the chat starts the run item when the card is made, and feeds it on every message and at the end',
  chat.includes('_cvRunStart(jobId, rc)') && chat.includes("rc.push(m); try{ _cvRunFeed(jobId); }") && chat.includes('_cvRunFeed(jobId, { final:true })'));
t('the run item is written as kind research, one per job', chat.includes("r.L=_cvLive({ key:r.key, kind:'research'") && chat.includes("key:'research-run:'+jobId"));   // on the one live-item core (DA-04c)
t('with the run item there, no separate sources list or report copy', chat.includes('if(d.job&&_CV_RR[d.job]) return;') && chat.includes('if(jobId&&_CV_RR[jobId]) return 1;'));

const card = fs.readFileSync(path.join(__dirname, '..', 'vera', 'research_card_element.js'), 'utf8');
t('the research card hands its state as data', /snapshot\(\)\s*\{/.test(card) && /report: this\._done/.test(card));

const caps = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
t('the server knows the research kind', /"research":\s*\{"desc"/.test(caps));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// 2026-09-29 (owner):
//   * "the timeline barely functions and picks up dates that are not part of a distinct timeline"
//   * "the web.research is a really compact table and hard to see anything - the records dont expand"
//   * "fabric results ... doesnt show enough depth on each record i.e. its neighbors, meta data, etc"
//   * "the web results element also need better depth and rendering of results & their text"
//   * "the canvas elements flicker if you put the mouse near it edge or the edge of inner frames/divs"
//   * "grabbing and dragging to resize shoots you to the very bottom almost instantly ... if the drag starting point is
//      below a certain point on the page"
//   * "a details menu that pops out for widgets details on the right but if blocks are off it has no background"
//   node tests/test_canvas_depth_timeline_flicker_resize.cjs      (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const CHAT = R('vera/chat/chat_panel.html'), CVS = R('vera/canvas/canvas_element.js'), DASH = R('vera/chat/vera-dashboard.js'), CSS = R('vera/ui/design.css'), DF = R('vera/fabric/data_fabric.py');
const cv = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));

// ── a timeline is a history, not a text with dates in it ──────────────────────────────────────────────────────────
{
  const i0 = CHAT.indexOf('const _TL_MON='), i1 = CHAT.indexOf('  const _cvCapTerm=(c)=>{');
  const ctx = {}; vm.createContext(ctx); vm.runInContext(CHAT.slice(i0, i1) + '\nthis.T=_tlTimeline; this.E=_tlEvents;', ctx);
  const filler = Array.from({ length: 30 }, (_, i) => 'This sentence ' + i + ' is about how the system works and has no date.').join(' ');
  const scattered = 'The library was released in 2019. ' + filler + ' Its data comes from a 2021 survey. ' + filler + ' Support ended by 2024.';
  t('three dates scattered through a long answer are not a timeline', ctx.T(scattered).length === 0, String(ctx.T(scattered).length));
  const cited = ['Vector databases store embeddings.', '1. "What is a vector database". Retrieved 18 November 2023.', '2. "Vector search". Retrieved 11 January 2024.', '3. "Embeddings". Accessed 3 March 2025.'].join('\n');
  t('a citation\'s retrieval dates are not events', ctx.E(cited).length === 0, JSON.stringify(ctx.E(cited).map((e) => e.when)));
  t('a date in an address is not an event', ctx.E('See https://github.blog/changelog/2026-09-29-bring-business-context for more.').length === 0);
  const chron = ['## History', '1976: Apple is founded in a garage.', '1984: The Macintosh launches.', '2007: The iPhone is announced.', '2011: Tim Cook becomes chief executive.'].join('\n');
  const ev = ctx.T(chron);
  t('a chronology is a timeline', ev.length === 4, String(ev.length));
  t('and its dates lead their lines', ev.every((e) => e.lead));
  const report = ['Apple Inc. was founded on April 1, 1976 by Steve Jobs and Steve Wozniak.', 'In 1984 the company launched the Macintosh.',
    'The iPhone was announced on January 9, 2007.', 'Tim Cook became chief executive in August 2011.', 'By 2018 it was worth a trillion dollars.'].join(' ');
  t('a history told in prose is a timeline (most of it is dated)', ctx.T(report).length === 5, String(ctx.T(report).length));
  t('two years are not a span to draw', ctx.T('In 2019 it began. In 2019 it grew. In 2020 it ended.').length === 0);
  t('the lifted timeline and the research timeline both ask the gate', /ev=_tlTimeline\(txt,\{\}\)/.test(CHAT) && /events=_tlTimeline\(text,\{\}\)/.test(CHAT));
}
// ── ...drawn as one: the span on an axis, the spine, the gaps ─────────────────────────────────────────────────────────
{
  const h = cv.BLOCK.timeline({ title: 'Apple', events: [
    { when: '1976-04-01', label: 'In 1976 Apple was founded.', lead: true, raw: '1976' }, { when: '1984', label: 'In 1984 the Macintosh launched.', lead: true, raw: '1984' },
    { when: '2007-01-09', label: 'The iPhone was announced on January 9, 2007.', url: 'https://example.org/iphone' }] });
  t('its span and count in the head', /3 events · 1976–2007/.test(h));
  t('a dot per event on the axis, each the way to its event', (h.match(/class="vc-tl-dot"/g) || []).length === 3 && /data-tl-go="2"/.test(h));
  t('years passing with nothing in them are said', /… 8 years/.test(h) && /… 23 years/.test(h));
  t('a label that opens with its date loses it (the axis has it)', />Apple was founded\.</.test(h) && />The Macintosh launched\.</.test(h));
  t('a date inside the sentence stays', /The iPhone was announced on January 9, 2007\./.test(h));
  t('a day reads as a day', />1 Apr</.test(h) && />9 Jan</.test(h));
  t('an event keeps its source', /href="https:\/\/example.org\/iphone"/.test(h));
  t('a dot press goes to its event', /const tg = t\.closest\('\[data-tl-go\]'\);/.test(CVS) && /row\.classList\.add\('flash'\)/.test(CVS));
}
// ── research: one list you open, with the text it read ───────────────────────────────────────────────────────────
{
  const i0 = CHAT.indexOf('  const _cvRowsOf=(c)=>{'), i1 = CHAT.indexOf('  const _cvCapTable=(c,name)=>{');
  const ctx = {}; vm.createContext(ctx); vm.runInContext(CHAT.slice(i0, i1).replace(/const /g, 'var ') + '\nthis.S=_cvCapSources;', ctx);
  const long = 'Pinecone is a managed vector database. '.repeat(12);
  const res = { query: 'vector databases', sources: [
      { url: 'https://a.org/x', title: 'A', snippet: 'a', text: long, chars: long.length }, { url: 'https://b.org/y', title: 'B', snippet: 'b', text: long, chars: long.length },
      { url: 'https://c.org/z', title: 'C', snippet: 'c', text: '', chars: 0, blocked: true, error: 'blocked' } ],
    more_links: [{ url: 'https://d.org/w', title: 'D', snippet: 'd' }] };
  const m = ctx.S(res);
  t('web.research is ONE records item, not cards drawn together', !Array.isArray(m) && m.kind === 'records' && m.content.kind === 'research', JSON.stringify(m && m.kind));
  t('its pages carry the text it read', m.content.items[0].text && m.content.items[0].text.length > 200);
  t('a page it could not read says so', m.content.items[2].meta.status === 'could not be read');
  t('the links it found but did not read follow, marked', m.content.items.length === 4 && m.content.items[3].meta.status === 'found, not read');
  const esc2 = ctx.S({ sources: [{ url: 'https://a.org/x', title: 'Features, Performance &amp; Use Cases', text: long }, { url: 'https://b.org/y', title: 'B &quot;q&quot;', text: long }] });
  const nav = 'Products\nResources\nPricing Docs Blog\nSign up\n' + 'The actual article begins here and runs on long enough to be the first real paragraph of the page. ' + long;
  const nv = ctx.S({ sources: [{ url: 'https://a.org/x', title: 'A', text: nav }, { url: 'https://b.org/y', title: 'B', text: long }] });
  t('a page\'s menu before its first paragraph is not its text', /^The actual article begins here/.test(nv.content.items[0].text), nv.content.items[0].text.slice(0, 40));
  t('a search engine\'s escaped title reads as text', esc2.content.items[0].title === 'Features, Performance & Use Cases' && esc2.content.items[1].title === 'B "q"', esc2.content.items[0].title);
  t('titled with the question', m.content.title === 'Research · vector databases' && /2 pages read · 2 more found/.test(m.content.why));
  const plain = ctx.S({ results: [{ url: 'https://a.org', title: 'A' }, { url: 'https://b.org', title: 'B' }] });
  t('a short search with no text is still its cards', Array.isArray(plain) && plain.length === 2);
}
// ── a record opened has its depth ──────────────────────────────────────────────────────────────────────────────────
{
  const el = {}; const K = 'k1';
  const C = { title: 'Research · x', kind: 'research', items: [{ id: 'u1', url: 'https://a.org/x', domain: 'a.org', title: 'A', snippet: 's', text: 'Line one of the page.\nLine two of the page.', meta: { read: '4.0k chars' } }] };
  cv.BLOCK.records(C, 'l', K, el); el._recUi[K].open.u1 = true;
  const h = cv.BLOCK.records(C, 'l', K, el);
  t('a read page opens to its text, formatted as paragraphs', /<p>Line one of the page\.<\/p><p>Line two of the page\.<\/p>/.test(h));
  t('its facts: site, and what the answer said about it', /<i>site<\/i>a\.org/.test(h) && /<i>read<\/i>4\.0k chars/.test(h));
  t('and reader mode for the whole article', /data-rec-act="read"/.test(h));
  const F = { title: 'From the fabric', kind: 'memory', view: 'list', items: [{ id: 'r1', title: 'R1', snippet: 's', tags: ['board', 'index'], ref: { record_id: 'r1' }, meta: { dataset_id: 'board_index' } }] };
  const e2 = {}; cv.BLOCK.records(F, 'l', 'k2', e2); const s2 = e2._recUi.k2; s2.open.r1 = true;
  s2.body.r1 = { text: 'The record.', rec: { source: { url: 'https://src.org', label: 'Board' }, tags: ['board'], created_at: '2026-09-27 13:32:24', data: { status: 'done', lane: 'shipped' }, total_chars: 1200 },
                 nb: [{ id: 'n1', dataset_id: 'topic.x', snippet: 'A neighbour record' }], nbOpen: { n1: true }, nbText: { n1: 'Its text.' } };
  const h2 = cv.BLOCK.records(F, 'l', 'k2', e2);
  t('a fabric record: its tags as chips', /<span>#board<\/span><span>#index<\/span>/.test(h2));
  t('its source, when it was written, its length', /<i>source<\/i><a href="https:\/\/src\.org"/.test(h2) && /<i>written<\/i>2026-09-27 13:32:24/.test(h2) && /1\.2k chars/.test(h2));
  t('its fields', /fields · 2/.test(h2) && /<span class="k">lane<\/span><span class="v">shipped<\/span>/.test(h2));
  t('its neighbours, each readable in place', /nearest records · 1/.test(h2) && /A neighbour record/.test(h2) && /<p>Its text\.<\/p>/.test(h2));
  s2.body.r1.nb = [{ id: 'n1', snippet: 'Same chunk text' }, { id: 'n2', snippet: 'Same chunk text' }, { id: 'n3', snippet: 'Same chunk text' }, { id: 'n4', snippet: 'Other' }];
  const h3 = cv.BLOCK.records(F, 'l', 'k2', e2);
  t('the same chunk three times over is one neighbour, counted', /nearest records · 2/.test(h3) && /×3/.test(h3));
  t('no score is not a relevance of 0%', !/relevance/.test(h2));
  t('and the buttons for both', /data-rec-act="rec"/.test(h2) && /data-rec-act="nb"/.test(h2));
  const g = cv.recGraph({ query: 'q', items: [{ id: 'r1', title: 'R1', meta: { dataset_id: 'board_index' } }] }, { r1: { nb: [{ id: 'n1', dataset_id: 'topic.x', snippet: 'near' }] } });
  t('the graph draws its neighbours, dashed', g.nodes.some((n) => n.id === 'r:n1') && g.edges.some((e) => e.from === 'r:r1' && e.to === 'r:n1' && e.rel === 'similar' && e.dashed) && /1 neighbour/.test(g.caption));
  t('a fabric record reads through the fabric, not memory.read alone', /callResult\('fabric\.record\.get', \{ record_id: String\(it\.ref\.record_id\), offset, max_chars: 8000 \}\)/.test(CVS) && /callResult\('fabric\.loom\.record_match'/.test(CVS));
  t('opened, a fabric record is read at once', /return this\._recAct\(key, 'rec', arg\);/.test(CVS));
  t('a click in an opened record reads, it does not close it', /ra\.matches\('\.vc-rbx-row,tr'\) && t\.closest\('\.vc-rbx-open'\)/.test(CVS));
  t('fabric.record.get exists and is read-only', /"fabric\.record\.get",/.test(DF) && /SELECT \* FROM fabric_records WHERE id=\? LIMIT 1/.test(DF) && /SELECT id, source_type, url, label FROM fabric_sources/.test(DF));
}
// ── no flicker at an edge ──────────────────────────────────────────────────────────────────────────────────────────
{
  t('the live layer belongs to its item', /const lv = t\.closest\('\.lv\[data-key\]'\); return lv \? lv\.dataset\.key : null;/.test(CVS));
  t('a change of item is settled for a beat', /this\._hovT = setTimeout\(\(\) => this\._hovSet\(this\._hovWant\), k \? 60 : 160\);/.test(CVS));
  t('hover is a class the render keeps, not only :hover', /\(this\._hovKey === key \? ' hov' : ''\)/.test(CVS) && /\.it:is\(:hover,\.hov\)/.test(CVS) && !/\.it:hover/.test(CVS));
}
// ── the resize corner near the bottom ──────────────────────────────────────────────────────────────────────────────
{
  const i0 = DASH.indexOf('  function _edgeSpeed('), i1 = DASH.indexOf('  function _scrollParent(');
  const now = { t: 1000 };
  const ctx = { window: { innerHeight: 900 }, Date: { now: () => now.t }, Math }; vm.createContext(ctx); vm.runInContext(DASH.slice(i0, i1) + '\nthis.S=_edgeSpeed;', ctx);
  const sc = { getBoundingClientRect: () => ({ top: 0, bottom: 900 }) };
  const st = { sy: 880, armed: false, edgeSince: 0 };
  t('grabbed in the bottom band, nothing scrolls', ctx.S(st, 880, sc) === 0 && ctx.S(st, 890, sc) === 0 && !st.armed);
  t('it arms once the pointer goes a clear 24 px further toward the edge', ctx.S(st, 905, sc) > 0 && st.armed);
  const s2 = { sy: 500, armed: false, edgeSince: 0 }; ctx.S(s2, 500, sc); now.t = 2000;
  const first = ctx.S(s2, 899, sc); now.t = 2700; const later = ctx.S(s2, 899, sc);
  t('it starts slow and reaches full speed after a moment at the edge', first > 0 && first < 4 && later > 11, first + ' → ' + later);
  t('the resize and the move both use it', /var d = _edgeSpeed\(edgeSt, lastY, scrollEl\);/.test(DASH) && /var d = _edgeSpeed\(m\.edge, m\.y, m\.scrollEl\);/.test(DASH));
}
// ── the details drawer keeps its ground with blocks off ──────────────────────────────────────────────────────────
t('the widget details drawer is not stripped with the panes', /\.vw-tip, \.vw-drawer, \.cmenu/.test(CSS));

if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all ok');

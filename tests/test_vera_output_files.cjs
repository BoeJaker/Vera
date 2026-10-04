// 2026-10-04 (owner): "add the various file output types to the menu under the responses in the chat ui - and if it
// doesnt have them add options for documents, presentations and spreadsheets and rendering options for these documents.
// in the canvas and chat and they should stream in like existing renders like code and mermaid"
//   node tests/test_vera_output_files.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const SRC = R('vera/vera_output_element.js'), CHAT = R('vera/chat/chat_panel.html'), AG = R('vera/agents/agents.py'), RC = R('vera/render/render_capabilities.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const noop = () => {}; const head = { querySelector: () => null, insertBefore: noop, appendChild: noop, firstChild: null };
global.window = {}; global.customElements = { get: () => null };
global.document = { readyState: 'complete', head, addEventListener: noop, querySelector: () => null, createElement: () => ({ setAttribute: noop }) };
new Function(SRC)();
const V = window.VeraOut;

// ── which fences are files ──
t('slides, sheets and documents are file fences; code is not', V.fileKind('slides') === 'slides' && V.fileKind('deck') === 'slides' && V.fileKind('csv') === 'sheet'
  && V.fileKind('TSV') === 'sheet' && V.fileKind('sheet') === 'sheet' && V.fileKind('document') === 'document' && V.fileKind('python') === '');

// ── slides ──
const deckSrc = '# Plan\n---\n# Why\n- cost\n- speed\nNote: say it slowly\n---\n# How\n```python\n# not a slide\n```';
const sl = V.slidesOf(deckSrc);
t('slides cut at --- rules, a code comment is not a slide, notes kept apart', sl.length === 3 && sl[1].title === 'Why' && sl[1].notes === 'say it slowly'
  && !sl[1].body.includes('say it') && sl[2].body.includes('# not a slide'));
t('without rules, slides cut at its headings', V.slidesOf('# A\nx\n## B\ny\n## C').map((s) => s.title).join() === 'A,B,C');
const deck = V.fence('slides', '', deckSrc, {}, {});
t('a deck card: the first slide drawn, paged 1 / 3, thumbnails, Present and Download', deck.includes('vo-file vo-slides') && deck.includes('<h1>Plan</h1>')
  && deck.includes('1 / 3') && (deck.match(/data-vo="fgo"/g) || []).length === 3 && deck.includes('data-vo="present"') && deck.includes('data-vp="dl:pptx"') && deck.includes('data-vp="dl:slides_pdf"'));
const liveDeck = V.fence('slides', '', '# One\n---\n# Two\n- half', { incomplete: true }, {});
t('streaming: the slide being written is the one shown, marked, no actions yet', liveDeck.includes('data-incomplete="1"') && liveDeck.includes('<h1>Two</h1>')
  && liveDeck.includes('writing…') && liveDeck.includes('2 / 2') && !liveDeck.includes('data-vo="present"'));

// ── sheets ──
const csv = 'Region,Q1,Q2\nNorth,"1,200",1350\nSouth,900,870\nTotal,=SUM(B2:B3),=SUM(C2:C3)';
const rows = V.rowsOf(csv, 'csv');
t('CSV with a quoted comma reads as cells', rows.length === 4 && rows[1][1] === '1,200' && rows[3][1] === '=SUM(B2:B3)');
const vals = V.sheetValues(rows);
t('formulas worked out, numbers read', vals[3][1] === 2100 && vals[3][2] === 2220 && vals[1][1] === 1200 && vals[0][0] === 'Region');
t('AVERAGE, MAX, arithmetic between cells; anything else left as written', (() => { const v = V.sheetValues([['a', 'b'], ['2', '4'], ['=AVERAGE(A2:B2)', '=A2*B2+1'], ['=MAX(A2:B3)', '=alert(1)']]);
  return v[2][0] === 3 && v[2][1] === 9 && v[3][0] === 9 && v[3][1] === '=alert(1)'; })());
const sheet = V.fence('csv', '', csv, {}, {});
t('a sheet card: columns lettered, rows numbered, the total shown with its formula on hover, sortable, Download', sheet.includes('vo-file vo-sheet') && sheet.includes('>A<') && sheet.includes('>C<')
  && sheet.includes('title="=SUM(B2:B3)"') && sheet.includes('>2,100<') && sheet.includes('data-vo="fsort"') && sheet.includes('data-vp="dl:xlsx"') && sheet.includes('data-vp="dl:ods"')
  && sheet.includes('sheet · 3 rows × 3 cols'));
t('a pipe or tab table is a sheet too', V.rowsOf('a\tb\n1\t2').length === 2 && V.rowsOf('| a | b |\n|---|---|\n| 1 | 2 |').length === 2);

// ── documents ──
const doc = V.fence('document', '', '# Memo\n\nHello **there**.\n\n## One\n\n## Two\n\n## Three', {}, {});
t('a document card: a paper page with its contents, Pop out and every document type', doc.includes('vo-paper') && doc.includes('vo-toc') && doc.includes('data-vo="fpop"')
  && ['docx', 'pdf', 'odt', 'rtf', 'epub', 'html', 'md', 'txt'].every((f) => doc.includes('data-vp="dl:' + f + '"')));

// ── the reply's menu, the code card's, the chat, the model, the server ──
const row = V.proseMenu('m1', { md: 'x' });
t('the reply\'s Send to: Word, PDF, PowerPoint, Excel and every other file type', ['dl:docx', 'dl:pdf', 'dl:pptx', 'dl:xlsx', 'dlpick'].every((a) => row.includes('data-vp="' + a + '"')));
t('a code card\'s Send to hands on code, not documents', !V.card('python', 'x=1').includes('data-vp="dl:docx"'));
t('the chat draws these fences with the kit as they stream', CHAT.includes("if(typeof VeraOut!=='undefined' && VeraOut.fileKind && VeraOut.fileKind(L)) return VeraOut.fence(lang, info, raw, st, {title:fname});"));
t('the chat tells the model it draws them; the model is told the three fences', CHAT.includes('chat_files:true,') && AG.includes('if body.get("chat_files") and "## Files you can write" not in _sys_prefix:')
  && AG.includes('- ```slides - a presentation') && AG.includes('- ```sheet (or ```csv)') && AG.includes('- ```document - a document'));
t('render.export makes every family, off the event loop', RC.includes('res = (_export(content, fmt, title=title, filename=filename) if fmt == "latex"')
  && RC.includes('else await _export_file(content, fmt, title=title, filename=filename))') && RC.includes('r = await run_argv(argv, timeout=150') && RC.includes('"families":    list(_xc.FAMILIES),'));
t('slides are offered only where LibreOffice can make them (pandoc is not the slides path)', RC.includes('elif f["via"] == "lo" and f["family"] == "presentation":\n            live = lo'));
// a real turn (2026-10-04): the chat streamed a long ```slides fence onto the canvas as a code item, drawn as source;
// and history's 300-character preview cut the fence open, so a finished deck said "streaming"
const CV = R('vera/canvas/canvas_element.js');
t('a canvas code item whose language is a file type is drawn as its file card', CV.includes("const fk = VO && vid && typeof VO.fileKind === 'function' ? VO.fileKind(c.lang) : '';")
  && CV.includes("VO.fileCard(fk, String(c.lang || ''), String(c.code || ''), { id: vid, canvas: false, cls: 'vc-kit', incomplete: streaming"));
t('a history preview stops before a fence it would cut open', CHAT.includes("if(fences.length%2===1){ const f=fences[fences.length-1]; cut=cut.slice(0,f.index);"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

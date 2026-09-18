// The context graphs follow the turn in view (Notes/42 defects 97 + 98; vera/chat/chat_panel.html).
// A restored turn's context is not lost - it is IN the question: every message is stored with the
// "[Retrieved Context]" block that was injected with it, which is why loadSession has to strip it before
// displaying the text. The composer writes one line per record, "[source] text", web and news carrying their
// label and url inside the bracket. So a turn's context reads back out of the transcript with NO request, and
// without going anywhere near ctxFetch - CTX_NODES/CTX_EDGES are what the NEXT question carries and must never
// be written by the rebuild.
// This runs the parser the page actually ships, cut out by its markers.
//   node tests/test_chat_turn_sync.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// the parser and the format are checked TOGETHER: if the composer's line changes, this fails rather than
// silently parsing a format nothing writes any more
t('the composer still writes the block this parser reads', src.indexOf("return'\\n\\n[Retrieved Context]\\n'+items.join('\\n');") >= 0);

const P0 = src.indexOf('  const _RCMARK='), P1 = src.indexOf('  function _ctxBroadcastFrames(){');
t('the parser is present in the page', P0 >= 0 && P1 > P0);
if (P0 < 0 || P1 < P0) process.exit(1);
const parse = new Function(src.slice(P0, P1) + '\nreturn _ctxNodesFromPrompt;')();

// exactly how the composer builds it (the builder maps each node to "[src] text" and joins with \n)
const build = (items) => '\n\n[Retrieved Context]\n' + items.join('\n');

const q = 'what is the estate topology?';
const n1 = parse(q + build(['[ontology] Estate is modelled as nodes and links', '[memory] you asked about topology last week', '[caps] nodes.list returns every host']), 'm7');
t('every record comes back, in order', n1.length === 3 && n1[0].source === 'ontology' && n1[1].source === 'memory' && n1[2].source === 'caps', JSON.stringify(n1.map(n => n.source)));
t('the text is kept and a label is derived from it', n1[0].text === 'Estate is modelled as nodes and links' && n1[0].label.startsWith('Estate is modelled'));
t('ids are unique and carry the turn', n1[0].id === 'rf:m7:0' && n1[2].id === 'rf:m7:2' && new Set(n1.map(n => n.id)).size === 3);
t('a rebuilt record is marked as restored and included', n1.every((n) => n.restored === true && n.included === true));

// web and news keep their label and url INSIDE the bracket - the source must not swallow them
const n2 = parse(q + build(['[web "Proxmox docs" https://pve.proxmox.com/wiki] clustering requires quorum', '[news "Outage" https://x.test/a] a datacentre lost power']), 'm9');
t('web keeps its family, not the whole bracket', n2[0].source === 'web', JSON.stringify(n2[0]));
t('web takes its label and url from the bracket', n2[0].label === 'Proxmox docs' && n2[0].url === 'https://pve.proxmox.com/wiki');
t('news likewise', n2[1].source === 'news' && n2[1].label === 'Outage' && n2[1].url === 'https://x.test/a');
t('and the text after the bracket is still the text', n2[0].text === 'clustering requires quorum');

// a question with no block, and a block followed by another injected block
t('a question carrying no context yields nothing', parse('just a question', 'm1').length === 0);
t('an empty string is safe', parse('', 'm1').length === 0 && parse(null, 'm1').length === 0);
const withNext = q + build(['[ontology] first']) + '\n\n[INTEGRATED CAPABILITY MODE]\nsomething else entirely';
const n3 = parse(withNext, 'm2');
t('a following injected block is not swallowed', n3.length === 1 && n3[0].text === 'first', JSON.stringify(n3));
// the source is what decides the family and therefore the colour, so a malformed line is dropped, not guessed
t('a line with no bracket is dropped', parse(q + build(['[ontology] kept', 'no bracket here']), 'm3').length === 1);
t('a record whose text is empty still counts, labelled by its source', (() => { const r = parse(q + build(['[skills] ']), 'm4'); return r.length === 1 && r[0].source === 'skills' && r[0].label === 'skills'; })());

// the rebuild must never write what the NEXT prompt carries - that was the whole reason not to use ctxFetch
const fn = src.slice(P0, P1);
t('the parser never assigns CTX_NODES/CTX_EDGES and never calls ctxFetch', !/CTX_NODES\s*=|CTX_EDGES\s*=|ctxFetch\s*\(/.test(fn));

// the wiring: the restore builds one frame per turn, and the rows/bar/meter follow the same frame as the graph
t('the restore collects a frame per turn', src.indexOf('const _rfClose=(amid)=>{') >= 0 && src.indexOf('_rfClose(m.wrap.dataset.mid||\'\');') >= 0);
t('frame ids are numeric — updateFrameUI interpolates them unquoted into onclick', src.indexOf('id:-(CTX_FRAMES.length+1)') >= 0);
t('a rebuilt frame binds to the turn by mid, which is what _ctxFrameInView matches on', src.indexOf('mid:_mid, amid:\'\'') >= 0 && src.indexOf('CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)') >= 0);
t('the rows, the budget bar and the header meter follow the active frame, as the graph does', src.indexOf('const fr=(_ctxActiveFrame!=null)?CTX_FRAMES.find(f=>f&&String(f.id)===String(_ctxActiveFrame)):null;') >= 0);
t('the assembled extras are added only to the LIVE set, never to a frame', src.indexOf('if(!fr){ try{ const ex=_ctxAssembledExtra(_CTX_ALL_LAYERS, CTX_NODES);') >= 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

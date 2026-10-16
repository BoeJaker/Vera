// DU-E-markdown: a long document reads as one - its headings as an outline that jumps, find in the text (marked in place,
// stepped through, Enter for the next), a reading width it can leave; a short one is drawn as before.
//   node tests/test_desk_markdown_read.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'md:probe';
const LONG = '# Report\n\nIntro ' + 'word '.repeat(80) + '\n\n## Findings\n\n' + 'The needle is here. '.repeat(20) + '\n\n### Detail\n\nMore text.\n\n## Sources\n\n- a\n- b\n';
const host = (ui) => { const h = { dataset: {}, _live: {} }; if (ui) h._mdUi = { [KEY]: Object.assign({ q: '', i: 0, rail: true, wide: false }, ui) }; return h; };
const draw = (md, el, title) => mod.BLOCK.markdown({ md, title }, 'l', KEY, el);

{ const h = draw('# Hi\n\nA short note.', host());
  t('a short document is drawn as before', h.startsWith('<div class="vc-md">') && !h.includes('vc-mdwrap')); }
{ const h = draw(LONG, host(), 'Something else');
  t('a long one: the bar - find, step, outline, width', h.includes('data-md-q="md:probe"') && h.includes('data-act="mdnext"') && h.includes('data-act="mdprev"') && h.includes('data-act="mdrail"') && h.includes('data-act="mdwidth"'));
  t('its headings, numbered for the jump', (h.match(/data-mdh="\d"/g) || []).length === 4);
  t('the outline lists them, nested by level', /<nav class="vc-mdrail"><button class="l1" data-act="mdgo" data-h="0"[^>]*>Report<\/button><button class="l2" data-act="mdgo" data-h="1"[^>]*>Findings<\/button><button class="l3"[^>]*>Detail<\/button><button class="l2"[^>]*>Sources<\/button><\/nav>/.test(h), (h.match(/<nav[\s\S]*?<\/nav>/) || [''])[0].slice(0, 300));
  t('a reading width by default', h.includes('<div class="vc-mdwrap" data-md-key="md:probe">') && C.includes('.vc-mdwrap:not(.wide) .vc-mdmain > .vc-md{max-width:72ch;margin:0 auto}'));
  t('the text is still the document', h.includes('The needle is here.') && h.includes('<div class="vc-md">')); }
{ t('full width when asked', draw(LONG, host({ wide: true })).includes('class="vc-mdwrap wide"'));
  t('the outline can be put away', !draw(LONG, host({ rail: false })).includes('vc-mdrail')); }
{ const h = draw(LONG, host(), 'Report');
  t('a first heading that repeats the card\'s title is still not drawn twice', !/data-mdh="\d"[^>]*>Report</.test(h)); }
t('find marks the drawn text in place (no redraw), Enter steps, a redraw marks again', C.includes('function mdMark(wrap, q) {') && C.includes("ev.key !== 'Enter'") && C.includes("this.addEventListener('vera:canvas:placed', () => { try { this._mdAgain(); }"));
t('jumps scroll the card, never the desk', C.includes("if (sc.classList && sc.classList.contains('it')) { sc = null; break; }"));
t('a long document scrolls inside its card (the body clips)', C.includes('.it-bd > .vc-mdwrap{flex:1 1 auto;min-height:0}.vc-mdwrap > .vc-mdmain{flex:1 1 auto;min-height:0;overflow:auto'));
t('the rail goes when the card is narrow', C.includes('@container (max-width: 520px){ .vc-mdrail{display:none} }'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

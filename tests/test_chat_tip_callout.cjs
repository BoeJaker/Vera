// The tip callout, and the rule that kept producing the thing it banned.
// (vera/vera_markdown_element.js, vera/chat/chat_panel.html)
//
// The report was "it gives a quick tip as the whole response and isn't formatting it - it's supposed to have a
// purple box". Half of that was already true: [!TIP] has been a recognised callout all along, rendered by the
// shared markdown element. What was missing was a tip looking like anything in particular - it shared `note`'s
// tone, so the one the aide reaches for most was the one you could not pick out of a reply at a glance - and,
// much more importantly, the model was not writing callouts at all. It was writing a bare sentence.
//
// WHY IT KEPT WRITING IT. The prompt banned the opening by QUOTING it. The exact phrase the model was not to
// write sat in the STUDIO block as a literal string, twice: once in a code comment above the rule and once in
// the rule itself. An instruction carrying its own counter-example is a good way to plant the phrase, and it
// landed hardest on a turn with no content of its own - "thanks" - where the model reaches for the most recent
// thing it was told. This is Notes/42 defect 95, which had already been "fixed" once by adding the rule that
// carries the specimen.
//
// So: the phrase appears nowhere in the prompt now, the rule describes the behaviour instead of quoting it, and
// the turn that triggered it has something to DO rather than only something not to.
//
//   node tests/test_chat_tip_callout.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const md = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera_markdown_element.js'), 'utf8');
const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the prompt no longer carries the phrase it bans ────────────────────────────────────────────────────────────
{
  t('the phrase appears nowhere in the page', chat.indexOf('Quick tip') < 0,
    'still present ' + (chat.match(/Quick tip/g) || []).length + ' time(s)');
  t('the rule describes the behaviour instead of quoting it',
    /A reply that opens by advertising your own tooling is always wrong, however it is phrased/.test(chat));
  /* the turn it actually happened on had nothing else to say, and a rule that only forbids leaves the model
     reaching for whatever it was told last - which was the tooling list */
  t('a contentless turn is given something to do, not only something not to do',
    /When the user says only thanks, or greets you, or closes the conversation, ANSWER THAT/.test(chat));
  t('and the callout syntax is told to the model, since nothing ever had',
    /is written as a callout and renders as one: a line of "> \[!TIP\]"/.test(chat));
  t('offering instead of doing is named too', /do not offer to put something somewhere/.test(chat));
}

// ── a tip has its own tone now, in both the renderer and the chat ──────────────────────────────────────────────
{
  t('tip is its own kind, not note wearing its name', /tip: \['tip',/.test(md));
  t('the shared renderer gives it a tone', /\.vmd-call\.tip \{ background:color-mix\(in srgb, var\(--acc4/.test(md));
  t('and the chat gives it the same one', /\.msg-body \.vmd-call\.tip\{background:color-mix\(in srgb,var\(--acc4/.test(chat));
  t('note keeps its own', /\.vmd-call\.note \{ background:color-mix\(in srgb, var\(--acc,/.test(md));
}

// ── and it actually renders: the real element, on real markdown ────────────────────────────────────────────────
{
  const win = {
    document: { createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, querySelector: () => null,
      querySelectorAll: () => [], addEventListener() {}, classList: { add() {}, remove() {} }, dataset: {} }),
      head: { appendChild() {} }, body: {}, getElementById: () => null, querySelectorAll: () => [],
      querySelector: () => null, addEventListener() {}, adoptedStyleSheets: [] },
    addEventListener() {}, customElements: { define() {}, get() {} }, HTMLElement: function () {},
    CSSStyleSheet: function () { this.replaceSync = () => {}; }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  };
  win.window = win;
  vm.runInContext(md, vm.createContext(win));
  const M = win.VeraMD;   // the shared renderer's global
  t('the markdown element loads', !!(M && typeof M.render === 'function'), Object.keys(win).slice(0, 8).join(','));
  if (M && typeof M.render === 'function') {
    const out = M.render('> [!TIP]\n> Press the dot to jump to that turn.');
    t('a [!TIP] block becomes a tip callout', /class="vmd-call tip"/.test(out), out.slice(0, 140));
    t('it is labelled Tip', /<b>Tip<\/b>/.test(out));
    t('and carries the text', /Press the dot/.test(out));
    const note = M.render('> [!NOTE]\n> Something to know.');
    t('a note is still a note', /class="vmd-call note"/.test(note) && /<b>Note<\/b>/.test(note));
    const warn = M.render('> [!WARNING]\n> Careful.');
    t('a warning is still a warning', /class="vmd-call warn"/.test(warn));
    const bq = M.render('> just a quote');
    t('an ordinary blockquote is untouched', /<blockquote>/.test(bq) && !/vmd-call/.test(bq));
    // the thing the user was shown instead: a bare sentence is prose, and SHOULD be - the fix is the prompt
    const bare = M.render('Quick tip: I have loaded your tools.');
    t('a bare sentence is prose, not a box (which is why the prompt is the fix)', !/vmd-call/.test(bare));
  }
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

// The deterministic hooks: what the canvas lifts out of a turn without being asked (vera/chat/chat_panel.html).
//
// "The goal of the canvas is to be a dynamic working area for the user, set up and maintained by both the llm and
// deterministic hooks that trigger on chat output of particular formats (catching fences or detecting dates for a
// timeline, or other resource that can be rendered or interacted with)."
//
// The rules already existed; they were seven hand-written querySelectorAll blocks inside one function, which is
// the shape you have to open and edit in the middle to add an eighth. They are a table now: a hook is a name,
// what it looks for, and what it makes. Appending one is the whole of adding a rule.
//
// The bodies were MOVED, not rewritten - every one is the text that was already in the harvest - so this pins
// both halves: that the table is a table, and that the rules it holds still lift the same things.
//
//   node tests/test_canvas_hooks.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the table ──────────────────────────────────────────────────────────────────────────────────────────────────
{
  t('the hooks are a table, not a function to edit in the middle', /const CV_HOOKS=\[\];/.test(src)
    && /const cvHook=\(name, sel, take\)=>\{ CV_HOOKS\.push\(\{ name, sel, take \}\); \};/.test(src));
  const names = (src.match(/cvHook\('([a-z-]+)'/g) || []).map((x) => x.replace(/cvHook\('|'/g, ''));
  t('every rule the harvest had is in it', names.join(',') === 'capability,code,diagram,table,widget,widget-element,image,timeline',
    names.join(','));
  t('and the harvest is now a loop over them',
    /CV_HOOKS\.forEach\(h=>\{/.test(src) && /body\.querySelectorAll\(h\.sel\)\.forEach\(\(el, i\)=>\{ try\{ h\.take\(el, out, i, body\); \}/.test(src));
  /* a hook that throws must not take the rest of the turn down with it - a bad rule should cost its own item,
     not every item */
  t('one hook throwing does not lose the others', (src.match(/\}catch\(_\)\{\}/g) || []).length >= 2
    && /else h\.take\(body, out, 0, body\);/.test(src));
  t('the cap on what one turn lands is kept', /return out\.slice\(0,12\);/.test(src));
}

// ── the rules still lift what they lifted ──────────────────────────────────────────────────────────────────────
{
  for (const k of ["k:'cap'", "k:'code'", "k:'diagram'", "k:'table'", "k:'widget'", "k:'image'"]) {
    t('still lifts ' + k, src.indexOf(k) >= 0);
  }
  t('a mermaid fence is still a diagram', /if\(lang==='mermaid'\)/.test(src));
  t('a widget fence is still left to the widget hook', /if\(lang==='widget'\) return;/.test(src));
  t('a directive chip is still never an item', /a directive's chip is never an item/.test(src));
  t('an icon is still not an image', /\\b\(ico\|icon\|avatar\)\\b/.test(src));
}

// ── and the new one: dates in a reply are a history ────────────────────────────────────────────────────────────
{
  // cut the timeline hook out and run it on a reply body we make by hand
  const A = "  cvHook('timeline', null, (body, out) => {", B = "  function _cvHarvest(body){";
  const i0 = src.indexOf(A), i1 = src.indexOf(B);
  t('the timeline hook is present', i0 >= 0 && i1 > i0);
  if (i0 >= 0 && i1 > i0) {
    // it leans on the research extractor, so give it the real one
    const E = "  const _TL_MON=", F = "  const _cvCapTerm=(c)=>{";
    const ctx = {}; vm.createContext(ctx);
    vm.runInContext(src.slice(src.indexOf(E), src.indexOf(F)) + src.slice(i0, i1)
      + '\nthis.H=CV_HOOKS_TEST;', Object.assign(ctx, { CV_HOOKS_TEST: null, cvHook: (n, s2, take) => { ctx.CV_HOOKS_TEST = take; } }));
    const take = ctx.CV_HOOKS_TEST;
    t('the hook registered', typeof take === 'function');
    if (typeof take === 'function') {
      const run = (text) => { const out = []; take({ textContent: text }, out); return out; };

      const history = 'Apple was founded on April 1, 1976. In 1984 it launched the Macintosh. '
        + 'The iPhone was announced on January 9, 2007 and Tim Cook became CEO in August 2011. '
        + 'By 2018 it was worth a trillion dollars.';
      const made = run(history);
      t('a reply full of dates becomes a timeline', made.length === 1 && made[0].kind === 'timeline', JSON.stringify(made.map((m) => m.kind)));
      t('with the events in order', made[0].content.events.map((e) => e.when).join(',') === '1976-04-01,1984,2007-01-09,2011-08,2018',
        made[0] && made[0].content.events.map((e) => e.when).join(','));
      /* this is the point of it: the same question answered from what the model knows has the same shape as one
         answered from the web, and only the web one was getting an axis */
      t('without any research run involved', !/research/.test(String(take)));

      // and the restraint, which matters more here than in a research report - ordinary replies are full of numbers
      t('two dates are not a history', run('We shipped in 2019 and again in 2020.').length === 0);
      t('a port number does not make a timeline',
        run('Bind 8080, then 9090, then 3000, and check the logs on the box please.').length === 0);
      t('short prose is left alone', run('It was 1984.').length === 0);
      t('and a reply with no dates makes nothing',
        run('x'.repeat(200) + ' this reply has no dates in it at all, only words and more words.').length === 0);
    }
  }
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

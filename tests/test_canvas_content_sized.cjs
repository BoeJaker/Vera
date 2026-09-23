// A SIZE IS A BOUND, AND THE CONTENT DECIDES INSIDE IT (the canvas's final form §3.2; vera/canvas/canvas_element.js).
//
// "the canvas ui layout is very blocky and the blocks need greater freedom to adapt to the size of their content
// (within bounds) and should also be able to expand in the canvas" (owner). An item has always taken only the height
// its content needed; what was fixed was the CEILING - 72 / 180 / 340 flat pixels whatever the column - so in a tall
// column every item was a letterbox with its own scrollbar while the column below it sat empty.
//   node tests/test_canvas_content_sized.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── the ceiling is a share of the column, floored and capped ────────────────────────────────────────────────── */
['s', 'm', 'l'].forEach((sz) => t('the ' + sz + ' face is bounded by a share of the column, not by a fixed number',
  new RegExp('\\.it\\[data-size="' + sz + '"\\] \\.it-bd\\{max-height:clamp\\([0-9]+px,calc\\(\\.[0-9]+ \\* var\\(--vc-vh,[0-9]+px\\)\\),[0-9]+px\\)\\}').test(SRC)));
t('xl is unbounded — the whole of it, and the column caps the card if it must (§2)', /\.it\[data-size="xl"\] \.it-bd\{max-height:none\}/.test(SRC));
t('the shares rise with the size: a glance, the working face, the whole thing',
  (() => { const f = ['s', 'm', 'l'].map((sz) => +(SRC.match(new RegExp('\\.it\\[data-size="' + sz + '"\\] \\.it-bd\\{max-height:clamp\\([0-9]+px,calc\\((\\.[0-9]+)'))[1]));
    return f[0] < f[1] && f[1] < f[2] && f[2] < 1; })());
t('the column publishes the viewport the shares are of, and only when it changes',
  /if \(VH && this\._vh !== VH\) \{ this\._vh = VH; this\.style\.setProperty\('--vc-vh', VH \+ 'px'\); \}/.test(SRC));
t('an element that never measures still has sensible numbers (the fallback)', /var\(--vc-vh,460px\)/.test(SRC));
t('a size change glides rather than jumping, and is instant under reduced motion or a scrolling transcript',
  /\.it-bd\{[^}]*transition:max-height \.24s/.test(SRC) &&
  /@media \(prefers-reduced-motion:reduce\)\{\.it-bd\{transition:none\}\}/.test(SRC) &&
  /\.stage\[data-scrolling\] \.it-bd\{transition:none\}/.test(SRC));

/* ── a dragged height records the size it LOOKS like, in the column it was dragged in ────────────────────────── */
t('with no column given, the thresholds are the ones this always used',
  V.sizeOfHeight(90) === 's' && V.sizeOfHeight(200) === 'm' && V.sizeOfHeight(300) === 'l' && V.sizeOfHeight(500) === 'xl');
{
  // a tall column: a third of it is the working face, so 300px is still "m" where it used to record "l"
  const tall = 1200;
  t('in a tall column, a third of the column reads as the working face',
    V.sizeOfHeight(300, tall) === 'm' && V.sizeOfHeight(150, tall) === 's' && V.sizeOfHeight(600, tall) === 'l' && V.sizeOfHeight(1000, tall) === 'xl',
    [150, 300, 600, 1000].map((h) => h + '->' + V.sizeOfHeight(h, tall)).join(' '));
  /* a short column: the same drag is a bigger share of it, so it records the same size or a larger one - never a
     smaller one. Asserted as the RELATION rather than as a picked value: 90px is already the working face of a
     380px column, which is the point. */
  const short = 380, order = ['s', 'm', 'l', 'xl'];
  t('in a shorter column the same drag means the same size or a bigger one, never a smaller one',
    [90, 200, 300, 600].every((h) => order.indexOf(V.sizeOfHeight(h, short)) >= order.indexOf(V.sizeOfHeight(h, tall))) &&
    order.indexOf(V.sizeOfHeight(300, short)) > order.indexOf(V.sizeOfHeight(300, tall)),
    [90, 200, 300, 600].map((h) => h + ': short ' + V.sizeOfHeight(h, short) + ' vs tall ' + V.sizeOfHeight(h, tall)).join(' · '));
  t('the order never inverts, whatever the column', [320, 700, 1400].every((vh) => {
    const order = ['s', 'm', 'l', 'xl']; let last = -1;
    return [40, 100, 200, 400, 900, 2000].every((h) => { const i = order.indexOf(V.sizeOfHeight(h, vh)); const ok = i >= last; last = i; return ok; });
  }));
  t('a drag in the column the user is looking at is measured against THAT column', /sizeOfHeight\(r\.h, this\._vh \|\| 0\)/.test(SRC));
}

/* ── and the size record itself is unchanged: the vocabulary is still s · m · l · xl ─────────────────────────── */
t('the sizes are the same four', JSON.stringify(V.ITEM_SIZES) === JSON.stringify(['s', 'm', 'l', 'xl']));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

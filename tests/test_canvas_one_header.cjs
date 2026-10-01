// SIX NOTES (owner, 2026-09-24): two headers per item; items not tall enough for code and exploded prose; items
// should span columns; the "NOW 1 suggested" line; the banner's ground with blocks off; and the "Vera can also"
// card, which should be a switch on the item instead.
//   node tests/test_canvas_one_header.cjs
const path = require('node:path'), fs = require('node:fs');
const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

/* ── one header ──────────────────────────────────────────────────────────────────────────────────────────────── */
t('the kinds whose drawer draws its own head are named', /const OWN_HEAD = new Set\(\['code', 'html', 'explode', 'session', 'diagram', 'notebook', 'panel'\]\);/.test(SRC));
t('the card does not draw a second header over them — only the corner controls (maximise, expand)',
  /\$\{ownHead && !compact \? `<span class="mx solo[^`]*?<span class="xp solo" data-act="open"/.test(SRC) && /\.xp\.solo\{position:absolute/.test(SRC));
t('a FOLDED item keeps the card header, because then it is the whole item', /ownHead && !compact/.test(SRC));
t('the expand mark is there when you are on the item, not always', /\.it:is\(:hover,\.hov\) > \.xp\.solo,\.it:focus-within > \.xp\.solo\{opacity:\.8\}/.test(SRC));

/* ── headers belong to Full ──────────────────────────────────────────────────────────────────────────────────── */
t('the element carries the tier, because a rule on <html> cannot cross a shadow root', /if \(this\.dataset\.tier !== tier\) this\.dataset\.tier = tier;/.test(SRC));
t('in Hover and Zen the header and the rail are held back until you are on the item',
  // held back as OVERLAYS that fade in, never display:none - an item grew by them when pointed at near an edge, and
  // the edge flickered (2026-10-01)
  /:host\(\[data-tier="hover"\]\) \.it:not\(\.compact\) > \.it-hd,:host\(\[data-tier="zen"\]\) \.it:not\(\.compact\) > \.it-hd\{position:absolute;[^}]*opacity:0;pointer-events:none/.test(SRC) &&
  /:host\(\[data-tier="hover"\]\) \.it:not\(\.compact\) > \.it-ft,:host\(\[data-tier="zen"\]\) \.it:not\(\.compact\) > \.it-ft\{position:absolute;[^}]*opacity:0/.test(SRC) &&
  /:is\(:hover,\.hov,:focus-within\) > :is\(\.it-hd,\.it-ft\)[^{]*\{opacity:1;pointer-events:auto\}/.test(SRC) &&
  !/:not\(:hover\):not\(:focus-within\) > \.it-(hd|ft)/.test(SRC));

/* ── room for what you read ──────────────────────────────────────────────────────────────────────────────────── */
t('code, a page, a passage and a structured graph take a bigger share of the column',
  /\.it\[data-type="code"\] \.it-bd,\.it\[data-type="html"\] \.it-bd,\.it\[data-type="explode"\] \.it-bd,\.it\[data-type="markdown"\] \.it-bd\{\s*max-height:clamp\(220px,calc\(\.62 \* var\(--vc-vh,460px\)\),900px\)\}/.test(SRC));
t('and xl gives them no ceiling at all', /\.it\[data-size="xl"\]\[data-type="code"\] \.it-bd/.test(SRC));
t('a structured graph\'s slot is a diagram\'s height, not a strip', /\.it\[data-type="explode"\] \.vc-live\{min-height:260px\}/.test(SRC));

/* ── spans ───────────────────────────────────────────────────────────────────────────────────────────────────── */
{
  const turns = { m1: { top: 0 }, m2: { top: 40 }, m3: { top: 80 } };
  const P = V.place([{ key: 'code', h: 300, mid: 'm1', want: 2 }, { key: 'a', h: 24, mid: 'm2', want: 1 / 3 },
                     { key: 'b', h: 24, mid: 'm2', want: 1 / 3 }], turns, { columns: 3, gap: 10, colWidth: 200, pad: 0, viewport: 900 });
  const at = Object.fromEntries(P.placements.map((p) => [p.key, p]));
  t('a wide item spans columns, and the chips take the column beside it',
    at.code.span === 2 && at.code.w === 410 && at.a.col === 2 && at.b.beside, JSON.stringify(P.placements.map((p) => [p.key, p.col, p.span || 1, p.w])));
  t('a span of everything is the whole stage',
    V.place([{ key: 'big', h: 400, mid: 'm1', want: 3 }], turns, { columns: 3, gap: 10, colWidth: 200, pad: 0 }).placements[0].w === 620);
  t('with one column a span is just that column',
    V.place([{ key: 'code', h: 300, mid: 'm1', want: 2 }], turns, { columns: 1, gap: 10, colWidth: 300, pad: 0 }).placements[0].w === 300);
  t('nothing sits beside a spanning item', V.place([{ key: 'code', h: 300, mid: 'm1', want: 2 }, { key: 'a', h: 24, mid: 'm1', want: 1 / 3 }],
    turns, { columns: 3, gap: 10, colWidth: 200, pad: 0 }).placements[1].beside === false);
  t('the kinds you READ ask for the span, and an opened item asks for the stage',
    /if \(d\.open\) return 4;/.test(SRC) && /if \(WIDE\[ty\] \|\| sz === 'l'\) return 2;/.test(SRC) &&
    /const u = unitsOf\(descOf\(c\)\); return u > 1 \? Math\.max\(1, Math\.min\(cols, Math\.round\(u\)\)\) : u;/.test(SRC));
}

/* ── the NOW line, and the banner's ground ───────────────────────────────────────────────────────────────────── */
t('the bar counts nothing — not the items, not the suggestions', V.nowText([1, 2, 3], null) === '' && V.nowText([], null) === '');
t('it speaks only for a decision', V.nowText([], { question: 'q', since: '' }) === 'waiting on you · 1 input');
t('the banner loses its ground when blocks are off, and keeps a blur so it stays readable',
  /:host\(\[blocks="off"\]\) \.addbar\{background:none;backdrop-filter:blur\(9px\)/.test(SRC));

/* ── the offer, on the item ──────────────────────────────────────────────────────────────────────────────────── */
t('the rule is its own function, and the band no longer carries the offer',
  typeof V.canExplode === 'function' && V.suggestionsOf({}, [{ key: 'c', type: 'code', content: { code: Array(20).fill('x').join('\n') } }], '', {}).length === 0);
t('an item that can be exploded carries the switch, and knows which way round it is',
  /const xplodable = !bid && !!canExplode\(b, this\.explodeOffer\(\)\);/.test(SRC) && /const xploded = xplodable && xplodedKeys\.has\(String\(b\.key\)\);/.test(SRC));
t('the switch says what it does, both ways', /\$\{xploded \? 'source' : 'graph'\}/.test(SRC));
t('taking it builds the bound item; taking it back removes it',
  /this\.call\('canvas\.add', \{ kind: 'explode', key: 'explode:' \+ key, content, at: 'now', size: 'l' \}\)/.test(SRC) &&
  /if \(bound\) return this\.call\('canvas\.remove'/.test(SRC));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

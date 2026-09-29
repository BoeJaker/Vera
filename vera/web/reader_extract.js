/* reader_extract.js - READER MODE, run inside the rendered page (browser.reader passes it to page.evaluate).

   browser.content hands back the whole <body> with its tags stripped: the menu, the cookie banner, the footer and the
   article run together as one grey paragraph (owner, 2026-09-28: "the web one needs to be able to reader mode - text
   properly formatted and prettyfied and parsed and rendered like markup from current bare scraped webpage and show
   the key body of text or a composite if its fragmented"). This finds the body a person would read and writes it as
   markdown, keeping what gives it shape - headings, paragraphs, lists, quotes, code, tables, links, figures.

   How it finds the body (the readability method, without the library - none is installed in the image):
     1. what is not content goes first: scripts, forms, navigation, headers, footers, asides, anything hidden on
        screen, and the blocks whose class or id says they are chrome (cookie, share, related, comments, ads...)
     2. every block of prose scores its parent (and half to its grandparent): one point, a point per comma, up to three
        for its length - and the score is cut by how much of the container is link text, which is what a menu is
     3. the best container is the body; its siblings that score close to it or read like prose join it
     4. A COMPOSITE when the page is fragmented: when the best container holds under ~45% of the page's prose and other
        containers outside it score within reach, the strongest few are taken too, in the order they sit on the page

   Input: max_chars. Output: {title, byline, site, published, image, description, markdown, words, minutes, composite,
   parts, url}. A function expression, so page.evaluate can take the file as it is. */
(maxChars) => {
  const LIMIT = Math.max(2000, +maxChars || 60000);
  const doc = document, body = doc.body;
  if (!body) return { markdown: '', words: 0, parts: 0 };
  const meta = (n) => { const m = doc.querySelector('meta[property="' + n + '"],meta[name="' + n + '"]'); return m ? String(m.getAttribute('content') || '').trim() : ''; };
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const h1 = doc.querySelector('h1');
  const title = clean(meta('og:title') || meta('twitter:title') || doc.title || (h1 && h1.textContent) || '');
  const bylineEl = doc.querySelector('[rel="author"],[itemprop="author"],.byline,.author,.post-author,.article-author');
  const byline = clean(meta('author') || meta('article:author') || (bylineEl && bylineEl.textContent) || '').slice(0, 120);
  const site = clean(meta('og:site_name') || location.hostname.replace(/^www\./, ''));
  const timeEl = doc.querySelector('time[datetime]');
  const published = meta('article:published_time') || meta('datePublished') || meta('date') || (timeEl ? timeEl.getAttribute('datetime') : '') || '';
  const image = meta('og:image') || '';
  const description = clean(meta('og:description') || meta('description'));

  // hidden on screen is not content: marked on the live page (the clone has no layout), dropped from the clone
  const all = body.querySelectorAll('*');
  for (let i = 0; i < all.length && i < 20000; i++) {
    const el = all[i]; const t = el.tagName;
    if (t === 'BR' || t === 'WBR' || t === 'IMG' || t === 'svg') continue;
    let hid = false; try { const cs = getComputedStyle(el); hid = cs.display === 'none' || cs.visibility === 'hidden'; } catch (e) {}
    if (hid) el.setAttribute('data-vr-hidden', '1');
  }
  const root = body.cloneNode(true);
  body.querySelectorAll('[data-vr-hidden]').forEach((el) => el.removeAttribute('data-vr-hidden'));
  root.querySelectorAll('[data-vr-hidden]').forEach((el) => el.remove());
  const JUNK_TAGS = 'script,style,noscript,template,iframe,svg,canvas,form,button,input,select,textarea,nav,footer,header,aside,dialog,[role="navigation"],[role="banner"],[role="contentinfo"],[role="complementary"],[role="dialog"],[aria-hidden="true"]';
  root.querySelectorAll(JUNK_TAGS).forEach((el) => el.remove());
  const CHROME = /(^|[\s_-])(nav|navbar|menu|sidebar|side-bar|footer|masthead|cookie|consent|gdpr|banner|subscribe|newsletter|signup|share|sharing|social|related|recommend|promo|advert|ads?|sponsor|comment|comments|disqus|breadcrumbs?|pagination|popup|modal|toolbar|skip-link|widget)([\s_-]|$)/i;
  const POSITIVE = /(article|body|content|entry|main|post|story|text|blog|prose|markdown)/i;
  root.querySelectorAll('[class],[id]').forEach((el) => {
    if (el.tagName === 'BODY' || el.tagName === 'ARTICLE' || el.tagName === 'MAIN') return;
    const sig = (el.getAttribute('class') || '') + ' ' + (el.getAttribute('id') || '');
    if (CHROME.test(sig) && !POSITIVE.test(sig) && (el.textContent || '').length < 4000) el.remove();
  });

  // score the containers by the prose they hold
  const linkLen = (el) => { let n = 0; el.querySelectorAll('a').forEach((a) => { n += clean(a.textContent).length; }); return n; };
  const scores = new Map();
  const add = (el, s) => { if (!el || el === root.parentNode) return; scores.set(el, (scores.get(el) || 0) + s); };
  let proseTotal = 0;
  root.querySelectorAll('p,pre,blockquote,li,td,dd,h2,h3').forEach((p) => {
    const txt = clean(p.textContent); if (txt.length < 25) return;
    const s = 1 + (txt.match(/,/g) || []).length + Math.min(3, Math.floor(txt.length / 100));
    proseTotal += txt.length;
    add(p.parentElement, s); if (p.parentElement) add(p.parentElement.parentElement, s / 2);
  });
  const ranked = [];
  scores.forEach((s, el) => {
    const len = clean(el.textContent).length || 1; const ld = linkLen(el) / len;
    const sig = (el.getAttribute('class') || '') + ' ' + (el.getAttribute('id') || '');
    let f = s * (1 - Math.min(0.95, ld));
    if (el.tagName === 'ARTICLE' || el.tagName === 'MAIN' || el.getAttribute('itemprop') === 'articleBody') f *= 1.4;
    else if (POSITIVE.test(sig)) f *= 1.2;
    ranked.push({ el, score: f, len });
  });
  ranked.sort((a, b) => b.score - a.score);
  let chosen = [];
  let composite = false;
  if (ranked.length) {
    const best = ranked[0]; chosen.push(best.el);
    // its siblings that read like the same body (readability's sibling step)
    const sibs = best.el.parentElement ? Array.from(best.el.parentElement.children) : [];
    sibs.forEach((sib) => {
      if (sib === best.el) return;
      const sc = scores.get(sib) || 0; const txt = clean(sib.textContent); const ld = txt.length ? linkLen(sib) / txt.length : 1;
      if (sc >= Math.max(8, best.score * 0.2) || (sib.tagName === 'P' && txt.length > 80 && ld < 0.25)) chosen.push(sib);
    });
    // FRAGMENTED: the best container holds too little of the page's prose - take the strongest others outside it
    const held = chosen.reduce((n, el) => n + clean(el.textContent).length, 0);
    if (proseTotal > 0 && held / proseTotal < 0.45) {
      const outside = (el) => chosen.every((c) => !c.contains(el) && !el.contains(c));
      for (let i = 1; i < ranked.length && chosen.length < 6; i++) {
        const r = ranked[i];
        if (r.score < best.score * 0.3) break;
        if (outside(r.el) && clean(r.el.textContent).length > 200) { chosen.push(r.el); composite = true; }
      }
    }
    // page order, not rank order
    chosen.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  }
  if (!chosen.length) chosen = [root];

  // the chosen nodes, as markdown
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
  const esc = (s) => s.replace(/([*_`\[\]])/g, '\\$1');
  function inline(node) {
    let out = '';
    node.childNodes.forEach((n) => {
      if (n.nodeType === 3) { out += esc(n.textContent.replace(/\s+/g, ' ')); return; }
      if (n.nodeType !== 1) return;
      const t = n.tagName;
      if (t === 'BR') { out += '  \n'; return; }
      if (t === 'IMG') { const src = abs(n.getAttribute('src') || n.getAttribute('data-src') || ''); const w = +(n.getAttribute('width') || 0);
        if (src && /^https?:/.test(src) && (!w || w > 60)) out += '![' + esc(clean(n.getAttribute('alt') || '')) + '](' + src + ')'; return; }
      const inner = inline(n);
      if (t === 'A') { const href = abs(n.getAttribute('href') || ''); const txt = clean(inner);
        out += (txt && /^https?:/.test(href)) ? '[' + txt + '](' + href + ')' : inner; return; }
      if (t === 'STRONG' || t === 'B') { const x = inner.trim(); out += x ? '**' + x + '** ' : ''; return; }
      if (t === 'EM' || t === 'I') { const x = inner.trim(); out += x ? '*' + x + '* ' : ''; return; }
      if (t === 'CODE' || t === 'KBD' || t === 'SAMP') { const x = clean(n.textContent); out += x ? '`' + x.replace(/`/g, "'") + '`' : ''; return; }
      if (t === 'SUP' || t === 'SUB') { out += inner; return; }
      out += inner;
    });
    return out;
  }
  const lines = [];
  const para = (s) => { const x = s.replace(/[ \t]+\n/g, '\n').replace(/ {2,}/g, ' ').trim(); if (x) lines.push(x, ''); };
  function table(tb) {
    const rows = Array.from(tb.querySelectorAll('tr')).map((tr) => Array.from(tr.children).map((c) => clean(inline(c)).replace(/\|/g, '\\|')));
    const w = Math.max(0, ...rows.map((r) => r.length));
    if (!rows.length || w < 2 || w > 10) { rows.forEach((r) => para(r.join(' · '))); return; }
    const pad = (r) => r.concat(Array(w - r.length).fill(''));
    lines.push('| ' + pad(rows[0]).join(' | ') + ' |', '|' + Array(w).fill(' --- ').join('|') + '|');
    rows.slice(1).forEach((r) => lines.push('| ' + pad(r).join(' | ') + ' |'));
    lines.push('');
  }
  function list(el, depth) {
    let i = 0; const ordered = el.tagName === 'OL';
    Array.from(el.children).forEach((li) => {
      if (li.tagName !== 'LI') return; i++;
      const nested = Array.from(li.children).filter((c) => c.tagName === 'UL' || c.tagName === 'OL');
      nested.forEach((c) => c.remove());
      const txt = clean(inline(li)); if (txt) lines.push('  '.repeat(depth) + (ordered ? i + '. ' : '- ') + txt);
      nested.forEach((c) => list(c, depth + 1));
    });
    if (!depth) lines.push('');
  }
  function block(el) {
    const t = el.tagName;
    if (/^H[1-6]$/.test(t)) { const txt = clean(inline(el)); if (txt && txt !== title) lines.push('#'.repeat(Math.min(6, +t[1] + (t === 'H1' ? 1 : 0))) + ' ' + txt, ''); return; }
    if (t === 'P') { para(inline(el)); return; }
    if (t === 'PRE') { const code = el.textContent.replace(/\n+$/, ''); if (code.trim()) { const lang = ((el.querySelector('code') || el).className.match(/language-([\w-]+)/) || [])[1] || ''; lines.push('```' + lang, code, '```', ''); } return; }
    if (t === 'BLOCKQUOTE') { const txt = clean(inline(el)); if (txt) lines.push('> ' + txt, ''); return; }
    if (t === 'UL' || t === 'OL') { list(el, 0); return; }
    if (t === 'TABLE') { table(el); return; }
    if (t === 'HR') { lines.push('---', ''); return; }
    if (t === 'IMG') { para(inline({ childNodes: [el] })); return; }
    if (t === 'FIGURE') { const img = el.querySelector('img'); if (img) para(inline({ childNodes: [img] })); const cap = el.querySelector('figcaption'); if (cap && clean(cap.textContent)) lines.push('*' + clean(cap.textContent) + '*', ''); return; }
    // a container: its blocks in order; loose text inside it is a paragraph of its own
    let loose = '';
    el.childNodes.forEach((n) => {
      if (n.nodeType === 3) { loose += n.textContent; return; }
      if (n.nodeType !== 1) return;
      if (/^(A|SPAN|STRONG|B|EM|I|CODE|SMALL|MARK|ABBR|TIME|SUP|SUB|BR|LABEL|CITE|Q|U|S)$/.test(n.tagName)) { loose += inline({ childNodes: [n] }); return; }
      if (clean(loose)) { para(loose); } loose = '';
      block(n);
    });
    if (clean(loose)) para(loose);
  }
  chosen.forEach((el, i) => { if (composite && i) lines.push('---', ''); block(el); });
  let md = lines.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (md.length > LIMIT) { const cut = md.lastIndexOf('\n\n', LIMIT); md = md.slice(0, cut > LIMIT * 0.6 ? cut : LIMIT) + '\n\n…'; }
  if (!md && description) md = description;
  const words = (md.replace(/[#>*_`\[\]\(\)|-]/g, ' ').match(/\S+/g) || []).length;
  return { title, byline, site, published, image, description, markdown: md, words, minutes: Math.max(1, Math.round(words / 230)),
           composite, parts: chosen.length, url: location.href };
}

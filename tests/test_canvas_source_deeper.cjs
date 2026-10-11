// canvas-enrichment slice 6 (first part): a page, deeper - the canvas asks the reader cache (mode=cache, never the web)
// for every source it draws; a read page shows who wrote it, how long, what it names (chips that look it up), where it
// points, and can be exploded
//   node tests/test_canvas_source_deeper.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const C = R('vera/canvas/canvas_element.js'), B = R('vera/web/browser_capabilities.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('reader.page answers mode=cache from the cache alone, never fetching', B.includes('if mode == "cache":') && B.includes('return _err("not read yet", url=url, markdown="", cached=False,'));
t('every render asks the cache for the sources it draws, once each, a dozen at a time', C.includes("try { this._srcPrefetch(blocks); } catch (e) {}") && C.includes("r = await this.callResult('reader.page', { url: String(b.content.url), max_chars: 40000, mode: 'cache' });")
  && C.includes('this._srcProbed.add(String(b.key))') && C.includes('.slice(0, 12);'));
t('a read page shows who, how long and when', C.includes("const meta = [rdr.site, rdr.byline, rdr.published ? String(rdr.published).slice(0, 10) : '', rdr.minutes ? rdr.minutes + ' min read' : '']"));   // one page view for every kind (ff152c45)
t('...what it names, each a chip that looks it up beside it', C.includes('data-act="srcent"') && C.includes("this.callResult('turn.entity.look', { session_id: this._sid(), text: t, type: String(btn.dataset.entY || '') })"));
t('...where it points (other sites, once each)', C.includes('<div class="vc-src-out"><span>points to</span>'));
t('...and Explode of the page', C.includes('data-src-act="explode"') && C.includes("return this.call('canvas.add', Object.assign({ kind: 'explode', key: 'explode:' + key, at: sb.state === 'pinned' ? 'pinned' : 'now', size: 'l',"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
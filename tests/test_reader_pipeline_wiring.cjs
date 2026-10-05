// canvas-enrichment slice 2: one page pipeline - reader.page (cache, then the fetched page read in memory, then a real
// render), web.research hands the model the article, web.fetch keeps the reader version, reader mode opens from cache
//   node tests/test_reader_pipeline_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const B = R('vera/web/browser_capabilities.py'), W = R('vera/web/web_capabilities.py'), C = R('vera/canvas/canvas_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('reader.page is a capability over reader_read', B.includes('"reader.page",') && B.includes('return await reader_read(url, max_chars='));
t('...the cache first', B.includes('hit = await _rc.get(redis, url, _cap_fn("fabric.objects.get"))') && B.includes('out.update(ok=True, cached=True,'));
t('...then the fetched page read IN MEMORY (network shut, served at its own address)', B.includes('fp = await _wc.fetch_page(url, timeout=10.0, max_chars=200_000)')
  && B.includes('await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)') && B.includes('await route.abort()'));
t('...then a real render only when that yields no article', B.includes('if not rec and mode in ("auto", "browser"):') && B.includes('r = await browser_reader(url=url, max_chars=200_000)'));
t('...kept, with its entities added in the background', B.includes('await _rc.put(redis, url, rec, _cap_fn("fabric.objects.put"))') && B.includes('_spawn_bg(_reader_entities(url,'));
t('web.research hands the model the article (reader.page), the plain fetch as fallback', W.includes('reader = (_orch.CAPABILITY_REGISTRY.get("reader.page") or {}).get("func")')
  && W.includes('r = await asyncio.wait_for(reader(url=h["url"], max_chars=max_chars, mode="fetch"), fetch_timeout + 8.0)')
  && W.includes('return await cap_web_fetch(url=h["url"], timeout=fetch_timeout, max_chars=max_chars, ingest_to_fabric=False)'));
t('web.fetch keeps the reader version of what it read, in the background', W.includes('_spawn(_rr(url, html=html, mode="fetch"))'));
t('the canvas reads a page through reader.page (cache first), browser.reader as fallback', C.includes("r = await this.callResult('reader.page', { url, max_chars: 40000 });")
  && C.includes("r = await this.callResult('browser.reader', { url, max_chars: 40000 });"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);

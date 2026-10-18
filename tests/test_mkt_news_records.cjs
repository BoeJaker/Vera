// The markets news answers on the desk (the redesign plan section 4: news reachable; one implementation): markets.news.feed and
// markets.news.digest declare `records`, so the desk draws them with its records browser (list / cards / table, the page in a
// drawer) - not a JSON tree, and no second news list.
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const D = JSON.parse(R('tests/fixtures/presentation_declared.json'));
t('markets.news.feed is records over its headlines', JSON.stringify((D['markets.news.feed'] || {}).map) === JSON.stringify({ rows: 'headlines', snippet: 'snippet', title: 'title', url: 'url' }) && D['markets.news.feed'].kind === 'records');
t('markets.news.digest is records over its items, the source in the site slot', D['markets.news.digest'] && D['markets.news.digest'].kind === 'records' && D['markets.news.digest'].map.rows === 'items' && D['markets.news.digest'].map.domain === 'source');
t('both say so when nothing was found', ['markets.news.feed', 'markets.news.digest'].every((k) => D[k].empty === 'no headlines found'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);

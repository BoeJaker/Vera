// The Data Fabric overview (owner, 2026-09-27: "the data fabric should have a dashboard and widgets of its own"):
// every tile of vera/widgets/layouts/fabric.json reads a capability the element may read on its own, its read.map
// turns that capability's REAL answer (trimmed copies of the design mirror's answers, 2026-09-27) into the form's
// shape, the form draws it - not its empty state, not its sample - and every drawn part carries its item (data-item),
// which is what opens the drawer on a click.
//   node tests/test_fabric_dashboard.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const layout = JSON.parse(fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'layouts', 'fabric.json'), 'utf8'));
/* the answers, as the design mirror gave them (lists trimmed to a few rows) */
const ANSWERS = {
 "fabric.health": {
  "db_path": "/home/boejaker/Vera/vera/fabric/vera_fabric.db",
  "db_size": 3923292160,
  "has_journal_file": false,
  "has_wal_files": false,
  "journal_mode": "wal",
  "records_count": 1058218,
  "datasets_count": 5549,
  "sources_count": 2106,
  "writer_task_alive": true,
  "write_queue_size": 0,
  "index_migration_done": true,
  "in_memory_sources": 2106,
  "auto_pull_sem": "ready"
 },
 "fabric.stats": {
  "postgres": {
   "available": true,
   "records": 613382,
   "datasets": 0
  },
  "faiss": {
   "available": false
  },
  "chroma": {
   "available": true,
   "count": 533919,
   "collection": "vera_fabric"
  },
  "neo4j": {
   "available": true
  },
  "sqlite": {
   "available": true,
   "path": "/home/boejaker/Vera/vera/fabric/vera_fabric.db"
  },
  "object_store": {
   "available": true,
   "mode": "garage"
  }
 },
 "fabric.bus.status": {
  "enabled": false,
  "filters": [],
  "task_alive": false,
  "stream": "vera:events",
  "note": "Uses shared REDIS pool — no separate connections"
 },
 "fabric.objects.status": {
  "enabled": true,
  "available": true,
  "mode": "garage",
  "endpoint": "http://localhost:3900",
  "default_bucket": "vera-data-fabric",
  "region": "garage",
  "has_boto": true,
  "last_error": ""
 },
 "fabric.sources": {
  "sources": [
   {
    "id": "open_meteo_uk",
    "label": "Open-Meteo — UK (London)",
    "dataset_id": "prebaked_open_meteo_uk",
    "source_type": "api",
    "interval": 0,
    "enabled": true,
    "pull_count": 1,
    "last_pulled": "2026-04-29T19:48:02.721568Z",
    "created_at": "2026-04-29T19:48:02.722908Z"
   },
   {
    "id": "ars_technica",
    "label": "Ars Technica",
    "dataset_id": "prebaked_ars_technica",
    "source_type": "rss",
    "interval": 0,
    "enabled": true,
    "pull_count": 1,
    "last_pulled": "2026-05-02T09:33:38.405192Z",
    "created_at": "2026-05-02T09:33:38.405948Z"
   },
   {
    "id": "github_trending",
    "label": "GitHub Trending (Unofficial RSS)",
    "dataset_id": "prebaked_github_trending",
    "source_type": "rss",
    "interval": 0,
    "enabled": true,
    "pull_count": 1,
    "last_pulled": "2026-05-02T09:33:56.129171Z",
    "created_at": "2026-05-02T09:33:56.130124Z"
   },
   {
    "id": "72b49bdc-b9fc-42bb-b7f7-dcbeb31fc25b",
    "label": "GitHub: BoeJaker/BoeJaker",
    "dataset_id": "topic_boejaker_repo_boejaker",
    "source_type": "github",
    "interval": 0,
    "enabled": true,
    "pull_count": 0,
    "last_pulled": "",
    "created_at": "2026-05-31T16:50:37.194749Z"
   },
   {
    "id": "72be4d5b-6672-48fb-82d6-1511a105055c",
    "label": "GitHub: BoeJaker/Python-Neural-Networks",
    "dataset_id": "topic_boejaker_repo_python_neural_networks",
    "source_type": "github",
    "interval": 0,
    "enabled": true,
    "pull_count": 0,
    "last_pulled": "",
    "created_at": "2026-05-31T16:50:39.574485Z"
   }
  ]
 },
 "fabric.tags.list_grouped": {
  "count": 4,
  "tags": [
   {
    "tag": "discovered",
    "datasets": 0,
    "sources": 1698
   },
   {
    "tag": "rss",
    "datasets": 0,
    "sources": 1503
   },
   {
    "tag": "ohlcv",
    "datasets": 53,
    "sources": 0
   },
   {
    "tag": "1d",
    "datasets": 36,
    "sources": 0
   }
  ]
 },
 "fabric.discover.history": {
  "count": 4,
  "crawls": [
   {
    "crawl_id": "disc_76d2c3875423b656",
    "dataset_id": "web.www_wired_com",
    "seed_url": "https://www.wired.com/tag/machine-learning/",
    "status": "done",
    "topic": "",
    "queued": 0,
    "pages_fetched": 1,
    "surfaces_found": 0,
    "subtables_found": 0,
    "created_at": "2026-09-14T18:13:10.913306Z",
    "updated_at": "2026-09-14T18:13:10.913319Z"
   },
   {
    "crawl_id": "disc_f75e46aec5f77400",
    "dataset_id": "research.web.get_the_latest_in_ai_and_ml",
    "seed_url": "https://www.timesofai.com/",
    "status": "done",
    "topic": "get the latest in AI and ML",
    "queued": 0,
    "pages_fetched": 32,
    "surfaces_found": 0,
    "subtables_found": 0,
    "created_at": "2026-08-25T19:34:08.469712Z",
    "updated_at": "2026-08-25T19:43:28.744447Z"
   },
   {
    "crawl_id": "disc_4756e08fbf426282",
    "dataset_id": "web.arxiv_org",
    "seed_url": "https://arxiv.org/list/cs.AI?dates=recent&filter=all&skipDate=True",
    "status": "done",
    "topic": "",
    "queued": 0,
    "pages_fetched": 0,
    "surfaces_found": 0,
    "subtables_found": 0,
    "created_at": "2026-08-20T19:35:52.101297Z",
    "updated_at": "2026-08-20T19:35:52.101318Z"
   },
   {
    "crawl_id": "disc_69a9ded625c71dd6",
    "dataset_id": "research.web.latest_developments_in_ai_and_ml_2026_au",
    "seed_url": "https://www.aiapps.com/blog/august-2026-ai-mega-update-major-breakthroughs-launches/",
    "status": "done",
    "topic": "latest developments in AI and ML 2026 August",
    "queued": 0,
    "pages_fetched": 12,
    "surfaces_found": 0,
    "subtables_found": 0,
    "created_at": "2026-08-16T19:39:40.341845Z",
    "updated_at": "2026-08-16T19:42:43.152949Z"
   }
  ]
 },
 "fabric.kb.list": {
  "knowledgebases": [
   {
    "kb_id": "kb_predictive_maintenance_implementation_challenges_for_legacy_",
    "subject": "predictive maintenance implementation challenges for legacy machinery",
    "description": "",
    "status": "building",
    "article_count": 0,
    "fact_count": 0,
    "updated_at": "2026-07-23T07:21:09.214744Z"
   },
   {
    "kb_id": "kb_regulatory_light_cryptocurrency_sector_t",
    "subject": "regulatory light cryptocurrency sector t",
    "description": "",
    "status": "building",
    "article_count": 0,
    "fact_count": 0,
    "updated_at": "2026-07-19T00:28:11.016981Z"
   },
   {
    "kb_id": "kb_research_web_site_github_com_boejaker_or_site_x_com_b",
    "subject": "research.web.site github com boejaker or site x com b",
    "description": "",
    "status": "ready",
    "article_count": 0,
    "fact_count": 261,
    "updated_at": "2026-07-16T17:31:31.552774Z"
   }
  ]
 },
 "fabric.graphs.list": {
  "graphs": [
   {
    "name": "fabric",
    "available": true,
    "kind": "neo4j",
    "description": "Primary fabric graph — datasets, records, sources, skills"
   },
   {
    "name": "memory",
    "available": false,
    "kind": "neo4j",
    "description": "Memory graph — sessions, conversation memory, activity chain"
   },
   {
    "name": "net",
    "available": true,
    "kind": "neo4j",
    "description": "Network/asset graph (NetHost, SshHost, Subnet, containers, k8s, proxmox)"
   }
  ]
 },
 "fabric.skills.list": {
  "skills": [
   {
    "id": "skill_efb3d627b23b8d52",
    "name": "Boejaker sitemap",
    "dataset_ids": [
     "topic.boejaker.data.sitemap"
    ],
    "created_at": "2026-06-04T19:27:07.422633Z",
    "updated_at": "2026-06-04T19:27:07.422673Z"
   },
   {
    "id": "skill_b6fa1bd65e1d4c4d",
    "name": "Pokemon",
    "dataset_ids": [
     "web.serebii_net"
    ],
    "created_at": "2026-05-30T19:48:55.525949Z",
    "updated_at": "2026-05-30T19:48:55.525956Z"
   }
  ]
 },
 "fabric.graphs.snapshot": {
  "graph": "fabric",
  "nodes": [
   {
    "id": "mem_1755731776917",
    "name": "could you inspect your own source code and suggest improvements",
    "label": "Query",
    "labels": [
     "Query",
     "Entity"
    ]
   },
   {
    "id": "mem_1755731581233",
    "name": "could you inspect your own source code and suggest improvements",
    "label": "Query",
    "labels": [
     "Query",
     "Entity"
    ]
   },
   {
    "id": "mem_1756039243341",
    "name": "Session",
    "label": "Entity",
    "labels": [
     "Entity",
     "Session"
    ]
   }
  ],
  "edges": [],
  "node_count": 3,
  "edge_count": 0
 }
};

const tiles = layout.widgets.map((w) => w.record).filter((r) => r && r.form !== 'section');
t('the layout is the fabric dashboard and has its tiles', layout.key === 'fabric' && layout.dashboard === 'fabric' && tiles.length >= 15, tiles.length);
t('every tile reads a capability the element reads on its own (no "Read" button, a refresh of its own)', tiles.every((r) => W.readable(r.source)), tiles.filter((r) => !W.readable(r.source)).map((r) => r.source).join(', '));
t('every tile has an answer to be checked against', tiles.every((r) => ANSWERS[r.source] !== undefined), tiles.filter((r) => ANSWERS[r.source] === undefined).map((r) => r.source).join(', '));
for (const r of tiles) {
  if (r.form === 'vgraph') continue;   /* the Vera graph mounts the estate's own graph element; its data is checked below */
  const data = W.mapped(r, r.form, ANSWERS[r.source]);
  const h = W.draw(r.form, data, r.frame.size, { record: r, draw: r.draw, sample: false });
  const drawn = !/class="wempty"/.test(h) && !/data-sample="1"/.test(h);
  const items = (h.match(/data-item="/g) || []).length;
  t(r.id + ' (' + r.form + ' <- ' + r.source + ') draws the real answer and its parts carry their items', drawn && items > 0, 'items=' + items + ' ' + h.slice(0, 200));
}
const fresh = tiles.find((r) => r.id === 'fabric-feed-fresh');
const fh = W.draw('table', W.mapped(fresh, 'table', ANSWERS['fabric.sources']), 'xl', { record: fresh, draw: fresh.draw, sample: false });
t('the feeds table sorts the most recently pulled first', fh.indexOf('2026-05-02T09:33') > -1 && fh.indexOf('2026-05-02T09:33') < fh.indexOf('2026-04-29T19:48'));
const types = W.mapped(tiles.find((r) => r.id === 'fabric-feed-types'), 'ranked', ANSWERS['fabric.sources']);
t('the feeds are counted by their source type', types && typeof types === 'object' && Object.keys(types).length >= 1 && Object.values(types).reduce((a, b) => a + b, 0) === ANSWERS['fabric.sources'].sources.length, JSON.stringify(types));
const stores = W.mapped(tiles.find((r) => r.id === 'fabric-stores'), 'pills', ANSWERS['fabric.stats']);
t('the stores are one pill each, green when available', Array.isArray(stores) && stores.length === 6 && stores.find((s) => s.name === 'postgres').status === true && stores.find((s) => s.name === 'faiss').status === false, JSON.stringify(stores));
const crawls = W.mapped(tiles.find((r) => r.id === 'fabric-crawls'), 'log', ANSWERS['fabric.discover.history']);
t('a crawl is a log line: when, its state, its seed', Array.isArray(crawls) && crawls[0].t === ANSWERS['fabric.discover.history'].crawls[0].updated_at && crawls[0].kind === crawls[0].status && crawls[0].text === crawls[0].seed_url);
const g = tiles.find((r) => r.form === 'vgraph');
t('the fabric graph tile reads the fabric graph', g && g.source === 'fabric.graphs.snapshot' && g.read.args.graph === 'fabric' && Array.isArray(ANSWERS['fabric.graphs.snapshot'].nodes));

console.log(fails ? '\n' + fails + ' FAILED' : '\nall passed');
process.exit(fails ? 1 : 0);

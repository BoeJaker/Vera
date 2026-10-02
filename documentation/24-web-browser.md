# 24 · Web & Browser

The `vera/web/` package gives Vera three tiers of web access. Lightweight
**search / fetch / crawl** capabilities (`web.*`) answer "go look something up"
cheaply; **platform API providers** (`web.api.*`) read sites such as Reddit,
GitHub and Hacker News through their own APIs instead of scraping them; and
**headless-browser automation** via Playwright (`browser.*`) renders dynamic
pages, interacts with them, and can run a model-driven navigation session.

All page fetching goes through one hardened HTTP layer, `web/web_client.py`,
which owns the browser fingerprint, per-domain throttling, retries, block
detection and the reader-proxy fallback — so the research pipeline, the fabric
and the web capabilities cannot drift apart. The `web.*` and `web.api.*`
surfaces are in everyday use by research, dream cycles, agent chats and the
IDE; `browser.*` needs Playwright and Chromium installed on the host.

## Contents

- [1. Choosing an acquisition path](#1-choosing-an-acquisition-path)
- [2. Source map](#2-source-map)
- [3. `web.*` — search, fetch, crawl](#3-web--search-fetch-crawl)
  - [3.1 Capabilities](#31-capabilities)
  - [3.2 Search engines and routing](#32-search-engines-and-routing)
  - [3.3 Discovery-first search](#33-discovery-first-search)
  - [3.4 Crawl boundaries and fabric ingest](#34-crawl-boundaries-and-fabric-ingest)
- [4. The shared web client](#4-the-shared-web-client)
  - [4.1 What a fetch does](#41-what-a-fetch-does)
  - [4.2 Retries and throttling](#42-retries-and-throttling)
  - [4.3 Vera's own origin](#43-veras-own-origin)
- [5. `web.api.*` — platform API providers](#5-webapi--platform-api-providers)
- [6. `browser.*` — Playwright automation](#6-browser--playwright-automation)
  - [6.1 Capabilities](#61-capabilities)
  - [6.2 Autonomous navigation](#62-autonomous-navigation)
  - [6.3 Reader mode](#63-reader-mode)
- [7. Configuration](#7-configuration)
- [8. Events](#8-events)
- [9. Examples](#9-examples)
- [10. Acquisition boundaries and troubleshooting](#10-acquisition-boundaries-and-troubleshooting)
- [See also](#see-also)
- [Screenshots](#screenshots)
- [Capabilities](#capabilities)

---

## 1. Choosing an acquisition path

Use the least powerful path that fits the job:

| Need | Use | Cost |
|---|---|---|
| Find pages for a query | `web.search` | One HTTP call per engine page |
| Read one known page | `web.fetch` (or `web.api.fetch` for a configured platform) | One fetch, reader fallback when blocked |
| Search and read the top results in one call | `web.research` | Search + parallel fetches |
| Walk a site | `web.crawl` / `web.search_and_crawl` | Bounded recursive fetches + fabric ingest |
| JavaScript-rendered page, screenshot, PDF | `browser.content`, `browser.reader`, `browser.screenshot`, `browser.pdf` | A Chromium page |
| Click/type/scroll | `browser.click`, `browser.type`, `browser.scroll`, `browser.select` | A Chromium page per call |
| Goal-driven multi-step browsing | `browser.navigate` | Chromium + one LLM call per step |

Plain HTTP fetch is the cheapest and most reproducible; crawl follows links and
policy; Playwright renders dynamic pages; autonomous navigation adds
model-driven decisions. Research and fabric pipelines consume these primitives
but retain their own provenance and storage contracts. For driving a web app as
an operator (with allowlists and confirmation), see [Operator](./34-operator.md).

## 2. Source map

| File | Responsibility |
|---|---|
| `vera/web/web_capabilities.py` | `web.search`, `web.fetch`, `web.crawl`, `web.search_and_crawl`, `web.research` |
| `vera/web/search_engines.py` | Pure engine ordering, pagination, result merging and redirect unwrapping shared with research |
| `vera/web/web_client.py` | Shared hardened fetch layer (fingerprint, rewrites, throttle, retries, block detection, reader fallback, crawl identities) |
| `vera/web/own_origin.py` | Pure rule for trusting Vera's own self-signed certificate |
| `vera/web/web_api_capabilities.py`, `web_api_panel.html` | `web.api.*` platform providers and the **Web APIs** widget |
| `vera/web/browser_capabilities.py` | `browser.*` Playwright automation |
| `vera/web/reader_extract.js` | In-page article extraction used by `browser.reader` |
| `vera/web/scraper.py` | Standalone endpoint-probing script; not loaded as a capability |

## 3. `web.*` — search, fetch, crawl

`web_capabilities.py` pulls the "go look something up" logic out of the
research pipeline so it is a first-class registry citizen that dream cycles,
agentic chats and IDE chats can all call cheaply.

### 3.1 Capabilities

| Cap | Route | Purpose / key args |
|---|---|---|
| `web.search` | `POST /web/search` | Ranked results (title, URL, snippet). `query`, `limit` (8, max 50), `engine` (`auto`/`searxng`/`brave`/`ddg`), `platform`, `discover`, `dataset_id` |
| `web.fetch` | `POST /web/fetch` | One URL → clean text + title. `url`, `timeout`, `max_chars`, `ingest_to_fabric` (true), `await_ingest`, `dataset_id` |
| `web.crawl` | `POST /web/crawl` | Recursive crawl from a seed. `depth` (1), `breadth` (3), `max_pages` (10; hard ceiling 25), `ingest_to_fabric` |
| `web.search_and_crawl` | `POST /web/search_and_crawl` | Search, then crawl the top results through the fabric discovery engine. `search_limit` (5), `crawl_depth` (1), `pages_per_result` (3), `engine` |
| `web.research` | `POST /web/research` | Search broadly and pull the useful content from the top `results` (5, max 10) in parallel, `max_chars` each; returns sources plus follow-up links |

### 3.2 Search engines and routing

All callers — `web.search` and the [Research](./07-research.md) subsystem — share
one dispatcher (`search_engines.engine_order`), so the same request always
picks the same engines:

- `engine="auto"` starts from the operator's configured default engine
  (normally `searxng`) and then completes the chain `searxng → brave → ddg`.
- A named engine is tried first and still falls through to `ddg` as the last
  resort.
- Requests for more than one engine page paginate (SearXNG `pageno`, Brave
  `offset`), so `limit` is honoured rather than silently capped at one page.

SearXNG lives at `VERA_SEARXNG_URL` (default `http://<BACKEND_HOST>:8888`);
Brave needs `BRAVE_API_KEY`. Timeouts are aggressive (`VERA_WEB_TIMEOUT`,
8 s) — these caps are meant to be responsive, not exhaustive.

Configured platform APIs are resolved through the same web-client boundary for
both `web.search` and research. A matching native provider leads the result
set, general engines fill the remainder, and normalized URL deduplication
preserves one stable order. If a provider is absent, malformed, or unavailable,
the caller continues through ordinary search without exposing provider
configuration or turning an optional integration into a hard failure.

### 3.3 Discovery-first search

`web.search` can recall already-processed discovery data in parallel with the
live search and feed results back into the [Data Fabric](./06-data-fabric.md).
The mode comes from the `discover` argument or `VERA_WEB_DISCOVER_MODE`:

| Mode | Behaviour |
|---|---|
| `background` (default) | Recall + snippet-entity update + background full-page fetch |
| `snippets` | Recall + snippet-entity update only |
| `off` | Plain search, no recall or ingest |

### 3.4 Crawl boundaries and fabric ingest

The shared web client defines canonical crawl URL, same-scope link, and
normalized content identities. Fragments and explicit default ports do not
consume extra page budget; embedded credentials, non-HTTP(S) targets, and
cross-host or cross-port links are excluded. A URL is claimed before its fetch
starts, so concurrent children cannot overshoot the page ceiling. Repeated page
content is skipped (`web.crawl.duplicate`) while successfully fetched earlier
pages remain available after a later failure.

Crawled pages are written to the fabric as dataset
`web.crawl.<sanitised-domain>` with records `{url, title, full_text, domain,
fetched_at, parent_url}`, so later `research.recall.search` or `fabric.query`
calls can find any page that has been crawled.

## 4. The shared web client

### 4.1 What a fetch does

`web_client.fetch_page(url)` returns `{url, final_url, domain, status, html,
text, title, chars, blocked, block_reason, via, via_reader, via_api,
elapsed_ms, error, reader_error}` where `via` is `direct`, `reader` or
`api:<id>`. In order it:

1. **API switchover** — if a configured `web.api` provider matches the URL's
   domain, the content comes from that platform's API.
2. **Domain rewrite** — hostile hosts are rewritten (`www.reddit.com` →
   `old.reddit.com` by default; extend with `VERA_WEB_REWRITES`).
3. **Per-domain throttle** — a minimum interval plus jitter between hits.
4. **Direct fetch** — a realistic desktop-Chrome fingerprint (UA + client
   hints), over HTTP/2 when the `h2` package is available.
5. **Block detection** — Cloudflare, DataDome, PerimeterX, consent and CAPTCHA
   interstitials are recognised so challenge boilerplate is never mistaken for
   content.
6. **Reader fallback** — when blocked or near-empty, retry through a
   server-side-rendering reader proxy (`VERA_WEB_READER`, default
   `https://r.jina.ai/`; empty disables; `VERA_WEB_READER_KEY` adds auth).

`text` is always extracted plain text, capped at `VERA_WEB_MAX_PAGE_CHARS`
(16 000).

### 4.2 Retries and throttling

The web client is the single retry and per-domain throttle owner for page,
reader-proxy and general search requests. By default it makes at most two
attempts (`VERA_WEB_REQUEST_ATTEMPTS`, clamped to 1–3), and retries only
transport failures or temporary `429`, `502`, `503` and `504` responses, with
backoff from `VERA_WEB_RETRY_BASE_S` (0.25 s). Numeric `Retry-After` is
honoured up to `VERA_WEB_RETRY_MAX_S` (5 s); other responses are not repeated.
Per-domain spacing is `VERA_WEB_DOMAIN_INTERVAL` (1.0 s) plus up to
`VERA_WEB_DOMAIN_JITTER` (0.6 s). Tune this boundary rather than adding
caller-local retry loops.

### 4.3 Vera's own origin

Vera serves HTTPS with a self-signed certificate. `own_origin.py` relaxes TLS
verification **only** for Vera's own origin — loopback or the host this
orchestrator serves as, on its own port (`VERA_ORCH_PORT`, 8999) — so a fetch of
a URL Vera itself published (for example a sandbox preview) does not fail.
Every other host keeps full verification; a URL that merely mentions
`localhost` in its query, or hits a different port, is treated as external.

## 5. `web.api.*` — platform API providers

Some platforms hard-block scrapers regardless of fingerprint. The operator can
register platform API providers, after which `web.fetch` and `web.search`
switch to the API transparently when a request targets that platform (a
configured Reddit provider runs before the `old.reddit` rewrite).

| Driver | Auth | Supports |
|---|---|---|
| `reddit` | OAuth (app-only client credentials, or password grant for a script app) | Search, thread/subreddit fetch |
| `github` | REST v3, token optional | Repo/issue fetch, repository search |
| `hackernews` | Algolia HN API, none | Item fetch, search |
| `rest` | Optional header | Generic JSON API: templated search URL, dot-path result mapping, optional fetch template |

A search is routed to a provider by an explicit `platform=`, a `site:` filter,
or a platform keyword (for example "subreddit", "repo", "hacker news").

| Cap | Route | Purpose |
|---|---|---|
| `web.api.drivers` | `GET /web/api/drivers` | Driver templates and the fields each needs |
| `web.api.list` | `GET /web/api/list` | Configured providers (redacted, `has_<field>` flags) |
| `web.api.save` / `web.api.delete` | `POST /web/api/save`, `/delete` | Provider CRUD |
| `web.api.test` | `POST /web/api/test` | Test credentials/connectivity |
| `web.api.search` | `POST /web/api/search` | Search through one provider |
| `web.api.fetch` | `POST /web/api/fetch` | Fetch a URL through the matching (or named) provider |

Records live in the Redis hash `vera:web_api`; secret fields are sealed with the
shared vault ([Security & Secrets](./29-security.md)). UI: the **Web APIs**
widget (`web_api`, inject mode, `/web/api/panel`).

## 6. `browser.*` — Playwright automation

`browser_capabilities.py` provides autonomous web navigation and page
interaction. Most interaction caps return a screenshot so an agent can "see"
the result of its action. Concurrency is bounded by a semaphore
(`BROWSER_MAX_SESSIONS`); each call gets a fresh browser context that is closed
afterwards, so no cookies persist between calls. URLs without a scheme are
given `https://`.

```bash
pip install playwright && playwright install chromium
```

### 6.1 Capabilities

| Cap | Route | Purpose |
|---|---|---|
| `browser.health` | `GET /browser/health` | Playwright / Chromium availability |
| `browser.screenshot` | `POST /browser/screenshot` | Full-page screenshot of a URL |
| `browser.content` | `POST /browser/content` | Text, links and metadata |
| `browser.reader` | `POST /browser/reader` | Reader mode: the article as markdown (`max_chars`, 60 000) |
| `browser.click` / `browser.type` / `browser.scroll` / `browser.select` | `POST /browser/<verb>` | Navigate, interact by CSS selector, return a screenshot (`type` can submit) |
| `browser.pdf` | `POST /browser/pdf` | Chromium print-to-PDF (base64) |
| `browser.search` | `POST /browser/search` | DuckDuckGo search, no API key |
| `browser.extract` | `POST /browser/extract` | **LLM**: visible text → structured data per a schema (JSON mode) |
| `browser.navigate` | `POST /browser/navigate` | **LLM**: goal-driven multi-step session |
| `browser.monitor` | `POST /browser/monitor` | Compare content hashes of a `selector` over `checks` polls every `interval_ms` (5000) |

The caps compose in a [DAG](./03-dag-engine.md):

```json
[
  ["browser.search",  "results", {"query": "Vera AI framework"}],
  ["browser.content", "page",    {"url": "{{results.urls[0]}}"}],
  ["llm.summarize",   "summary", {"text": "{{page.text}}"}]
]
```

### 6.2 Autonomous navigation

`browser.navigate(goal, start_url="https://www.google.com", max_steps=8,
prefer_gpu=true)` loops up to `max_steps` (hard cap 15). Each step summarises
the page (inputs, buttons, top links with selectors, a text snippet) and asks
the cluster in JSON mode for the next action — one of `goto`, `click`, `type`,
`scroll`, `extract` or `done`. Each step emits `browser.navigate.step`. A blank
`goal` with a real `start_url` falls back to "extract the key information" rather
than failing.

> [!WARNING]
> Navigation acts on whatever the page says. Page text is untrusted input, and
> forms, downloads and cookies can carry credentials or mutations. Use the
> [Operator](./34-operator.md) for anything that needs an allowlist or
> confirmation of destructive actions.

### 6.3 Reader mode

`browser.reader` loads the page, waits briefly for lazy JavaScript, and runs
`reader_extract.js` in the page to return the article a person would read (or a
composite of its parts) as markdown — not the whole body stripped of tags.

## 7. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `VERA_SEARXNG_URL` | `http://<BACKEND_HOST>:8888` | SearXNG instance |
| `BRAVE_API_KEY` | empty | Brave Search key |
| `VERA_WEB_TIMEOUT` | 8.0 | Per-request timeout (s) |
| `VERA_WEB_DISCOVER_MODE` | `background` | `web.search` discovery mode |
| `VERA_WEB_UA` | realistic Chrome | User agent override |
| `VERA_WEB_READER` / `VERA_WEB_READER_KEY` | `https://r.jina.ai/` / empty | Reader proxy |
| `VERA_WEB_READER_TIMEOUT` | 25.0 | Reader fallback timeout floor (s) |
| `VERA_WEB_MAX_PAGE_CHARS` | 16000 | Text kept per page |
| `VERA_WEB_REWRITES` | empty | Extra `from=to` host rewrites, comma-separated |
| `VERA_WEB_REQUEST_ATTEMPTS` | 2 (1–3) | Attempts per request |
| `VERA_WEB_RETRY_BASE_S` / `VERA_WEB_RETRY_MAX_S` | 0.25 / 5.0 | Backoff base / `Retry-After` cap |
| `VERA_WEB_DOMAIN_INTERVAL` / `VERA_WEB_DOMAIN_JITTER` | 1.0 / 0.6 | Per-domain spacing (s) |
| `BROWSER_HEADLESS` | `1` | `0` for a visible browser |
| `BROWSER_TIMEOUT_MS` | 30000 | Page load timeout |
| `BROWSER_VIEWPORT_W` / `BROWSER_VIEWPORT_H` | 1280 / 900 | Viewport |
| `BROWSER_USER_AGENT` | realistic Chrome | Browser UA |
| `BROWSER_MAX_SESSIONS` | 3 | Concurrent browser pages |
| `BROWSER_SCREENSHOT_Q` | 85 | Screenshot quality |

See also [Configuration](./10-configuration.md).

## 8. Events

| Event | When |
|---|---|
| `web.search.done` | A search completed |
| `web.fetch.done`, `web.fetch.error`, `web.fetch.ingested` | Fetch lifecycle and fabric ingest |
| `web.crawl.page`, `web.crawl.duplicate`, `web.crawl.error`, `web.crawl.done` | Crawl progress |
| `web.research.done` | `web.research` finished |
| `browser.screenshot`, `browser.search`, `browser.monitor`, `browser.navigate.step` | Browser activity |

## 9. Examples

```bash
# Search, forcing SearXNG
curl -s -X POST http://localhost:8999/web/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"redis streams consumer groups","limit":10,"engine":"searxng"}'

# Fetch a page without fabric ingest
curl -s -X POST http://localhost:8999/web/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","ingest_to_fabric":false}'

# Search and read the top three results in one call
curl -s -X POST http://localhost:8999/web/research \
  -H 'Content-Type: application/json' \
  -d '{"query":"playwright reader mode","results":3}'
```

## 10. Acquisition boundaries and troubleshooting

Every acquisition should retain the requested URL, final URL, timestamp,
status, and extraction method. Respect robots/publisher rules and do not treat
rendered page text as trusted instructions. Browser state, cookies, downloads
and forms can carry credentials or mutations, so autonomous navigation requires
the same allowlist and confirmation discipline described in
[Operator](./34-operator.md).

When debugging, separate DNS/TLS, HTTP status, JavaScript readiness, selector
failure, anti-bot response, and LLM extraction. Retrying with a heavier browser
does not fix an authorization or policy error.

| Symptom | Check |
|---|---|
| `web.search` returns few results | Is SearXNG reachable at `VERA_SEARXNG_URL`? The chain falls back to Brave (needs a key) and DuckDuckGo |
| `blocked: true` with a `block_reason` | Anti-bot interstitial; the reader fallback should run unless `VERA_WEB_READER` is empty. For Reddit, configure a `web.api` provider |
| `CERTIFICATE_VERIFY_FAILED` against another host | Expected — only Vera's own origin is exempt |
| `browser.*` errors immediately | `browser.health`; install Playwright and Chromium |
| Calls queue behind each other | `BROWSER_MAX_SESSIONS` limits concurrent pages |
| Crawl stops early | `max_pages` (ceiling 25), cross-host links excluded, duplicates skipped |

---

## See also

- [Research System](./07-research.md) — the heavier pipeline `web.*` was factored out of
- [Data Fabric](./06-data-fabric.md) — crawl/ingest target and discovery recall
- [Operator](./34-operator.md) — policy-governed web UI automation
- [Integrations](./23-integrations.md) — API access to services Vera manages
- [Configuration](./10-configuration.md) — search and browser env vars

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->

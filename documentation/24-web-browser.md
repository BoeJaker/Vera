# 24 · Web & Browser

> **Doc status:** concise reference for the `web/` module. Expand as the surface grows.

The `web/` package gives Vera two tiers of web access: lightweight **search/fetch/crawl** caps (`web.*`) and full **headless-browser automation** via Playwright (`browser.*`).

---

## 1. `web.*` — search, fetch, crawl

`web_capabilities.py` pulls the "go look something up" logic out of the research pipeline so it's a first-class registry citizen that dream cycles, agentic chats, and IDE chats can all call cheaply.

| Cap | Purpose |
|---|---|
| `web.search` | One query → result list (engine fallback chain) |
| `web.fetch` | One URL → cleaned text + metadata |
| `web.crawl` | One seed URL → recursive walk, optional fabric ingest |
| `web.search_and_crawl` | Composite: search → take top N → fetch each → ingest |

**Routing.** All engines share one dispatcher with a fallback chain `searxng → brave → ddg`; force one with `engine="…"`. SearXNG host from `VERA_SEARXNG_URL` (default `http://<BACKEND_HOST>:8888`), Brave key from `BRAVE_API_KEY`. Timeouts are aggressive (8 s default) — these caps are meant to be responsive, not exhaustive. Crawled content can be ingested into the [Data Fabric](./06-data-fabric.md).

---

## 2. `browser.*` — Playwright automation

`browser_capabilities.py` is autonomous web navigation and page interaction. Most caps return a screenshot so an agent can "see" the result of its action.

| Cap | Purpose |
|---|---|
| `browser.screenshot` | Full-page PNG of a URL |
| `browser.content` | Extract text, links, metadata |
| `browser.click` / `browser.type` / `browser.scroll` / `browser.select` | Interact with elements, return screenshot |
| `browser.navigate` | Multi-step autonomous navigation session |
| `browser.search` | Search via a search engine, return results |
| `browser.extract` | **LLM**: extract structured data from a page |
| `browser.monitor` | Watch a URL for changes (polls at interval) |
| `browser.pdf` | Convert a URL to PDF (base64) |
| `browser.health` | Playwright / browser availability |

```
pip install playwright && playwright install chromium
```

Config: `BROWSER_HEADLESS`, `BROWSER_TIMEOUT_MS`, `BROWSER_VIEWPORT_W/H`, `BROWSER_USER_AGENT`, `BROWSER_MAX_SESSIONS`, `BROWSER_SCREENSHOT_Q`. The `browser.*` caps compose naturally in a [DAG](./03-dag-engine.md) (search → content → extract).

---

## See also

- [Research System](./07-research.md) — the heavier pipeline `web.*` was factored out of
- [Data Fabric](./06-data-fabric.md) — crawl/ingest target
- [Configuration](./10-configuration.md) — search + browser env vars

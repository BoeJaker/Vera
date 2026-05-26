```python
import time
import json
import math
import hashlib
import logging
import statistics
from typing import Dict, Any, List
from urllib.parse import urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

# --- CONFIG ---
USER_AGENT = "ReconTool/2.0"
HEADERS = {"User-Agent": USER_AGENT}
MAX_WORKERS = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# --- UTILS ---
def safe_json(r):
    try:
        return r.json()
    except:
        return None


def deep_structure(obj):
    if isinstance(obj, dict):
        return {k: deep_structure(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [deep_structure(obj[0])] if obj else []
    else:
        return type(obj).__name__


def hash_structure(data):
    try:
        return hashlib.sha256(json.dumps(deep_structure(data)).encode()).hexdigest()
    except:
        return None


def entropy(values):
    freq = {}
    for v in values:
        freq[v] = freq.get(v, 0) + 1
    total = len(values)
    return -sum((c / total) * math.log2(c / total) for c in freq.values()) if total else 0


# --- STATIC SCOPING ---
def extract_links(base_url: str) -> List[str]:
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        domain = urlparse(base_url).netloc

        return list({
            urljoin(base_url, a["href"])
            for a in soup.find_all("a", href=True)
            if urlparse(urljoin(base_url, a["href"])).netloc == domain
        })
    except:
        return []


# --- PLAYWRIGHT DISCOVERY ---
def discover_endpoints(url: str, duration=8):
    from playwright.sync_api import sync_playwright

    endpoints = {}
    session_headers = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            try:
                req = response.request
                ct = response.headers.get("content-type", "")

                if "json" in ct or req.resource_type in ["xhr", "fetch"]:
                    u = req.url
                    if u not in endpoints:
                        endpoints[u] = {"hits": 0, "types": set(), "sizes": []}

                    endpoints[u]["hits"] += 1
                    endpoints[u]["types"].add(ct)

                    try:
                        endpoints[u]["sizes"].append(len(response.body()))
                    except:
                        pass
            except:
                pass

        page.on("response", handle_response)
        page.goto(url, wait_until="networkidle")
        time.sleep(duration)

        # extract session
        cookies = context.cookies()
        cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        session_headers = {
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header
        }

        browser.close()

    return endpoints, session_headers


# --- PARAM MUTATION ---
def mutate_params(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    mutations = []
    for k in params:
        for v in ["1", "2", "10"]:
            new_params = params.copy()
            new_params[k] = v
            query = "&".join(f"{x}={y}" for x, y in new_params.items())
            mutations.append(f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query}")

    return mutations[:5]


# --- CLASSIFIER ---
def classify(url):
    if any(x in url for x in ["analytics", "track"]):
        return "telemetry"
    if "page=" in url or "limit=" in url:
        return "pagination"
    if "auth" in url:
        return "auth"
    return "data"


# --- RELIABILITY ---
def probe(url, headers):
    try:
        start = time.time()
        r = requests.get(url, headers=headers, timeout=10)
        latency = time.time() - start

        if r.status_code != 200:
            return None

        data = safe_json(r)
        if data is None:
            return None

        return {
            "hash": hashlib.sha256(json.dumps(data).encode()).hexdigest(),
            "structure": hash_structure(data),
            "latency": latency,
            "size": len(r.content)
        }
    except:
        return None


def test_endpoint(url, headers, iterations=8):
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(probe, url, headers) for _ in range(iterations)]
        for f in futures:
            r = f.result()
            if r:
                results.append(r)

    if not results:
        return {}

    hashes = [r["hash"] for r in results]

    return {
        "success_rate": len(results) / iterations,
        "content_variation": len(set(hashes)),
        "structure_variation": len(set(r["structure"] for r in results)),
        "avg_latency": statistics.mean(r["latency"] for r in results),
        "entropy": entropy(hashes)
    }


# --- SCORING ---
def score(meta, url):
    s = 0
    if meta["hits"] > 1:
        s += 2
    if any("json" in t for t in meta["types"]):
        s += 2
    if "api" in url:
        s += 1
    return s


# --- PIPELINE ---
def run(target: str):
    logging.info(f"Target: {target}")

    links = extract_links(target)
    logging.info(f"Links found: {len(links)}")

    endpoints, session = discover_endpoints(target)
    logging.info(f"Endpoints discovered: {len(endpoints)}")

    scored = sorted(
        [(u, m, score(m, u)) for u, m in endpoints.items()],
        key=lambda x: x[2],
        reverse=True
    )

    results = []

    for url, meta, sc in scored[:10]:
        rel = test_endpoint(url, session)

        results.append({
            "url": url,
            "score": sc,
            "class": classify(url),
            "params": list(parse_qs(urlparse(url).query).keys()),
            "reliability": rel,
            "mutations": mutate_params(url)
        })

    return {
        "target": target,
        "links": len(links),
        "endpoints": len(endpoints),
        "results": results
    }


if __name__ == "__main__":
    target = input("Target URL: ").strip()
    report = run(target)
    print(json.dumps(report, indent=2))
```

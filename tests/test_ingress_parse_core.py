"""Parse once (vera/fabric/ingress_parse_core.py; web-ingress WI-4): one ParsedDoc per body over the fixture site,
the record both the crawl and the page ingest build from it, the dedupe decision, and the deferred-enrichment job."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

from vera.fabric import ingress_core as ic  # noqa: E402
from vera.fabric import ingress_parse_core as pc  # noqa: E402
import fixture_site as site  # noqa: E402


def _html(path):
    return site.respond("GET", path).body.decode()


def test_a_page_parses_once_into_the_doc_every_consumer_needs():
    d = pc.parse_html(_html("/a"), "http://fixture.test/a")
    assert d["schema"] == "ParsedDoc/1" and d["reader"] == "html" and d["kind"] == "page"
    assert d["title"] == "Lab space in Cambridge" and d["lang"] == "en"
    assert "vacancy in Cambridge fell to 2.1 per cent" in d["text"] and "<" not in d["text"]
    assert d["headings"] == [{"level": 1, "text": "Lab space in Cambridge"}]
    assert [l["url"] for l in d["links"]] == ["http://fixture.test/b", "http://fixture.test/c"]
    assert d["content_hash"] == ic.content_hash(d["text"]) and d["url_key"] == "http://fixture.test/a"
    assert d["word_count"] > 20 and d["nav_only"] is False
    assert json.loads(json.dumps(d)) == d                                  # cacheable as JSON


def test_structured_data_and_tables_are_captured():
    c = pc.parse_html(_html("/c"), "http://fixture.test/c")
    kinds = {(s["source"], s["type"]) for s in c["structured"]}
    assert ("json-ld", "NewsArticle") in kinds and ("__NEXT_DATA__", "") in kinds
    nxt = next(s for s in c["structured"] if s["source"] == "__NEXT_DATA__")
    assert nxt["data"]["buyer"] == "Fixture REIT"
    assert "Fixture REIT" not in c["text"]                                  # scripts never leak into the text
    b = pc.parse_html(_html("/b"), "http://fixture.test/b")
    assert b["tables"] == [{"rows": 3}] and "Cambridge 52.5" in b["text"]


def test_the_same_content_under_two_urls_hashes_the_same_and_the_near_dup_does_not():
    a = pc.parse_html(_html("/a"), "http://fixture.test/a")
    a2 = pc.parse_html(_html("/a?utm_source=newsletter"), "http://fixture.test/a?utm_source=newsletter")
    copy = pc.parse_html(_html("/a-copy"), "http://fixture.test/a-copy")
    assert a["content_hash"] == a2["content_hash"] and a["url_key"] == a2["url_key"]
    assert a["content_hash"] != copy["content_hash"]
    assert ic.near_duplicate_text(a["text"], copy["text"])                   # short text: shingle Jaccard
    b = pc.parse_html(_html("/b"), "http://fixture.test/b")
    assert not ic.near_duplicate_text(a["text"], b["text"])
    assert ic.shingle_jaccard(a["text"], copy["text"]) > 0.8 and ic.shingle_jaccard(a["text"], b["text"]) < 0.2


def test_chrome_only_pages_fall_back_to_the_body_and_read_as_nav_only():
    d = pc.parse_html("<html><body><nav><a href='/x'>Home</a> <a href='/y'>About</a></nav><main>Hi</main></body></html>",
                      "https://h.example/")
    assert d["text"] and d["nav_only"] is True
    t = pc.parse_text("city,vacancy_pct\nCambridge,2.1\n", "https://h.example/d.csv", reader="csv")
    assert t["reader"] == "csv" and t["kind"] == "file" and t["word_count"] == 2 and t["links"] == []


def test_crawl_record_has_the_shape_the_crawl_wrote_by_hand():
    d = pc.parse_html(_html("/a"), "http://fixture.test/a")
    r = pc.crawl_record(d, "p_abc", "http://fixture.test/a", ["web", "discovery"], hostname="fixture.test", depth=1)
    assert r["id"] == "p_abc" and r["url"] == "http://fixture.test/a" and r["depth"] == 1 and r["hostname"] == "fixture.test"
    assert r["headings"] == ["Lab space in Cambridge"] and r["links"][0] == {"url": "http://fixture.test/b", "anchor": "/b"}
    assert r["content_hash"] == d["content_hash"] and r["link_count"] == 2 and r["tags"] == ["web", "discovery"]


def test_dedupe_decision_skips_only_the_same_record_and_links_others():
    assert pc.dedupe_decision("h", None, "ds", "p_1") == {"action": "ingest", "canonical": ""}
    assert pc.dedupe_decision("h", {"record_id": None}, "ds", "p_1")["action"] == "ingest"
    assert pc.dedupe_decision("h", {"record_id": "p_1"}, "ds", "p_1") == {"action": "skip", "canonical": "p_1"}
    assert pc.dedupe_decision("h", {"record_id": "p_other"}, "ds", "p_1") == {"action": "link", "canonical": "p_other"}
    assert pc.dedupe_decision("", {"record_id": "p_1"}, "ds", "p_1")["action"] == "ingest"


def test_t3_job_shape():
    j = pc.t3_job("topic.x", [{"rid": "p_1", "ck": "web", "url": "https://h/1"}, {"rid": "", "ck": "web"},
                              {"rid": "p_2", "ck": "code", "url": "https://h/2"}], crawl_id="c1", llm_ent=True)
    assert j["kind"] == "discovery.t3" and j["dedupe_key"] == "discovery.t3:c1" and j["total"] == 2
    assert j["payload"]["records"] == [{"rid": "p_1", "ck": "web", "url": "https://h/1"}, {"rid": "p_2", "ck": "code", "url": "https://h/2"}]
    assert j["payload"]["llm_ent"] is True and j["payload"]["dataset_id"] == "topic.x"
    assert "2 pages" in j["title"]


def test_discovery_uses_the_doc_core_the_dedupe_and_the_deferral():
    import ast
    src = (ROOT / "vera" / "fabric" / "discovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ingest = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "discover_ingest_page")
    d = ast.dump(ingest)
    assert "dedupe_decision" in d and "_ingress_lookup" in d and "_ingress_note" in d
    impl = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_crawl_impl")
    di = ast.dump(impl)
    assert "_defer_t3" in di and "_deferred_t3" in di and "_submit_t3" in di and "want_tiers" in di
    assert "register_handler" in src and "T3_KIND" in src
    from vera.minds import switchboard_core as SB
    assert SB.REGISTRY["ingress.dedupe"].default == "off" and SB.REGISTRY["discovery.defer_enrichment"].default == "off"



def test_a_deferred_crawl_runs_one_entity_pass_and_switches_stay_fresh():
    """18 Oct on prod: ingest_dataset's post-ingest pipeline extracted entities for every crawl page as well as the
    crawl's own pass; and a crawl read discovery.defer_enrichment from a cache nothing had refreshed."""
    import ast
    df = (ROOT / "vera" / "fabric" / "data_fabric.py").read_text(encoding="utf-8")
    tree = ast.parse(df)
    ing = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "ingest_dataset")
    assert "post_entities" in [a.arg for a in ing.args.args + ing.args.kwonlyargs]
    assert "extract=post_entities" in df
    post = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_post_ingest_pipeline")
    assert "extract" in [a.arg for a in post.args.args] and 'if extract and cfg.get("auto_extract_entities"' in df
    disc = (ROOT / "vera" / "fabric" / "discovery.py").read_text(encoding="utf-8")
    i = disc.index("if _defer_t3:     # one entity pass")
    assert "post_entities=False" in disc[i:i + 400]
    ing_caps = (ROOT / "vera" / "fabric" / "ingress_capabilities.py").read_text(encoding="utf-8")
    assert 'schedule(_refresh_tick, SWITCH_TTL_S, name="ingress.switches"' in ing_caps and "singleton=False" in ing_caps

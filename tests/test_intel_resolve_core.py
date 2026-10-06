"""Names to Wikidata identities: candidates and facts parsed, evidence scored,
and an abstention whenever the evidence is not clear (shapes from 6 Oct 2026)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_resolve_core as r  # noqa: E402


def _claim(prop_val):
    return {"mainsnak": {"datavalue": {"value": prop_val}}}


SAVILLS = {"claims": {"P31": [_claim({"id": "Q4830453"}), _claim({"id": "Q891723"})],
                      "P856": [_claim("https://www.savills.co.uk"), _claim("https://www.savills.com/")],
                      "P414": [{"mainsnak": {"datavalue": {"value": {"id": "Q171240"}}},
                                "qualifiers": {"P249": [{"datavalue": {"value": "SVS"}}]}}],
                      "P159": [_claim({"id": "Q84"})]}}


def test_parsing():
    c = r.candidates({"search": [{"id": "Q10458056", "label": "Savills", "description": "real estate services provider"},
                                 {"id": "P31", "label": "instance of"}]})
    assert [x["qid"] for x in c] == ["Q10458056"]
    f = r.facts(SAVILLS)
    assert f["domains"] == ["savills.co.uk", "savills.com"] and f["tickers"] == [{"exchange": "Q171240", "ticker": "SVS"}]
    assert "Q891723" in f["types"] and r.brand("www.jll.co.uk") == r.brand("jll.com") == "jll"


def test_a_company_named_by_its_sources_resolves():
    cand = {"qid": "Q10458056", "label": "Savills", "description": "real estate services provider"}
    s = r.score("Savills", "org", cand, r.facts(SAVILLS), source_domains=["savills.co.uk", "bbc.co.uk"], context=3.0)
    assert s["score"] >= 0.8 and s["parts"]["site"] == 1.0
    assert r.choose([dict(s, qid="Q10458056")])["qid"] == "Q10458056"


def test_two_items_for_one_organisation_are_not_ambiguous():
    a = dict(r.score("JLL", "org", {"label": "JLL", "description": "professional services firm"},
                     {"types": ["Q4830453"], "domains": ["jll.com"]}, source_domains=["jll.co.uk"], rank=0, of=5), qid="Q1703389")
    b = dict(r.score("JLL", "org", {"label": "JLL", "description": "company"},
                     {"types": ["Q4830453"], "domains": ["jll.com"]}, source_domains=["jll.co.uk"], rank=1, of=5), qid="Q999")
    got = r.choose([a, b])
    assert got["qid"] == "Q1703389" and "same organisation" in got["note"]


def test_the_wrong_world_is_refused():
    car = {"qid": "Q613883", "label": "Mercury", "description": "automobile marque of the Ford Motor Company"}
    s = r.score("Mercury", "org", car, {"types": [], "domains": ["ford.com"]}, source_domains=["egi.co.uk"], context=-6.0)
    assert s["score"] <= 0.55 and r.choose([dict(s, qid="Q613883")])["qid"] is None


def test_wrong_kind_or_name_and_ambiguity_abstain():
    city = {"label": "Leeds", "description": "city in West Yorkshire, England"}
    assert r.score("Leeds", "org", city, {"types": ["Q515"]})["score"] <= 0.4
    other = {"label": "Acme Records", "description": "record label"}
    assert r.name_fit("Acme", other) == 0.6 and r.score("Zebra", "org", other, None)["score"] <= 0.4
    tie = [{"qid": "Q1", "score": 0.81, "brands": []}, {"qid": "Q2", "score": 0.80, "brands": []}]
    assert r.choose(tie)["qid"] is None and "ambiguous" in r.choose(tie)["why"]
    assert r.choose([])["qid"] is None

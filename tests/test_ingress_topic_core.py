"""Topic maps (vera/fabric/ingress_topic_core.py; web-ingress WI-8a): keys from intel topic ids, member roles,
saturation and gaps, seeds from an intel topic, the summary - and the tables and SQL the capabilities use."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.fabric import ingress_receipts_core as rc  # noqa: E402
from vera.fabric import ingress_topic_core as tc  # noqa: E402


def test_topic_key_is_the_intel_id_or_a_slug_of_the_name():
    assert tc.topic_key("uk-commercial-real-estate-market") == "uk-commercial-real-estate-market"
    assert tc.topic_key("UK Office Vacancy!") == "uk-office-vacancy"
    assert tc.topic_key("", "Lab space in Cambridge") == "lab-space-in-cambridge"
    assert tc.topic_key("", "") == ""


def test_member_roles():
    assert tc.member_role("page", seed=True) == "seed"
    assert tc.member_role("page", inbound=7) == "authority"
    assert tc.member_role("page", outbound=12) == "hub"
    assert tc.member_role("file") == "file" and tc.member_role("weird") == "page" and tc.member_role("api") == "api"


def test_saturation_and_gaps():
    assert tc.saturation(0.05, 5, 0, 0)["state"] == "young"
    assert tc.saturation(0.05, 50, 1, 20)["state"] == "saturated"
    assert tc.saturation(0.4, 50, 10, 20)["state"] == "yielding"
    s = tc.saturation(None, 50, 1, 20)
    assert s["state"] == "saturated" and s["recent_new_share"] == 0.05       # falls back to the recent share
    assert tc.saturation(None, 50, 0, 0)["state"] == "unknown"
    assert tc.gaps({"page": 12, "file": 2}) == ["dataset", "api", "feed"]
    assert tc.gaps({k: 1 for k in tc.KINDS_FOR_GAPS}) == []


def test_seeds_from_an_intel_topic():
    t = {"queries": ["lab space cambridge", "lab space cambridge", ""], "questions": ["who is building?"],
         "sites": ["www.Savills.co.uk/research", "https://bidwells.co.uk/"], "entities": ["Savills", "Bidwells"]}
    s = tc.seeds_from_intel_topic(t)
    assert s["queries"] == ["lab space cambridge", "who is building?"]
    assert s["seeds"] == ["https://savills.co.uk/research", "https://bidwells.co.uk/"]
    assert s["entities"] == ["Savills", "Bidwells"]


def test_summary_counts_hosts_kinds_roles_and_saturation():
    topic = {"topic_key": "t", "name": "T", "intel_topic_id": "t", "dataset_ids": ["topic.t"], "kb_ids": [],
             "worldview_concepts": ["c1"]}
    members = [{"url_key": "https://a.example/1", "kind": "page", "role": "seed", "novelty": 1.0, "recent": True},
               {"url_key": "https://a.example/2", "kind": "page", "role": "page", "novelty": 0.0, "recent": True},
               {"url_key": "https://b.example/f.pdf", "kind": "file", "role": "file", "novelty": None, "recent": False}]
    s = tc.summary(topic, members, {"a.example": 0.3, "b.example": None})
    assert s["members"] == 3 and s["hosts"] == 2 and s["by_kind"] == {"page": 2, "file": 1}
    assert s["by_role"] == {"seed": 1, "page": 1, "file": 1} and s["top_hosts"][0] == ("a.example", 2)
    assert s["linked"] == {"datasets": ["topic.t"], "kbs": [], "worldview_concepts": ["c1"]}
    assert s["saturation"]["state"] == "young" and s["gaps"] == ["dataset", "api", "feed"]


def test_the_tables_and_sql_exist():
    ddl = " ".join(rc.DDL)
    assert "web_topic (" in ddl and "web_topic_members (" in ddl and "web_topic" in rc.MAP_TABLES
    assert set(rc.LINK_TOPIC) == {"dataset", "kb", "concept"}
    assert "ON CONFLICT (topic_key, url_key)" in rc.UPSERT_MEMBER and "$7" in rc.UPSERT_MEMBER
    assert "INTERVAL '7 days'" in rc.SELECT_MEMBERS

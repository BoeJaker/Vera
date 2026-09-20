"""Machine chatter must not cost what real text costs (vera/fabric/embed_policy_core.py).

The incident, 2026-09-20. Embeds are pinned to the CPU nodes (deny_gpu, on
purpose), and those nodes are LXC containers sharing one host. Over a 3.3-hour
window, 6,946 of 11,755 seconds of embedding — 59% — went on Home Assistant
entity rows pushed in by an n8n `ha-sync` workflow:

    binary_sensor.boejaker360_in_game binary_sensor off Boejaker360 2026-09-20T…

while the same node took 113–236s to embed a single line of research text.

`EMBED_EXCLUDED_DATASETS` already existed and `ingest_dataset` never consulted
it — only the backfill did. So declaring a dataset excluded stopped the catch-up
job and changed nothing about the per-ingest cost. These pin the decision, and
that both callers make the same one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.fabric import embed_policy_core as ep  # noqa: E402


# ── the case this was built for ──────────────────────────────────────────────

def test_home_assistant_entities_are_excluded_by_default():
    """The dataset that was 59% of all embedding time."""
    assert ep.embedding_excluded("vera.ha.entities",
                                 patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is True


def test_other_home_assistant_datasets_are_excluded_too():
    for did in ("vera.ha.status", "vera.ha.anything.deeper"):
        assert ep.embedding_excluded(did, patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is True, did


def test_real_content_is_still_embedded():
    """Research pages, news and papers are what vector search is FOR — the
    point is to stop paying for machine state, not to stop embedding."""
    for did in ("prebaked_arxiv_ai", "research.web.pages", "vera.notes",
                "mkt.yahoo.btc_usd.1d", "cve.nvd", "vera.hardware.inventory"):
        assert ep.embedding_excluded(did, patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is False, did


def test_a_dataset_merely_containing_ha_is_not_excluded():
    """`vera.hardware` starts with 'ha' — a prefix match would have caught it."""
    assert ep.embedding_excluded("vera.hardware",
                                 patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is False
    assert ep.embedding_excluded("haproxy.logs",
                                 patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is False


# ── the registered set (what producers use) ──────────────────────────────────

def test_the_registered_set_still_excludes():
    assert ep.embedding_excluded("ide.claude_sessions",
                                 exact={"ide.claude_sessions"}) is True


def test_either_source_excluding_is_enough():
    assert ep.embedding_excluded("x.y", exact={"x.y"}, patterns=["nope.*"]) is True
    assert ep.embedding_excluded("nope.z", exact={"x.y"}, patterns=["nope.*"]) is True
    assert ep.embedding_excluded("other", exact={"x.y"}, patterns=["nope.*"]) is False


def test_no_policy_at_all_excludes_nothing():
    assert ep.embedding_excluded("vera.ha.entities") is False


def test_blank_dataset_id_is_not_excluded():
    for bad in ("", "   ", None):
        assert ep.embedding_excluded(bad, patterns=["*"]) is False


def test_matching_is_case_sensitive():
    """Dataset ids are identifiers, not prose."""
    assert ep.embedding_excluded("VERA.HA.ENTITIES",
                                 patterns=ep.DEFAULT_EXCLUDED_PATTERNS) is False


# ── configuring it ───────────────────────────────────────────────────────────

def test_unset_takes_the_defaults():
    assert ep.parse_patterns(None) == list(ep.DEFAULT_EXCLUDED_PATTERNS)


def test_an_empty_value_means_exclude_nothing():
    """Falling back to the defaults here would make the policy impossible to
    switch off, which is worse than it being on."""
    for raw in ("", "   ", ","):
        assert ep.parse_patterns(raw) == [], repr(raw)
        assert ep.embedding_excluded("vera.ha.entities",
                                     patterns=ep.parse_patterns(raw)) is False


def test_patterns_parse_from_commas_spaces_and_newlines():
    for raw in ("a.*,b.*", "a.* b.*", "a.*\nb.*", " a.* , b.* "):
        assert ep.parse_patterns(raw) == ["a.*", "b.*"], repr(raw)


def test_an_operator_list_replaces_the_defaults_rather_than_adding():
    """Otherwise 'exclude only X' would silently still exclude HA."""
    pats = ep.parse_patterns("mkt.*")
    assert ep.embedding_excluded("mkt.yahoo.gld.1d", patterns=pats) is True
    assert ep.embedding_excluded("vera.ha.entities", patterns=pats) is False

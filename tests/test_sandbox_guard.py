"""Tests for the dev-sandbox write guard. The critical invariant: the guard is
a STRICT no-op in prod (VERA_IS_DEV_SANDBOX unset) and only ever suppresses
writes inside a sandbox — never the reverse."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.sandbox_guard import (  # noqa: E402
    is_dev_sandbox, write_blocked, is_write_cypher,
)


def test_prod_is_never_blocked():
    # No VERA_IS_DEV_SANDBOX → prod → guard inert regardless of other flags.
    assert write_blocked({}) is False
    assert write_blocked({"VERA_SANDBOX_WRITE_GUARD": "1"}) is False
    assert is_dev_sandbox({}) is False


def test_dev_sandbox_blocks_by_default():
    assert is_dev_sandbox({"VERA_IS_DEV_SANDBOX": "1"}) is True
    assert write_blocked({"VERA_IS_DEV_SANDBOX": "1"}) is True
    assert write_blocked({"VERA_IS_DEV_SANDBOX": "true"}) is True


def test_dev_sandbox_escape_hatch_disables_guard():
    # A dev deliberately allowing writes (e.g. seeding a mirror).
    env = {"VERA_IS_DEV_SANDBOX": "1", "VERA_SANDBOX_WRITE_GUARD": "0"}
    assert is_dev_sandbox(env) is True      # still a sandbox
    assert write_blocked(env) is False      # but writes allowed
    assert write_blocked({"VERA_IS_DEV_SANDBOX": "1",
                          "VERA_SANDBOX_WRITE_GUARD": "off"}) is False


def test_write_cypher_detection():
    assert is_write_cypher("MERGE (n:X {id:$id}) SET n.a=1") is True
    assert is_write_cypher("CREATE (n:X)") is True
    assert is_write_cypher("MATCH (a)-[r]->(b) DETACH DELETE a") is True
    assert is_write_cypher("MATCH (n:X {id:$id}) REMOVE n.tag") is True


def test_read_cypher_passes_through():
    assert is_write_cypher("MATCH (n:X) RETURN n") is False
    assert is_write_cypher("MATCH (a)-[r]->(b) RETURN a,r,b") is False
    assert is_write_cypher("RETURN 1") is False
    assert is_write_cypher("") is False


def test_setting_word_inside_identifier_not_misclassified():
    # 'RESET' / 'ASSET' contain 'SET' as a substring but not as a word.
    assert is_write_cypher("MATCH (n) WHERE n.asset = 1 RETURN n") is False
    assert is_write_cypher("MATCH (n) WHERE n.preset='x' RETURN n") is False

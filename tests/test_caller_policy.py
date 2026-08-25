import pytest

from vera.fabric.caller_policy import CallerPolicy


pytestmark = pytest.mark.critical


def test_actions_are_explicitly_classified():
    policy = CallerPolicy(write_actions={"artifact.put", "artifact.reference"})
    assert policy.allows("artifact.get", "codex", {})
    assert not policy.allows("artifact.put", "codex", {})
    assert policy.allows("artifact.put", "user", {})


def test_parse_rejects_unknown_actor_and_non_object():
    with pytest.raises(ValueError):
        CallerPolicy.parse('{"writers":["root"]}', write_actions={"write"})
    with pytest.raises(ValueError):
        CallerPolicy.parse("[]", write_actions={"write"})

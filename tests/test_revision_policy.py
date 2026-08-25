import pytest

from vera.fabric.revision_policy import RevisionPolicy


pytestmark = pytest.mark.critical


def test_default_policy_is_readable_but_human_write_only():
    policy = RevisionPolicy()
    assert policy.allows("revision.read", "autonomous", {})
    assert policy.allows("revision.write", "user", {})
    assert not policy.allows("revision.write", "codex", {})


def test_namespace_rules_override_global_rules():
    policy = RevisionPolicy({
        "readers": ["user"], "writers": ["user"],
        "namespaces": {"agents": {"readers": ["codex"],
                                      "writers": ["codex"]}},
    })
    resource = {"namespace": "agents"}
    assert policy.allows("revision.read", "codex", resource)
    assert policy.allows("revision.write", "codex", resource)
    assert not policy.allows("revision.write", "user", resource)


@pytest.mark.parametrize("raw", ["[]", "not-json", '{"writers":"user"}',
                                  '{"writers":["root"]}'])
def test_invalid_policy_fails_closed(raw):
    with pytest.raises(ValueError):
        RevisionPolicy.from_json(raw)

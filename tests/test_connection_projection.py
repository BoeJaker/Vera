import pytest

from Vera.vera.integrations.connection_projection import project_connections


pytestmark = pytest.mark.critical


def _fixture():
    return project_connections(
        integrations=[{
            "id": "svc-1", "label": "Service", "kind": "generic",
            "base_url": "https://user:pass@example.test:443/api?token=secret#fragment",
            "access": {"embed": True, "api": False},
            "api": {"has_auth": True}, "conn_id": "provider-1",
        }],
        accounts=[{
            "id": "acct-1", "label": "Mail", "mail_enabled": True,
            "imap_host": "mail.example.test", "imap_port": 993,
            "smtp_host": "mail.example.test", "smtp_port": 587,
            "has_app_password": True,
        }],
        providers=[{
            "id": "provider-1", "label": "Model", "kind": "openai",
            "base_url": "https://example.test/v1?api_key=secret",
            "enabled": True, "has_key": False, "env_key": True,
        }])


def test_projection_is_stable_redacted_and_non_authoritative():
    first = _fixture()
    second = _fixture()
    assert first == second
    assert first["schema"] == "vera.connection-projection/v1"
    assert first["authorizes"] is False
    assert first["activates"] is False
    assert first["merges_records"] is False
    rendered = str(first)
    assert "user:pass" not in rendered
    assert "token=secret" not in rendered
    assert "api_key=secret" not in rendered


def test_explicit_reference_resolves_without_merging_authority():
    projection = _fixture()
    assert projection["links"] == [{
        "source": "connection:integration:svc-1", "relation": "references",
        "target": "connection:provider:provider-1", "state": "resolved",
        "target_ref_sha256": projection["links"][0]["target_ref_sha256"],
    }]
    assert projection["collisions"][0]["merged"] is False


def test_shared_endpoint_is_reported_as_collision_not_auto_merged():
    projection = _fixture()
    assert projection["collisions"][0] == {
        "endpoint": "https://example.test",
        "connection_ids": ["connection:integration:svc-1",
                           "connection:provider:provider-1"],
        "merged": False,
    }
    collision = project_connections(
        integrations=[{"id": "one", "base_url": "https://same.test/api",
                       "access": {"embed": True}}],
        providers=[{"id": "two", "base_url": "https://same.test/api"}])
    assert collision["collisions"][0]["connection_ids"] == [
        "connection:integration:one", "connection:provider:two"]
    assert collision["collisions"][0]["merged"] is False


def test_unsafe_source_id_is_hashed_instead_of_echoed():
    projection = project_connections(accounts=[{"id": "token with spaces"}])
    rendered = str(projection)
    assert "token with spaces" not in rendered
    assert projection["connections"][0]["source"]["record_id"].startswith("sha256-")


def test_unresolved_reference_and_invalid_endpoint_are_explicit_gaps():
    projection = project_connections(integrations=[{
        "id": "one", "base_url": "https://user@:bad/path?secret=x",
        "conn_id": "missing", "access": {},
    }])
    assert {gap["code"] for gap in projection["gaps"]} == {
        "invalid_endpoint", "explicit_link_unresolved"}
    assert projection["links"][0]["target"] == ""


def test_duplicate_source_identity_and_unbounded_input_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        project_connections(accounts=[{"id": "same"}, {"id": "same"}])
    with pytest.raises(ValueError, match="500"):
        project_connections(providers=[{"id": str(index)} for index in range(501)])


def test_source_availability_is_bound_into_projection_identity():
    complete = project_connections()
    incomplete = project_connections(available_sources={"integration"})
    assert complete["complete"] is True
    assert incomplete["complete"] is False
    assert incomplete["sources"]["account"] == {"available": False, "records": 0}
    assert complete["projection_id"] != incomplete["projection_id"]

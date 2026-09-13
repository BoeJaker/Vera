import ast
import json
from pathlib import Path

import pytest

import Vera.vera.commerce.commerce_effects as effects


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def _plan(**overrides):
    arguments = {
        "account_ref": "account-secret-id",
        "listing_ref": "listing-secret-id",
        "provider": "eBay",
        "mode": "publish",
        "idempotency_key": "private-idempotency-key",
        "approval_receipt_ref": "private-approval-receipt",
        "retry": True,
    }
    arguments.update(overrides)
    return effects.plan_marketplace_listing_effect(**arguments)


def test_marketplace_plan_is_stable_payload_free_and_observe_only():
    first = _plan()
    second = _plan()
    assert first == second
    assert first["enforcement"] == "observe_only"
    assert first["delivery"]["mode"] == "publish"
    assert first["delivery"]["provider_idempotency_forwarded"] is False
    assert first["executes"] is False
    assert first["blocks_current_call"] is False
    encoded = json.dumps(first)
    for secret in ("account-secret-id", "listing-secret-id", "ebay",
                   "private-idempotency-key", "private-approval-receipt"):
        assert secret not in encoded.lower()


def test_marketplace_plan_separates_logical_write_modes():
    assert _plan(mode="push")["plan"]["plan_id"] != _plan(mode="archive")["plan"]["plan_id"]


@pytest.mark.parametrize("field", ["account_ref", "listing_ref", "provider"])
def test_marketplace_plan_requires_a_complete_boundary_identity(field):
    with pytest.raises(ValueError):
        _plan(**{field: ""})


def test_marketplace_plan_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported"):
        _plan(mode="delete-everything")


def test_replay_evidence_only_suppresses_the_projected_execution():
    original = _plan(retry=False)
    replayed = effects.apply_replay_evidence(
        original, {"already_succeeded": True, "successful_receipt_id": "receipt:1"})
    assert original["decision"]["would_execute"] is True
    assert replayed["decision"]["would_admit"] is True
    assert replayed["decision"]["would_execute"] is False
    assert replayed["replay"]["would_suppress"] is True


def test_observation_failure_never_blocks_the_native_call(monkeypatch):
    class BrokenEvidence:
        def record(self, _shadow):
            raise OSError("disk unavailable")

    monkeypatch.setattr(effects, "default_external_effect_shadow_evidence",
                        lambda **_kwargs: BrokenEvidence())
    arguments = {"account_ref": "a", "listing_ref": "l",
                 "provider": "ebay", "mode": "push"}
    expected = effects.plan_marketplace_listing_effect(**arguments)
    result = effects.observe_marketplace_listing_effect(**arguments)
    assert result["decision"] == expected["decision"]
    assert result["blocks_current_call"] is False


def test_marketplace_calls_observe_before_opening_credentials_or_calling_provider():
    platforms = (ROOT / "vera" / "commerce" / "commerce_platforms.py").read_text()
    push = platforms[platforms.index("async def cap_listing_push"):
                     platforms.index("async def cap_orders_sync")]
    assert push.index("observe_marketplace_listing_effect(") < push.index(
        '_db_get_account, acct_ref["id"], True') < push.index("conn.push_listing(")

    listings = (ROOT / "vera" / "commerce" / "commerce_listing.py").read_text()
    publish = listings[listings.index("async def cap_listing_publish"):
                       listings.index("async def cap_listing_archive")]
    assert publish.index("observe_marketplace_listing_effect(") < publish.index(
        '_db_get_account, acct_ref["id"], True') < publish.index("conn.publish_listing(")


def test_marketplace_controls_are_not_forwarded_to_connectors():
    for relative in ("commerce_platforms.py", "commerce_listing.py"):
        source = (ROOT / "vera" / "commerce" / relative).read_text()
        assert "idempotency_key=idempotency_key" in source
        assert "approval_receipt_ref=approval_receipt_ref" in source
        assert ".push_listing(acct, product, idempotency_key" not in source
        assert ".publish_listing(acct, prod, opts, idempotency_key" not in source


def test_modified_commerce_modules_parse_as_python():
    for relative in ("commerce_effects.py", "commerce_platforms.py",
                     "commerce_listing.py"):
        source = (ROOT / "vera" / "commerce" / relative).read_text()
        ast.parse(source, filename=relative)

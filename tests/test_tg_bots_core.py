"""tg_bots_core: a list of bots that still reads a single-token config.

Imports lowercase `vera.telegram.tg_bots_core` with the repo root on sys.path so
the WORKTREE copy is exercised.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.telegram import tg_bots_core as core  # noqa: E402

pytestmark = pytest.mark.critical

LEGACY = {"token": "111:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "admin_chat_id": "42", "auto_start": True}


def test_a_legacy_single_token_config_is_the_default_bot():
    bots = core.normalise_bots(LEGACY)
    assert [b["id"] for b in bots] == ["default"]
    assert bots[0]["token"] == LEGACY["token"] and bots[0]["admin_chat_id"] == "42"
    assert core.default_bot(bots)["id"] == "default"


def test_the_default_bot_keeps_its_legacy_keys_and_others_get_a_suffix():
    assert core.key_for("vera:tg:offset", "default") == "vera:tg:offset"
    assert core.key_for("vera:tg:offset", None) == "vera:tg:offset"
    assert core.key_for("vera:tg:offset", "ops") == "vera:tg:offset:ops"
    assert core.key_for("vera:tg:bot_info", "Ops Bot!") == "vera:tg:bot_info:opsbot"


def test_listed_bots_join_the_legacy_one_and_the_list_wins_on_a_clash():
    cfg = dict(LEGACY, bots=[{"id": "ops", "token": "222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                             {"id": "default", "token": "333:ccccccccccccccccccccccccccccccc", "label": "listed"}])
    bots = core.normalise_bots(cfg)
    assert [b["id"] for b in bots] == ["default", "ops"]
    assert bots[0]["token"].startswith("333:"), "the explicit list entry beats the legacy fields"
    assert core.bot_by_id(bots, "ops")["token"].startswith("222:")


def test_a_disabled_or_tokenless_bot_never_becomes_the_default():
    bots = core.normalise_bots({"bots": [{"id": "a", "token": "1:x" + "x" * 30, "enabled": False},
                                         {"id": "b", "token": ""},
                                         {"id": "c", "token": "3:z" + "z" * 30}]})
    assert core.default_bot(bots)["id"] == "c"
    assert [b["id"] for b in core.enabled_bots(bots)] == ["c"]
    assert core.default_bot([]) is None


def test_merge_adds_updates_and_removes_by_id_without_resending_the_token():
    cur = [{"id": "ops", "token": "T", "admin_chat_id": "1", "enabled": True}]
    out = core.merge_bots(cur, [{"id": "ops", "enabled": False}, {"id": "alerts", "token": "U"}])
    by = {b["id"]: b for b in out}
    assert by["ops"]["token"] == "T" and by["ops"]["enabled"] is False
    assert by["alerts"]["token"] == "U"
    assert [b["id"] for b in core.merge_bots(out, '[{"id": "ops", "remove": true}]')] == ["alerts"]
    assert core.merge_bots(cur, "not json") == cur
    assert core.merge_bots(cur, 42) == cur


def test_redacted_config_masks_every_token():
    cfg = dict(LEGACY, bots=[{"id": "ops", "token": "222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}])
    red = core.redacted_config(cfg)
    assert "aaaaaaaaaaaaaaaa" not in str(red) and "bbbbbbbbbbbbbbbb" not in str(red)
    assert red["token_set"] is True
    assert {b["id"]: b["token_set"] for b in red["bots"]} == {"default": True, "ops": True}


def test_the_capability_module_is_wired_for_more_than_one_bot():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "vera", "telegram", "telegram_capabilities.py"), encoding="utf-8").read()
    assert "async def _poll_loop(bot: Dict[str, Any])" in src
    assert "_bots.key_for(KEY_OFFSET, bot_id)" in src
    assert "_bots.key_for(KEY_BOT_INFO, bid)" in src
    assert "fabric = _sys.modules.get(\"data_fabric\")" in src, "the tuple typo that silenced fabric ingest"
    assert "_sys,modules" not in src

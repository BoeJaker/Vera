"""Every module's credentials in the Platforms store (data-connections phases 3-4, owner 2026-10-11).

A module keeps "@secret:<key>" where it used to keep a sealed value: saves go through vault()/keep(), reads through
open_value()/open_value_sync() - which still open a value sealed the old way - and an adopter moves old sealed
values when platform.secrets.adopt runs (dry run first).
"""
import asyncio
import os
import re
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.platforms import platform_access as PA  # noqa: E402


def run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def fake_registry():
    """A registry whose platform.secrets.list func carries the platforms module's in-process functions."""
    store = {}

    async def _store_secret(k, v, label=""):
        store[k] = v
        return {"key": k}, ""

    async def secret_value(ref):
        return store.get(str(ref).replace("@secret:", ""), "")

    def secret_value_sync(ref):
        return store.get(str(ref).replace("@secret:", ""), "")

    async def platform_secrets_list():
        return {}
    platform_secrets_list.__globals__  # noqa: B018 - a real function; patch its globals below
    g = {"_store_secret": _store_secret, "secret_value": secret_value, "secret_value_sync": secret_value_sync}
    fn = types.FunctionType(platform_secrets_list.__code__, g, "cap_secrets_list")
    return {"platform.secrets.list": {"func": fn}}, store


class FakeSecrets:
    @staticmethod
    def is_sealed(v):
        return isinstance(v, str) and v.startswith("fernet:")

    @staticmethod
    def seal(v):
        return "fernet:" + v

    @staticmethod
    def open_secret(v):
        return v[7:] if isinstance(v, str) and v.startswith("fernet:") else v


def test_keep_vaults_new_values_and_leaves_refs_and_old_seals(monkeypatch):
    monkeypatch.setattr(PA, "_vsecrets", lambda: FakeSecrets)
    reg, store = fake_registry()
    assert run(PA.keep(reg, "ha_token", "TOK", "HA token")) == "@secret:ha_token" and store == {"ha_token": "TOK"}
    assert run(PA.keep(reg, "x", "@secret:other")) == "@secret:other"
    assert run(PA.keep(reg, "x", "fernet:OLD")) == "fernet:OLD"                    # the adopter moves those
    assert run(PA.keep({}, "x", "NEW")) == "fernet:NEW"                            # no store loaded: sealed as before
    assert run(PA.keep(reg, "x", "")) == ""


def test_adopt_and_open_read_both_kinds(monkeypatch):
    monkeypatch.setattr(PA, "_vsecrets", lambda: FakeSecrets)
    reg, store = fake_registry()
    assert run(PA.adopt_value(reg, "mcp_gh_token", "fernet:GH")) == "@secret:mcp_gh_token" and store["mcp_gh_token"] == "GH"
    assert run(PA.adopt_value({}, "k", "fernet:GH")) == "fernet:GH"                # no store: left as it was
    assert PA.open_any_sync(reg, "@secret:mcp_gh_token") == "GH"
    assert PA.open_any_sync(reg, "fernet:OLD") == "OLD"
    assert run(PA.open_any(reg, "@secret:mcp_gh_token")) == "GH"
    assert PA.needs_adopting("fernet:x") and not PA.needs_adopting("@secret:x") and not PA.needs_adopting("")
    assert PA.key_for("Web API", "My-Reddit", "client_secret") == "web_api_my_reddit_client_secret"


MODULES = {
    "web_api": "vera/web/web_api_capabilities.py",
    "mcp_catalog": "vera/mcp/mcp_catalog_capabilities.py",
    "godseye": "vera/godseye/godseye_capabilities.py",
    "integrations": "vera/integrations/integrations_capabilities.py",
    "ha": "vera/homeassistant/ha_capabilities.py",
    "n8n": "vera/n8n/n8n_capabilities.py",
    "providers": "vera/providers/providers_capabilities.py",
    "portainer": "vera/remote/portainer_capabilities.py",
    "prometheus": "vera/remote/metrics_capabilities.py",
    "proxmox": "vera/proxmox/proxmox_capabilities.py",
    "ssh_enrol": "vera/provisioning/enroll_capabilities.py",
    "accounts": "vera/accounts/accounts_capabilities.py",
    "telegram": "vera/telegram/telegram_capabilities.py",
    "foundry": "vera/foundry/foundry_capabilities.py",
}
# sync writers: the old seal stays on save (no await there) and the adopter moves it; reads accept references
SYNC_WRITERS = {
    "remote_ide": "vera/ide/ide_remote_capabilities.py",
    "vscode": "vera/ide/vscode_capabilities.py",
    "ssh_exec": "vera/execution/exec_capabilities.py",
    "commerce": "vera/commerce/commerce_platforms.py",
}


def test_sync_writers_read_references_and_register_an_adopter():
    for name, rel in SYNC_WRITERS.items():
        s = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert f'register_adopter("{name}", _adopt)' in s, name
        assert re.search(r"_pa\(\)\.(open_value_sync|open_any_sync|is_ref)\(", s), name


def test_commerce_keeps_a_reference_while_its_value_is_unchanged():
    s = open(os.path.join(ROOT, SYNC_WRITERS["commerce"]), encoding="utf-8").read()
    assert "_store_form(" in s and "if _pa().is_ref(raw) and new == _open_field(raw):" in s


def test_ssh_exec_adopts_decoded_values_not_xor_text():
    s = open(os.path.join(ROOT, SYNC_WRITERS["ssh_exec"]), encoding="utf-8").read()
    body = s[s.index("async def _adopt(dry_run: bool):"):s.index('_pa().register_adopter("ssh_exec"')]
    assert "plain = _deobfuscate(v)" in body and "adopt_value" not in body


def test_telegram_mask_shows_no_secret_characters():
    sys.path.insert(0, ROOT)
    from vera.telegram import tg_bots_core as T
    tok = "123456789:AAH-secretpartXYZW"
    shown = T.redact_token(tok)
    assert shown.startswith("123456789:") and "XYZW" not in shown and "AAH" not in shown


def test_each_module_saves_to_the_store_reads_both_and_registers_an_adopter():
    for name, rel in MODULES.items():
        s = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert f'register_adopter("{name}", _adopt)' in s, name
        assert re.search(r"_pa\(\)\.(keep|vault)\(", s), name                        # saves into the store
        assert re.search(r"_pa\(\)\.(open_any|open_value_sync|open_any_sync|open_value)\(", s), name


def test_godseye_opens_with_open_secret_not_a_missing_function():
    s = open(os.path.join(ROOT, MODULES["godseye"]), encoding="utf-8").read()
    assert "vsecrets.open(" not in s and "vsecrets.open_secret(v)" in s

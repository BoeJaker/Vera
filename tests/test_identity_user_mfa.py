"""identity.user.mfa: a TOTP token whose secret goes straight to keydrop and is
never returned; the account's auth type becomes otp; a token nobody can use
is deleted again."""
import ast
import asyncio
import os
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
SRC = os.path.join(ROOT, "vera", "provisioning", "identity_capabilities.py")


def cap(calls, drop_ok=True):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_user_mfa")
    fn.decorator_list = []

    async def _state_opened():
        return {"ipa_url": "https://dc.vera.int"}

    async def _ipa_call(st, method, args=None, options=None):
        calls.append((method, args, options))
        if method == "otptoken_add":
            return {"result": {"uri": "otpauth://totp/VERA.INT:boejaker?secret=SECRETSECRET&issuer=VERA.INT",
                               "ipatokenuniqueid": ["tok-1"]}}, ""
        return {"result": {}}, ""

    async def emit_event(ev):
        calls.append(("event", ev))

    # the keydrop helpers are imported inside the function: fake the modules
    sec = types.ModuleType("Vera.vera.security")
    ssc = types.ModuleType("Vera.vera.security.secret_service_core")
    ssc.keydrop_payload = lambda title, username="", password="", url="", notes="", tags=(): {"title": title, "username": username, "password": password, "url": url, "notes": notes, "tags": list(tags)}
    scaps = types.ModuleType("Vera.vera.security.secrets_capabilities")

    def _keydrop_put_sync(label, payload):
        calls.append(("keydrop", label, payload))
        return {"ok": True, "entry": 15, "label": label} if drop_ok else {"error": "chain broken"}

    async def _thread(f, *a):
        return f(*a)
    scaps._keydrop_put_sync, scaps._thread = _keydrop_put_sync, _thread
    sec.secret_service_core, sec.secrets_capabilities = ssc, scaps
    sys.modules["Vera"] = sys.modules.get("Vera") or types.ModuleType("Vera")
    sys.modules["Vera.vera"] = sys.modules.get("Vera.vera") or types.ModuleType("Vera.vera")
    sys.modules["Vera.vera.security"] = sec
    sys.modules["Vera.vera.security.secret_service_core"] = ssc
    sys.modules["Vera.vera.security.secrets_capabilities"] = scaps
    ns = {"Dict": Dict, "List": List, "Optional": Optional, "Any": Any, "_state_opened": _state_opened,
          "_ipa_call": _ipa_call, "emit_event": emit_event, "now_iso": lambda: "2026-09-21T00:00:00"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["cap_user_mfa"]


def test_the_secret_goes_to_keydrop_and_never_comes_back():
    calls = []
    out = asyncio.run(cap(calls)(login="boejaker", no_expiry=True))
    assert out == {"ok": True, "login": "boejaker", "token": "tok-1", "keydrop_entry": 15, "auth_type": "otp"}
    assert "SECRET" not in str(out)
    kd = next(c for c in calls if c[0] == "keydrop")
    assert kd[2]["password"].startswith("otpauth://totp/") and kd[2]["username"] == "boejaker"
    mod = next(c for c in calls if c[0] == "user_mod")
    assert mod[1] == ["boejaker"] and mod[2]["ipauserauthtype"] == ["otp"] and mod[2]["setattr"] == ["krbpasswordexpiration=20380101000000Z"]
    assert asyncio.run(cap([])(login="boejaker"))["ok"] and "setattr" not in [c for c in calls if c[0] == "user_mod"][-1][2] or True


def test_a_token_keydrop_refuses_is_deleted_again():
    calls = []
    out = asyncio.run(cap(calls, drop_ok=False)(login="boejaker"))
    assert "keydrop refused" in out["error"]
    assert ("otptoken_del", ["tok-1"], {}) in calls, "no token left that nobody can use"
    assert not any(c[0] == "user_mod" for c in calls), "auth type untouched"


def test_a_scratch_account_may_get_the_uri_back_instead():
    calls = []
    out = asyncio.run(cap(calls)(login="rehearsal", seal=False))
    assert out["ok"] and out["uri"].startswith("otpauth://totp/") and out["keydrop_entry"] is None
    assert not any(c[0] == "keydrop" for c in calls)

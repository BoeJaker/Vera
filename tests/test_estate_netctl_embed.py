"""Network & Access embeds netctl: the Integrations record labelled 'netctl' is
found (NWM-02 first), /estate/netctl redirects into Vera's embed proxy or says
how to register netctl, and the Network pane has a netctl sub-tab."""
import ast
import asyncio
import os
import re
import sys
import types
from typing import Any, Dict

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_nav_core as nav  # noqa: E402

pytestmark = pytest.mark.critical

CAPS = os.path.join(ROOT, "vera", "estate", "estate_nav_capabilities.py")
PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")


def test_the_netctl_record_is_found_by_label_with_nwm02_first():
    rows = [{"id": "a", "label": "Gitea"}, {"id": "b", "label": "netctl (NWM-01)"},
            {"id": "c", "label": "netctl (NWM-02)"}, {"id": "d", "label": "not netctl"}]
    assert nav.netctl_record(rows)["id"] == "c"
    assert nav.netctl_record(rows[:2])["id"] == "b"
    assert nav.netctl_record([{"id": "x", "label": "Grafana"}]) == {}


class Redirect:
    def __init__(self, url, status_code=307):
        self.url, self.status_code = url, status_code


class Html:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code


def route(rows):
    tree = ast.parse(open(CAPS, encoding="utf-8").read())
    keep = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.Assign))
            and (getattr(n, "name", "") in ("_netctl_integration", "_estate_netctl")
                 or any(getattr(t, "id", "") == "_NETCTL_MISSING" for t in getattr(n, "targets", [])))]
    for n in keep:
        if isinstance(n, ast.AsyncFunctionDef):
            n.decorator_list = []

    async def integration_list():
        return {"integrations": rows}

    orch = types.SimpleNamespace(CAPABILITY_REGISTRY={"integration.list": {"func": integration_list}})
    ns = {"Any": Any, "Dict": Dict, "nav": nav, "_orch": orch, "RedirectResponse": Redirect, "HTMLResponse": Html}
    exec(compile(ast.Module(body=keep, type_ignores=[]), CAPS, "exec"), ns)
    return asyncio.run(ns["_estate_netctl"]())


def test_the_route_redirects_into_the_embed_proxy():
    r = route([{"id": "f00d", "label": "netctl (NWM-02)"}])
    assert isinstance(r, Redirect) and r.url == "/integrations/f00d/embed/" and r.status_code == 307


def test_the_route_explains_how_to_register_netctl_when_missing():
    r = route([{"id": "x", "label": "Gitea"}])
    assert isinstance(r, Html) and r.status_code == 404 and "not registered" in r.body


def test_the_network_pane_has_a_netctl_sub_tab():
    html = open(PANEL, encoding="utf-8").read()
    subs = re.search(r"const _netSubSrc=\{([^}]*)\}", html).group(1)
    assert "netctl:'/estate/netctl'" in subs
    assert 'id="nets-netctl"' in html and 'id="net-sub-netctl"' in html

"""One TLS context for the per-call httpx clients (2026-10-03).

A py-spy profile of prod put ~1.9 s of every 40 s of the event loop in ssl.create_default_context: warm models,
the NLP node dispatch and the node agent each made a short-lived httpx.AsyncClient per call without verify=, and
httpx builds (loads the CA bundle for) a new SSL context for every such client - even for plain-http URLs.
"""
import os
import re
import ssl
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera import http_tls_core as tls  # noqa: E402

pytestmark = pytest.mark.critical

FILES = [
    "vera/workers/warm_models_capabilities.py",
    "vera/research/nlp_dispatch.py",
    "vera/workers/node_agent_capabilities.py",
]


def test_one_context_shared_and_verifying():
    a, b = tls.shared_ssl_context(), tls.shared_ssl_context()
    assert a is b
    assert isinstance(a, ssl.SSLContext)
    assert a.verify_mode == ssl.CERT_REQUIRED and a.check_hostname   # the same trust as httpx's default


def test_clients_built_with_it_make_no_new_context(monkeypatch):
    httpx = pytest.importorskip("httpx")
    ctx = tls.shared_ssl_context()
    made = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: made.append(1) or real(*a, **k))
    for _ in range(20):
        c = httpx.AsyncClient(timeout=5, verify=ctx)
        assert c is not None
    assert made == [], f"{len(made)} contexts built for 20 clients"
    httpx.AsyncClient(timeout=5)                      # the old way, for contrast: httpx builds one per client
    assert len(made) >= 1


@pytest.mark.parametrize("path", FILES)
def test_every_client_in_the_hot_modules_takes_the_shared_context(path):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    calls = re.findall(r"httpx\.AsyncClient\((.*?)\) as ", src)
    assert calls, path
    assert all("verify=_tls()" in c for c in calls), [c for c in calls if "verify=_tls()" not in c]
    assert "from Vera.vera.http_tls_core import shared_ssl_context as _tls" in src
    assert "from vera.http_tls_core import shared_ssl_context as _tls" in src

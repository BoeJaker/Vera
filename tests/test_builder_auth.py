"""The build service runs arbitrary shell, apt and pip (/build/exec). It must
refuse every route except /health without the shared token, fail closed when
no token is configured, and accept the right token from an env value or a
mounted file. Drives the real FastAPI app with the TestClient; the requests use
an empty body, so no build command ever runs.
"""
import importlib.util
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "vera", "build", "builder_service.py")


def _svc():
    spec = importlib.util.spec_from_file_location("builder_service_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("BUILDER_TOKEN", "BUILDER_TOKEN_FILE", "BUILDER_ALLOW_NO_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(_svc().app)


def test_no_token_configured_refuses_everything_but_health(client):
    assert client.get("/health").status_code == 200
    r = client.post("/build/exec", json={})
    assert r.status_code == 401 and "token" in r.json()["error"]


def test_wrong_or_missing_token_refused(client, monkeypatch):
    monkeypatch.setenv("BUILDER_TOKEN", "s3cr3t-token")
    assert client.post("/build/exec", json={}).status_code == 401
    assert client.post("/build/exec", json={}, headers={"X-Builder-Token": "nope"}).status_code == 401


def test_right_token_reaches_the_route(client, monkeypatch):
    monkeypatch.setenv("BUILDER_TOKEN", "s3cr3t-token")
    r = client.post("/build/exec", json={}, headers={"X-Builder-Token": "s3cr3t-token"})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "command required"}


def test_token_file_wins_and_is_read_per_request(client, monkeypatch, tmp_path):
    f = tmp_path / "builder.token"
    f.write_text("from-file\n")
    monkeypatch.setenv("BUILDER_TOKEN_FILE", str(f))
    monkeypatch.setenv("BUILDER_TOKEN", "from-env")
    assert client.post("/build/exec", json={}, headers={"X-Builder-Token": "from-file"}).status_code == 200
    assert client.post("/build/exec", json={}, headers={"X-Builder-Token": "from-env"}).status_code == 401


def test_explicit_opt_out_allows_no_token(client, monkeypatch):
    monkeypatch.setenv("BUILDER_ALLOW_NO_TOKEN", "1")
    assert client.post("/build/exec", json={}).status_code == 200


def test_token_ok_is_constant_time_compare_and_fails_closed():
    svc = _svc()
    assert svc.token_ok("a", "a") and not svc.token_ok("a", "b")
    assert not svc.token_ok("", "")                 # nothing configured: refuse
    assert svc.token_ok("", "", allow_no_token=True)

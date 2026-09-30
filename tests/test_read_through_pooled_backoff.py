"""A slow prod cannot stall a sandbox's pages through the read-through.

Each reading used a new httpx client (a TLS handshake per tile) and waited up to
40 s; six such waits fill a browser's connections to the sandbox and the page
stops answering. Now: one pooled client, 3 s to connect, 20 s to read, and after
prod fails a reading the sandbox answers locally for _READ_THROUGH_BACKOFF_S
without calling prod. No retry: a failed reading returns None and the local
capability answers, as before.
"""
import asyncio

import httpx
import pytest

pytestmark = pytest.mark.critical


@pytest.fixture
def co(monkeypatch):
    from vera import capability_orchestration as O
    monkeypatch.setattr(O, "_READ_THROUGH_URL", "https://prod.invalid/mcp/call")
    monkeypatch.setattr(O, "_READ_THROUGH_DOWN_UNTIL", 0.0)
    monkeypatch.setattr(O, "_READ_THROUGH_CLIENT", None)
    monkeypatch.setattr(O, "_READ_THROUGH_CLIENT_LOOP", None)
    return O


class FakeClient:
    def __init__(self, behave):
        self.behave, self.calls = behave, 0

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        return self.behave(json)


def test_a_failed_reading_answers_locally_and_backs_off(co, monkeypatch):
    def slow(_body):
        raise httpx.ReadTimeout("prod is slow")
    client = FakeClient(slow)
    monkeypatch.setattr(co, "_read_through_client", lambda: client)

    async def run():
        first = await co._upstream_read("obs.redis", {})
        second = await co._upstream_read("sysmon.status", {})
        return first, second

    first, second = asyncio.run(run())
    assert first is None and second is None     # both answered locally
    assert client.calls == 1, "during the back-off prod must not be called again"


def test_after_the_back_off_prod_is_tried_again(co, monkeypatch):
    client = FakeClient(lambda body: httpx.Response(
        200, json={"type": "tool_result", "content": {"read": body["name"]}}))
    monkeypatch.setattr(co, "_read_through_client", lambda: client)
    monkeypatch.setattr(co, "_READ_THROUGH_DOWN_UNTIL", 0.0)
    assert asyncio.run(co._upstream_read("obs.redis", {})) == {"read": "obs.redis"}
    assert client.calls == 1


def test_an_answer_from_prod_that_is_not_200_does_not_back_off(co, monkeypatch):
    client = FakeClient(lambda _b: httpx.Response(404, json={}))
    monkeypatch.setattr(co, "_read_through_client", lambda: client)

    async def run():
        return [await co._upstream_read("obs.x", {}) for _ in range(2)]

    assert asyncio.run(run()) == [None, None]
    assert client.calls == 2, "prod answered, so it is not down"


def test_one_client_is_reused_within_a_loop(co):
    async def run():
        a = co._read_through_client()
        b = co._read_through_client()
        await a.aclose()
        return a, b

    a, b = asyncio.run(run())
    assert a is b


def test_timeouts():
    from vera import capability_orchestration as O
    assert O._READ_THROUGH_CONNECT_S <= 5
    assert O._READ_THROUGH_TIMEOUT_S <= 20
    assert O._READ_THROUGH_BACKOFF_S > 0

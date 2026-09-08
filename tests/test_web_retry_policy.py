import asyncio

import httpx
import pytest

from vera.web import web_client


pytestmark = pytest.mark.critical


def _response(status, retry_after=""):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(status, headers=headers)


def test_retry_delay_honors_numeric_header_and_caps_it(monkeypatch):
    monkeypatch.setattr(web_client, "_RETRY_MAX_S", 5.0)
    assert web_client.retry_delay(1, "2") == 2.0
    assert web_client.retry_delay(1, "200") == 5.0


def test_retry_delay_uses_deterministic_exponential_fallback(monkeypatch):
    monkeypatch.setattr(web_client, "_RETRY_BASE_S", 0.25)
    monkeypatch.setattr(web_client, "_RETRY_MAX_S", 5.0)
    assert web_client.retry_delay(1) == 0.25
    assert web_client.retry_delay(2, "invalid") == 0.5


@pytest.mark.asyncio
async def test_retryable_response_retries_once_through_single_owner(monkeypatch):
    retryable = _response(429, "1.5")
    responses = iter([retryable, _response(200)])
    calls, throttles, sleeps = [], [], []

    async def request():
        calls.append(True)
        return next(responses)

    async def throttle(domain):
        throttles.append(domain)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(web_client, "throttle_domain", throttle)
    result = await web_client.request_with_policy(
        request, domain="example.test", attempts=2, sleep=sleep)

    assert result.status_code == 200
    assert len(calls) == 2
    assert throttles == ["example.test", "example.test"]
    assert sleeps == [1.5]
    assert retryable.is_closed


@pytest.mark.asyncio
async def test_non_retryable_response_is_never_repeated():
    calls = []

    async def request():
        calls.append(True)
        return _response(404)

    result = await web_client.request_with_policy(
        request, domain="example.test", throttle=False, attempts=3)

    assert result.status_code == 404
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transport_failure_is_bounded_and_final_error_surfaces():
    calls, sleeps = [], []

    async def request():
        calls.append(True)
        raise httpx.ConnectError("offline")

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(httpx.ConnectError, match="offline"):
        await web_client.request_with_policy(
            request, domain="example.test", throttle=False,
            attempts=2, sleep=sleep)

    assert len(calls) == 2
    assert sleeps == [web_client.retry_delay(1)]


@pytest.mark.asyncio
async def test_request_cancellation_is_never_retried_or_swallowed():
    calls = []

    async def request():
        calls.append(True)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await web_client.request_with_policy(
            request, domain="example.test", throttle=False, attempts=3)

    assert len(calls) == 1


def test_research_and_web_search_transports_use_shared_request_policy():
    from pathlib import Path

    root = Path(__file__).parents[1] / "vera"
    research = (root / "research" / "researcher_api.py").read_text(encoding="utf-8")
    web = (root / "web" / "web_capabilities.py").read_text(encoding="utf-8")

    assert research.count("_request_with_web_policy(") >= 4
    assert web.count("_wc.request_with_policy(") >= 3

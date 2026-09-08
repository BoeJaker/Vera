import pytest

from vera.web import web_client
from vera.web import search_engines


pytestmark = pytest.mark.critical


@pytest.fixture(autouse=True)
def clear_provider_hook():
    web_client.set_search_api_hook(None)
    yield
    web_client.set_search_api_hook(None)


@pytest.mark.asyncio
async def test_search_provider_absent_returns_empty_fallback():
    assert await web_client.search_via_api("query", 8) == ([], "", "")


@pytest.mark.asyncio
async def test_search_provider_receives_bounded_request_and_returns_results():
    calls = []

    async def provider(query, limit, platform):
        calls.append((query, limit, platform))
        return ([{"url": "https://example.test/one", "title": "One"}],
                "native", "clean query")

    web_client.set_search_api_hook(provider)
    resolved = await web_client.search_via_api("site:example.test query", 500,
                                               platform="example")

    assert calls == [("site:example.test query", 50, "example")]
    assert resolved == ([{"url": "https://example.test/one", "title": "One"}],
                        "native", "clean query")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], ({}, "engine", "query")])
async def test_search_provider_malformed_response_fails_closed(response):
    async def provider(query, limit, platform):
        return response

    web_client.set_search_api_hook(provider)
    assert await web_client.search_via_api("query", 8) == ([], "", "")


@pytest.mark.asyncio
async def test_search_provider_exception_fails_closed():
    async def provider(query, limit, platform):
        raise RuntimeError("provider unavailable")

    web_client.set_search_api_hook(provider)
    assert await web_client.search_via_api("query", 8) == ([], "", "")


def test_provider_results_lead_general_results_with_normalized_url_dedupe():
    provider = [
        {"url": "https://example.test/one/", "title": "Native"},
        {"url": "https://example.test/two", "title": "Second"},
    ]
    general = [
        {"url": "https://example.test/one", "title": "Duplicate"},
        {"url": "https://other.test/three", "title": "Third"},
    ]

    assert search_engines.merge_pages((provider, general), 3) == [
        provider[0], provider[1], general[1]
    ]

import asyncio

from vera.catalog import benchmark_capabilities as bench


def test_generate_disables_native_thinking(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": "BANANA", "done": True}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            captured["url"] = url
            captured["body"] = json
            return Response()

    monkeypatch.setattr(bench.httpx, "AsyncClient", Client)
    result = asyncio.run(bench._generate("http://ollama", "thinking-model", "prompt"))

    assert result["response"] == "BANANA"
    assert captured["url"] == "http://ollama/api/generate"
    assert captured["body"]["think"] is False
    assert captured["body"]["stream"] is False


def test_embedding_benchmark_uses_embedding_warmup(monkeypatch):
    calls = []

    async def embed(url, model, text, timeout=60):
        calls.append((url, model, text))
        return [1.0, 0.0]

    async def generate(*args, **kwargs):
        raise AssertionError("embedding benchmark must not call /api/generate")

    async def run_pack(url, model, pack):
        return {"role": "embed", "label": pack["label"], "kind": "embed",
                "passed": 6, "total": 6, "accuracy": 1.0, "items": []}

    async def no_op(*args, **kwargs):
        return None

    async def empty_meta(*args, **kwargs):
        return {}

    monkeypatch.setattr(bench, "_instance_url", lambda _: "http://ollama")
    monkeypatch.setattr(bench, "_instance", lambda _: {"label": "CPU", "has_gpu": False})
    monkeypatch.setattr(bench, "_model_meta", empty_meta)
    monkeypatch.setattr(bench, "_embed_one", embed)
    monkeypatch.setattr(bench, "_generate", generate)
    monkeypatch.setattr(bench, "_run_embed_pack", run_pack)
    monkeypatch.setattr(bench, "_store_result", no_op)
    monkeypatch.setattr(bench, "emit_event", no_op)

    result = asyncio.run(bench._benchmark("cpu", "embed-model", role="embed"))

    assert result["ok"] is True
    assert result["accuracy"] == 1.0
    assert calls == [("http://ollama", "embed-model", "benchmark warm-up")]


def test_vision_pack_allows_final_answer_after_reasoning(monkeypatch):
    calls = []

    async def generate(*args, **kwargs):
        calls.append(kwargs)
        return {"response": "red", "thinking": "brief", "done_reason": "stop"}

    monkeypatch.setattr(bench, "_generate", generate)
    perf = {"_gen_tps": [], "_prompt_tps": [], "_ttft": [], "_total_s": 0.0,
            "calls": 0}
    pack = {"label": "vision", "items": [
        {"color": (220, 20, 20), "prompt": "colour?",
         "match": {"type": "contains", "value": "red"}},
    ]}

    result = asyncio.run(bench._run_vision_pack("http://ollama", "vlm", pack, perf, {}))

    assert result["accuracy"] == 1.0
    assert calls[0]["num_predict"] == 128
    assert result["items"][0]["thinking_chars"] == 5
    assert result["items"][0]["done_reason"] == "stop"

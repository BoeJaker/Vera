from datetime import datetime, timezone

import pytest

from Vera.vera.fabric import memory


pytestmark = pytest.mark.critical


class _Backend:
    name = "example"

    async def store(self, record):  # pragma: no cover - guard must skip it
        raise AssertionError("protected sandbox write reached backend")

    async def update(self, record_id, updates):  # pragma: no cover
        raise AssertionError("protected sandbox update reached backend")


class _SlowBackend:
    name = "slow"

    async def store(self, record):
        await __import__("asyncio").sleep(1)
        return True

    async def search(self, query, *, limit, filters, embedding):
        await __import__("asyncio").sleep(1)
        return []

    async def update(self, record_id, updates):
        await __import__("asyncio").sleep(1)
        return True


@pytest.mark.asyncio
async def test_sandbox_guard_does_not_claim_suppressed_writes_succeeded(monkeypatch):
    store = memory.HybridMemoryStore()
    store.register(_Backend())
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: True)

    assert await store.store(memory.MemoryRecord(text="held out")) == {
        "example": False,
    }
    assert await store.update("record-1", {"archived": True}) == {
        "example": False,
    }


def test_chroma_metadata_normalizes_datetime_timestamps():
    now = datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc)
    record = memory.MemoryRecord(
        text="held out", created_at=now, updated_at=now,
    )

    _, _, metadata = record.to_chroma_doc()

    assert metadata["created_at"] == now.isoformat()
    assert metadata["updated_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_hybrid_store_and_update_isolate_backend_timeout(monkeypatch):
    store = memory.HybridMemoryStore()
    store.register(_SlowBackend())
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: False)
    monkeypatch.setattr(memory, "MEMORY_AUTO_EMBED", False)
    monkeypatch.setattr(memory, "_BACKEND_OP_TIMEOUT_S", 0.01)

    assert await store.store(memory.MemoryRecord(text="held out")) == {
        "slow": False,
    }
    assert await store.update("record-1", {"archived": True}) == {
        "slow": False,
    }


@pytest.mark.asyncio
async def test_hybrid_search_isolates_backend_timeout_and_propagates_cancel(monkeypatch):
    store = memory.HybridMemoryStore()
    store.register(_SlowBackend())
    monkeypatch.setattr(memory, "_BACKEND_OP_TIMEOUT_S", 0.01)

    assert await store.search("held out", backends=["slow"]) == []

    class _Cancelled(_SlowBackend):
        async def search(self, query, *, limit, filters, embedding):
            raise __import__("asyncio").CancelledError()

    cancelled = memory.HybridMemoryStore()
    cancelled.register(_Cancelled())
    with pytest.raises(__import__("asyncio").CancelledError):
        await cancelled.search("held out", backends=["slow"])

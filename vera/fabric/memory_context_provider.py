"""Adapter from the portable MemoryProvider contract to ContextProvider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence

from vera.context_provider import ContextCancellation, ContextCitation, ContextItem
from vera.fabric.memory_provider import MemoryAccessContext, MemoryProvider, MemoryQuery


class MemoryContextProvider:
    """Expose authorized revision-bound memory search as cited context."""

    def __init__(self, provider: MemoryProvider, access: MemoryAccessContext, *,
                 provider_id: str, token_counter: Callable[[str], int],
                 namespace: str = "", session_id: str = "",
                 record_type: str = "", tags: Sequence[str] = ()):
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise ValueError("context provider_id is required")
        if not callable(token_counter):
            raise TypeError("token_counter must be callable")
        self.provider_id = provider_id
        self._provider = provider
        self._access = access
        self._token_counter = token_counter
        self._filters = {
            "namespace": namespace, "session_id": session_id,
            "record_type": record_type, "tags": tuple(tags),
        }

    async def search(self, query: str, *, limit: int,
                     cancellation: ContextCancellation | None = None
                     ) -> Sequence[ContextItem]:
        memory_query = MemoryQuery(
            tenant_id=self._access.tenant_id, text=query, limit=limit,
            include_text=True, **self._filters)
        page = await asyncio.to_thread(
            self._provider.search, memory_query, self._access,
            cancellation=cancellation)
        items = []
        for hit in page.hits:
            projection = hit.projection
            if not isinstance(projection, Mapping):
                raise ValueError("memory hit projection must be an object")
            text = projection.get("text")
            citations = projection.get("citations")
            record_id = str(projection.get("record_id") or "")
            revision_id = str(projection.get("revision_id") or "")
            if not isinstance(text, str) or not text:
                raise ValueError("memory context requires text")
            if not isinstance(citations, list) or not citations or not all(
                    isinstance(citation, Mapping) for citation in citations):
                raise ValueError("memory context requires citations")
            if not any(
                    str(citation.get("record_id") or "") == record_id
                    and str(citation.get("revision_id") or "") == revision_id
                    for citation in citations):
                raise ValueError("memory context requires an authoritative citation")
            context_citations = tuple(
                ContextCitation(str(citation.get("citation_id") or ""),
                                str(citation.get("uri") or ""))
                for citation in citations)
            token_count = self._token_counter(text)
            items.append(ContextItem(
                item_id=str(projection.get("memory_id") or ""), text=text,
                source=record_id, revision=revision_id,
                provider=self.provider_id, score=hit.score,
                token_count=token_count, citations=context_citations))
        return tuple(items)

"""An empty query is labelled skipped:empty, apart from the embedder being off (four-minds FM-0.4)."""
import os

import pytest

pytestmark = pytest.mark.critical

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vera", "fabric", "memory.py"),
           encoding="utf-8").read()


def test_empty_and_off_are_different_outcomes():
    i = SRC.index("async def embed_text_outcome(")
    body = SRC[i:i + 2500]
    assert 'if not MEMORY_AUTO_EMBED:\n        return None, "skipped:off"' in body
    assert 'if not text.strip():' in body and 'return None, "skipped:empty"' in body
    assert body.index('"skipped:off"') < body.index('"skipped:empty"')

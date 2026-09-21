"""Regression guard for the Chroma client/server protocol contract."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _major_version(text: str, pattern: str) -> int:
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None
    return int(match.group(1))


@pytest.mark.critical
def test_chroma_client_and_server_use_the_same_protocol_generation():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    client_major = _major_version(
        requirements,
        r"^chromadb-client\s*>=\s*(\d+)(?:\.\d+)*\s*,\s*<\s*(\d+)",
    )
    server_major = _major_version(
        compose,
        r"^\s*image:\s*chromadb/chroma:(\d+)(?:\.\d+)*\s*$",
    )

    assert client_major == server_major, (
        "Chroma HTTP client and server must use the same major protocol "
        f"generation (client={client_major}, server={server_major})"
    )

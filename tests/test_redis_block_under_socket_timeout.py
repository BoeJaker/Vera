"""A blocking stream read on the shared Redis client must return before the
client's socket timeout.

block=5000 against socket_timeout=4 made every idle XREAD/XREADGROUP fail with
"Timeout reading from ..." (logged about every 6 s by result_listener on prod
and every sandbox), dropped the pooled connection each time, and left a
message delivered in the 4-5 s window on a socket nobody read. Readers now use
capability_orchestration.STREAM_BLOCK_MS; this pins the relationship and that
no reader goes back to a literal.
"""
import pathlib
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = pathlib.Path(__file__).resolve().parents[1]
ORCH = ROOT / "vera" / "capability_orchestration.py"


def _const(name):
    m = re.search(rf"^{name}\s*=\s*(\d+)\s*$", ORCH.read_text(encoding="utf-8"), re.M)
    assert m, f"{name} is not defined in capability_orchestration.py"
    return int(m.group(1))


def test_block_returns_before_socket_timeout():
    # A full second of headroom for the reply to cross the network.
    assert _const("STREAM_BLOCK_MS") + 1000 <= _const("REDIS_SOCKET_TIMEOUT_S") * 1000


def test_shared_client_uses_the_named_timeout():
    src = ORCH.read_text(encoding="utf-8")
    connect = src[src.index("async def _connect_redis"):]
    connect = connect[:connect.index("await _r.ping()")]
    assert "socket_timeout=REDIS_SOCKET_TIMEOUT_S" in connect


def test_no_stream_reader_uses_a_literal_block():
    offenders = []
    for path in (ROOT / "vera").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bblock\s*=\s*\d{4,}", line.split("#", 1)[0]):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "use STREAM_BLOCK_MS:\n" + "\n".join(offenders)

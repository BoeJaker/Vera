"""The dashboard snapshot asks Redis for its server figures only.

obs.redis with keys=true is KEYS plus a TYPE per key; called by every snapshot it was 140 of 893 busy main-thread
samples on prod (py-spy, 2026-10-05). The snapshot's readers use only connected_clients, used_memory_human and error.
"""
import os
import re

SRC = open(os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py"), encoding="utf-8").read()


def test_the_snapshot_asks_redis_for_its_figures_only():
    a = SRC.index("_SNAPSHOT_SOURCES = (")
    block = SRC[a:SRC.index("\n)\n", a)]
    assert '("redis", "obs.redis", {"keys": False}),' in block


def test_the_snapshot_readers_use_only_the_figures():
    uses = set(re.findall(r'rds\.get\(["\']([a-z_]+)["\']', SRC))
    assert uses <= {"error", "connected_clients", "used_memory_human", "uptime_days"}, uses

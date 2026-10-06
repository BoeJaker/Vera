"""The server raises its open-files soft limit at start-up (2026-10-06: 1024 ran out and prod answered nothing)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera import proc_limits_core as pl  # noqa: E402


def test_the_soft_limit_rises_to_the_wanted_level_within_the_hard_limit():
    assert pl.nofile_target(1024, 1048576) == pl.WANT
    assert pl.nofile_target(1024, 4096) == 4096
    assert pl.nofile_target(1024, -1) == pl.WANT                 # unlimited hard limit


def test_it_is_never_lowered():
    assert pl.nofile_target(pl.WANT, 1048576) is None
    assert pl.nofile_target(200000, 1048576) is None
    assert pl.nofile_target(4096, 4096) is None


class _Res:
    RLIMIT_NOFILE, RLIM_INFINITY = 7, -1

    def __init__(self, soft, hard):
        self.lim = (soft, hard)

    def getrlimit(self, _k):
        return self.lim

    def setrlimit(self, _k, v):
        self.lim = v


def test_raise_sets_it_and_reports_before_and_after():
    r = _Res(1024, 1048576)
    assert pl.raise_nofile(r) == (1024, pl.WANT) and r.lim == (pl.WANT, 1048576)
    assert pl.raise_nofile(r) == (pl.WANT, pl.WANT)              # a second call changes nothing


def test_health_warns_from_a_quarter_of_the_limit():
    assert pl.files_status(200, 65536, 37) == {"open": 200, "limit": 65536, "share": 0.003, "redis_conns": 37,
                                              "warn": False}
    assert pl.files_status(1000, 1024, 936)["warn"] is True       # 2026-10-06 would have warned days earlier
    assert pl.files_status(256, 1024)["warn"] is True and pl.files_status(255, 1024)["warn"] is False
    assert pl.files_status(-1, 1024)["warn"] is False and pl.files_status(10, 0)["share"] == -1.0


def test_health_carries_the_files_block():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py"),
               encoding="utf-8").read()
    assert '"files":_health_files(),' in src
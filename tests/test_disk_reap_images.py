"""Disk plan C5 (17 Oct 2026): session snapshot images and workspace volumes are reaped like
the session sandboxes - only when unused, known to be old, and never by force - and a session
whose snapshot was reaped starts from the base image instead of failing."""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.docker import disk_headroom as D  # noqa: E402

pytestmark = pytest.mark.critical

NOW = time.mktime((2026, 10, 17, 12, 0, 0, 0, 0, -1))


def test_hours_since_reads_both_docker_timestamp_shapes():
    img = D.hours_since("2026-10-16 12:00:00 +0000 UTC", time.time())
    assert img is not None and img > 0
    a = D.hours_since("2026-10-03 12:00:00 +0100 BST", 1760000000.0)
    b = D.hours_since("2026-10-03T11:00:00Z", 1760000000.0)
    assert a == pytest.approx(b)
    assert D.hours_since("2026-10-03T12:00:00+01:00", 1760000000.0) == pytest.approx(a)
    assert D.hours_since("garbage", NOW) is None and D.hours_since("", NOW) is None


def test_only_old_unused_session_images_are_reapable():
    old, young = 15 * 24.0, 2 * 24.0
    images = [
        {"ref": "vera-session:a", "id": "sha256:" + "a" * 64, "age_hours": old},      # goes
        {"ref": "vera-session:b", "id": "sha256:" + "b" * 64, "age_hours": young},    # too young
        {"ref": "vera-session:c", "id": "sha256:" + "c" * 64, "age_hours": None},     # unknown age
        {"ref": "vera-session:d", "id": "sha256:" + "d" * 64, "age_hours": old},      # used by ref
        {"ref": "vera-session:e", "id": "sha256:" + "e" * 64, "age_hours": old},      # used by short id
        {"ref": "python:3.12-slim", "id": "sha256:" + "f" * 64, "age_hours": old},    # not a session image
    ]
    used = ["vera-session:d", "eeeeeeeeeeee", "nginx:latest"]
    assert [i["ref"] for i in D.reapable_images(images, used)] == ["vera-session:a"]
    assert D.reapable_images(images, used, retain_hours=24 * 30) == []


def test_only_old_dangling_session_volumes_are_reapable():
    vols = [
        {"name": "vera-sbx-x-ws", "dangling": True, "age_hours": 400},
        {"name": "vera-sbx-y-ws", "dangling": False, "age_hours": 400},
        {"name": "vera-sbx-z-ws", "dangling": True, "age_hours": 10},
        {"name": "vera-sbx-u-ws", "dangling": True, "age_hours": None},
        {"name": "vera-dev-mirror", "dangling": True, "age_hours": 400},
        {"name": "pgdata", "dangling": True, "age_hours": 4000},
    ]
    assert [v["name"] for v in D.reapable_volumes(vols)] == ["vera-sbx-x-ws"]


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_reap_never_forces_and_scans_images_after_the_containers():
    src = _src("vera/evolve/evolve_capabilities.py")
    body = src[src.index("async def docker_disk_reap"):src.index("async def _disk_daily_reap")]
    assert '"rmi", "-f"' not in body and '"rm", "-f"' not in body
    assert body.index('"docker", "rm", "-v"') < body.index("reapable_images")
    assert "none removed" in body                       # a failed scan removes nothing
    daily = src[src.index("async def _disk_daily_reap"):]
    assert "skip_in_sandbox=True" in daily[:2500]


def test_a_session_whose_snapshot_was_reaped_starts_from_the_base_image():
    src = _src("vera/remote/session_sandbox_capabilities.py")
    start = src[src.index('restored = bool(committed and not base_image)'):]
    check = start.index("_image_present(dk, host, image)")
    assert check < start.index("dk.cap_docker_run(")
    block = start[check:start.index("dk.cap_docker_run(")]
    assert "_DEFAULT_BASE" in block and 'startswith("vera-session:")' in block
    assert "restored, committed = fallback, False, \"\"" in block
